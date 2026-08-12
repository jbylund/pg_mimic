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

Transaction control (savepoints included), SET/RESET/SHOW and DISCARD/DEALLOCATE
are classified from the raw SQL text (a small, fixed grammar) rather than via
sqlglot's parse tree: sqlglot's postgres dialect
doesn't reliably produce clean nodes for these (e.g. "START TRANSACTION" parses
as a stray column-alias expression, and bare "SHOW x" falls back to a generic
Command node) -- simple regexes are more robust here than fighting the parser.
sqlglot *is* used for everything that needs real expression evaluation:
session-function SELECTs and the information_schema lookups in pg_mimic.catalog.
"""

from __future__ import annotations

import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute

from .catalog import information_schema_statement, pg_catalog_statement
from .errors import (
    ACTIVE_SQL_TRANSACTION,
    INVALID_SAVEPOINT_SPECIFICATION,
    INVALID_SQL_STATEMENT_NAME,
    NO_ACTIVE_SQL_TRANSACTION,
    SYNTAX_ERROR,
    PgError,
)
from .results import ResultColumn
from .session import Statement, StaticStatement, statement_from_rows
from .typeinfo import is_typeinfo_query, typeinfo_statement
from .types import TEXT

if TYPE_CHECKING:
    from .connection import Connection

logger = logging.getLogger(__name__)


# A quoted identifier keeps its case; an unquoted one folds to lower, exactly as
# Postgres folds them -- see `_identifier`.
_IDENT = r"(?:\"[^\"]+\"|\w+)"

# The name in a SET/RESET, which is a GUC name rather than an identifier: no dot,
# in either spelling. A dotted name is a *custom* GUC -- `SET app.tenant_id = 5`,
# the row-level-security/multi-tenancy pattern -- and matching it here would
# swallow it, leaving a session with nowhere to see it and a session fronting a
# real backend unable to forward it. Custom GUCs therefore fall through to the
# session, and stay there until Session.set_parameter() gives them a home (#35).
_SETTING_NAME = r"(?:\"[^\".]+\"|\w+)"

_BEGIN_RE = re.compile(r"^\s*(BEGIN|START\s+TRANSACTION)\b", re.IGNORECASE)
_COMMIT_RE = re.compile(r"^\s*(COMMIT|END)\b", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"^\s*ROLLBACK\b", re.IGNORECASE)

# Savepoints. `ROLLBACK TO [SAVEPOINT] x` has to be tested *before* _ROLLBACK_RE
# everywhere, here and in is_transaction_end(): it starts with ROLLBACK but does
# not end the transaction.
_SAVEPOINT_RE = re.compile(rf"^\s*SAVEPOINT\s+({_IDENT})\s*;?\s*$", re.IGNORECASE)
_RELEASE_RE = re.compile(rf"^\s*RELEASE\s+(?:SAVEPOINT\s+)?({_IDENT})\s*;?\s*$", re.IGNORECASE)
_ROLLBACK_TO_RE = re.compile(rf"^\s*ROLLBACK\s+(?:TRANSACTION\s+|WORK\s+)?TO\s+(?:SAVEPOINT\s+)?({_IDENT})\s*;?\s*$", re.IGNORECASE)

# SET, in every spelling a client actually sends. The specific forms are matched
# before the generic one: `SET TIME ZONE 'UTC'` and `SET SCHEMA 'x'` have no
# TO/= at all, so the generic pattern never sees them -- and the `(?!TO|=)`
# guards keep it the other way round too, so a GUC that happens to be spelled
# `schema` (`SET schema TO 'x'`) still goes to the generic pattern.
_SET_TIME_ZONE_RE = re.compile(r"^\s*SET\s+(?:SESSION\s+|LOCAL\s+)?TIME\s+ZONE\s+(?!TO\b|=)(.+?)\s*;?\s*$", re.IGNORECASE)
_SET_SCHEMA_RE = re.compile(r"^\s*SET\s+(?:SESSION\s+|LOCAL\s+)?SCHEMA\s+(?!TO\b|=)(.+?)\s*;?\s*$", re.IGNORECASE)
_SET_CHARACTERISTICS_RE = re.compile(r"^\s*SET\s+SESSION\s+CHARACTERISTICS\s+AS\s+TRANSACTION\s+(.+?)\s*;?\s*$", re.IGNORECASE)
_SET_RE = re.compile(rf"^\s*SET\s+(?:SESSION\s+|LOCAL\s+)?({_SETTING_NAME})\s*(?:TO|=)\s*(.+?)\s*;?\s*$", re.IGNORECASE)
_RESET_RE = re.compile(rf"^\s*RESET\s+(ALL|{_SETTING_NAME})\s*;?\s*$", re.IGNORECASE)
_SHOW_RE = re.compile(r"^\s*SHOW\s+(\S+?)\s*;?\s*$", re.IGNORECASE)
# The read counterpart of SET TIME ZONE, and the one multi-word SHOW worth
# special-casing -- _SHOW_RE only matches a single token.
_SHOW_TIME_ZONE_RE = re.compile(r"^\s*SHOW\s+TIME\s+ZONE\s*;?\s*$", re.IGNORECASE)

# Connection-state resets. DISCARD ALL is what pgbouncer sends between pooled
# clients, so anything behind a pooler hits it on every checkout.
_DISCARD_RE = re.compile(r"^\s*DISCARD\s+(ALL|PLANS|SEQUENCES|TEMPORARY|TEMP)\s*;?\s*$", re.IGNORECASE)
_DEALLOCATE_RE = re.compile(rf"^\s*DEALLOCATE\s+(?:PREPARE\s+)?(ALL|{_IDENT})\s*;?\s*$", re.IGNORECASE)

# SET ROLE / SET SESSION AUTHORIZATION (and their RESETs) change *authorization*,
# not display state, and pg_mimic has no roles to change to: no role catalog to
# validate the name against and no privilege model to apply it to. Accepting one
# would be a lie the rest of the connection can't back up -- current_user would
# go on reporting the startup user either way -- so they pass through to the
# session, which is the only place that can know what a role means here (and the
# only place that can forward them to a real backend). `SET ROLE x` has no TO/=
# so it never reaches _SET_RE; `SET ROLE TO x` does, hence this set.
_PASSED_THROUGH_SETTINGS = frozenset({"role", "session_authorization"})


def is_transaction_end(sql: str) -> bool:
    """COMMIT/END/ROLLBACK -- the statements that end a transaction block.

    `ROLLBACK TO SAVEPOINT x` deliberately isn't one: real Postgres stays in `T`.
    """
    stripped = sql.strip()
    if _ROLLBACK_TO_RE.match(stripped):
        return False
    return bool(_COMMIT_RE.match(stripped) or _ROLLBACK_RE.match(stripped))


def allowed_in_failed_transaction(sql: str) -> bool:
    """What a client may still send once the transaction has failed (`tx_status`
    `E`): the transaction-ending statements, plus `ROLLBACK TO SAVEPOINT`, which
    is the whole point of a savepoint -- it undoes the failure and leaves the
    transaction usable."""
    stripped = sql.strip()
    return is_transaction_end(stripped) or bool(_ROLLBACK_TO_RE.match(stripped))


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
    """BEGIN / START TRANSACTION / COMMIT / END / ROLLBACK, and the savepoint
    statements nested transactions (psycopg's nested `transaction()`, SQLAlchemy's
    `begin_nested()`) are built on: SAVEPOINT / RELEASE / ROLLBACK TO.

    Savepoints are answered here rather than forwarded, for the same reason
    BEGIN/COMMIT are: the transaction status this connection reports in
    ReadyForQuery is connection state, and a session that answered SAVEPOINT
    couldn't set it. A session fronting a real backend that needs the statements
    forwarded uses the seam that already exists -- drop or replace this one link
    in `Session.middleware` and the whole transaction-control group, savepoints
    included, arrives at your `query()` as a coherent unit.
    """
    # Before _ROLLBACK_RE: `ROLLBACK TO x` is not a transaction end.
    match = _ROLLBACK_TO_RE.match(ctx.sql)
    if match:
        return _savepoint_statement(ctx.connection, ctx.sql, "ROLLBACK TO", _identifier(match.group(1)))
    match = _SAVEPOINT_RE.match(ctx.sql)
    if match:
        return _savepoint_statement(ctx.connection, ctx.sql, "SAVEPOINT", _identifier(match.group(1)))
    match = _RELEASE_RE.match(ctx.sql)
    if match:
        return _savepoint_statement(ctx.connection, ctx.sql, "RELEASE", _identifier(match.group(1)))
    if _BEGIN_RE.match(ctx.sql):
        return _transaction_statement(ctx.connection, "BEGIN")
    if _COMMIT_RE.match(ctx.sql):
        return _transaction_statement(ctx.connection, "COMMIT")
    if _ROLLBACK_RE.match(ctx.sql):
        return _transaction_statement(ctx.connection, "ROLLBACK")
    return None


async def set_show(ctx: MiddlewareContext) -> Statement | None:
    """SET and RESET in the spellings clients use, and SHOW, against the
    connection's session vars:

    - `SET [SESSION|LOCAL] <name> TO|= <value>` (and `TO DEFAULT`)
    - `SET TIME ZONE <value>` -- an alias for the `timezone` GUC
    - `SET SCHEMA <value>` -- the SQL-standard alias for `SET search_path TO <value>`
    - `SET SESSION CHARACTERISTICS AS TRANSACTION ...` -- recorded as the
      `default_transaction_*` GUCs it actually sets
    - `RESET <name>` / `RESET ALL`
    - `SHOW <name>` and `SHOW TIME ZONE`

    `SET ROLE` and `SET SESSION AUTHORIZATION` pass through -- see
    `_PASSED_THROUGH_SETTINGS` for why. Any setting in the GUC_REPORT set gets a
    ParameterStatus back, the same as real Postgres.
    """
    statement = _set_variants(ctx.connection, ctx.sql)
    if statement is not None:
        return statement
    if _SHOW_TIME_ZONE_RE.match(ctx.sql):
        return _show_statement(ctx.connection, "timezone")
    show_match = _SHOW_RE.match(ctx.sql)
    if show_match:
        return _show_statement(ctx.connection, show_match.group(1))
    return None


async def session_reset(ctx: MiddlewareContext) -> Statement | None:
    """DISCARD and DEALLOCATE -- wholesale resets of connection state.

    `DISCARD ALL` is what pgbouncer sends when it hands a server connection to the
    next client, so every pooled deployment sends it; it clears session vars,
    prepared statements, portals and any open savepoints. The narrower
    `DISCARD PLANS|SEQUENCES|TEMP` have nothing to clear in a mimic (no plan
    cache, no sequences, no temp tables) but are still answered here rather than
    handed to a session that has nothing to do with them.

    `DEALLOCATE` belongs with them: SQL-level prepared statement names and
    protocol-level ones share a namespace in Postgres, and the protocol-level
    ones live on the Connection, so only the connection can drop them.
    """
    match = _DISCARD_RE.match(ctx.sql)
    if match:
        return _discard_statement(ctx.connection, ctx.sql, match.group(1).upper())
    match = _DEALLOCATE_RE.match(ctx.sql)
    if match:
        return _deallocate_statement(ctx.connection, ctx.sql, match.group(1))
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
    substituted, substitutions, on_execute = _substitute_session_functions(ctx.connection, expr)
    if not substitutions:
        return None
    return _evaluate_select(substituted, expr, on_execute)


async def static_select(ctx: MiddlewareContext) -> Statement | None:
    """Evaluate any table-less SELECT (`SELECT 1`, `SELECT 1 + 1`, ...) with
    sqlglot rather than passing it to the session.

    Not enabled by default -- it answers queries the session may well have its
    own meaning for. Add it to `Session.middleware` if you want it.
    """
    expr = ctx.select_without_tables()
    if expr is None:
        return None
    substituted, _substitutions, on_execute = _substitute_session_functions(ctx.connection, expr)
    return _evaluate_select(substituted, expr, on_execute)


async def information_schema(ctx: MiddlewareContext) -> Statement | None:
    """SELECTs against information_schema, answered from Session.schema()."""
    expr = ctx.expression
    if not isinstance(expr, exp.Select):
        return None
    tables = list(expr.find_all(exp.Table))
    if tables and any((t.db or "").lower() == "information_schema" for t in tables):
        return await information_schema_statement(ctx.connection, expr)
    return None


async def asyncpg_typeinfo(ctx: MiddlewareContext) -> Statement | None:
    """asyncpg's type introspection.

    Matched on the raw SQL rather than the parse tree, because sqlglot cannot parse
    this one at all -- and answered directly rather than executed, because its
    executor has no recursive CTE support. See pg_mimic.typeinfo.
    """
    if not is_typeinfo_query(ctx.sql):
        return None
    return typeinfo_statement(ctx.connection, ctx.sql)


async def pg_catalog(ctx: MiddlewareContext) -> Statement | None:
    """SELECTs against pg_catalog -- psql's \\dt, \\d and friends.

    Answered from Session.schema() like information_schema, but the SQL psql
    writes needs rewriting before sqlglot's executor will run it; see
    pg_mimic.catalog.
    """
    # exp.Query rather than exp.Select: psql's publications section is a UNION, and
    # matching only Select sent it to the session, which answered with a row of its
    # own that psql then printed as a publication.
    expr = ctx.expression
    if not isinstance(expr, exp.Query):
        return None
    tables = list(expr.find_all(exp.Table))
    if tables and any(_is_pg_catalog(table) for table in tables):
        return await pg_catalog_statement(ctx.connection, expr)
    return None


def _is_pg_catalog(table: exp.Table) -> bool:
    """Explicitly `pg_catalog.x`, or a bare `pg_*` name -- psql writes both, and a
    user table called pg_something would be shadowing a reserved prefix anyway."""
    if (table.db or "").lower() == "pg_catalog":
        return True
    return not table.db and table.name.lower().startswith("pg_")


DEFAULT_MIDDLEWARE = (
    transaction_control,
    set_show,
    session_reset,
    session_functions,
    information_schema,
    # Ahead of pg_catalog: this query names pg_catalog tables, but has to be
    # answered rather than executed.
    asyncpg_typeinfo,
    pg_catalog,
)


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


def _identifier(raw: str) -> str:
    """A SQL identifier as Postgres stores it: quoted keeps its case, unquoted folds
    to lower. For savepoint and prepared-statement names -- settings go through
    `_setting_name` instead."""
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
        return raw[1:-1]
    return raw.lower()


def _setting_name(raw: str) -> str:
    """The key a setting is tracked under. Unlike an identifier this folds whether
    quoted or not: GUC names are matched case-insensitively in Postgres however
    they were spelled, so `SET "SEARCH_PATH"` and `SET search_path` are one
    setting -- and SHOW has always read them back this way."""
    return raw.strip().strip('"').lower()


def _transaction_statement(connection: Connection, tag: str) -> Statement:
    def on_execute() -> None:
        # A BEGIN inside an open transaction block does not start a second one:
        # Postgres warns "there is already a transaction in progress" and carries
        # on with the first. The savepoints already taken in it are still live, so
        # this is the one case that must not clear them.
        redundant_begin = tag == "BEGIN" and connection.tx_status == b"T"
        connection.tx_status = b"T" if tag == "BEGIN" else b"I"
        if not redundant_begin:
            # Every savepoint belongs to the transaction that opened it, so a new
            # or finished transaction block starts with an empty stack either way.
            connection.savepoints.clear()

    return StaticStatement(tag, None, [], on_execute)


# The message real Postgres gives for a savepoint statement outside a transaction
# block, keyed by which statement it was. All three are 25P01.
_NO_TRANSACTION_MESSAGE = {
    "SAVEPOINT": "SAVEPOINT can only be used in transaction blocks",
    "RELEASE": "RELEASE SAVEPOINT can only be used in transaction blocks",
    "ROLLBACK TO": "ROLLBACK TO SAVEPOINT can only be used in transaction blocks",
}


def _savepoint_statement(connection: Connection, sql: str, kind: str, name: str) -> Statement:
    """SAVEPOINT / RELEASE / ROLLBACK TO, against `Connection.savepoints`.

    Deferred to Execute like every other state change here, so Parse stays free of
    side effects -- and so the transaction status these check is the one in force
    when the statement actually runs.
    """

    def on_execute() -> None:
        if connection.tx_status == b"I":
            raise PgError(NO_ACTIVE_SQL_TRANSACTION, _NO_TRANSACTION_MESSAGE[kind])
        if kind == "SAVEPOINT":
            # Repeating a name is legal: the new savepoint shadows the old one,
            # which is why this is a stack and not a set.
            connection.savepoints.append(name)
            return

        stack = connection.savepoints
        try:
            index = len(stack) - 1 - stack[::-1].index(name)
        except ValueError:
            raise PgError(INVALID_SAVEPOINT_SPECIFICATION, f'savepoint "{name}" does not exist') from None
        # RELEASE drops the named savepoint along with everything inside it;
        # ROLLBACK TO keeps it, so the same name can be rolled back to again.
        del stack[index if kind == "RELEASE" else index + 1 :]
        if kind == "ROLLBACK TO":
            # The point of the whole feature: the failed-transaction state ends
            # here, and the transaction itself carries on.
            connection.tx_status = b"T"

    return StaticStatement(sql, None, [], on_execute)


def _discard_statement(connection: Connection, sql: str, kind: str) -> Statement:
    def on_execute() -> None:
        if kind != "ALL":
            return  # PLANS/SEQUENCES/TEMP: a mimic has none of those to throw away
        # Real Postgres refuses DISCARD ALL inside a transaction block (25001);
        # the narrower forms are allowed there, hence the check being in here.
        if connection.tx_status != b"I":
            raise PgError(ACTIVE_SQL_TRANSACTION, "DISCARD ALL cannot run inside a transaction block")
        _reset_all(connection)
        connection.statements.clear()
        connection.portals.clear()
        connection.savepoints.clear()

    return StaticStatement(sql, None, [], on_execute)


def _deallocate_statement(connection: Connection, sql: str, raw_name: str) -> Statement:
    # An unquoted ALL is the keyword; `DEALLOCATE "all"` names a statement.
    discard_every = raw_name.upper() == "ALL" and not raw_name.startswith('"')
    # Not folded to lower like a real identifier would be: these names come off
    # the wire in Parse messages, where they are exact byte strings, and a client
    # deallocating one it named itself means the name it used.
    name = raw_name.strip('"')

    def on_execute() -> None:
        if discard_every:
            connection.statements.clear()
        elif connection.statements.pop(name, None) is None:
            raise PgError(INVALID_SQL_STATEMENT_NAME, f'prepared statement "{name}" does not exist')

    return StaticStatement(sql, None, [], on_execute)


_PLACEHOLDER_RE = re.compile(r"^\$\d+$")


def _reject_placeholder_value(raw: str) -> None:
    """Refuse `SET x TO $1`, as Postgres does.

    SET takes no parameters -- its value is part of the statement, not something
    Bind supplies -- so Postgres answers the Parse with a syntax error. Accepting
    it here would report a ParameterStatus whose value is the literal text "$1",
    and a client that reads the setting back acts on it: psycopg takes
    client_encoding at its word and every later row fails to decode, with the
    connection unusable and nothing to blame it on.
    """
    if _PLACEHOLDER_RE.match(raw.strip()):
        raise PgError(SYNTAX_ERROR, f'syntax error at or near "{raw.strip()}"')


def _set_value(raw: str) -> str | None:
    """The value a SET assigns, or None for the spellings that mean "back to the
    built-in default" (`SET x TO DEFAULT`, `SET TIME ZONE LOCAL`)."""
    value = raw.strip()
    if value.upper() in ("DEFAULT", "LOCAL"):
        return None
    return value.strip("'\"")


def _transaction_characteristics(clause: str) -> list[tuple[str, str | None]]:
    """The `default_transaction_*` GUCs `SET SESSION CHARACTERISTICS AS TRANSACTION
    ...` actually sets -- which is what real Postgres does with it too."""
    assignments: list[tuple[str, str | None]] = []
    isolation = re.search(
        r"ISOLATION\s+LEVEL\s+(READ\s+UNCOMMITTED|READ\s+COMMITTED|REPEATABLE\s+READ|SERIALIZABLE)", clause, re.IGNORECASE
    )
    if isolation:
        assignments.append(("default_transaction_isolation", " ".join(isolation.group(1).split()).lower()))
    if re.search(r"READ\s+ONLY", clause, re.IGNORECASE):
        assignments.append(("default_transaction_read_only", "on"))
    elif re.search(r"READ\s+WRITE", clause, re.IGNORECASE):
        assignments.append(("default_transaction_read_only", "off"))
    if re.search(r"NOT\s+DEFERRABLE", clause, re.IGNORECASE):
        assignments.append(("default_transaction_deferrable", "off"))
    elif re.search(r"\bDEFERRABLE\b", clause, re.IGNORECASE):
        assignments.append(("default_transaction_deferrable", "on"))
    return assignments


def _set_variants(connection: Connection, sql: str) -> Statement | None:
    """The SET/RESET half of `set_show`. Returns None for the spellings that are
    the session's business rather than the connection's."""
    match = _SET_TIME_ZONE_RE.match(sql)
    if match:
        return _assignment_statement(connection, sql, [("timezone", _set_value(match.group(1)))])

    match = _SET_SCHEMA_RE.match(sql)
    if match:
        # The SQL standard defines SET SCHEMA as exactly this, and so does Postgres.
        return _assignment_statement(connection, sql, [("search_path", _set_value(match.group(1)))])

    match = _SET_CHARACTERISTICS_RE.match(sql)
    if match:
        return _assignment_statement(connection, sql, _transaction_characteristics(match.group(1)))

    match = _SET_RE.match(sql)
    if match:
        name = _setting_name(match.group(1))
        if name in _PASSED_THROUGH_SETTINGS:
            return None
        _reject_placeholder_value(match.group(2))
        return _assignment_statement(connection, sql, [(name, _set_value(match.group(2)))])

    match = _RESET_RE.match(sql)
    if match:
        name = _setting_name(match.group(1))
        if name == "all":
            return _reset_all_statement(connection, sql)
        if name in _PASSED_THROUGH_SETTINGS:
            return None
        # RESET <name> is defined as SET <name> TO DEFAULT.
        return _assignment_statement(connection, sql, [(name, None)])

    return None


def _assignment_statement(connection: Connection, sql: str, assignments: Sequence[tuple[str, str | None]]) -> Statement:
    def on_execute() -> None:
        for name, value in assignments:
            _apply_set_config(connection, name, value)

    return StaticStatement(sql, None, [], on_execute)


def _reset_all_statement(connection: Connection, sql: str) -> Statement:
    def on_execute() -> None:
        _reset_all(connection)

    return StaticStatement(sql, None, [], on_execute)


def _reset_all(connection: Connection) -> None:
    """Drop every SET this connection has made. Reported settings are reported
    again on the way out, at whatever value they revert to."""
    names = list(connection.session_vars)
    connection.session_vars.clear()
    for name in names:
        _report_setting(connection, name)


# The GUC_REPORT settings: real Postgres sends a ParameterStatus whenever one of
# these changes, and that is how a client learns things it can't otherwise ask
# about mid-connection -- psycopg's `conn.info.encoding` is just the cached
# client_encoding report. Keys are the lowercased name tracked in session_vars;
# values are the name Postgres reports under, casing included, because a client
# indexes its parameter cache by that exact string.
#
# search_path only joined the set in PG 18. Reported here regardless: a client
# that doesn't expect it caches a value it never reads, which is harmless, where
# one that does expect it would otherwise read a stale search_path.
_REPORTED_SETTINGS = {
    "application_name": "application_name",
    "client_encoding": "client_encoding",
    "datestyle": "DateStyle",
    "intervalstyle": "IntervalStyle",
    "is_superuser": "is_superuser",
    "search_path": "search_path",
    "session_authorization": "session_authorization",
    "standard_conforming_strings": "standard_conforming_strings",
    "timezone": "TimeZone",
}


def _report_setting(connection: Connection, key: str) -> None:
    """Queue the ParameterStatus a change to `key` owes the client, if it owes one."""
    reported = _REPORTED_SETTINGS.get(key)
    if reported is not None:
        connection.report_parameter(reported, _setting_value(connection, key))


_DEFAULT_SETTINGS = {
    "application_name": lambda c: c.startup_params.get("application_name", ""),
    "client_encoding": lambda c: c.server.parameter_status.get("client_encoding", "UTF8"),
    "datestyle": lambda c: c.server.parameter_status.get("DateStyle", "ISO, MDY"),
    "default_transaction_deferrable": lambda c: "off",
    "default_transaction_isolation": lambda c: "read committed",
    "default_transaction_read_only": lambda c: "off",
    "intervalstyle": lambda c: c.server.parameter_status.get("IntervalStyle", "postgres"),
    "is_superuser": lambda c: "off",
    "jit": lambda c: "off",
    "search_path": lambda c: '"$user", public',
    "server_encoding": lambda c: c.server.parameter_status.get("server_encoding", "UTF8"),
    "server_version": lambda c: c.server.parameter_status.get("server_version", ""),
    "session_authorization": lambda c: c.username,
    "standard_conforming_strings": lambda c: "on",
    "timezone": lambda c: c.server.parameter_status.get("TimeZone", "UTC"),
}


def _show_statement(connection: Connection, name: str) -> Statement:
    key = _setting_name(name)
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


def _setting_value(connection: Connection, key: str) -> str:
    """Current value of a setting. Unknown settings read as empty rather than
    failing, matching what SHOW already does for them."""
    if key in connection.session_vars:
        return connection.session_vars[key]
    if key in _DEFAULT_SETTINGS:
        return _DEFAULT_SETTINGS[key](connection)
    return ""


def _literal_text(node: exp.Expression) -> str | None:
    """The text of a literal argument, or None if it isn't a plain literal."""
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Literal):
        return str(node.this)
    if isinstance(node, exp.Boolean):
        return "on" if node.this else "off"
    return None


def _substitute_session_functions(
    connection: Connection, expr: exp.Expression
) -> tuple[exp.Expression, int, Callable[[], None] | None]:
    """Replace session-state functions with literals.

    Returns the rewritten expression, how many substitutions were made, and any
    deferred side effect. The count of zero is how `session_functions` tells
    "this SELECT is about the connection" apart from "this SELECT is the
    session's business".

    `set_config` is the one function here that *writes*, so its effect is handed
    back to run at Execute rather than applied during this rewrite -- Parse must
    not change session state, exactly as a SET statement's does not.
    """
    expr = expr.copy()
    substitutions = 0
    effects: list[Callable[[], None]] = []

    for node_type, resolver in _SESSION_NULLARY_FUNCS.items():
        for node in list(expr.find_all(node_type)):
            node.replace(exp.Literal.string(resolver(connection)))
            substitutions += 1

    for node in list(expr.find_all(exp.Anonymous)):
        name = node.this.lower() if isinstance(node.this, str) else ""
        if name == "current_setting" and node.expressions and isinstance(node.expressions[0], exp.Literal):
            key = str(node.expressions[0].this).lower()
            node.replace(exp.Literal.string(_setting_value(connection, key)))
            substitutions += 1
        elif name == "set_config" and len(node.expressions) >= 2 and isinstance(node.expressions[0], exp.Literal):
            key = str(node.expressions[0].this).lower()
            new_value = _literal_text(node.expressions[1])
            if new_value is None and not isinstance(node.expressions[1], exp.Null):
                continue  # not a literal we can evaluate -- leave it for the session
            effects.append(partial(_apply_set_config, connection, key, new_value))
            # set_config returns the value it just set; NULL resets and returns empty.
            node.replace(exp.Literal.string(new_value if new_value is not None else ""))
            substitutions += 1
        elif name == "pg_backend_pid":
            node.replace(exp.Literal.number(connection.pid))
            substitutions += 1

    return expr, substitutions, _run_all(effects) if effects else None


def _run_all(effects: list[Callable[[], None]]) -> Callable[[], None]:
    def run() -> None:
        for effect in effects:
            effect()

    return run


def _apply_set_config(connection: Connection, key: str, value: str | None) -> None:
    """Apply one setting change, from SET/RESET or from set_config(). None means
    "back to the built-in default", i.e. forget the override."""
    if value is None:
        connection.session_vars.pop(key, None)
    else:
        connection.session_vars[key] = value
    _report_setting(connection, key)


def _evaluate_select(
    substituted: exp.Expression,
    original: exp.Select,
    on_execute: Callable[[], None] | None = None,
) -> Statement | None:
    try:
        result = sqlglot_execute(substituted, dialect="postgres")
    except Exception:
        return None  # not something we can statically evaluate -- fall through

    return statement_from_rows(original.sql(dialect="postgres"), result.columns, [tuple(row) for row in result.rows], on_execute)
