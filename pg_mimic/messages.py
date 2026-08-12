"""Pure functions to build/parse Postgres wire protocol messages.

General framing (see docs: https://www.postgresql.org/docs/current/protocol-message-formats.html):
one-byte type tag + Int32 length (includes itself, excludes the tag) + payload.
StartupMessage/SSLRequest/CancelRequest are the exception -- no leading tag byte;
those are handled in stream.py since they only ever appear as the very first
message on a connection.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import pack_int16, pack_int32, read_cstring, unpack_int16, unpack_int32

# Frontend (client -> server) message tags
BIND = b"B"
CLOSE = b"C"
DESCRIBE = b"D"
EXECUTE = b"E"
FLUSH = b"H"
PARSE = b"P"
PASSWORD = b"p"
QUERY = b"Q"
SYNC = b"S"
TERMINATE = b"X"
# CopyData and CopyDone travel in both directions and keep the same tag either
# way -- copy-in sends them frontend to backend, copy-out backend to frontend.
COPY_DATA = b"d"
COPY_DONE = b"c"
COPY_FAIL = b"f"

# Backend (server -> client) message tags. Tags are scoped to their direction, so
# a byte here can collide with a frontend one: CopyOutResponse ('H') is the same
# byte as the frontend's Flush.
AUTHENTICATION = b"R"
BACKEND_KEY_DATA = b"K"
BIND_COMPLETE = b"2"
CLOSE_COMPLETE = b"3"
COMMAND_COMPLETE = b"C"
COPY_IN_RESPONSE = b"G"
COPY_OUT_RESPONSE = b"H"
DATA_ROW = b"D"
EMPTY_QUERY_RESPONSE = b"I"
ERROR_RESPONSE = b"E"
NEGOTIATE_PROTOCOL_VERSION = b"v"
NO_DATA = b"n"
NOTICE_RESPONSE = b"N"
PARAMETER_DESCRIPTION = b"t"
PARAMETER_STATUS = b"S"
PARSE_COMPLETE = b"1"
PORTAL_SUSPENDED = b"s"
READY_FOR_QUERY = b"Z"
ROW_DESCRIPTION = b"T"

# Special (untagged) pre-startup messages, distinguished by the Int32 magic
# number that follows their Int32 length.
SSL_REQUEST_CODE = 80877103
CANCEL_REQUEST_CODE = 80877102
GSSENC_REQUEST_CODE = 80877104

# The protocol version pg_mimic speaks, in the wire's own encoding: major in the
# high 16 bits, minor in the low ones. 3.0 has been the only version for two
# decades; 3.2 arrived with PostgreSQL 18, and libpq 18 asks for it. A minor
# version is by definition negotiable -- see NegotiateProtocolVersion below --
# where a major one is not. (libpq 18 still requests 3.0 unless asked for more
# with max_protocol_version.)
PROTOCOL_MAJOR = 3
PROTOCOL_MINOR = 0
PROTOCOL_VERSION = (PROTOCOL_MAJOR << 16) | PROTOCOL_MINOR

# Startup parameters under this prefix are protocol extension requests rather
# than settings, and are the other thing NegotiateProtocolVersion reports on.
PROTOCOL_EXTENSION_PREFIX = "_pq_."

# Statement/portal target kinds used by Describe and Close
TARGET_STATEMENT = ord("S")
TARGET_PORTAL = ord("P")


def _message(tag: bytes, payload: bytes = b"") -> bytes:
    return tag + pack_int32(len(payload) + 4) + payload


def _cstring(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


# --- Backend -> frontend builders -------------------------------------------------


def make_authentication_ok() -> bytes:
    return _message(AUTHENTICATION, pack_int32(0))


def make_authentication_cleartext_password() -> bytes:
    return _message(AUTHENTICATION, pack_int32(3))


def make_authentication_md5_password(salt: bytes) -> bytes:
    return _message(AUTHENTICATION, pack_int32(5) + salt)


def make_authentication_sasl(mechanisms: list[str]) -> bytes:
    payload = pack_int32(10)
    for mech in mechanisms:
        payload += _cstring(mech)
    payload += b"\x00"
    return _message(AUTHENTICATION, payload)


def make_authentication_sasl_continue(data: bytes) -> bytes:
    return _message(AUTHENTICATION, pack_int32(11) + data)


def make_authentication_sasl_final(data: bytes) -> bytes:
    return _message(AUTHENTICATION, pack_int32(12) + data)


def make_parameter_status(name: str, value: str) -> bytes:
    return _message(PARAMETER_STATUS, _cstring(name) + _cstring(value))


def make_backend_key_data(pid: int, secret: int) -> bytes:
    return _message(BACKEND_KEY_DATA, pack_int32(pid) + pack_int32(secret))


def make_ready_for_query(status: bytes) -> bytes:
    assert status in (b"I", b"T", b"E")
    return _message(READY_FOR_QUERY, status)


@dataclass
class FieldSpec:
    name: str
    oid: int
    table_oid: int = 0
    column_attnum: int = 0
    type_size: int = -1
    type_modifier: int = -1
    format_code: int = 0  # 0 = text, 1 = binary


def make_row_description(columns: list[FieldSpec]) -> bytes:
    payload = pack_int16(len(columns))
    for col in columns:
        payload += _cstring(col.name)
        payload += pack_int32(col.table_oid)
        payload += pack_int16(col.column_attnum)
        payload += pack_int32(col.oid)
        payload += pack_int16(col.type_size)
        payload += pack_int32(col.type_modifier)
        payload += pack_int16(col.format_code)
    return _message(ROW_DESCRIPTION, payload)


def make_data_row(values: list[bytes | None]) -> bytes:
    payload = pack_int16(len(values))
    for value in values:
        if value is None:
            payload += pack_int32(-1)
        else:
            payload += pack_int32(len(value)) + value
    return _message(DATA_ROW, payload)


def make_command_complete(tag: str) -> bytes:
    return _message(COMMAND_COMPLETE, _cstring(tag))


def make_empty_query_response() -> bytes:
    return _message(EMPTY_QUERY_RESPONSE)


def _fields_message(tag: bytes, fields: dict[str, str]) -> bytes:
    payload = b""
    for code, value in fields.items():
        payload += code.encode("ascii") + value.encode("utf-8") + b"\x00"
    payload += b"\x00"
    return _message(tag, payload)


def make_error_response(fields: dict[str, str]) -> bytes:
    return _fields_message(ERROR_RESPONSE, fields)


def make_notice_response(fields: dict[str, str]) -> bytes:
    return _fields_message(NOTICE_RESPONSE, fields)


def make_fatal_error(sqlstate: str, message: str) -> bytes:
    """An ErrorResponse at FATAL severity: the connection is being dropped, not
    just the current command failed. Clients treat the two differently -- psycopg
    marks the connection unusable on a FATAL rather than expecting a
    ReadyForQuery it will never get."""
    return make_error_response({"S": "FATAL", "V": "FATAL", "C": sqlstate, "M": message})


def make_negotiate_protocol_version(version: int, unrecognized_options: list[str]) -> bytes:
    """Tell the client which protocol version it is actually getting, and which
    of its `_pq_.` extension requests went unrecognised.

    Sent before authentication, and only when there is something to say: a client
    that asked for exactly what we speak gets no such message. The version field
    carries the full major/minor word, not the minor alone -- which is what libpq
    compares against its own, whatever the message-format table's wording
    suggests."""
    payload = pack_int32(version) + pack_int32(len(unrecognized_options))
    for option in unrecognized_options:
        payload += _cstring(option)
    return _message(NEGOTIATE_PROTOCOL_VERSION, payload)


def make_parse_complete() -> bytes:
    return _message(PARSE_COMPLETE)


def make_bind_complete() -> bytes:
    return _message(BIND_COMPLETE)


def make_close_complete() -> bytes:
    return _message(CLOSE_COMPLETE)


def make_no_data() -> bytes:
    return _message(NO_DATA)


def make_portal_suspended() -> bytes:
    return _message(PORTAL_SUSPENDED)


def make_parameter_description(oids: list[int]) -> bytes:
    payload = pack_int16(len(oids))
    for oid in oids:
        payload += pack_int32(oid)
    return _message(PARAMETER_DESCRIPTION, payload)


def _copy_response(tag: bytes, column_count: int) -> bytes:
    """Int8 overall copy format, Int16 column count, then one Int16 format code
    per column.

    The format is always 0 (text): binary COPY is refused while the statement is
    still being parsed, so a 1 never belongs here. There is no CopyBothResponse
    ('W') builder either -- that one exists only for streaming replication, which
    pg_mimic doesn't emulate.
    """
    return _message(tag, b"\x00" + pack_int16(column_count) + pack_int16(0) * column_count)


def make_copy_in_response(column_count: int) -> bytes:
    return _copy_response(COPY_IN_RESPONSE, column_count)


def make_copy_out_response(column_count: int) -> bytes:
    return _copy_response(COPY_OUT_RESPONSE, column_count)


def make_copy_data(data: bytes) -> bytes:
    return _message(COPY_DATA, data)


def make_copy_done() -> bytes:
    return _message(COPY_DONE)


# --- Frontend -> backend parsers ---------------------------------------------------
# Each parser takes the message *payload* (tag + length already stripped by stream.py).


def parse_query(payload: bytes) -> str:
    sql, _ = read_cstring(payload)
    return sql


def parse_password_message(payload: bytes) -> bytes:
    """Raw payload for the 'p' message. Its exact shape (cleartext password,
    MD5 response, SASLInitialResponse, SASLResponse) depends on which auth
    phase the connection is in -- interpreted by auth.py, not here."""
    return payload


def parse_sasl_initial_response(payload: bytes) -> tuple[str, bytes]:
    mechanism, offset = read_cstring(payload)
    resp_len = unpack_int32(payload, offset)
    offset += 4
    if resp_len == -1:
        return mechanism, b""
    return mechanism, payload[offset : offset + resp_len]


@dataclass
class ParsedParse:
    statement_name: str
    sql: str
    param_oids: list[int]


def parse_parse(payload: bytes) -> ParsedParse:
    statement_name, offset = read_cstring(payload)
    sql, offset = read_cstring(payload, offset)
    num_oids = unpack_int16(payload, offset)
    offset += 2
    oids = []
    for _ in range(num_oids):
        oids.append(unpack_int32(payload, offset))
        offset += 4
    return ParsedParse(statement_name, sql, oids)


@dataclass
class ParsedBind:
    portal_name: str
    statement_name: str
    param_format_codes: list[int]
    params: list[bytes | None]
    result_format_codes: list[int]


def _read_format_codes(payload: bytes, offset: int) -> tuple[list[int], int]:
    count = unpack_int16(payload, offset)
    offset += 2
    codes = []
    for _ in range(count):
        codes.append(unpack_int16(payload, offset))
        offset += 2
    return codes, offset


def parse_bind(payload: bytes) -> ParsedBind:
    portal_name, offset = read_cstring(payload)
    statement_name, offset = read_cstring(payload, offset)
    param_format_codes, offset = _read_format_codes(payload, offset)

    num_params = unpack_int16(payload, offset)
    offset += 2
    params: list[bytes | None] = []
    for _ in range(num_params):
        length = unpack_int32(payload, offset)
        offset += 4
        if length == -1:
            params.append(None)
        else:
            params.append(payload[offset : offset + length])
            offset += length

    result_format_codes, offset = _read_format_codes(payload, offset)
    return ParsedBind(portal_name, statement_name, param_format_codes, params, result_format_codes)


@dataclass
class ParsedExecute:
    portal_name: str
    max_rows: int


def parse_execute(payload: bytes) -> ParsedExecute:
    portal_name, offset = read_cstring(payload)
    max_rows = unpack_int32(payload, offset)
    return ParsedExecute(portal_name, max_rows)


@dataclass
class ParsedDescribeOrClose:
    kind: int  # TARGET_STATEMENT or TARGET_PORTAL
    name: str


def parse_describe(payload: bytes) -> ParsedDescribeOrClose:
    kind = payload[0]
    name, _ = read_cstring(payload, 1)
    return ParsedDescribeOrClose(kind, name)


def parse_close(payload: bytes) -> ParsedDescribeOrClose:
    return parse_describe(payload)


def parse_copy_fail(payload: bytes) -> str:
    """The client's stated reason for aborting a copy-in."""
    reason, _ = read_cstring(payload)
    return reason


def parse_startup_message(payload: bytes) -> dict[str, str]:
    """Parse the StartupMessage body (after the Int32 length + Int32 protocol
    version, which stream.py already consumed to detect this isn't a special
    SSLRequest/CancelRequest/GSSENCRequest). Body is repeated `key\\0 value\\0`
    pairs terminated by a lone `\\0`."""
    params: dict[str, str] = {}
    offset = 0
    while offset < len(payload) and payload[offset] != 0:
        key, offset = read_cstring(payload, offset)
        value, offset = read_cstring(payload, offset)
        params[key] = value
    return params


def parse_cancel_request(payload: bytes) -> tuple[int, int]:
    pid = unpack_int32(payload, 0)
    secret = unpack_int32(payload, 4)
    return pid, secret
