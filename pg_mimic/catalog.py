"""The middleware chain: the boilerplate a Postgres client expects any server to
answer, so a session author doesn't have to implement it to get a working
connection. `Session.prepare()` walks `Session.middleware` and only falls through
to the author's own describe()/query() if every link passes.

The line the default chain draws is *state the session has already declared*:
transaction status, session vars, and `Session.schema()`. Answering those on the
session's behalf is a convenience; answering an ordinary query is not. So
`SELECT 1` goes to the session -- it's a query with whatever meaning the session
gives it -- while `SELECT current_user` doesn't, because only the connection can
know. `static_select` restores the older evaluate-everything behaviour for anyone
who wants it, but it isn't in `DEFAULT_MIDDLEWARE`.

`middleware` is a plain class attribute: reorder it, drop links, append your own
`async (MiddlewareContext) -> Statement | None`, or set it to `()` for a session
that sees every statement untouched.

Transaction control and SET/SHOW are classified from the raw SQL text (a
small, fixed grammar) rather than via sqlglot's parse tree: sqlglot's
postgres dialect doesn't reliably produce clean nodes for these (e.g.
"START TRANSACTION" parses as a stray column-alias expression, and bare
"SHOW x" falls back to a generic Command node) -- simple regexes are more
robust here than fighting the parser. sqlglot *is* used for everything that
needs real expression evaluation: session-function SELECTs and
information_schema lookups.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, AsyncIterator, Awaitable, Callable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute

from .results import ResultColumn
from .session import Portal, Row, Statement, drain_rows
from .types import TEXT

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)

Middleware = Callable[["MiddlewareContext"], Awaitable["Statement | None"]]


class StaticStatement(Statement):
    """A Statement whose result is already fully known (SET/SHOW/BEGIN/static
    SELECT/information_schema/...) -- no CallbackStatement/Session involved."""

    def __init__(
        self,
        sql: str,
        columns: list[ResultColumn] | None,
        rows: list[Row],
        on_execute=None,
    ):
        self.sql = sql
        self.param_oids: list[int | None] = []
        self._columns = columns
        self._rows = rows
        self._on_execute = on_execute

    async def describe(self) -> list[ResultColumn] | None:
        return self._columns

    def bind(self, params: list[str | None]) -> Portal:
        return StaticPortal(self._rows, self._on_execute)


class StaticPortal(Portal):
    def __init__(self, rows: list[Row], on_execute):
        self._rows = rows
        self._on_execute = on_execute
        self._row_source: AsyncIterator[Row] | None = None

    async def execute(self, max_rows: int) -> tuple[list[Row], bool]:
        if self._row_source is None:
            if self._on_execute is not None:
                self._on_execute()
            self._row_source = _rows_as_async_iter(self._rows)
        return await drain_rows(self._row_source, max_rows)


async def _rows_as_async_iter(rows: list[Row]) -> AsyncIterator[Row]:
    for row in rows:
        yield row


_BEGIN_RE = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^\s*(COMMIT|END)\b", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"^\s*ROLLBACK\b", re.IGNORECASE)
_SET_RE = re.compile(r"^\s*SET\s+(?:SESSION\s+|LOCAL\s+)?(\w+)\s*(?:TO|=)\s*(.+?)\s*;?\s*$", re.IGNORECASE)
_SHOW_RE = re.compile(r"^\s*SHOW\s+(\S+?)\s*;?\s*$", re.IGNORECASE)


def is_transaction_end(sql: str) -> bool:
    stripped = sql.strip()
    return bool(_COMMIT_RE.match(stripped) or _ROLLBACK_RE.match(stripped))


def split_statements(sql: str) -> list[str]:
    """Split a simple-query string into individual statement texts on ';'
    boundaries, using sqlglot rather than a naive string split so semicolons
    inside string literals etc. don't misfire. The single-statement case
    (by far the common one) always returns the original text unchanged --
    only genuine multi-statement batches get sqlglot's re-rendered SQL,
    since there's no reliable way to recover the original substrings once
    parsed. Falls back to treating the whole input as one statement if
    sqlglot can't parse it at all (best-effort -- pg_mimic isn't a full SQL
    parser, and a client sending syntax sqlglot doesn't support should still
    reach the session, not get a hard failure here)."""
    try:
        expressions = [e for e in sqlglot.parse(sql, dialect="postgres") if e is not None]
    except Exception:
        return [sql]
    if len(expressions) <= 1:
        return [sql]
    return [expr.sql(dialect="postgres") for expr in expressions]


class MiddlewareContext:
    """What a middleware gets to look at: the connection, the raw SQL, and the
    declared parameter OIDs. Parses lazily and once, so a chain of SELECT-shaped
    middleware doesn't re-parse the same statement for each link."""

    __slots__ = ("connection", "sql", "param_oids", "_expression")

    _UNPARSED = object()

    def __init__(self, connection: "Connection", sql: str, param_oids: list[int | None]):
        self.connection = connection
        self.sql = sql.strip()
        self.param_oids = param_oids
        self._expression: Any = self._UNPARSED

    @property
    def expression(self) -> "exp.Expression | None":
        """The parsed statement, or None if sqlglot can't parse it. Not an error:
        pg_mimic isn't a full SQL parser, and syntax sqlglot doesn't support must
        still reach the session rather than fail here."""
        if self._expression is self._UNPARSED:
            try:
                self._expression = sqlglot.parse_one(self.sql, dialect="postgres")
            except Exception:
                self._expression = None
        return self._expression

    def select_without_tables(self) -> "exp.Select | None":
        """The parsed statement if it's a SELECT referencing no tables, else None."""
        expr = self.expression
        if isinstance(expr, exp.Select) and not list(expr.find_all(exp.Table)):
            return expr
        return None


# --- the middleware themselves ------------------------------------------------------
#
# Each takes a MiddlewareContext and returns a Statement to answer with, or None
# to pass the statement down the chain (and ultimately to the session author's
# own describe()/query()).


async def transaction_control(ctx: MiddlewareContext) -> Statement | None:
    """BEGIN / START TRANSACTION / COMMIT / END / ROLLBACK."""
    if _BEGIN_RE.match(ctx.sql):
        return _transaction_statement(ctx.connection, "BEGIN")
    if _COMMIT_RE.match(ctx.sql):
        return _transaction_statement(ctx.connection, "COMMIT")
    if _ROLLBACK_RE.match(ctx.sql):
        return _transaction_statement(ctx.connection, "ROLLBACK")
    return None


async def set_show(ctx: MiddlewareContext) -> Statement | None:
    """SET <name> TO <value> and SHOW <name>, against the connection's session vars."""
    set_match = _SET_RE.match(ctx.sql)
    if set_match:
        return _set_statement(ctx.connection, ctx.sql, set_match)
    show_match = _SHOW_RE.match(ctx.sql)
    if show_match:
        return _show_statement(ctx.connection, show_match.group(1))
    return None


async def session_functions(ctx: MiddlewareContext) -> Statement | None:
    """Table-less SELECTs over session state: version(), current_user,
    current_setting('x'), pg_backend_pid(), and friends.

    Deliberately narrower than evaluating every table-less SELECT: this only
    fires when the statement actually references something only the connection
    can answer. `SELECT 1` means whatever the session says it means, so it goes
    to the session -- see `static_select` to opt back into evaluating those.
    """
    expr = ctx.select_without_tables()
    if expr is None:
        return None
    substituted, substitutions = _substitute_session_functions(ctx.connection, expr)
    if not substitutions:
        return None
    return _evaluate_select(substituted, expr)


async def static_select(ctx: MiddlewareContext) -> Statement | None:
    """Evaluate any table-less SELECT (`SELECT 1`, `SELECT 1 + 1`, ...) with
    sqlglot rather than passing it to the session.

    Not enabled by default -- it answers queries the session may well have its
    own meaning for. Add it to `Session.middleware` if you want it.
    """
    expr = ctx.select_without_tables()
    if expr is None:
        return None
    substituted, _ = _substitute_session_functions(ctx.connection, expr)
    return _evaluate_select(substituted, expr)


async def information_schema(ctx: MiddlewareContext) -> Statement | None:
    """SELECTs against information_schema, answered from Session.schema()."""
    expr = ctx.expression
    if not isinstance(expr, exp.Select):
        return None
    tables = list(expr.find_all(exp.Table))
    if tables and any((t.db or "").lower() == "information_schema" for t in tables):
        return await _catalog_select_statement(ctx.connection, expr)
    return None


DEFAULT_MIDDLEWARE = (transaction_control, set_show, session_functions, information_schema)


async def resolve(
    connection: "Connection",
    sql: str,
    param_oids: list[int | None],
    middleware: "Sequence[Middleware]" = DEFAULT_MIDDLEWARE,
) -> Statement | None:
    """Walk `middleware` in order, returning the first Statement one produces,
    or None if every link passes (the caller falls back to the session author's
    own describe()/query())."""
    if not sql.strip():
        return None
    ctx = MiddlewareContext(connection, sql, param_oids)
    for link in middleware:
        statement = await link(ctx)
        if statement is not None:
            logger.debug("%s answered %r", getattr(link, "__name__", link), ctx.sql)
            return statement
    return None


def _transaction_statement(connection: "Connection", tag: str) -> Statement:
    def on_execute() -> None:
        connection.tx_status = b"T" if tag == "BEGIN" else b"I"

    return StaticStatement(tag, None, [], on_execute)


def _set_statement(connection: "Connection", sql: str, match: re.Match) -> Statement:
    name, raw_value = match.group(1).lower(), match.group(2).strip()

    def on_execute() -> None:
        if raw_value.upper() == "DEFAULT":
            connection.session_vars.pop(name, None)
        else:
            connection.session_vars[name] = raw_value.strip("'\"")

    return StaticStatement(sql, None, [], on_execute)


_DEFAULT_SETTINGS = {
    "server_version": lambda c: c.server.parameter_status.get("server_version", ""),
    "client_encoding": lambda c: c.server.parameter_status.get("client_encoding", "UTF8"),
    "timezone": lambda c: "UTC",
    "search_path": lambda c: '"$user", public',
    "standard_conforming_strings": lambda c: "on",
    "is_superuser": lambda c: "off",
}


def _show_statement(connection: "Connection", name: str) -> Statement:
    key = name.strip('"').lower()
    if key in connection.session_vars:
        value = connection.session_vars[key]
    elif key in _DEFAULT_SETTINGS:
        value = _DEFAULT_SETTINGS[key](connection)
    else:
        value = ""
    return StaticStatement(f"SHOW {name}", [ResultColumn(key, TEXT)], [(value,)])


_SESSION_NULLARY_FUNCS = {
    exp.CurrentDatabase: lambda c: c.database,
    exp.CurrentSchema: lambda c: "public",
    exp.CurrentUser: lambda c: c.username,
    exp.SessionUser: lambda c: c.username,
    exp.CurrentVersion: lambda c: c.server.parameter_status.get("server_version", ""),
}


def _substitute_session_functions(connection: "Connection", expr: exp.Expression) -> tuple[exp.Expression, int]:
    """Replace session-state functions with literals. Returns the rewritten
    expression and how many substitutions were made -- a count of zero is how
    `session_functions` tells "this SELECT is about the connection" apart from
    "this SELECT is the session's business"."""
    expr = expr.copy()
    substitutions = 0
    for node_type, resolver in _SESSION_NULLARY_FUNCS.items():
        for node in list(expr.find_all(node_type)):
            node.replace(exp.Literal.string(resolver(connection)))
            substitutions += 1
    for node in list(expr.find_all(exp.Anonymous)):
        name = node.this.lower() if isinstance(node.this, str) else ""
        if name == "current_setting" and node.expressions and isinstance(node.expressions[0], exp.Literal):
            setting_name = node.expressions[0].this
            key = setting_name.lower()
            if key in connection.session_vars:
                value = connection.session_vars[key]
            elif key in _DEFAULT_SETTINGS:
                value = _DEFAULT_SETTINGS[key](connection)
            else:
                continue
            node.replace(exp.Literal.string(value))
            substitutions += 1
        elif name == "pg_backend_pid":
            node.replace(exp.Literal.number(connection.pid))
            substitutions += 1
    return expr, substitutions


def _evaluate_select(substituted: exp.Expression, original: exp.Select) -> Statement | None:
    try:
        result = sqlglot_execute(substituted, dialect="postgres")
    except Exception:
        return None  # not something we can statically evaluate -- fall through

    rows = [tuple(row) for row in result.rows]
    if rows:
        columns = [ResultColumn.for_type(name, type(value)) for name, value in zip(result.columns, rows[0])]
    else:
        columns = [ResultColumn(name, TEXT) for name in result.columns]
    return StaticStatement(original.sql(dialect="postgres"), columns, rows)


def _build_information_schema(user_schema: dict) -> tuple[dict, dict]:
    """user_schema: {table_name: {col_name: type_str}} (Session.schema()'s shape)."""
    tables_rows = []
    columns_rows = []
    for table_name, cols in user_schema.items():
        tables_rows.append(
            {
                "table_catalog": "pg_mimic",
                "table_schema": "public",
                "table_name": table_name,
                "table_type": "BASE TABLE",
            }
        )
        for i, (col_name, col_type) in enumerate(cols.items(), start=1):
            columns_rows.append(
                {
                    "table_catalog": "pg_mimic",
                    "table_schema": "public",
                    "table_name": table_name,
                    "column_name": col_name,
                    "ordinal_position": i,
                    "data_type": col_type,
                    "is_nullable": "YES",
                }
            )

    schema = {
        "information_schema": {
            "tables": {
                "table_catalog": "TEXT",
                "table_schema": "TEXT",
                "table_name": "TEXT",
                "table_type": "TEXT",
            },
            "columns": {
                "table_catalog": "TEXT",
                "table_schema": "TEXT",
                "table_name": "TEXT",
                "column_name": "TEXT",
                "ordinal_position": "INT",
                "data_type": "TEXT",
                "is_nullable": "TEXT",
            },
        }
    }
    tables = {"information_schema": {"tables": tables_rows, "columns": columns_rows}}
    return schema, tables


async def _catalog_select_statement(connection: "Connection", expr: exp.Select) -> Statement | None:
    schema_fn = getattr(connection.session, "schema", None)
    user_schema = (await schema_fn()) if schema_fn is not None else None
    sqlglot_schema, sqlglot_tables = _build_information_schema(user_schema or {})

    try:
        result = sqlglot_execute(expr, schema=sqlglot_schema, tables=sqlglot_tables, dialect="postgres")
    except Exception:
        return None

    rows = [tuple(row) for row in result.rows]
    if rows:
        columns = [ResultColumn.for_type(name, type(value)) for name, value in zip(result.columns, rows[0])]
    else:
        columns = [ResultColumn(name, TEXT) for name in result.columns]
    return StaticStatement(expr.sql(dialect="postgres"), columns, rows)
