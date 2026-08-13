"""PgServer -- asyncio.start_server/start_unix_server wrapper, and the
per-socket accept callback that handles the untagged startup-phase messages
(SSLRequest/GSSENCRequest/CancelRequest) before a Connection is even created."""

from __future__ import annotations

import asyncio
import inspect
import logging
import os
import signal
import struct
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Awaitable, Callable

from . import messages
from .auth import AuthPlugin, IdentityProvider, SimpleIdentityProvider, TrustAuthPlugin
from .connection import Connection
from .errors import FEATURE_NOT_SUPPORTED, PgError, ProtocolViolation
from .session import BaseSession
from .stream import DEFAULT_MAX_MESSAGE_SIZE, ConnectionClosed, PgStream

logger = logging.getLogger(__name__)

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


@dataclass
class StartupRequest:
    """What a client asked for in its StartupMessage: the protocol version, and
    the parameters (minus any `_pq_.` extension requests, which are answered by
    NegotiateProtocolVersion rather than passed on as settings)."""

    protocol_version: int
    params: dict[str, str]


class PgServer:
    def __init__(
        self,
        session_factory: SessionFactory,
        auth_plugin_factory: AuthPluginFactory | None = None,
        identity_provider: IdentityProvider | None = None,
        server_version: str = "16.0 (pg_mimic)",
        max_message_size: int = DEFAULT_MAX_MESSAGE_SIZE,
    ):
        self.session_factory = session_factory
        self.auth_plugin_factory = auth_plugin_factory or (lambda username: TrustAuthPlugin())
        self.identity_provider = identity_provider or SimpleIdentityProvider()
        self.max_message_size = max_message_size
        self.parameter_status = {**DEFAULT_PARAMETER_STATUS, "server_version": server_version}

        self._server: asyncio.base_events.Server | None = None
        self._connections: dict[int, Connection] = {}
        # channel -> the connections listening to it. Server-wide because that is
        # what LISTEN/NOTIFY is: a NOTIFY reaches every connection subscribed to
        # the channel, not just the one that raised it. See notify().
        self._listeners: dict[str, set[Connection]] = {}
        self._tasks: set[asyncio.Task] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._closing = False
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

    def run(self, host: str = "127.0.0.1", port: int = 5432, **kwargs: Any) -> None:
        """Serve until interrupted. For when the server *is* the program.

            PgServer(session_factory=MySession).run(port=5432)

        Blocking, and owns the event loop -- it calls `asyncio.run`, so there must
        not already be one. Inside an existing loop, or anywhere the caller wants
        the loop for something else, use `start_server()` and `serve_forever()`
        directly; this is only the common case wrapped up.

        SIGINT and SIGTERM stop it and it returns; neither raises. The signals are
        handled *by the loop* rather than left to Python's default handler, and
        that is not tidiness: a default-handled SIGINT raises KeyboardInterrupt
        into whichever coroutine happens to be running, so a connection parked in
        `read_message()` dies with an exception nobody retrieves and asyncio prints
        it. Intermittently, depending on where the signal lands. Handling it here
        means one future resolves and the shutdown is ordinary.

        The listening address goes out through this module's logger at INFO rather
        than to stdout, so a program that embeds pg_mimic decides where its own
        output goes. `logging.basicConfig(level=logging.INFO)` is enough to see it.
        """

        async def serve() -> None:
            loop = asyncio.get_running_loop()
            stop = loop.create_future()

            def request_stop() -> None:
                if not stop.done():
                    stop.set_result(None)

            installed = []
            for signal_number in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(signal_number, request_stop)
                    installed.append(signal_number)
                except (NotImplementedError, RuntimeError):
                    # Windows, and any loop that will not take one. The
                    # KeyboardInterrupt path below is the fallback there.
                    pass

            await self.start_server(host=host, port=port, **kwargs)
            logger.info("pg_mimic listening on %s:%s", host, port)
            try:
                await stop
            finally:
                for signal_number in installed:
                    loop.remove_signal_handler(signal_number)
                # Not left to the loop shutdown: close() drops live connections,
                # and without it a client still attached keeps the server from
                # finishing at all -- see close()'s own note.
                self.close()
                await self.wait_closed()

        try:
            asyncio.run(serve())
        except KeyboardInterrupt:  # pragma: no cover -- only where add_signal_handler is unavailable
            pass
        logger.info("pg_mimic stopped")

    async def __aenter__(self) -> PgServer:
        """Start on an ephemeral port, unless the caller already started us.

        Deliberately no serve_forever() here: asyncio.start_server() is already
        accepting connections by the time it returns, and serve_forever() only
        parks a coroutine for a process whose whole job is running the server.
        A test's job is the test, so it needs the server started and nothing
        holding its own coroutine hostage.
        """
        if self._server is None:
            await self.start_server(host="127.0.0.1", port=0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
        await self.wait_closed()

    def close(self) -> None:
        """Stop listening and drop live connections.

        Closing the client transports matters as much as cancelling the handler
        tasks. From Python 3.12, a cancelled `Server.serve_forever()` calls
        `wait_closed()` before re-raising, and that waits for every active
        connection to go away -- so with one still open, serve_forever never
        returns and close() cannot shut the server down at all. Cancelling the
        handler is not enough: the connection stays counted as active until its
        transport actually closes.
        """
        self._closing = True
        if self._server is not None:
            self._server.close()
            # 3.13+ can drop clients the server knows about but we may not yet.
            close_clients = getattr(self._server, "close_clients", None)
            if close_clients is not None:
                close_clients()
        for task in self._tasks:
            task.cancel()
        for writer in list(self._writers):
            try:
                writer.close()
            except Exception:
                pass  # already closing, or the transport is gone

    async def wait_closed(self) -> None:
        if self._server is not None:
            await self._server.wait_closed()

    def sockets(self):
        return self._server.sockets if self._server is not None else None

    @property
    def port(self) -> int:
        """The port actually bound -- the answer to starting on port 0, which is
        what anything embedding the server in a test wants to do."""
        sockets = self.sockets()
        assert sockets, "not listening on a TCP socket -- call start_server() first"
        return sockets[0].getsockname()[1]

    def dsn(self, user: str = "postgres", dbname: str = "postgres", host: str = "127.0.0.1") -> str:
        """A libpq connection string pointing at this server.

        `user` and `dbname` are startup parameters, not credentials or real
        databases: trust auth (the default) accepts any user, and a mimic has
        whatever databases its session says it has. They are arguments because a
        session -- or a test asserting on `current_user`/`current_database()` --
        may care what it was told. `host` is not read back off the socket
        because a server bound to 0.0.0.0 reports an address nothing can connect
        to.
        """
        return f"host={host} port={self.port} user={user} dbname={dbname}"

    def cancel(self, pid: int, secret: int) -> None:
        conn = self._connections.get(pid)
        if conn is not None and conn.secret == secret:
            conn.request_cancel()

    # --- LISTEN/NOTIFY registry ---------------------------------------------------

    def add_listener(self, channel: str, connection: Connection) -> None:
        self._listeners.setdefault(channel, set()).add(connection)

    def remove_listener(self, channel: str, connection: Connection) -> None:
        subscribers = self._listeners.get(channel)
        if subscribers is None:
            return
        subscribers.discard(connection)
        # Dropped when empty rather than left as a husk: channel names come from
        # clients, so a long-lived server would otherwise accumulate one entry per
        # name anything ever listened to.
        if not subscribers:
            del self._listeners[channel]

    def listeners(self, channel: str) -> set[Connection]:
        """The connections currently listening to `channel`. A copy, so a caller
        may deliver to it while a delivery changes the registry underneath."""
        return set(self._listeners.get(channel, ()))

    def notify(self, channel: str, payload: str = "", pid: int = 0) -> int:
        """Deliver a notification to every connection listening to `channel`, and
        report how many got it.

        Public: a `NOTIFY` statement is only one way to raise an event, and code
        embedding the server may want to raise one from outside a session
        entirely. Delivery is synchronous but never blocking -- see
        Connection._deliver_async.

        One listener failing does not stop the rest. A connection can be closing
        while this runs, and a fanout that gave up half way would leave the
        outcome depending on set iteration order.

        Must be called on the loop the server is running on, because it writes to
        connections' transports and asyncio transports are not thread-safe. That
        is automatic from inside a session or a middleware, and is the one thing
        to watch when the server is on a thread of its own -- hop to its loop
        (`testing.ServerThread.loop`) rather than calling straight through::

            thread.loop.call_soon_threadsafe(server.notify, "orders", "42")
        """
        delivered = 0
        for connection in self.listeners(channel):
            try:
                connection.notify(channel, payload, pid)
            except Exception:
                logger.debug("dropping notification on channel %r: connection %s is gone", channel, connection.pid)
            else:
                delivered += 1
        return delivered

    def _allocate_pid(self) -> int:
        pid = self._next_pid
        self._next_pid += 1
        return pid

    async def _client_connected_cb(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing:
            # Accepted just as the server was closing: the socket exists but this
            # callback had not run yet, so close() could not have known to drop it.
            # Left open it would keep asyncio counting an active connection, which
            # is exactly what stops the server shutting down.
            writer.close()
            return
        task = asyncio.current_task()
        if task is not None:
            self._tasks.add(task)
        self._writers.add(writer)
        try:
            await self._handle_client(reader, writer)
        except asyncio.CancelledError:
            pass
        finally:
            if task is not None:
                self._tasks.discard(task)
            self._writers.discard(writer)
            # The callback owns this connection, so close it on every path out --
            # the early returns (a client that hangs up before the startup message,
            # or a CancelRequest) otherwise leave the transport open, and asyncio
            # counts it as an active connection until it actually closes.
            try:
                writer.close()
            except Exception:
                pass

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        stream = PgStream(reader, writer, max_message_size=self.max_message_size)
        try:
            startup = await self._negotiate_startup(stream)
        except ConnectionClosed:
            return
        except PgError as e:
            # A startup packet we won't read (see PgStream._check_length) or a
            # protocol version we don't speak. There is no Connection yet to
            # report it, so it is reported here, and the caller's finally drops
            # the socket -- which is what a FATAL means.
            stream.write(messages.make_fatal_error(e.sqlstate, e.message))
            await stream.drain_quietly()
            return
        if startup is None:
            return  # was a CancelRequest, already handled

        session = self.session_factory()
        if inspect.isawaitable(session):
            session = await session

        pid = self._allocate_pid()
        secret = struct.unpack("!i", os.urandom(4))[0]
        connection = Connection(stream, session, self, pid, secret, startup.params, protocol_version=startup.protocol_version)
        self._connections[pid] = connection
        try:
            await connection.run()
        finally:
            self._connections.pop(pid, None)

    async def _negotiate_startup(self, stream: PgStream) -> StartupRequest | None:
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

        major, minor = code >> 16, code & 0xFFFF
        if major != messages.PROTOCOL_MAJOR:
            # Postgres's own wording and SQLSTATE for this. A major version is
            # not negotiable -- 2.0 framed its messages differently, and
            # whatever a 4.0 does we have never seen -- so the client is told
            # rather than left to fail somewhere further in, where the symptom
            # would be a parse error against bytes we misread.
            raise ProtocolViolation(
                f"unsupported frontend protocol {major}.{minor}: server supports "
                f"{messages.PROTOCOL_MAJOR}.0 to {messages.PROTOCOL_MAJOR}.{messages.PROTOCOL_MINOR}",
                sqlstate=FEATURE_NOT_SUPPORTED,
            )

        params = messages.parse_startup_message(payload)
        extensions = [name for name in params if name.startswith(messages.PROTOCOL_EXTENSION_PREFIX)]
        for name in extensions:
            # Not settings, and not ours: a session must not see `_pq_.foo` as
            # something it was asked to apply. Reported back instead, below.
            del params[name]
        if minor > messages.PROTOCOL_MINOR or extensions:
            # A minor version above ours means the client has to be told what it
            # is actually getting, or it goes on believing 3.2 features are
            # available. Saying so is the whole point of the message: libpq 18
            # asks for 3.2 under `max_protocol_version=latest` (3.0 remains its
            # default) and downgrades itself on this reply.
            stream.write(messages.make_negotiate_protocol_version(messages.PROTOCOL_VERSION, extensions))
            await stream.drain()
        return StartupRequest(protocol_version=code, params=params)
