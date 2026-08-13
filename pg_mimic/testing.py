"""Standing a server up inside a test, with the two non-obvious parts solved.

The first is small: a test cannot pick a fixed port, so the server has to be
started on port 0 and asked which one it got (`PgServer.port`).

The second is the reason this module exists. A *blocking* client -- sync
`psycopg.connect()`, a DBAPI driver, anything that doesn't await -- cannot share
an event loop with the server it is talking to. The call blocks the thread, the
thread is what runs the loop, so nothing is left to accept the connection or
answer the query, and the test deadlocks until its timeout. `serve_in_thread()`
therefore gives the server its own loop in its own thread. That also happens to
be how an embedded mimic really runs: the server doesn't care what the client's
thread is doing.

`serve()` is the counterpart for async tests, where sharing the loop is fine
because an awaiting client hands control back.

Deliberately importable without pytest. pytest is a dev-only dependency of
pg_mimic, so the fixtures built on these helpers live in
`pg_mimic.pytest_plugin` (loaded through a `pytest11` entry point) and only that
module imports it.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Any, AsyncIterator, Iterator

from .server import PgServer, SessionFactory

DEFAULT_HOST = "127.0.0.1"

# Long enough that a server which is shutting down normally always makes it, short
# enough that one which isn't reports as a failure instead of stalling the suite.
SHUTDOWN_TIMEOUT = 5.0

__all__ = ["ServerThread", "serve", "serve_in_thread"]


@asynccontextmanager
async def serve(session_factory: SessionFactory, *, host: str = DEFAULT_HOST, **kwargs: Any) -> AsyncIterator[PgServer]:
    """Run a server on the current event loop for the duration of the block.

    For async tests. Extra keyword arguments go to `PgServer` (auth_plugin_factory,
    identity_provider, server_version)::

        async with serve(MySession) as server:
            conn = await asyncpg.connect(host="127.0.0.1", port=server.port, user="u", database="d")

    A blocking client called from inside this block will deadlock against the
    server -- see the module docstring, and use `serve_in_thread()` instead.
    """
    server = PgServer(session_factory, **kwargs)
    await server.start_server(host=host, port=0)
    try:
        yield server
    finally:
        server.close()
        await server.wait_closed()


class ServerThread:
    """A `PgServer` running on its own event loop in its own thread.

    `serve_in_thread()` is the shape most callers want. This class is public for
    the case that one doesn't fit: a server that has to be constructed first
    (custom auth plugins, a session object the test keeps a handle on) and
    started and stopped explicitly.
    """

    def __init__(self, server: PgServer, host: str = DEFAULT_HOST):
        self.server = server
        self.port: int | None = None
        self._host = host
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._startup_error: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="pg_mimic-server", daemon=True)

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        """The loop the server runs on.

        Public because anything that reaches into a live connection from the
        outside has to get onto it first -- `PgServer.notify()` writes to client
        transports, and asyncio transports are not thread-safe::

            thread.loop.call_soon_threadsafe(server.notify, "orders", "42")
        """
        return self._loop

    def start(self) -> int:
        """Start serving and return the bound port. Blocks until the socket is
        bound, so a client can connect the moment this returns."""
        self._thread.start()
        self._ready.wait()
        if self._startup_error is not None:
            raise self._startup_error
        assert self.port is not None
        return self.port

    def stop(self) -> None:
        if self._loop.is_closed():
            return  # already stopped -- the thread closes its loop on the way out
        # close() touches loop-owned state (the listening socket, handler tasks,
        # client transports), so it has to run on the server's own loop rather
        # than on the caller's thread.
        self._loop.call_soon_threadsafe(self.server.close)
        self._thread.join(timeout=SHUTDOWN_TIMEOUT)
        # An assertion rather than a warning: a thread still alive here means
        # close() could not drop a connection, which is a real bug in the server
        # and has twice been one (see tests/test_shutdown.py). Leaking the thread
        # quietly would let that pass as a green test.
        assert not self._thread.is_alive(), "server thread did not shut down -- a connection is still open"

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            try:
                self._loop.run_until_complete(self.server.start_server(host=self._host, port=0))
                self.port = self.server.port
            except BaseException as exc:
                # Hand the failure to start (a bind that was refused, a bad
                # PgServer argument) back to start(), which is otherwise waiting
                # on an event nobody will ever set and shows up as a mystery hang.
                self._startup_error = exc
                return
            finally:
                self._ready.set()
            try:
                self._loop.run_until_complete(self.server.serve_forever())
            except asyncio.CancelledError:
                pass
            finally:
                self._cancel_pending_handlers()
        finally:
            # Finalise the sessions' async generators before closing: a query()
            # generator abandoned mid-iteration is finalised by the loop that
            # started it, and doing that on a closed loop raises from inside the
            # garbage collector. Closing at all matters because a suite that
            # stands a server up per test otherwise leaks a loop -- and its
            # selector and self-pipe file descriptors -- per test.
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.close()

    def _cancel_pending_handlers(self) -> None:
        pending = [task for task in self.server._tasks if not task.done()]
        if not pending:
            return
        # Cancel rather than await: a connection the test left open keeps its
        # handler task alive forever, so gathering it would hang here until
        # stop()'s join gives up -- five seconds and a leaked thread per test, for
        # what looks like a passing test.
        for task in pending:
            task.cancel()
        self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))


@contextmanager
def serve_in_thread(session_factory: SessionFactory, *, host: str = DEFAULT_HOST, **kwargs: Any) -> Iterator[PgServer]:
    """Run a server on its own event loop in a background thread.

    For sync tests, and for async tests that call a blocking client anyway.
    Yields the server, so `server.port` and `server.dsn()` give the client what
    it needs. Extra keyword arguments go to `PgServer`::

        with serve_in_thread(MySession) as server:
            with psycopg.connect(server.dsn()) as conn:
                ...

    On the way out the server is closed and the thread joined; a connection left
    open by the block is dropped rather than waited for.
    """
    server = PgServer(session_factory, **kwargs)
    thread = ServerThread(server, host=host)
    thread.start()
    try:
        yield server
    finally:
        thread.stop()
