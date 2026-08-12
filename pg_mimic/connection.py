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
    PgError,
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
    ):
        self.stream = stream
        self.session = session
        self.server = server
        self.pid = pid
        self.secret = secret
        self.startup_params = startup_params
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

    async def run(self) -> None:
        try:
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
            await self.session.init(self)
            await self._command_loop()
        except ConnectionClosed:
            pass
        finally:
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
            self.stream.write(
                messages.make_error_response(
                    {
                        "S": "FATAL",
                        "V": "FATAL",
                        "C": "28P01",
                        "M": f'password authentication failed for user "{self.username}"',
                    }
                )
            )
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
                self.stream.write(messages.make_ready_for_query(self.tx_status))
                self._ignore_until_sync = False
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
