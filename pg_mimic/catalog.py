"""SET/SHOW/transaction-control interception, static (no-table) SELECT
evaluation, and a minimal information_schema subset -- mirrors mysql-mimic's
middleware chain, adapted to Postgres semantics. This is the fallback chain
`Session.prepare()` walks before handing an unrecognized statement to the
session author's own describe()/query().

Transaction control and SET/SHOW are classified from the raw SQL text (a
small, fixed grammar) rather than via sqlglot's parse tree: sqlglot's
postgres dialect doesn't reliably produce clean nodes for these (e.g.
"START TRANSACTION" parses as a stray column-alias expression, and bare
"SHOW x" falls back to a generic Command node) -- simple regexes are more
robust here than fighting the parser. sqlglot *is* used for everything that
needs real expression evaluation: static SELECTs and information_schema
lookups.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, AsyncIterator

import sqlglot
from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute

from .results import ResultColumn
from .session import Portal, Row, Statement, drain_rows
from .types import TEXT

if TYPE_CHECKING:
    from .connection import Connection


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


async def resolve(connection: "Connection", sql: str, param_oids: list[int | None]) -> Statement | None:
    """Returns a Statement if this SQL text is one of the intercepted
    statement shapes, else None (the caller falls back to the session
    author's own describe()/query())."""
    stripped = sql.strip()
    if not stripped:
        return None

    if _BEGIN_RE.match(stripped):
        return _transaction_statement(connection, "BEGIN")
    if _COMMIT_RE.match(stripped):
        return _transaction_statement(connection, "COMMIT")
    if _ROLLBACK_RE.match(stripped):
        return _transaction_statement(connection, "ROLLBACK")

    set_match = _SET_RE.match(stripped)
    if set_match:
        return _set_statement(connection, stripped, set_match)

    show_match = _SHOW_RE.match(stripped)
    if show_match:
        return _show_statement(connection, show_match.group(1))

    try:
        expr = sqlglot.parse_one(stripped, dialect="postgres")
    except Exception:
        return None

    if isinstance(expr, exp.Select):
        tables = list(expr.find_all(exp.Table))
        if not tables:
            return _static_select_statement(connection, expr)
        if any((t.db or "").lower() == "information_schema" for t in tables):
            return await _catalog_select_statement(connection, expr)

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


def _substitute_session_functions(connection: "Connection", expr: exp.Expression) -> exp.Expression:
    expr = expr.copy()
    for node_type, resolver in _SESSION_NULLARY_FUNCS.items():
        for node in list(expr.find_all(node_type)):
            node.replace(exp.Literal.string(resolver(connection)))
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
        elif name == "pg_backend_pid":
            node.replace(exp.Literal.number(connection.pid))
    return expr


def _static_select_statement(connection: "Connection", expr: exp.Select) -> Statement | None:
    try:
        substituted = _substitute_session_functions(connection, expr)
        result = sqlglot_execute(substituted, dialect="postgres")
    except Exception:
        return None  # not something we can statically evaluate -- fall through

    rows = [tuple(row) for row in result.rows]
    if rows:
        columns = [ResultColumn.for_type(name, type(value)) for name, value in zip(result.columns, rows[0])]
    else:
        columns = [ResultColumn(name, TEXT) for name in result.columns]
    return StaticStatement(expr.sql(dialect="postgres"), columns, rows)


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
