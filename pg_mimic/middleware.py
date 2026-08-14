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

import inspect
import logging
import re
from functools import partial
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute
from sqlglot.tokens import TokenType

from . import settings_catalog, settings_values
from .catalog import information_schema_statement, pg_catalog_statement
from .copy import copy_statement
from .describe import oid_for_declared_type
from .errors import (
    ACTIVE_SQL_TRANSACTION,
    CANT_CHANGE_RUNTIME_PARAM,
    INVALID_SAVEPOINT_SPECIFICATION,
    INVALID_SQL_STATEMENT_NAME,
    NO_ACTIVE_SQL_TRANSACTION,
    SYNTAX_ERROR,
    UNDEFINED_OBJECT,
    PgError,
)
from .results import ResultColumn
from .session import Session, Statement, StaticStatement, statement_from_rows
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
_SET_RE = re.compile(rf"^\s*SET\s+(SESSION\s+|LOCAL\s+)?({_SETTING_NAME})\s*(?:TO|=)\s*(.+?)\s*;?\s*$", re.IGNORECASE)
_RESET_RE = re.compile(rf"^\s*RESET\s+(ALL|{_SETTING_NAME})\s*;?\s*$", re.IGNORECASE)
_SHOW_RE = re.compile(r"^\s*SHOW\s+(\S+?)\s*;?\s*$", re.IGNORECASE)
# The read counterpart of SET TIME ZONE, and the one multi-word SHOW worth
# special-casing -- _SHOW_RE only matches a single token.
_SHOW_TIME_ZONE_RE = re.compile(r"^\s*SHOW\s+TIME\s+ZONE\s*;?\s*$", re.IGNORECASE)
# `SHOW ALL` is the whole table, not a parameter named "all" -- unquoted only, since
# `SHOW "all"` asks for a parameter of that name and there isn't one.
_SHOW_ALL_RE = re.compile(r"^\s*SHOW\s+ALL\s*;?\s*$", re.IGNORECASE)

# Connection-state resets. DISCARD ALL is what pgbouncer sends between pooled
# clients, so anything behind a pooler hits it on every checkout.
_DISCARD_RE = re.compile(r"^\s*DISCARD\s+(ALL|PLANS|SEQUENCES|TEMPORARY|TEMP)\s*;?\s*$", re.IGNORECASE)
_DEALLOCATE_RE = re.compile(rf"^\s*DEALLOCATE\s+(?:PREPARE\s+)?(ALL|{_IDENT})\s*;?\s*$", re.IGNORECASE)

# SQL-level prepared statements. The parenthesised list on PREPARE declares
# parameter types; on EXECUTE it supplies values. Both are optional, and both are
# read from the raw text for the reason the rest of this module is: sqlglot parses
# either into a bare Command whose whole tail is one string literal.
_PREPARE_RE = re.compile(
    rf"^\s*PREPARE\s+({_IDENT})\s*(?:\(([^)]*)\))?\s+AS\s+(.+?)\s*;?\s*$",
    re.IGNORECASE | re.DOTALL,
)
_EXECUTE_RE = re.compile(rf"^\s*EXECUTE\s+({_IDENT})\s*(?:\((.*)\))?\s*;?\s*$", re.IGNORECASE | re.DOTALL)

# SET ROLE / SET SESSION AUTHORIZATION (and their RESETs) change *authorization*,
# not display state, and pg_mimic has no roles to change to: no role catalog to
# validate the name against and no privilege model to apply it to. Accepting one
# would be a lie the rest of the connection can't back up -- current_user would
# go on reporting the startup user either way -- so they pass through to the
# session, which is the only place that can know what a role means here (and the
# only place that can forward them to a real backend). `SET ROLE x` has no TO/=
# so it never reaches _SET_RE; `SET ROLE TO x` does, hence this set.
_PASSED_THROUGH_SETTINGS = frozenset({"role", "session_authorization"})

# LISTEN/UNLISTEN/NOTIFY. The channel is an ordinary identifier -- `NOTIFY CHAN`
# and `NOTIFY chan` are one channel, `NOTIFY "CHAN"` another -- and the payload is
# a bare string literal rather than an expression: PostgreSQL 18 answers
# `NOTIFY c, 'a' || 'b'` with a syntax error, so there is nothing to evaluate here.
_LISTEN_RE = re.compile(rf"^\s*LISTEN\s+({_IDENT})\s*;?\s*$", re.IGNORECASE)
_UNLISTEN_RE = re.compile(rf"^\s*UNLISTEN\s+(\*|{_IDENT})\s*;?\s*$", re.IGNORECASE)
_NOTIFY_RE = re.compile(rf"^\s*NOTIFY\s+({_IDENT})\s*(?:,\s*'((?:[^']|'')*)')?\s*;?\s*$", re.IGNORECASE)


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


async def copy_stdio(ctx: MiddlewareContext) -> Statement | None:
    """`COPY ... FROM STDIN` and `COPY ... TO STDOUT`.

    Here rather than in the session because the copy sub-protocol is framing, not
    query semantics -- `Session.copy_in()`/`copy_out()` deal in decoded rows. A
    COPY that reached the session instead would leave the client waiting for a
    CopyInResponse that never comes, so this link also raises (rather than passing
    the statement along) for a COPY it recognises but can't serve. See
    pg_mimic.copy.
    """
    return copy_statement(ctx.connection.session, ctx.sql)


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
    if _SHOW_ALL_RE.match(ctx.sql):
        return _show_all_statement(ctx.connection)
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


async def listen_notify(ctx: MiddlewareContext) -> Statement | None:
    """`LISTEN <channel>`, `UNLISTEN <channel>`, `UNLISTEN *` and
    `NOTIFY <channel>[, 'payload']`.

    Here rather than in the session for the same reason transaction control is:
    the subscription lives on the connection and the fanout on the server, and a
    session can reach neither. `Connection.notify_listeners()` is the seam for a
    session that wants to raise an event itself.

    All four are transactional, which is the part worth getting right and is
    modelled on PostgreSQL 18 rather than assumed. A `NOTIFY` is delivered at
    `COMMIT` and dropped by `ROLLBACK`; a `LISTEN` does not start receiving until
    its transaction commits; and both revert with a `ROLLBACK TO SAVEPOINT`. See
    Connection.notify_listeners() and SessionState.
    """
    match = _LISTEN_RE.match(ctx.sql)
    if match:
        return _listen_statement(ctx.connection, ctx.sql, _identifier(match.group(1)))
    match = _UNLISTEN_RE.match(ctx.sql)
    if match:
        raw = match.group(1)
        return _unlisten_statement(ctx.connection, ctx.sql, None if raw == "*" else _identifier(raw))
    match = _NOTIFY_RE.match(ctx.sql)
    if match:
        payload = (match.group(2) or "").replace("''", "'")
        return _notify_statement(ctx.connection, ctx.sql, _identifier(match.group(1)), payload)
    return None


def _listen_statement(connection: Connection, sql: str, channel: str) -> Statement:
    def on_execute() -> None:
        connection.state.listening.add(channel)
        # Outside a transaction the write is immediately real; inside one it waits
        # for the COMMIT that syncs it. Listening twice is listening once -- a set,
        # which is also why a repeated LISTEN delivers a notification once.
        if connection.tx_status == b"I":
            connection.sync_listeners()

    return StaticStatement(sql, None, [], on_execute)


def _unlisten_statement(connection: Connection, sql: str, channel: str | None) -> Statement:
    """`UNLISTEN x`, or `UNLISTEN *` when `channel` is None.

    Unsubscribing from a channel that was never subscribed to is not an error in
    Postgres, so `discard` rather than `remove`.
    """

    def on_execute() -> None:
        if channel is None:
            connection.state.listening.clear()
        else:
            connection.state.listening.discard(channel)
        if connection.tx_status == b"I":
            connection.sync_listeners()

    return StaticStatement(sql, None, [], on_execute)


def _notify_statement(connection: Connection, sql: str, channel: str, payload: str) -> Statement:
    def on_execute() -> None:
        connection.notify_listeners(channel, payload)

    return StaticStatement(sql, None, [], on_execute)


async def prepared_statements(ctx: MiddlewareContext) -> Statement | None:
    """SQL-level `PREPARE` and `EXECUTE`, against the same registry the protocol's
    Parse writes to.

    Postgres has one prepared-statement namespace with two entrances, and they
    genuinely share: SQL can `DEALLOCATE` a statement that Parse created, and
    `pg_prepared_statements` lists both with a `from_sql` flag to tell them apart.
    So PREPARE resolves its inner query exactly as Parse would and stores the
    Statement under the given name, and EXECUTE binds that same Statement to the
    arguments the SQL supplies.

    Doing it here rather than leaving it to the session is what makes DEALLOCATE
    coherent: one registry, so a name is either in it or is 26000.
    """
    match = _PREPARE_RE.match(ctx.sql)
    if match:
        return await _prepare_statement(ctx, match)
    match = _EXECUTE_RE.match(ctx.sql)
    if match:
        return _execute_statement(ctx, match)
    return None


async def _prepare_statement(ctx: MiddlewareContext, match: re.Match) -> Statement:
    name = _identifier(match.group(1))
    declared = [oid_for_declared_type(part) for part in _split_arguments(match.group(2) or "")]
    inner = match.group(3)

    # Resolved now rather than in on_execute, which is synchronous -- and resolved
    # through the session, so the prepared query goes down exactly the path it
    # would have taken had the client sent it directly.
    prepared = await ctx.connection.session.prepare(inner, declared)

    def on_execute() -> None:
        ctx.connection.state.statements[name] = prepared

    return StaticStatement(ctx.sql, None, [], on_execute)


def _execute_statement(ctx: MiddlewareContext, match: re.Match) -> Statement:
    name = _identifier(match.group(1))
    prepared = ctx.connection.state.statements.get(name)
    if prepared is None:
        raise PgError(INVALID_SQL_STATEMENT_NAME, f'prepared statement "{name}" does not exist')
    return _PreparedExecution(prepared, _split_arguments(match.group(2) or ""))


class _PreparedExecution(Statement):
    """`EXECUTE p (...)` as the prepared statement it names.

    A thin wrapper rather than the Statement itself, because the arguments come
    from the SQL text here instead of from Bind -- so bind() ignores what the
    protocol passes and uses them. `sql` is the *prepared* query's text, which is
    what makes the command tag right: Postgres answers `EXECUTE p` of a SELECT
    with `SELECT n`, not `EXECUTE`.
    """

    def __init__(self, prepared: Statement, arguments: list[str | None]):
        self.sql = prepared.sql
        self.param_oids: list[int | None] = []
        self._prepared = prepared
        self._arguments = arguments

    async def describe(self) -> list[ResultColumn] | None:
        return await self._prepared.describe()

    def bind(self, params: list[str | None]) -> Any:
        return self._prepared.bind(self._arguments)


def _split_arguments(text: str) -> list[str | None]:
    """The parenthesised list on PREPARE or EXECUTE, split on its top-level commas.

    Tokenized rather than split on ",", so a comma inside a string literal is a
    character rather than a separator. Values come back as the decoded text a
    bound parameter would have been, and a bare NULL as None.
    """
    if not text.strip():
        return []
    try:
        tokens = sqlglot.Dialect.get_or_raise("postgres").tokenize(text)
    except Exception:
        raise PgError(SYNTAX_ERROR, f"could not read the argument list {text!r}") from None

    arguments: list[str | None] = []
    current: list[str] = []
    for token in tokens:
        if token.token_type is TokenType.COMMA:
            arguments.append(_argument_value(current))
            current = []
        else:
            current.append(token.text)
    arguments.append(_argument_value(current))
    return arguments


def _argument_value(tokens: list[str]) -> str | None:
    text = " ".join(tokens).strip()
    return None if text.upper() == "NULL" else text


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
    return _evaluate_select(substituted, expr, on_execute, connection=ctx.connection)


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
    # First, and cheap: COPY is the one statement whose classification the client
    # is already blocked on, so nothing else gets a look at it.
    copy_stdio,
    transaction_control,
    set_show,
    session_reset,
    listen_notify,
    prepared_statements,
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


def _end_transaction(connection: Connection, committed: bool) -> None:
    """Settle the settings at COMMIT or ROLLBACK, reporting what moved.

    Both ends can change what the client sees -- a COMMIT drops the SET LOCALs, a
    ROLLBACK drops everything the transaction set -- so the settings that differ
    afterwards owe a ParameterStatus just as a SET does.
    """
    before = dict(connection.state.session_vars)
    if committed:
        connection.state.commit_transaction()
    else:
        connection.state.end_transaction()
    for name in set(before) | set(connection.state.session_vars):
        if before.get(name) != connection.state.session_vars.get(name):
            _report_setting(connection, name)

    # The LISTEN/NOTIFY half of settling the transaction. A COMMIT is where the
    # notifications it raised become real; a ROLLBACK has already dropped them,
    # end_transaction() having restored the list from the frame BEGIN opened. Both
    # then push the subscriptions the transaction left behind to the registry --
    # which is what makes a LISTEN take effect at commit and a rolled-back one not
    # take effect at all.
    if committed:
        connection.flush_pending_notifies()
    connection.sync_listeners()


def _transaction_statement(connection: Connection, tag: str) -> Statement:
    def on_execute() -> None:
        # A BEGIN inside an open transaction block does not start a second one:
        # Postgres warns "there is already a transaction in progress" and carries
        # on with the first. The savepoints already taken in it are still live, so
        # this is the one case that must not clear them.
        redundant_begin = tag == "BEGIN" and connection.tx_status == b"T"
        was_open = connection.tx_status != b"I"
        connection.tx_status = b"T" if tag == "BEGIN" else b"I"
        if redundant_begin:
            return
        # Every savepoint belongs to the transaction that opened it, so a new or
        # finished transaction block starts with an empty stack either way.
        connection.state.savepoints.clear()

        if tag == "BEGIN":
            connection.state.open_scope()
        elif was_open:
            _end_transaction(connection, committed=tag == "COMMIT")

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
            connection.state.savepoints.append(name)
            connection.state.open_scope()
            return

        stack = connection.state.savepoints
        try:
            index = len(stack) - 1 - stack[::-1].index(name)
        except ValueError:
            raise PgError(INVALID_SAVEPOINT_SPECIFICATION, f'savepoint "{name}" does not exist') from None
        # RELEASE drops the named savepoint along with everything inside it;
        # ROLLBACK TO keeps it, so the same name can be rolled back to again.
        del stack[index if kind == "RELEASE" else index + 1 :]
        # Frame 0 is the transaction itself, so savepoint N is frame N + 1.
        if kind == "RELEASE":
            # Keeps whatever the released scope set -- only the frames go.
            connection.state.discard_scopes(index + 1)
        else:
            before = dict(connection.state.session_vars)
            connection.state.restore_scope(index + 1)
            for setting in set(before) | set(connection.state.session_vars):
                if before.get(setting) != connection.state.session_vars.get(setting):
                    _report_setting(connection, setting)
            # restore_scope put the subscriptions back too; the registry has to be
            # told. A no-op unless the rolled-back scope contained a LISTEN that
            # had already committed -- which it cannot have, so this is belt and
            # braces rather than load-bearing, and cheap enough to keep.
            connection.sync_listeners()
            # The point of the whole feature: the failed-transaction state ends
            # here, and the transaction itself carries on.
            connection.tx_status = b"T"

    return StaticStatement(sql, None, [], on_execute)


def _discard_statement(connection: Connection, sql: str, kind: str) -> Statement:
    async def on_execute() -> None:
        if kind != "ALL":
            return  # PLANS/SEQUENCES/TEMP: a mimic has none of those to throw away
        # Real Postgres refuses DISCARD ALL inside a transaction block (25001);
        # the narrower forms are allowed there, hence the check being in here.
        if connection.tx_status != b"I":
            raise PgError(ACTIVE_SQL_TRANSACTION, "DISCARD ALL cannot run inside a transaction block")
        # _reset_all first, because it does what reset() cannot: it reports each
        # changed setting back to the client. reset() then clears the rest of the
        # DISCARD ALL surface (and the settings again, by then already empty).
        await _reset_all(connection)
        connection.state.reset()
        # `DISCARD ALL` includes `UNLISTEN *` -- reset() cleared the set, and this
        # is what makes the server stop delivering to us.
        connection.sync_listeners()

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
            connection.state.statements.clear()
        elif connection.state.statements.pop(name, None) is None:
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
        name = _setting_name(match.group(2))
        if name in _PASSED_THROUGH_SETTINGS:
            return None
        _reject_placeholder_value(match.group(3))
        local = (match.group(1) or "").strip().upper() == "LOCAL"
        return _assignment_statement(connection, sql, [(name, _set_value(match.group(3)))], local=local)

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


def _assignment_statement(
    connection: Connection,
    sql: str,
    assignments: Sequence[tuple[str, str | None]],
    local: bool = False,
) -> Statement:
    async def on_execute() -> None:
        outside_a_transaction = local and connection.tx_status == b"I"
        if outside_a_transaction:
            # Real Postgres does nothing and warns while doing it -- the exact
            # wording, severity and SQLSTATE read off a PostgreSQL 18 socket.
            connection.notice(
                "SET LOCAL can only be used in transaction blocks",
                severity="WARNING",
                C=NO_ACTIVE_SQL_TRANSACTION,
            )
        # Every name before any assignment, so a statement that assigns several --
        # SET SESSION CHARACTERISTICS is one -- either applies all of them or none.
        # This runs even when the SET LOCAL above is going nowhere: 18.4 warns and
        # *then* refuses `SET LOCAL shared_buffers = '1GB'`, both, in that order.
        for name, _ in assignments:
            _check_settable(name)
        if outside_a_transaction:
            return
        for name, value in assignments:
            await _apply_set_config(connection, name, value, local=local)

    return StaticStatement(sql, None, [], on_execute)


def _reset_all_statement(connection: Connection, sql: str) -> Statement:
    async def on_execute() -> None:
        await _reset_all(connection)

    return StaticStatement(sql, None, [], on_execute)


async def _reset_all(connection: Connection) -> None:
    """Drop every SET this connection has made. Reported settings are reported again
    on the way out, at whatever value they revert to.

    One `_apply_set_config` per name rather than clearing the dicts, so a session
    hears each reset as it would hear `RESET x` written out (#35). A session that
    refuses one leaves the rest reset, which is what Postgres does with a failure
    partway through -- there is no transaction around a RESET ALL.
    """
    for name in list(connection.state.session_vars):
        await _apply_set_config(connection, name, None)


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
        # Every reported setting has a built-in default, so the value is never the
        # None that means "no such setting" -- the `or` is for the type checker.
        connection.report_parameter(reported, _setting_value(connection, key) or "")


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
    "port": lambda c: str(c.server.port),
    "server_version": lambda c: c.server.parameter_status.get("server_version", ""),
    "server_version_num": lambda c: _version_num(c.server.parameter_status.get("server_version", "")),
    "session_authorization": lambda c: c.username,
    "standard_conforming_strings": lambda c: "on",
    "timezone": lambda c: c.server.parameter_status.get("TimeZone", "UTC"),
}


def _version_num(version: str) -> str:
    """PostgreSQL's numeric spelling of a version string: 16.0 is 160000, 18.4 is 180004.

    Derived rather than catalogued because the two have to agree. The catalogue is
    generated from whichever server it was pointed at, so leaving this to it tells a
    client the release of *that* server while `SHOW server_version` and the startup
    ParameterStatus report pg_mimic's own -- and a client that gates a feature on the
    number then acts on a version it is not talking to. `port` is the same story: the
    catalogued 5432 is a fact about PostgreSQL's default, not about this listener.
    """
    head = version.split()[0] if version.split() else ""
    parts = head.split(".")
    try:
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
    except (IndexError, ValueError):
        return ""
    return str(major * 10000 + minor)


def _show_statement(connection: Connection, name: str) -> Statement:
    key = _setting_name(name)

    def rows() -> list[tuple]:
        # Read when the statement runs, not when it is parsed. A prepared SHOW is
        # parsed once and executed many times, and the setting moves under it --
        # see #63. The same goes for whether the setting exists at all: a `SET
        # app.x` between Parse and Execute turns this from an error into a value.
        value = _setting_value(connection, key)
        if value is None:
            raise _unrecognized_setting(key)
        return [(value,)]

    return StaticStatement(f"SHOW {name}", [ResultColumn(key, TEXT)], rows)


def _show_all_statement(connection: Connection) -> Statement:
    """Every parameter this connection can see, in Postgres's three columns.

    Answered from the catalogue plus whatever the connection has set. Before there
    was a catalogue this read as a parameter named "all" and returned one bogus
    empty row; once unknown names started erroring it became a 42704, which is worse
    for the tools that run it on connect.
    """
    columns = [ResultColumn("name", TEXT), ResultColumn("setting", TEXT), ResultColumn("description", TEXT)]

    def rows() -> list[tuple]:
        names = set(settings_catalog.SETTINGS) | set(_DEFAULT_SETTINGS)
        names |= set(connection.state.session_vars) | set(connection.state.known_settings)
        found = []
        for name in sorted(names):
            value = _setting_value(connection, name)
            if value is None:
                continue
            entry = settings_catalog.SETTINGS.get(name)
            found.append((name, value, entry["short_desc"] if entry else ""))
        return found

    return StaticStatement("SHOW ALL", columns, rows)


_SESSION_NULLARY_FUNCS = {
    exp.CurrentDatabase: lambda c: c.database,
    exp.CurrentSchema: lambda c: "public",
    exp.CurrentUser: lambda c: c.username,
    exp.SessionUser: lambda c: c.username,
    exp.CurrentVersion: lambda c: c.server.parameter_status.get("server_version", ""),
}


def _setting_value(connection: Connection, key: str) -> str | None:
    """Current value of a setting, or None if this connection has never heard of
    the name at all -- which every caller turns into 42704 or NULL as its own
    semantics require.

    The three states are Postgres's, not an invention here. A name is *known* if
    it is one pg_mimic answers for itself (`_DEFAULT_SETTINGS`), one of the ~400
    a real server is born knowing (`settings_catalog`), or one this connection has
    written; a known name with no current override reads as its default, or as the
    empty string if it never had one; an unknown one does not read at all. What
    makes the middle state worth tracking is
    `current_setting('app.tenant', true) IS NULL`, the usual row-level-security
    probe for "was this ever set?" -- answering it with an empty string sends the
    caller down the wrong branch silently. See #32.

    Order is Postgres's too. The catalog default outranks `known_settings` because
    RESET means different things to the two kinds of name: `SET work_mem = '8MB';
    RESET work_mem` reads back 4MB, its default, while the same pair on `app.x`
    reads back empty. Only a name with no default falls through to the blank.
    """
    if key in connection.state.session_vars:
        # The one place a stored value is read, and so the one place it is rendered.
        # Everything below is already text: pg_mimic's own answers and the catalogue
        # defaults are both spelled the way Postgres reports them (#115).
        return settings_values.render(key, connection.state.session_vars[key])
    if key in _DEFAULT_SETTINGS:
        return _DEFAULT_SETTINGS[key](connection)
    catalogued = settings_catalog.default(key)
    if catalogued is not None:
        return catalogued
    if key in connection.state.known_settings:
        return ""  # written once, since reset: PG keeps the name and blanks the value
    return None


def _unrecognized_setting(name: str) -> PgError:
    """Postgres's own wording, quoted name included -- clients match on it."""
    return PgError(UNDEFINED_OBJECT, f'unrecognized configuration parameter "{name}"')


# Why a session may not set a parameter, keyed by the catalogue's `context`. All
# four are 55P02; the wording is what differs, and clients match on wording, so
# one message for all 199 would be a worse mimic than four. Read off a
# PostgreSQL 18.4 socket -- guc.c picks these in set_config_with_handle().
_UNSETTABLE_CONTEXTS = {
    "postmaster": "cannot be changed without restarting the server",
    "sighup": "cannot be changed now",
    "internal": "cannot be changed",
    "backend": "cannot be set after connection start",
    "superuser-backend": "cannot be set after connection start",
}

# The two contexts a session may set: 151 `user` parameters and, by decision
# rather than by the manual, the 48 `superuser` ones (#77). Read literally
# pg_mimic reports `is_superuser = off` and so should refuse those 48 with 42501
# -- but there is no privilege model behind that answer, and refusing them would
# break clients that set `log_*` for their own diagnostics against something that
# has no log. `sighup` is not the same case: PostgreSQL refuses
# `SET autovacuum_naptime` exactly as it refuses `SET shared_buffers`, and the 104
# are server-operational parameters no client library sets on a connection.
_SETTABLE_CONTEXTS = frozenset({"user", "superuser"})


def _check_settable(name: str) -> None:
    """Raise unless `name` is a parameter this connection may assign.

    A dotted name is a custom GUC and always assignable -- Postgres creates a
    placeholder for it, which is what the row-level-security pattern in #32 rests
    on. `SET app.tenant = 'acme'` never reaches here from a SET statement at all
    (`_SETTING_NAME` excludes the dot), but `set_config('app.tenant', ...)` does,
    since it takes the name as a string argument rather than as syntax.

    An *undotted* name that isn't in the catalogue is not a custom GUC and not a
    parameter: PostgreSQL 18.4 answers `SET mytenant TO 'acme'` with 42704, the
    same as reading one.
    """
    if "." in name:
        return
    context = settings_catalog.context(name)
    if context is None:
        raise _unrecognized_setting(name)
    if context in _SETTABLE_CONTEXTS:
        return
    reason = _UNSETTABLE_CONTEXTS.get(context)
    if reason is None:  # a context the catalogue grew that this doesn't know
        reason = "cannot be changed"
    raise PgError(CANT_CHANGE_RUNTIME_PARAM, f'parameter "{name}" {reason}')


def _missing_ok(node: exp.Expression) -> bool | None:
    """`current_setting`'s second argument, or None if it isn't a boolean this can
    read statically -- a column reference or a parameter, say, which belongs to
    the session rather than here.

    Quoted spellings count: the argument is `boolean`, so Postgres coerces the
    unknown-typed literal in `current_setting('x', 't')` and means the same thing
    by it -- measured on PostgreSQL 18.
    """
    if isinstance(node, exp.Boolean):
        return bool(node.this)
    if isinstance(node, exp.Literal) and node.is_string:
        text = str(node.this).strip().lower()
        if text in ("t", "true", "y", "yes", "on", "1"):
            return True
        if text in ("f", "false", "n", "no", "off", "0"):
            return False
    return None


def _literal_text(node: exp.Expression) -> str | None:
    """The text of a literal argument, or None if it isn't a plain literal."""
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Literal):
        return str(node.this)
    if isinstance(node, exp.Boolean):
        return "on" if node.this else "off"
    return None


def _current_setting_literal(connection: Connection, arguments: list[exp.Expression]) -> exp.Expression | None:
    """What `current_setting(name[, missing_ok])` reads as, or None if the call
    isn't one this can answer statically.

    The flag is the whole point of the two-argument form: true asks for NULL where
    the one-argument form raises 42704. A NULL flag makes the call NULL whatever
    the setting is -- `current_setting(text, bool)` is strict, so the name is never
    looked at, and PostgreSQL 18 answers NULL even for `work_mem`.

    The name is folded but not trimmed, because Postgres matches GUCs
    case-insensitively and `current_setting('  work_mem  ')` is still an error there.
    """
    raw = str(arguments[0].this)
    missing_ok: bool | None = False
    if len(arguments) > 1:
        if isinstance(arguments[1], exp.Null):
            return exp.Null()
        missing_ok = _missing_ok(arguments[1])
        if missing_ok is None:
            return None
    value = _setting_value(connection, raw.lower())
    if value is not None:
        return exp.Literal.string(value)
    if missing_ok:
        return exp.Null()
    raise _unrecognized_setting(raw)


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
            replacement = _current_setting_literal(connection, node.expressions)
            if replacement is None:
                continue  # a missing_ok we can't read -- leave it for the session
            node.replace(replacement)
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
        elif name == "pg_notify" and len(node.expressions) == 2:
            channel = _literal_text(node.expressions[0])
            message = _literal_text(node.expressions[1])
            if channel is None:
                continue  # not a literal we can evaluate -- leave it for the session
            # Verbatim, not folded: `pg_notify` takes the channel as a string
            # rather than an identifier, so `pg_notify('PCHAN', ...)` does not
            # reach `LISTEN pchan` -- measured on PostgreSQL 18.
            effects.append(partial(connection.notify_listeners, channel, message or ""))
            # pg_notify returns void, which renders as the empty string.
            node.replace(exp.Literal.string(""))
            substitutions += 1
        elif name == "pg_backend_pid":
            node.replace(exp.Literal.number(connection.pid))
            substitutions += 1

    return expr, substitutions, _run_all(effects) if effects else None


def _run_all(effects: list[Callable[[], Any]]) -> Callable[[], Awaitable[None]]:
    """One effect out of many, awaiting the ones that need it.

    A query may carry several -- `SELECT set_config('a','1',false), pg_notify(...)`
    -- and they run in the order they were found. Portal.execute awaits what this
    returns, so an effect reaching the session is no different here from one that
    only writes connection state (#35)."""

    async def run() -> None:
        for effect in effects:
            result = effect()
            if inspect.isawaitable(result):
                await result

    return run


async def _apply_set_config(connection: Connection, key: str, value: str | None, local: bool = False) -> None:
    """Apply one setting change, from SET/RESET or from set_config(). None means
    "back to the built-in default", i.e. forget the override.

    Every write lands in `session_vars`, which is what SHOW reads. A write that is
    not `SET LOCAL` also lands in `committed_vars`, which is what survives COMMIT.
    Naming a setting is also what makes it exist: `known_settings` records the name
    for good, so a RESET leaves it readable-but-blank rather than unrecognised --
    unless it has a built-in default, which outranks the blank (see `_setting_value`).

    That reaches only the names this module handles. A dotted custom GUC never
    arrives here *from a SET statement* -- `_SETTING_NAME` excludes the dot on
    purpose, so `SET app.tenant = 'acme'` falls through to the session -- and so
    does not become known. `current_setting('app.tenant', true)` is therefore still
    NULL after one, which is the gap `Session.set_parameter()` closes (#35). The one
    route that does bring a dotted name here is `set_config('app.tenant', ...)`,
    which names it as a string rather than as syntax.

    Both callers come through here rather than through their own checks so that
    `_check_settable` cannot be bypassed: refusing `SET shared_buffers` while
    allowing `SELECT set_config('shared_buffers', '1GB', false)` would be a hole
    in the same wall (#77).
    """
    _check_settable(key)
    # After _check_settable, in Postgres's order: a parameter no session may change
    # is refused for that before its value is looked at (#105). What lands in the
    # dicts is the *value*, not the text a client typed, so `SET row_security = 'tr'`
    # reads back `on` -- see _setting_value, which renders it again (#115). RESET
    # has no value to parse.
    stored = None if value is None else settings_values.parse(key, value)
    undo = _record_setting(connection, key, stored, drop=value is None, local=local)
    _report_setting(connection, key)
    try:
        await _tell_session(connection, key, value)
    except Exception:
        # The session refused it. Put back what was there and say so again, so a
        # client's ParameterStatus cache does not keep a value this connection no
        # longer holds (#35).
        undo()
        _report_setting(connection, key)
        raise


def _record_setting(connection: Connection, key: str, stored: Any, *, drop: bool, local: bool) -> Callable[[], None]:
    """Write one setting, and hand back what would put things as they were.

    `SET LOCAL` writes only session_vars, which is what makes it local. The undo is
    built before the write rather than reconstructed after, because only here knows
    which dicts were touched.
    """
    targets = [connection.state.session_vars]
    if not local:
        targets.append(connection.state.committed_vars)
    previous = [(target, key in target, target.get(key)) for target in targets]
    was_known = key in connection.state.known_settings

    connection.state.known_settings.add(key)
    for target in targets:
        if drop:
            target.pop(key, None)
        else:
            target[key] = stored

    def undo() -> None:
        for target, had, value in previous:
            if had:
                target[key] = value
            else:
                target.pop(key, None)
        if not was_known:
            connection.state.known_settings.discard(key)

    return undo


async def _tell_session(connection: Connection, key: str, value: str | None) -> None:
    """Hand the change to the session, if there is one that takes it.

    A bare BaseSession bypasses the middleware chain entirely and has no hook; a
    Session has one that does nothing until it is overridden."""
    session = getattr(connection, "session", None)
    if isinstance(session, Session):
        await session.set_parameter(key, value)


def _evaluate_select(
    substituted: exp.Expression,
    original: exp.Select,
    on_execute: Callable[[], None] | None = None,
    connection: Connection | None = None,
) -> Statement | None:
    try:
        result = sqlglot_execute(substituted, dialect="postgres")
    except Exception:
        return None  # not something we can statically evaluate -- fall through

    rows = [tuple(row) for row in result.rows]

    def rows_again() -> list[tuple]:
        # Substituted *and* evaluated afresh, because the substitution is where a
        # session function reads connection state: `current_setting('x')` becomes a
        # literal at that point, so deferring only the evaluation would still bake
        # the value in at Parse. See #63.
        again, _substitutions, _on_execute = _substitute_session_functions(connection, original)
        return [tuple(row) for row in sqlglot_execute(again, dialect="postgres").rows]

    return statement_from_rows(
        original.sql(dialect="postgres"),
        result.columns,
        rows if connection is None else rows_again,
        on_execute,
    )
