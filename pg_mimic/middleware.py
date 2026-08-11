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

`Session.middleware` is a plain class attribute: reorder it, drop links, append
your own `async (MiddlewareContext) -> Statement | None`, or set it to `()` for a
session that sees every statement untouched.

Transaction control and SET/SHOW are classified from the raw SQL text (a small,
fixed grammar) rather than via sqlglot's parse tree: sqlglot's postgres dialect
doesn't reliably produce clean nodes for these (e.g. "START TRANSACTION" parses
as a stray column-alias expression, and bare "SHOW x" falls back to a generic
Command node) -- simple regexes are more robust here than fighting the parser.
sqlglot *is* used for everything that needs real expression evaluation:
session-function SELECTs and the information_schema lookups in pg_mimic.catalog.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute

from .catalog import information_schema_statement
from .results import ResultColumn
from .session import Statement, StaticStatement
from .types import TEXT

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)


_BEGIN_RE = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^\s*(COMMIT|END)\b", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"^\s*ROLLBACK\b", re.IGNORECASE)
_SET_RE = re.compile(r"^\s*SET\s+(?:SESSION\s+|LOCAL\s+)?(\w+)\s*(?:TO|=)\s*(.+?)\s*;?\s*$", re.IGNORECASE)
_SHOW_RE = re.compile(r"^\s*SHOW\s+(\S+?)\s*;?\s*$", re.IGNORECASE)


def is_transaction_end(sql: str) -> bool:
    stripped = sql.strip()
    return bool(_COMMIT_RE.match(stripped) or _ROLLBACK_RE.match(stripped))


class MiddlewareContext:
    """What a middleware gets to look at: the connection, the raw SQL, and the
    declared parameter OIDs. Parses lazily and once, so a chain of SELECT-shaped
    middleware doesn't re-parse the same statement for each link."""

    __slots__ = ("connection", "sql", "param_oids", "_expression")

    _UNPARSED = object()

    def __init__(self, connection: Connection, sql: str, param_oids: list[int | None]):
        self.connection = connection
        self.sql = sql.strip()
        self.param_oids = param_oids
        self._expression: Any = self._UNPARSED

    @property
    def expression(self) -> exp.Expression | None:
        """The parsed statement, or None if sqlglot can't parse it. Not an error:
        pg_mimic isn't a full SQL parser, and syntax sqlglot doesn't support must
        still reach the session rather than fail here."""
        if self._expression is self._UNPARSED:
            try:
                self._expression = sqlglot.parse_one(self.sql, dialect="postgres")
            except Exception:
                self._expression = None
        return self._expression

    def select_without_tables(self) -> exp.Select | None:
        """The parsed statement if it's a SELECT referencing no tables, else None."""
        expr = self.expression
        if isinstance(expr, exp.Select) and not list(expr.find_all(exp.Table)):
            return expr
        return None


# Defined below MiddlewareContext rather than beside the other module-level names:
# a type alias is evaluated at runtime, so `from __future__ import annotations`
# doesn't apply and a forward reference here would have to stay quoted.
Middleware = Callable[[MiddlewareContext], Awaitable[Statement | None]]


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
        return await information_schema_statement(ctx.connection, expr)
    return None


DEFAULT_MIDDLEWARE = (transaction_control, set_show, session_functions, information_schema)


async def resolve(
    connection: Connection,
    sql: str,
    param_oids: list[int | None],
    middleware: Sequence[Middleware] = DEFAULT_MIDDLEWARE,
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


def _transaction_statement(connection: Connection, tag: str) -> Statement:
    def on_execute() -> None:
        connection.tx_status = b"T" if tag == "BEGIN" else b"I"

    return StaticStatement(tag, None, [], on_execute)


def _set_statement(connection: Connection, sql: str, match: re.Match) -> Statement:
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


def _show_statement(connection: Connection, name: str) -> Statement:
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


def _substitute_session_functions(connection: Connection, expr: exp.Expression) -> tuple[exp.Expression, int]:
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
