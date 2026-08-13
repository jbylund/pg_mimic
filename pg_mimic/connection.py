"""Per-connection protocol state machine: auth handshake, then the command
dispatch loop for both the simple ('Q') and extended (P/B/D/E/H/S/C) query
protocols, driving a single Statement/Portal interface either way.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any, AsyncIterator

import sqlglot
from sqlglot.tokens import TokenType

from . import messages
from .auth import AuthPlugin
from .copy import DIRECTION_IN, CopyEncoder, CopyInDecoder, CopyOptions, CopyPortal, CopyStatement
from .errors import (
    FEATURE_NOT_SUPPORTED,
    IN_FAILED_SQL_TRANSACTION,
    INTERNAL_ERROR,
    INVALID_SQL_STATEMENT_NAME,
    PROTOCOL_VIOLATION,
    QUERY_CANCELED,
    SUCCESSFUL_COMPLETION,
    PgError,
    ProtocolViolation,
)
from .messages import TARGET_STATEMENT, FieldSpec, ParsedBind, ParsedParse
from .middleware import allowed_in_failed_transaction
from .results import ResultColumn, encode_row, format_code_for
from .session import BaseSession, Row, Session, Statement
from .state import SessionState
from .stream import ConnectionClosed, PgStream
from .types import decode_binary_param, decode_text_param

if TYPE_CHECKING:
    from .server import PgServer


_ROW_COUNT_COMMANDS = {"SELECT", "DELETE", "UPDATE", "MOVE", "FETCH", "COPY"}
_KEYWORD_RE = re.compile(r"[A-Za-z]+")
# How often a COPY TO STDOUT hands its output to the socket. COPY exists for bulk
# data, so the rows are flushed as they're produced rather than piling up whole in
# the writer's queue.
_COPY_OUT_DRAIN_INTERVAL = 100

# The handful of command tags real Postgres spells with two words -- see
# src/include/tcop/cmdtaglist.h. Everything else is just the leading keyword.
_TWO_WORD_TAG_RE = re.compile(
    r"^(DISCARD\s+(?:ALL|PLANS|SEQUENCES|TEMP|TEMPORARY)|DEALLOCATE\s+(?:PREPARE\s+)?ALL)\s*;?\s*$",
    re.IGNORECASE,
)


def command_tag(sql: str, row_count: int) -> str:
    two_word = _TWO_WORD_TAG_RE.match(sql.strip())
    if two_word:
        # As Postgres reports them: DISCARD TEMPORARY completes as "DISCARD TEMP",
        # and the optional PREPARE noise word is not part of DEALLOCATE's tag.
        tag = " ".join(two_word.group(1).upper().split())
        return tag.replace("TEMPORARY", "TEMP").replace("DEALLOCATE PREPARE", "DEALLOCATE")
    match = _KEYWORD_RE.match(sql.strip())
    keyword = match.group(0).upper() if match else ""
    if keyword == "INSERT":
        return f"INSERT 0 {row_count}"
    if keyword in _ROW_COUNT_COMMANDS:
        return f"{keyword} {row_count}"
    return keyword or "SELECT"


def split_statements(sql: str) -> list[str]:
    """Split a simple-query string into individual statement texts on ';'
    boundaries, using sqlglot's tokenizer rather than a naive string split so
    semicolons inside string literals, comments and dollar-quoted bodies don't
    misfire.

    The tokenizer rather than the parser, because each statement is handed on as
    the client wrote it. Parsing and re-rendering does not round-trip: sqlglot
    writes `SAVEPOINT a` back as `SAVEPOINT AS a`, `DISCARD ALL` as `DISCARD AS
    ALL` and `RELEASE a` as `RELEASE AS a`, so every statement the middleware
    classifies from its raw text stopped being recognised the moment it arrived in
    a batch rather than on its own. Slicing the original string has no such gap.

    Falls back to treating the whole input as one statement if sqlglot can't
    tokenize it -- pg_mimic isn't a full SQL parser, and a client sending syntax
    sqlglot doesn't support should still reach the session, not get a hard failure
    here."""
    try:
        tokens = sqlglot.Dialect.get_or_raise("postgres").tokenize(sql)
    except Exception:
        return [sql]
    statements = []
    start = 0
    for token in tokens:
        if token.token_type is TokenType.SEMICOLON:
            statements.append(sql[start : token.start])
            start = token.end + 1
    statements.append(sql[start:])
    statements = [statement for statement in statements if statement.strip()]
    # One statement (with or without a trailing semicolon) is by far the common
    # case, and is returned as the untouched original.
    return statements if len(statements) > 1 else [sql]


class Connection:
    def __init__(
        self,
        stream: PgStream,
        session: BaseSession,
        server: PgServer,
        pid: int,
        secret: int,
        startup_params: dict[str, str],
        protocol_version: int = messages.PROTOCOL_VERSION,
    ):
        self.stream = stream
        self.session = session
        self.server = server
        self.pid = pid
        self.secret = secret
        self.startup_params = startup_params
        # What the client asked for, not what it got: the server answers 3.0
        # regardless (having said so with NegotiateProtocolVersion), and this is
        # kept because "which version did this client want" is the first
        # question to ask of a client behaving unlike the last one.
        self.protocol_version = protocol_version
        self.username = startup_params.get("user", "")
        self.database = startup_params.get("database", self.username)

        # Everything `DISCARD ALL` would reset, shared with the session. See
        # pg_mimic.state; the wire machinery below deliberately stays here.
        self.state = SessionState(
            username=self.username,
            database=self.database,
            application_name=startup_params.get("application_name", ""),
        )

        self.tx_status = b"I"
        self._ignore_until_sync = False
        self._current_task: asyncio.Task | None = None
        # The ParameterStatus reports a GUC_REPORT change owes the client, which
        # only the connection can deliver. See report_parameter().
        self._pending_parameter_status: dict[str, str] = {}

        # Async messages waiting for a point in the stream where they may legally
        # go out, and whether we are standing on one. See _deliver_async().
        self._pending_async: list[bytes] = []
        self._at_ready_for_query = False
        # The channels this connection is registered for in the server's registry.
        # Mirrors state.listening once a transaction has made it real; kept
        # separately because the two differ for the length of a transaction, and
        # because unregistering at close needs the effective set, not the pending
        # one. See sync_listeners().
        self._registered_channels: set[str] = set()

    def request_cancel(self) -> None:
        if self._current_task is not None:
            self._current_task.cancel()

    def report_parameter(self, name: str, value: str) -> None:
        """Queue a ParameterStatus for a reported setting that just changed.

        Queued rather than written straight out because real Postgres reports
        changed GUCs immediately before ReadyForQuery, not in the middle of a
        command's own messages -- so a `SELECT set_config(...)` reports after its
        DataRow, and an extended-protocol SET reports at Sync. Last write wins:
        two SETs of the same parameter in one batch owe the client one report of
        the value it ended up with.
        """
        self._pending_parameter_status[name] = value

    def _flush_parameter_status(self) -> None:
        for name, value in self._pending_parameter_status.items():
            self.stream.write(messages.make_parameter_status(name, value))
        self._pending_parameter_status.clear()

    # --- server-to-client push: notices and notifications ----------------------------
    #
    # Everything above this point writes to the stream because the client asked
    # for something. These two do not, which makes *where* in the stream they land
    # the whole problem: a NotificationResponse dropped between two DataRows is
    # not a protocol error the client reports, it is a row the client silently
    # mis-parses.
    #
    # The rule, measured on PostgreSQL 18 by reading a raw socket rather than
    # inferred from a client not complaining:
    #
    #   NOTIFY arriving mid-query   -> T D D D D D C A Z   (after CommandComplete)
    #   NOTIFY arriving while idle  -> A                   (on its own, at once)
    #   NOTIFY while a portal is
    #     suspended mid-drain       -> D D s ... then A Z at Sync
    #
    # So a notification goes out at exactly one kind of place: between the
    # ReadyForQuery that ended a command and the next command. A suspended portal
    # is *not* such a place -- the connection is waiting on the client, but it has
    # sent PortalSuspended rather than ReadyForQuery, and real Postgres holds the
    # notification until Sync. `_at_ready_for_query` tracks precisely that, so the
    # test is a fact about the stream rather than a guess about who is running.
    #
    # Notices are the other half and follow the other rule: raised *during* a
    # query they belong to that query's response, ahead of its CommandComplete
    # (again measured -- a `DROP TABLE IF EXISTS` of a missing table answers
    # N C Z). So a notice raised from inside this connection's own command is
    # written straight out, and only one raised from anywhere else has to queue.

    def notice(self, message: str, severity: str = "NOTICE", **fields: str) -> None:
        """Send the client a NoticeResponse -- Postgres's out-of-band `RAISE
        NOTICE` / `WARNING` channel, surfaced by psycopg's `add_notice_handler`
        and asyncpg's `add_log_listener`.

        Raised from inside a query it attaches to that query's response, before
        `CommandComplete`, as a real backend's does. Extra `ErrorResponse` fields
        ride along as keyword arguments keyed by their protocol field byte, the
        same as `PgError` -- `notice("...", D="a longer explanation")`.
        """
        payload = messages.make_notice_response({"S": severity, "V": severity, "C": SUCCESSFUL_COMPLETION, "M": message, **fields})
        if self._in_own_command():
            self.stream.write(payload)
        else:
            self._deliver_async(payload)

    def notify(self, channel: str, payload: str = "", pid: int | None = None) -> None:
        """Send *this* client a NotificationResponse, as though `pid` had run
        `NOTIFY channel, payload`. Defaults to this connection's own pid.

        The direct primitive: it delivers to one client and does not consult the
        server's registry or wait for a transaction. `notify_listeners()` is the
        one that behaves like the SQL statement.
        """
        self._deliver_async(messages.make_notification_response(self.pid if pid is None else pid, channel, payload))

    def notify_listeners(self, channel: str, payload: str = "") -> None:
        """Raise a notification the way the `NOTIFY` statement does: fanned out to
        every connection on the server listening to `channel`, this one included,
        and deferred to commit if a transaction is open.

        The deferral is not a detail. Measured against PostgreSQL 18, a `NOTIFY`
        in a transaction that rolls back is never delivered -- and delivering it
        anyway would mean a rolled-back unit of work still announced itself, which
        is a false positive in exactly the event-driven code this feature exists to
        test.
        """
        if self.tx_status == b"I":
            self.server.notify(channel, payload, self.pid)
            return
        # Duplicates within one transaction collapse on (channel, payload), as
        # they do there -- `NOTIFY c,'x'; NOTIFY c,'x'; NOTIFY c,'y'` commits as
        # two notifications, not three.
        entry = (channel, payload)
        if entry not in self.state.pending_notifies:
            self.state.pending_notifies.append(entry)

    def flush_pending_notifies(self) -> None:
        """Fan out what a COMMIT just made real. Called by the middleware that
        answers COMMIT; a ROLLBACK drops the same list instead (SessionState
        restores it from the scope frame, so there is nothing to do there)."""
        for channel, payload in self.state.take_pending_notifies():
            self.server.notify(channel, payload, self.pid)

    def _in_own_command(self) -> bool:
        """Are we running inside this connection's own command dispatch?

        Which is what tells a notice the session raised while answering a query
        apart from one raised by a background task that happens to hold a
        reference to this connection. The first may write immediately; the second
        may not.
        """
        return self._current_task is not None and asyncio.current_task() is self._current_task

    def _deliver_async(self, payload: bytes) -> None:
        """Write an async message if the stream is standing somewhere one may go,
        and otherwise hold it until it is.

        No drain: asyncio hands a write straight to the transport unless flow
        control has kicked in, so an idle connection's notification reaches the
        socket without one -- which is what lets this stay synchronous and be
        callable from a fanout that is not this connection's own task.
        """
        if self._at_ready_for_query:
            self.stream.write(payload)
        else:
            self._pending_async.append(payload)

    def _flush_async(self) -> None:
        for payload in self._pending_async:
            self.stream.write(payload)
        self._pending_async.clear()

    # --- LISTEN/UNLISTEN, against the server's channel registry ----------------------

    def sync_listeners(self) -> None:
        """Make `state.listening` real, registering and unregistering with the
        server to match.

        Called wherever a subscription becomes effective -- a LISTEN outside a
        transaction, a COMMIT, a ROLLBACK, `DISCARD ALL`. Idempotent by
        construction (it diffs against what is registered), which is what makes
        calling it at every one of those points correct rather than merely safe.

        The indirection exists because LISTEN is transactional in a way that is
        not just rollback: measured on PostgreSQL 18, a `LISTEN` inside an open
        transaction does not receive a notification sent before its `COMMIT`. So
        `state.listening` is the pending set and `_registered_channels` the live
        one, and they differ for the length of a transaction.
        """
        for channel in self._registered_channels - self.state.listening:
            self.server.remove_listener(channel, self)
        for channel in self.state.listening - self._registered_channels:
            self.server.add_listener(channel, self)
        self._registered_channels = set(self.state.listening)

    def _drop_listeners(self) -> None:
        """Leave the registry entirely, on the way out. Reads
        `_registered_channels` rather than `state.listening`, which may hold
        subscriptions from a transaction that never committed."""
        for channel in self._registered_channels:
            self.server.remove_listener(channel, self)
        self._registered_channels.clear()

    def _claim_session(self) -> bool:
        """Claim this connection's session, or refuse the connection saying why.

        `PgServer` calls `session_factory()` once per connection, so one session per
        connection is the design. Nothing stopped a factory returning the same object
        every time, and the result was not an error but a wrong answer: a session
        holds `_connection` and `state`, both rebound on every connect, so every
        connection went on resolving through whichever attached last -- connection A
        reporting connection B's `current_user`, and reading B's `search_path`.

        That is the failure this project refuses everywhere else, a full answer that
        is quietly the wrong one, and an identity is the worst thing to be wrong
        about in a server people use to test authentication. See #84.

        Sequential reuse stays legal: the claim is released on teardown, so a factory
        may hand the same object to the next connection once this one is done with
        it. Only an overlap is refused.
        """
        held = self.session._connection
        if held is None or held is self:
            return True
        self.stream.write(
            messages.make_fatal_error(
                INTERNAL_ERROR,
                f"this Session is already answering for another connection (pid {held.pid}) and cannot also "
                f"answer for this one: a session holds per-connection state, so sharing one gives every "
                f"connection the last one's identity and settings. Pass a factory that builds one session per "
                f"connection -- session_factory=MySession rather than session_factory=lambda: shared.",
            )
        )
        return False

    async def run(self) -> None:
        try:
            # Before authentication, and before anything is written: a session that
            # cannot be claimed is a server the client must not believe it reached.
            # Raising this later still refuses the connection, but only after
            # ReadyForQuery has gone out -- so the client reports a successful
            # connect and finds out at its first query.
            if isinstance(self.session, Session) and not self._claim_session():
                return
            if not await self._authenticate():
                return
            await self._send_startup_completion()
            if isinstance(self.session, Session):
                # Set directly rather than relying on Session.init() being
                # called via super() by whatever override a session author
                # writes -- forgetting that call must not silently disable
                # the whole middleware chain (it fell back to always
                # constructing a bare CallbackStatement instead).
                self.session._connection = self
                # Same reasoning for the shared state: assigned here rather than
                # handed to init(), so a session that overrides init() without
                # calling super() still has it -- and has it before init() runs.
                self.session.state = self.state
            # Set before init() rather than after: the startup burst ended with a
            # ReadyForQuery, so a session that greets its client with a notice from
            # init() is standing on a legal point to send one.
            self._at_ready_for_query = True
            await self.session.init(self)
            await self.stream.drain()
            await self._command_loop()
        except ConnectionClosed:
            pass
        except ProtocolViolation as e:
            # Raised by the framing layer, from the read at the top of the
            # command loop rather than from anything a statement did -- so
            # _dispatch's error handling never saw it, and could not have: it
            # answers errors and keeps the connection, and there is no keeping a
            # connection whose byte stream we have stopped being able to follow.
            self.stream.write(messages.make_fatal_error(e.sqlstate, e.message))
            await self.stream.drain_quietly()
        finally:
            # Before session.close(), and outside its try: a connection that stays
            # in the registry after it has gone is a fanout writing to a dead
            # stream, and a session whose close() raises must not be what leaves it
            # there.
            self._drop_listeners()
            # Release the session before close(), and whatever close() does: a
            # factory handing back the same object for the next connection is fine
            # once this one is done with it, and must not be refused because a
            # close() raised on the way out.
            if isinstance(self.session, Session) and self.session._connection is self:
                self.session._connection = None
            try:
                await self.session.close()
            except Exception:
                pass
            await self.stream.close()

    # --- startup / auth -----------------------------------------------------------

    async def _authenticate(self) -> bool:
        plugin: AuthPlugin = self.server.auth_plugin_factory(self.username)
        ok = await plugin.authenticate(self.stream, self.username, self.server.identity_provider)
        if not ok:
            self.stream.write(messages.make_fatal_error("28P01", f'password authentication failed for user "{self.username}"'))
            await self.stream.drain()
            return False
        self.stream.write(messages.make_authentication_ok())
        return True

    async def _send_startup_completion(self) -> None:
        for name, value in self.server.parameter_status.items():
            self.stream.write(messages.make_parameter_status(name, value))
        if "application_name" not in self.server.parameter_status:
            # Reported per-connection rather than from the server-wide defaults,
            # because it comes from this client's own startup packet. Real
            # Postgres always sends it, empty string included, and a client that
            # set it expects to read it back (psycopg's
            # `conn.info.parameter_status("application_name")`).
            self.stream.write(messages.make_parameter_status("application_name", self.startup_params.get("application_name", "")))
        self.stream.write(messages.make_backend_key_data(self.pid, self.secret))
        self.stream.write(messages.make_ready_for_query(self.tx_status))
        await self.stream.drain()

    # --- command dispatch loop ------------------------------------------------------

    async def _command_loop(self) -> None:
        while True:
            tag, payload = await self.stream.read_message()
            # A command has arrived, so the gap async messages were allowed into
            # has closed. Anything raised from here on queues until the next one.
            self._at_ready_for_query = False
            if tag == messages.TERMINATE:
                return
            self._current_task = asyncio.ensure_future(self._dispatch(tag, payload))
            try:
                await self._current_task
            except asyncio.CancelledError:
                self.stream.write(
                    messages.make_error_response(
                        {"S": "ERROR", "V": "ERROR", "C": QUERY_CANCELED, "M": "canceling statement due to user request"}
                    )
                )
                if self.tx_status != b"I":
                    self.tx_status = b"E"
                self._ignore_until_sync = True
            finally:
                self._current_task = None

            if tag == messages.QUERY or tag == messages.SYNC:
                self._flush_parameter_status()
                # After the ParameterStatus reports and before ReadyForQuery, which
                # is where PostgreSQL 18 puts them -- and the only place in a
                # command's response where they cannot land inside a DataRow run.
                self._flush_async()
                self.stream.write(messages.make_ready_for_query(self.tx_status))
                self._ignore_until_sync = False
                self._at_ready_for_query = True
            await self.stream.drain()

    async def _dispatch(self, tag: bytes, payload: bytes) -> None:
        if self._ignore_until_sync and tag not in (messages.SYNC, messages.TERMINATE):
            return
        if tag in (messages.COPY_DATA, messages.COPY_DONE, messages.COPY_FAIL):
            # Copy messages only mean something inside copy mode, which _run_copy
            # drives by reading the stream itself. Reaching the normal dispatch loop
            # means a COPY already ended -- failed, or was cancelled -- and the
            # client hasn't noticed yet. Real Postgres accepts and drops these rather
            # than answering with an error for each one.
            return
        try:
            if tag == messages.QUERY:
                await self._handle_simple_query(messages.parse_query(payload))
            elif tag == messages.PARSE:
                await self._handle_parse(messages.parse_parse(payload))
            elif tag == messages.BIND:
                await self._handle_bind(messages.parse_bind(payload))
            elif tag == messages.DESCRIBE:
                await self._handle_describe(messages.parse_describe(payload))
            elif tag == messages.EXECUTE:
                await self._handle_execute(messages.parse_execute(payload))
            elif tag == messages.CLOSE:
                await self._handle_close(messages.parse_close(payload))
            elif tag == messages.SYNC:
                pass
            elif tag == messages.FLUSH:
                await self.stream.drain()
            else:
                raise PgError("08P01", f"unsupported message type {tag!r}")
        except ProtocolViolation:
            # A bad frame read mid-copy: framing, not a statement, so it does not
            # get the ErrorResponse-and-carry-on treatment below. run() reports it
            # and hangs up.
            raise
        except PgError as e:
            fields = {"S": "ERROR", "V": "ERROR", "C": e.sqlstate, "M": e.message}
            fields.update(e.fields)
            self.stream.write(messages.make_error_response(fields))
            if self.tx_status != b"I":
                self.tx_status = b"E"
            self._ignore_until_sync = True
        except ConnectionClosed:
            raise
        except Exception as e:
            # A session's describe()/query() raised something other than a
            # PgError (an ordinary bug, not a deliberate client-facing error).
            # Report it rather than letting it crash the whole connection.
            fields = {"S": "ERROR", "V": "ERROR", "C": INTERNAL_ERROR, "M": str(e) or repr(e)}
            self.stream.write(messages.make_error_response(fields))
            if self.tx_status != b"I":
                self.tx_status = b"E"
            self._ignore_until_sync = True

    # --- simple query protocol -------------------------------------------------------

    async def _handle_simple_query(self, sql: str) -> None:
        if not sql.strip():
            self.stream.write(messages.make_empty_query_response())
            return

        # A single 'Q' message may hold several ';'-separated statements --
        # each gets its own RowDescription/DataRow*/CommandComplete (or
        # EmptyQueryResponse), same as real Postgres; only one ReadyForQuery
        # closes the whole batch (sent by the caller, _command_loop). If a
        # statement here raises, the loop stops and the remaining statements
        # in the batch are never run -- matching real Postgres, which aborts
        # the rest of a simple-query batch on error.
        for stmt_sql in split_statements(sql):
            await self._execute_one_simple_statement(stmt_sql)

    async def _execute_one_simple_statement(self, sql: str) -> None:
        if self.tx_status == b"E" and not allowed_in_failed_transaction(sql):
            raise PgError(IN_FAILED_SQL_TRANSACTION, "current transaction is aborted, commands ignored")
        if not sql.strip():
            self.stream.write(messages.make_empty_query_response())
            return

        statement = await self.session.prepare(sql, [])
        if isinstance(statement, CopyStatement):
            # The copy sub-protocol stands in for Bind/Execute entirely: there's no
            # portal to drain and no RowDescription, just the data stream and the
            # CommandComplete that ends it.
            await self._run_copy(statement)
            return
        columns = await statement.describe()
        portal = statement.bind([])
        rows, _suspended = await portal.execute(0)

        if columns is not None:
            self.stream.write(messages.make_row_description(_field_specs(columns)))
            for row in rows:
                self.stream.write(messages.make_data_row(encode_row(row, columns)))
        # The statement's own text, not what the client typed. They are the same
        # for everything except `EXECUTE p`, which Postgres completes with the tag
        # of the statement it ran -- `SELECT 1`, not `EXECUTE`. The extended path
        # already reads it this way, via PortalEntry.sql.
        self.stream.write(messages.make_command_complete(command_tag(getattr(statement, "sql", sql) or sql, len(rows))))

    # --- extended query protocol -----------------------------------------------------

    async def _handle_parse(self, parsed: ParsedParse) -> None:
        param_oids: list[int | None] = [oid if oid != 0 else None for oid in parsed.param_oids]
        statement = await self.session.prepare(parsed.sql, param_oids)
        self.state.statements[parsed.statement_name] = statement
        self.stream.write(messages.make_parse_complete())

    def _get_statement(self, name: str) -> Statement:
        try:
            return self.state.statements[name]
        except KeyError:
            raise PgError(INVALID_SQL_STATEMENT_NAME, f'prepared statement "{name}" does not exist') from None

    async def _handle_bind(self, parsed: ParsedBind) -> None:
        statement = self._get_statement(parsed.statement_name)
        param_oids = getattr(statement, "param_oids", [])
        text_params = [
            self._decode_param(value, format_code_for(parsed.param_format_codes, i), param_oids, i)
            for i, value in enumerate(parsed.params)
        ]
        portal = statement.bind(text_params)
        columns = await statement.describe()
        self.state.portals[parsed.portal_name] = PortalEntry(portal, columns, statement.sql, parsed.result_format_codes)
        self.stream.write(messages.make_bind_complete())

    def _decode_param(self, value: bytes | None, format_code: int, param_oids: list[int | None], index: int) -> Any:
        if value is None:
            return None
        oid = param_oids[index] if index < len(param_oids) else None
        if format_code == 0:
            text = value.decode("utf-8")
            return decode_text_param(oid, text) if oid is not None else text
        if oid is None:
            raise PgError(
                FEATURE_NOT_SUPPORTED,
                f"binary parameter format requires a known type OID (param ${index + 1} has none)",
            )
        try:
            return decode_binary_param(oid, value)
        except ValueError as e:
            raise PgError(FEATURE_NOT_SUPPORTED, str(e)) from None

    def _get_portal(self, name: str) -> PortalEntry:
        try:
            return self.state.portals[name]
        except KeyError:
            raise PgError(INVALID_SQL_STATEMENT_NAME, f'portal "{name}" does not exist') from None

    async def _handle_describe(self, parsed) -> None:
        # Result formats are chosen at Bind, so a statement-level Describe can only
        # honestly report text (format 0) -- real Postgres does the same. Only a
        # portal-level Describe, which happens after Bind, reflects what was asked for.
        format_codes: list[int] = []
        if parsed.kind == TARGET_STATEMENT:
            statement = self._get_statement(parsed.name)
            param_oids = [oid if oid is not None else 0 for oid in getattr(statement, "param_oids", [])]
            self.stream.write(messages.make_parameter_description(param_oids))
            columns = await statement.describe()
        else:
            entry = self._get_portal(parsed.name)
            columns = entry.columns
            format_codes = entry.result_format_codes

        if columns is None:
            self.stream.write(messages.make_no_data())
        else:
            self.stream.write(messages.make_row_description(_field_specs(columns, format_codes)))

    async def _handle_execute(self, parsed) -> None:
        entry = self._get_portal(parsed.portal_name)
        if self.tx_status == b"E" and not allowed_in_failed_transaction(entry.sql):
            raise PgError(IN_FAILED_SQL_TRANSACTION, "current transaction is aborted, commands ignored")
        if isinstance(entry.portal, CopyPortal):
            # See _execute_one_simple_statement: a COPY is driven by the copy
            # sub-protocol, and maxRows/PortalSuspended have no meaning for it.
            await self._run_copy(entry.portal.statement)
            return
        rows, suspended = await entry.portal.execute(parsed.max_rows)
        entry.rows_returned += len(rows)

        if entry.columns is not None:
            for row in rows:
                try:
                    values = encode_row(row, entry.columns, entry.result_format_codes)
                except ValueError as e:
                    raise PgError(FEATURE_NOT_SUPPORTED, str(e)) from None
                self.stream.write(messages.make_data_row(values))

        if suspended:
            self.stream.write(messages.make_portal_suspended())
        else:
            self.stream.write(messages.make_command_complete(command_tag(entry.sql, entry.rows_returned)))

    async def _handle_close(self, parsed) -> None:
        if parsed.kind == TARGET_STATEMENT:
            self.state.statements.pop(parsed.name, None)
        else:
            self.state.portals.pop(parsed.name, None)
        self.stream.write(messages.make_close_complete())

    # --- COPY sub-protocol -----------------------------------------------------------

    async def _run_copy(self, statement: CopyStatement) -> None:
        """Drive a COPY to completion in place of the Bind/Execute an ordinary
        statement goes through.

        Either direction ends with the CommandComplete the client expects on the
        normal path -- `COPY <n>`, from the same command_tag() every other
        statement uses. Anything raised on the way out is an ordinary error
        response: _dispatch's handler covers this exactly as it covers a query,
        and a client that has been left mid-copy by it stops mattering because
        _dispatch drops stray copy messages.
        """
        if statement.direction == DIRECTION_IN:
            row_count = await self._copy_in(statement)
        else:
            row_count = await self._copy_out(statement)
        self.stream.write(messages.make_command_complete(command_tag(statement.sql, row_count)))

    async def _copy_in(self, statement: CopyStatement) -> int:
        self.stream.write(messages.make_copy_in_response(statement.column_count))
        # Drained here rather than at the end of dispatch: the client won't send a
        # byte until it has seen this.
        await self.stream.drain()

        reader = _CopyInReader(self.stream, statement.options)
        row_count = await statement.copy_in(reader.rows())
        # A session that stopped reading early still leaves the client mid-copy, so
        # the rest of its messages have to be consumed before this connection can go
        # back to normal command processing.
        await reader.finish()
        return reader.row_count if row_count is None else row_count

    async def _copy_out(self, statement: CopyStatement) -> int:
        # The header names and the row source are resolved before CopyOutResponse
        # goes out, because failing here is a plain ErrorResponse where failing after
        # it aborts a copy already in flight.
        header = await statement.header_names() if statement.options.header else None
        rows = await statement.copy_out()

        # The first row is pulled early for the same reason. CopyOutResponse's column
        # count is not decoration -- psycopg truncates every row it parses to it --
        # and the copy sub-protocol has no describe() to ask, so the first row is the
        # one place the arity is certainly right. It costs one buffered row. With no
        # rows at all nothing reads the count, and the statement says what it can.
        try:
            first_row: Row | None = await rows.__anext__()
        except StopAsyncIteration:
            first_row = None
        column_count = len(first_row) if first_row is not None else len(header or ()) or statement.column_count

        encoder = CopyEncoder(statement.options)
        self.stream.write(messages.make_copy_out_response(column_count))
        if header is not None:
            self.stream.write(messages.make_copy_data(encoder.header(header)))

        row_count = 0
        if first_row is not None:
            self.stream.write(messages.make_copy_data(encoder.row(first_row)))
            row_count = 1
        async for row in rows:
            self.stream.write(messages.make_copy_data(encoder.row(row)))
            row_count += 1
            if row_count % _COPY_OUT_DRAIN_INTERVAL == 0:
                await self.stream.drain()
        self.stream.write(messages.make_copy_done())
        return row_count


class _CopyInReader:
    """The frontend side of copy-in mode: CopyData/CopyDone/CopyFail off the
    stream, decoded rows out.

    One object rather than a bare generator because the row iterator the session
    gets and the "make sure the client's CopyDone was consumed" cleanup are two
    views of the same in-progress read -- a session that stops iterating early
    must not leave the connection parked mid-copy.
    """

    def __init__(self, stream: PgStream, options: CopyOptions):
        self._stream = stream
        self._decoder = CopyInDecoder(options)
        self._rows: AsyncIterator[Row] | None = None
        self._done = False
        self.row_count = 0

    def rows(self) -> AsyncIterator[Row]:
        self._rows = self._iterate()
        return self._rows

    async def _iterate(self) -> AsyncIterator[Row]:
        while not self._done:
            for row in await self._read_batch():
                self.row_count += 1
                yield row

    async def finish(self) -> None:
        if self._rows is not None:
            await self._rows.aclose()  # type: ignore[attr-defined]
        while not self._done:
            await self._read_batch()

    async def _read_batch(self) -> list[Row]:
        tag, payload = await self._stream.read_message()
        if tag == messages.COPY_DATA:
            return self._decoder.feed(payload)
        if tag == messages.COPY_DONE:
            self._done = True
            return self._decoder.finish()
        if tag == messages.COPY_FAIL:
            self._done = True
            raise PgError(QUERY_CANCELED, f"COPY from stdin failed: {messages.parse_copy_fail(payload)}")
        if tag in (messages.FLUSH, messages.SYNC):
            # Real Postgres ignores these during copy-in rather than treating them as
            # the protocol violation every other message type is. Ignoring Sync
            # specifically matters: answering it with a ReadyForQuery would put the
            # connection a message ahead of the CommandComplete still to come.
            return []
        raise PgError(PROTOCOL_VIOLATION, f"unexpected message type 0x{tag[0]:02X} during COPY from stdin")


class PortalEntry:
    __slots__ = ("portal", "columns", "sql", "rows_returned", "result_format_codes")

    def __init__(self, portal, columns: list[ResultColumn] | None, sql: str, result_format_codes: list[int] | None = None):
        self.portal = portal
        self.columns = columns
        self.sql = sql
        self.rows_returned = 0
        self.result_format_codes = result_format_codes or []


def _field_specs(columns: list[ResultColumn], format_codes: list[int] | None = None) -> list[FieldSpec]:
    codes = format_codes or []
    return [FieldSpec(name=c.name, oid=c.oid, format_code=format_code_for(codes, i)) for i, c in enumerate(columns)]
