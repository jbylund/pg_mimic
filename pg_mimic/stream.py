"""Message framing over asyncio streams.

Postgres framing is simpler than MySQL's: a 1-byte type tag + Int32 length
(includes itself, excludes the tag) + payload, with no packet-sequence
numbering and no split-packet handling needed. The one wrinkle is the
untagged "special" messages that can only appear as the very first thing on a
connection (StartupMessage, SSLRequest, CancelRequest, GSSENCRequest) -- they
have no leading tag byte, so the server must peek the first 8 bytes to tell
them apart (see messages.SSL_REQUEST_CODE et al.).
"""

from __future__ import annotations

import asyncio

from .types import unpack_int32


class ConnectionClosed(Exception):
    """Raised when the peer closes the connection mid-read."""


class PgStream:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.reader = reader
        self.writer = writer

    async def _read_exactly(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError as e:
            raise ConnectionClosed from e

    async def read_startup_packet(self) -> tuple[int, bytes]:
        """Read the length-prefixed startup-phase message. Returns (code, payload)
        where code is either the protocol version (0x00030000 for 3.0) or one of
        the special magic numbers (SSLRequest/CancelRequest/GSSENCRequest)."""
        header = await self._read_exactly(8)
        length = unpack_int32(header, 0)
        code = unpack_int32(header, 4)
        remaining = length - 8
        payload = await self._read_exactly(remaining) if remaining > 0 else b""
        return code, payload

    async def read_message(self) -> tuple[bytes, bytes]:
        """Read one regular tagged message. Returns (tag, payload)."""
        header = await self._read_exactly(5)
        tag = header[0:1]
        length = unpack_int32(header, 1)
        payload = await self._read_exactly(length - 4) if length > 4 else b""
        return tag, payload

    def write(self, data: bytes) -> None:
        self.writer.write(data)

    async def drain(self) -> None:
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
