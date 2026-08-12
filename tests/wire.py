"""Minimal frontend-side wire helpers for tests that need to drive the
extended protocol (Parse/Bind/Describe/Execute/Close/Sync) at a lower level
than any particular client library exposes -- e.g. explicitly closing a named
prepared statement/portal, or issuing Describe(Statement) before any Bind.
pg_mimic itself has no need for frontend-message *builders* (it only parses
those), so these live here rather than in the package.
"""

from __future__ import annotations

import asyncio

from pg_mimic.types import pack_int16, pack_int32, unpack_int32

TARGET_STATEMENT = b"S"
TARGET_PORTAL = b"P"

PROTOCOL_3_0 = 196608


def _cstring(s: str) -> bytes:
    return s.encode("utf-8") + b"\x00"


def _frontend_message(tag: bytes, payload: bytes = b"") -> bytes:
    return tag + pack_int32(len(payload) + 4) + payload


def make_startup(
    user: str = "test",
    database: str = "test",
    version: int = PROTOCOL_3_0,
    extra: dict[str, str] | None = None,
) -> bytes:
    params = _cstring("user") + _cstring(user) + _cstring("database") + _cstring(database)
    for key, value in (extra or {}).items():
        params += _cstring(key) + _cstring(value)
    body = pack_int32(version) + params + b"\x00"
    return pack_int32(len(body) + 4) + body


def make_lying_message(tag: bytes, length: int, payload: bytes = b"") -> bytes:
    """A tagged frame whose Int32 length says something other than the truth.

    The one thing no client library will let a test send, and the whole point of
    talking the protocol by hand: a length of 0, a negative one, or a claim to be
    about to send two gigabytes."""
    return tag + pack_int32(length) + payload


def make_lying_startup(length: int, version: int = PROTOCOL_3_0, payload: bytes = b"") -> bytes:
    """The same, for the untagged startup packet."""
    return pack_int32(length) + pack_int32(version) + payload


def make_cancel_request(pid: int, secret: int) -> bytes:
    return pack_int32(16) + pack_int32(80877102) + pack_int32(pid) + pack_int32(secret)


def make_query(sql: str) -> bytes:
    return _frontend_message(b"Q", _cstring(sql))


def make_parse(sql: str, statement_name: str = "") -> bytes:
    payload = _cstring(statement_name) + _cstring(sql) + pack_int16(0)
    return _frontend_message(b"P", payload)


def make_bind(statement_name: str = "", portal_name: str = "", params: list[bytes] | None = None) -> bytes:
    params = params or []
    payload = _cstring(portal_name) + _cstring(statement_name) + pack_int16(0) + pack_int16(len(params))
    for value in params:
        payload += pack_int32(len(value)) + value
    payload += pack_int16(0)
    return _frontend_message(b"B", payload)


def make_describe(kind: bytes, name: str = "") -> bytes:
    return _frontend_message(b"D", kind + _cstring(name))


def make_execute(portal_name: str = "", max_rows: int = 0) -> bytes:
    return _frontend_message(b"E", _cstring(portal_name) + pack_int32(max_rows))


def make_close(kind: bytes, name: str = "") -> bytes:
    return _frontend_message(b"C", kind + _cstring(name))


def make_copy_data(data: bytes) -> bytes:
    return _frontend_message(b"d", data)


def make_copy_fail(reason: str) -> bytes:
    return _frontend_message(b"f", _cstring(reason))


SYNC = _frontend_message(b"S")
FLUSH = _frontend_message(b"H")
COPY_DONE = _frontend_message(b"c")


async def read_message(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    header = await reader.readexactly(5)
    length = unpack_int32(header, 1)
    payload = await reader.readexactly(length - 4) if length > 4 else b""
    return header[0:1], payload


def parse_error_fields(payload: bytes) -> dict[str, str]:
    return {s[:1].decode(): s[1:].decode() for s in payload.split(b"\x00") if s}


async def connect_and_get_backend_key(
    port: int, startup: bytes | None = None
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, int, int]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(make_startup() if startup is None else startup)
    await writer.drain()

    pid = secret = None
    while True:
        tag, payload = await read_message(reader)
        if tag == b"K":
            pid, secret = unpack_int32(payload, 0), unpack_int32(payload, 4)
        if tag == b"Z":
            break
    assert pid is not None and secret is not None
    return reader, writer, pid, secret
