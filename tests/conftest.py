"""The suite's own fixtures: a configurable MockSession, and the psycopg/asyncpg
connections the tests drive it through.

The server itself comes from `pg_mimic.testing`, the shipped helpers, so this
suite exercises the same code users get rather than a private copy of it. That
module's docstring explains why the server runs on its own thread and loop.

`MockSession` and `ServerThread` are re-exported for the tests that import them
directly: several build their own PgServer (custom auth plugins, a session they
keep a handle on) and want the thread around it without the context manager.
"""

from __future__ import annotations

import re

import asyncpg
import psycopg
import pytest
import pytest_asyncio

from pg_mimic import PgError, ResultColumn, Session
from pg_mimic.errors import UNDEFINED_TABLE
from pg_mimic.testing import ServerThread, serve_in_thread

# test_testing.py runs the fixture plugin in a nested pytest session to check the
# fixtures it advertises actually work.
pytest_plugins = ["pytester"]

__all__ = ["MockSession", "ServerThread"]

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


@pytest.fixture
def mock_session():
    return MockSession()


@pytest.fixture
def pg_server(mock_session):
    with serve_in_thread(lambda: mock_session) as server:
        yield server


@pytest.fixture
def dsn(pg_server):
    # user/dbname are named rather than left at their defaults because tests assert
    # on what current_user/current_database() report back.
    return pg_server.dsn(user="test", dbname="test")


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
    conn = await asyncpg.connect(host="127.0.0.1", port=pg_server.port, user="test", database="test")
    try:
        yield conn
    finally:
        await conn.close()
