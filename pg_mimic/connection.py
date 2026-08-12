"""Per-connection protocol state machine: auth handshake, then the command
dispatch loop for both the simple ('Q') and extended (P/B/D/E/H/S/C) query
protocols, driving a single Statement/Portal interface either way.
"""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

import sqlglot

from . import messages
from .auth import AuthPlugin
from .errors import (
    FEATURE_NOT_SUPPORTED,
    IN_FAILED_SQL_TRANSACTION,
    INTERNAL_ERROR,
    INVALID_SQL_STATEMENT_NAME,
    PgError,
)
from .messages import TARGET_STATEMENT, FieldSpec, ParsedBind, ParsedParse
from .middleware import allowed_in_failed_transaction
from .results import ResultColumn, encode_row, format_code_for
from .session import BaseSession, Session, Statement
from .stream import ConnectionClosed, PgStream
from .types import decode_binary_param, decode_text_param

if TYPE_CHECKING:
    from .server import PgServer


_ROW_COUNT_COMMANDS = {"SELECT", "DELETE", "UPDATE", "MOVE", "FETCH", "COPY"}
_KEYWORD_RE = re.compile(r"[A-Za-z]+")

# The handful of command tags real Postgres spells with two words -- see
# src/include/tcop/cmdtaglist.h. Everything else is just the leading keyword.
_TWO_WORD_TAG_RE = re.compile(
    r"^(DISCARD\s+(?:ALL|PLANS|SEQUENCES|TEMP|TEMPORARY)|DEALLOCATE\s+ALL)\s*;?\s*$",
    re.IGNORECASE,
)


def command_tag(sql: str, row_count: int) -> str:
    two_word = _TWO_WORD_TAG_RE.match(sql.strip())
    if two_word:
        # DISCARD TEMPORARY completes as "DISCARD TEMP", the way Postgres reports it.
        return " ".join(two_word.group(1).upper().split()).replace("TEMPORARY", "TEMP")
    match = _KEYWORD_RE.match(sql.strip())
    keyword = match.group(0).upper() if match else ""
    if keyword == "INSERT":
        return f"INSERT 0 {row_count}"
    if keyword in _ROW_COUNT_COMMANDS:
        return f"{keyword} {row_count}"
    return keyword or "SELECT"


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

        self.tx_status = b"I"
        self.session_vars: dict[str, str] = {}
        self.statements: dict[str, Statement] = {}
        self.portals: dict[str, PortalEntry] = {}
        self._ignore_until_sync = False
        self._current_task: asyncio.Task | None = None

        # Session state the middleware owns but only the connection can carry:
        # the open savepoint names (innermost last) and the ParameterStatus
        # reports a GUC_REPORT change owes the client. See report_parameter().
        self.savepoints: list[str] = []
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
                        {"S": "ERROR", "V": "ERROR", "C": "57014", "M": "canceling statement due to user request"}
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
        columns = await statement.describe()
        portal = statement.bind([])
        rows, _suspended = await portal.execute(0)

        if columns is not None:
            self.stream.write(messages.make_row_description(_field_specs(columns)))
            for row in rows:
                self.stream.write(messages.make_data_row(encode_row(row, columns)))
        self.stream.write(messages.make_command_complete(command_tag(sql, len(rows))))

    # --- extended query protocol -----------------------------------------------------

    async def _handle_parse(self, parsed: ParsedParse) -> None:
        param_oids: list[int | None] = [oid if oid != 0 else None for oid in parsed.param_oids]
        statement = await self.session.prepare(parsed.sql, param_oids)
        self.statements[parsed.statement_name] = statement
        self.stream.write(messages.make_parse_complete())

    def _get_statement(self, name: str) -> Statement:
        try:
            return self.statements[name]
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
        self.portals[parsed.portal_name] = PortalEntry(portal, columns, statement.sql, parsed.result_format_codes)
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
            return self.portals[name]
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
            self.statements.pop(parsed.name, None)
        else:
            self.portals.pop(parsed.name, None)
        self.stream.write(messages.make_close_complete())


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
