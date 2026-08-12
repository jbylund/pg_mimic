"""Message framing over asyncio streams.

Postgres framing is simpler than MySQL's: a 1-byte type tag + Int32 length
(includes itself, excludes the tag) + payload, with no packet-sequence
numbering and no split-packet handling needed. The one wrinkle is the
untagged "special" messages that can only appear as the very first thing on a
connection (StartupMessage, SSLRequest, CancelRequest, GSSENCRequest) -- they
have no leading tag byte, so the server must peek the first 8 bytes to tell
them apart (see messages.SSL_REQUEST_CODE et al.).

Every length on the way in is the peer's word for how much to buffer, so it is
checked before it is acted on -- see _check_length.
"""

from __future__ import annotations

import asyncio

from .errors import ProtocolViolation
from .types import unpack_int32

# The largest tagged message accepted from a client, unless the server is
# constructed with another. Real Postgres's own frame limit is 1GB, which is the
# size of the values it has to be able to store; a mimic holds the whole message
# in memory and answers from Python objects, so it has no such obligation, and a
# limit that large leaves the hole this exists to close. 64MiB is the same
# default MySQL's `max_allowed_packet` picked for the same job, and it clears
# what real clients send by orders of magnitude: psycopg splits a COPY stream
# into 128KiB CopyData messages and asyncpg into 512KiB ones, so what actually
# reaches this number is a single enormous bind parameter -- a file being
# inserted into a bytea column -- which stays legal.
DEFAULT_MAX_MESSAGE_SIZE = 64 * 1024 * 1024

# Real Postgres's MAX_STARTUP_PACKET_LENGTH, kept to the byte. A startup packet
# carries a protocol version and a handful of parameters, so every real client
# fits inside it with room to spare -- and the alternative is letting an unopened
# connection, one that has not even said who it is yet, name its own buffer size.
MAX_STARTUP_PACKET_LENGTH = 10000


class ConnectionClosed(Exception):
    """Raised when the peer closes the connection mid-read."""


class PgStream:
    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ):
        self.reader = reader
        self.writer = writer
        self.max_message_size = max_message_size

    async def _read_exactly(self, n: int) -> bytes:
        try:
            return await self.reader.readexactly(n)
        except asyncio.IncompleteReadError as e:
            raise ConnectionClosed from e

    @staticmethod
    def _check_length(length: int, minimum: int, maximum: int, what: str) -> None:
        """Reject a length the peer had no business sending, before allocating to it.

        Both bounds guard the same fact: this number arrived over the socket and
        the header it describes has already been read. Below the minimum it
        contradicts that header -- 0, or a negative Int32 -- and a subtraction
        would quietly turn it into "no payload" rather than into the framing
        error it is. Above the maximum it is an instruction to sit on a read of
        up to 2GB, buffering whatever a peer that may never finish sending
        chooses to send, which one bad Int32 is enough to ask for.

        `what` is a constant rather than something built from the message's tag:
        this runs on every message, and only ever says anything on the frame
        that ends the connection anyway.
        """
        if length < minimum:
            raise ProtocolViolation(f"invalid {what} length {length}: must be at least {minimum}, counting the length itself")
        if length > maximum:
            raise ProtocolViolation(f"invalid {what} length {length}: exceeds the {maximum}-byte maximum")

    async def read_startup_packet(self) -> tuple[int, bytes]:
        """Read the length-prefixed startup-phase message. Returns (code, payload)
        where code is either the protocol version (0x00030000 for 3.0) or one of
        the special magic numbers (SSLRequest/CancelRequest/GSSENCRequest)."""
        header = await self._read_exactly(8)
        length = unpack_int32(header, 0)
        code = unpack_int32(header, 4)
        # The length covers itself and the code, so the 8 bytes already in hand
        # are its floor. A server configured below Postgres's startup ceiling
        # means it: the smaller of the two wins.
        self._check_length(length, 8, min(MAX_STARTUP_PACKET_LENGTH, self.max_message_size), "startup packet")
        remaining = length - 8
        payload = await self._read_exactly(remaining) if remaining > 0 else b""
        return code, payload

    async def read_message(self) -> tuple[bytes, bytes]:
        """Read one regular tagged message. Returns (tag, payload)."""
        header = await self._read_exactly(5)
        tag = header[0:1]
        length = unpack_int32(header, 1)
        self._check_length(length, 4, self.max_message_size, "message")
        payload = await self._read_exactly(length - 4) if length > 4 else b""
        return tag, payload

    def write(self, data: bytes) -> None:
        self.writer.write(data)

    async def drain(self) -> None:
        await self.writer.drain()

    async def drain_quietly(self) -> None:
        """Push out a last message on a connection that is already ending -- a
        FATAL report. The peer may have hung up first, in which case there is
        nobody left to report anything to."""
        try:
            await self.writer.drain()
        except (ConnectionError, OSError):
            pass

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass
