"""Runs PgServer in a dedicated background thread with its own event loop.

This matters for the sync psycopg test fixtures: if the server shared the
same event loop as the (pytest-asyncio-managed) test loop, a blocking sync
`psycopg.connect()` call from a plain sync fixture would deadlock -- nothing
would be driving the server's loop while the sync call blocks waiting for a
response. Running the server on its own thread/loop makes it independent of
whatever the test thread is doing, sync or async, exactly like a real
embedded mimic server would run in practice.
"""

from __future__ import annotations

import asyncio
import re
import threading

import asyncpg
import psycopg
import pytest
import pytest_asyncio

from pg_mimic import PgError, PgServer, ResultColumn, Session
from pg_mimic.errors import UNDEFINED_TABLE

# pg_catalog isn't emulated, so a session fronting a real store would report the
# table as missing. Saying so matters: a client that gets a plausible-but-wrong
# answer to its type-introspection query may retry it indefinitely (asyncpg
# does), where a clean error just fails.
_CATALOG_RE = re.compile(r"\bpg_(catalog|type|attribute|namespace|range|class|proc|enum)\b", re.IGNORECASE)


class MockSession(Session):
    """Configurable test double: set `.rows`/`.columns` and it answers every
    query with them, ignoring the SQL text -- except catalog queries, which it
    reports as missing tables the way a real session would."""

    def __init__(self):
        self.rows: list[tuple] = []
        self.columns: list[ResultColumn] | None = None
        self.queries: list[tuple[str, list]] = []
        self.error: Exception | None = None

    async def describe(self, sql, param_oids):
        self._reject_catalog(sql)
        return self.columns

    @staticmethod
    def _reject_catalog(sql):
        match = _CATALOG_RE.search(sql)
        if match:
            raise PgError(UNDEFINED_TABLE, f'relation "{match.group(0)}" does not exist')

    async def query(self, sql, params):
        self.queries.append((sql, params))
        if self.error is not None:
            raise self.error
        for row in self.rows:
            yield row


class ServerThread:
    def __init__(self, server: PgServer):
        self.server = server
        self.port: int | None = None
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self.server.start_server(host="127.0.0.1", port=0))
        self.port = self.server.sockets()[0].getsockname()[1]
        self._ready.set()
        try:
            self._loop.run_until_complete(self.server.serve_forever())
        except asyncio.CancelledError:
            pass
        finally:
            pending = [t for t in self.server._tasks if not t.done()]
            if pending:
                self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))

    def start(self) -> int:
        self._thread.start()
        self._ready.wait()
        return self.port

    def stop(self) -> None:
        self._loop.call_soon_threadsafe(self.server.close)
        self._thread.join(timeout=5)


@pytest.fixture
def mock_session():
    return MockSession()


@pytest.fixture
def pg_server(mock_session):
    server = PgServer(session_factory=lambda: mock_session)
    thread = ServerThread(server)
    thread.start()
    try:
        yield server, thread.port
    finally:
        thread.stop()


@pytest.fixture
def dsn(pg_server):
    _server, port = pg_server
    return f"host=127.0.0.1 port={port} user=test dbname=test"


@pytest_asyncio.fixture
async def aconn(dsn):
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        yield conn
    finally:
        await conn.close()


@pytest.fixture
def conn(dsn):
    with psycopg.Connection.connect(dsn, autocommit=True) as conn:
        yield conn


@pytest_asyncio.fixture
async def apg_conn(pg_server):
    """asyncpg against the same server the psycopg fixtures use.

    Worth testing separately rather than assuming psycopg covers it: asyncpg
    always requests binary results, where psycopg only does on request, so it
    exercises the binary path far harder.
    """
    _server, port = pg_server
    conn = await asyncpg.connect(host="127.0.0.1", port=port, user="test", database="test")
    try:
        yield conn
    finally:
        await conn.close()
