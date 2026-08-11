"""PgServer -- asyncio.start_server/start_unix_server wrapper, and the
per-socket accept callback that handles the untagged startup-phase messages
(SSLRequest/GSSENCRequest/CancelRequest) before a Connection is even created."""

from __future__ import annotations

import asyncio
import inspect
import os
import struct
from typing import Awaitable, Callable

from . import messages
from .auth import AuthPlugin, IdentityProvider, SimpleIdentityProvider, TrustAuthPlugin
from .connection import Connection
from .session import BaseSession
from .stream import ConnectionClosed, PgStream

SessionFactory = Callable[[], BaseSession | Awaitable[BaseSession]]
AuthPluginFactory = Callable[[str], AuthPlugin]

DEFAULT_PARAMETER_STATUS = {
    "server_encoding": "UTF8",
    "client_encoding": "UTF8",
    "DateStyle": "ISO, MDY",
    "IntervalStyle": "postgres",
    "integer_datetimes": "on",
    "standard_conforming_strings": "on",
    "TimeZone": "UTC",
}


class PgServer:
    def __init__(
        self,
        session_factory: SessionFactory,
        auth_plugin_factory: AuthPluginFactory | None = None,
        identity_provider: IdentityProvider | None = None,
        server_version: str = "16.0 (pg_mimic)",
    ):
        self.session_factory = session_factory
        self.auth_plugin_factory = auth_plugin_factory or (lambda username: TrustAuthPlugin())
        self.identity_provider = identity_provider or SimpleIdentityProvider()
        self.parameter_status = {**DEFAULT_PARAMETER_STATUS, "server_version": server_version}

        self._server: asyncio.base_events.Server | None = None
        self._connections: dict[int, Connection] = {}
        self._tasks: set[asyncio.Task] = set()
        self._next_pid = 10000

    async def start_server(self, host: str | None = None, port: int | None = None, **kwargs) -> asyncio.base_events.Server:
        self._server = await asyncio.start_server(self._client_connected_cb, host=host, port=port, **kwargs)
        return self._server

    async def start_unix_server(self, path: str, **kwargs) -> asyncio.base_events.Server:
        self._server = await asyncio.start_unix_server(self._client_connected_cb, path=path, **kwargs)
        return self._server

    async def serve_forever(self) -> None:
        assert self._server is not None, "call start_server()/start_unix_server() first"
        async with self._server:
            await self._server.serve_forever()

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
        for task in self._tasks:
            task.cancel()

    async def wait_closed(self) -> None:
        if self._server is not None:
            await self._server.wait_closed()

    def sockets(self):
        return self._server.sockets if self._server is not None else None

    def cancel(self, pid: int, secret: int) -> None:
        conn = self._connections.get(pid)
        if conn is not None and conn.secret == secret:
            conn.request_cancel()

    def _allocate_pid(self) -> int:
        pid = self._next_pid
        self._next_pid += 1
        return pid

    async def _client_connected_cb(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = PgStream(reader, writer)
        try:
            startup_params = await self._negotiate_startup(stream)
        except ConnectionClosed:
            return
        if startup_params is None:
            return  # was a CancelRequest, already handled

        session = self.session_factory()
        if inspect.isawaitable(session):
            session = await session

        pid = self._allocate_pid()
        secret = struct.unpack("!i", os.urandom(4))[0]
        connection = Connection(stream, session, self, pid, secret, startup_params)
        self._connections[pid] = connection
        try:
            await connection.run()
        finally:
            self._connections.pop(pid, None)

    async def _negotiate_startup(self, stream: PgStream) -> dict[str, str] | None:
        code, payload = await stream.read_startup_packet()

        # No TLS support in v1 -- always decline, and let the client fall back
        # to plaintext (real clients that opportunistically probe for SSL
        # accept an 'N' response and continue in the clear).
        if code in (messages.SSL_REQUEST_CODE, messages.GSSENC_REQUEST_CODE):
            stream.write(b"N")
            await stream.drain()
            code, payload = await stream.read_startup_packet()

        if code == messages.CANCEL_REQUEST_CODE:
            pid, secret = messages.parse_cancel_request(payload)
            self.cancel(pid, secret)
            await stream.close()
            return None

        return messages.parse_startup_message(payload)
