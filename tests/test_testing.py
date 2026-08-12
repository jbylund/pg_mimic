"""The public test helpers: PgServer's port/dsn/async-context conveniences,
pg_mimic.testing's two context managers, and the pytest plugin.

The rest of the suite already leans on `serve_in_thread`/`ServerThread` through
conftest, which is the broad coverage. These tests pin the properties that are
easy to lose and that nothing else would notice: that a blocking client works
from inside an async test, that shutdown doesn't wait for a connection the test
abandoned, that the fixtures are reachable without any conftest plumbing, and
that importing the helpers doesn't require pytest.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import subprocess
import sys
import textwrap

import psycopg
import pytest
from conftest import MockSession

from pg_mimic import PgServer, ResultColumn
from pg_mimic.testing import ServerThread, serve, serve_in_thread


def _one_row_session() -> MockSession:
    session = MockSession()
    session.columns = [ResultColumn.for_type("n", int)]
    session.rows = [(42,)]
    return session


def _blocking_query(dsn: str) -> list[tuple]:
    with psycopg.Connection.connect(dsn, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT n FROM t")
            return cur.fetchall()


async def _async_query(dsn: str) -> list[tuple]:
    conn = await psycopg.AsyncConnection.connect(dsn, autocommit=True)
    try:
        async with conn.cursor() as cur:
            await cur.execute("SELECT n FROM t")
            return await cur.fetchall()
    finally:
        await conn.close()


async def test_port_and_dsn_describe_the_bound_socket():
    async with PgServer(session_factory=_one_row_session) as server:
        assert server.port > 0
        assert server.dsn(user="u", dbname="d") == f"host=127.0.0.1 port={server.port} user=u dbname=d"


async def test_aenter_accepts_connections_without_serve_forever():
    """start_server() is already serving, so nothing has to hold serve_forever()."""
    async with PgServer(session_factory=_one_row_session) as server:
        assert await _async_query(server.dsn()) == [(42,)]


async def test_aenter_leaves_an_already_started_server_alone():
    server = PgServer(session_factory=_one_row_session)
    await server.start_server(host="127.0.0.1", port=0)
    port = server.port
    async with server:
        assert server.port == port


async def test_aexit_stops_listening():
    async with PgServer(session_factory=_one_row_session) as server:
        port = server.port
    with pytest.raises(ConnectionRefusedError):
        await asyncio.open_connection("127.0.0.1", port)


async def test_serve_answers_an_async_client():
    async with serve(_one_row_session) as server:
        assert await _async_query(server.dsn()) == [(42,)]


async def test_serve_drops_a_connection_the_test_left_open():
    """From 3.12 `wait_closed()` waits for every active connection, so a client
    the block never closed would stall teardown if close() didn't drop it."""
    async with serve(_one_row_session) as server:
        conn = await psycopg.AsyncConnection.connect(server.dsn(), autocommit=True)
    await conn.close()


async def test_serve_forwards_arguments_to_pgserver():
    async with serve(_one_row_session, server_version="9.6.0 (mimic)") as server:
        conn = await psycopg.AsyncConnection.connect(server.dsn(), autocommit=True)
        try:
            assert conn.info.parameter_status("server_version") == "9.6.0 (mimic)"
        finally:
            await conn.close()


def test_serve_in_thread_answers_a_blocking_client():
    with serve_in_thread(_one_row_session) as server:
        assert _blocking_query(server.dsn()) == [(42,)]


async def test_serve_in_thread_answers_a_blocking_client_from_an_async_test():
    """The deadlock the threading exists to avoid, as a test: a blocking client
    holds the thread that runs this test's event loop, so a server sharing that
    loop would never get to accept the connection, let alone answer. Its own
    loop in its own thread makes the client's blocking irrelevant."""
    assert asyncio.get_running_loop() is not None
    with serve_in_thread(_one_row_session) as server:
        assert _blocking_query(server.dsn()) == [(42,)]


def test_serve_in_thread_shuts_down_with_a_connection_still_open():
    """Reaching the end of this test *is* the assertion: stop() insists the
    thread joined, and awaiting the abandoned handler task instead of cancelling
    it would hang until the join timeout and leak the thread."""
    with serve_in_thread(_one_row_session) as server:
        never_closed = psycopg.Connection.connect(server.dsn(), autocommit=True)
        with never_closed.cursor() as cur:
            cur.execute("SELECT n FROM t")
            assert cur.fetchall() == [(42,)]
    never_closed.close()


def test_serve_in_thread_stops_listening_on_exit():
    with serve_in_thread(_one_row_session) as server:
        port = server.port
    with pytest.raises(psycopg.OperationalError):
        psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d", connect_timeout=2)


def test_server_thread_stop_is_idempotent():
    """A fixture that stops in a finally block shouldn't trip over a test that
    also stopped explicitly -- stop() leaves the loop closed behind it."""
    thread = ServerThread(PgServer(session_factory=_one_row_session))
    thread.start()
    thread.stop()
    thread.stop()


def test_server_thread_reports_a_failure_to_start(monkeypatch):
    """Without this the caller waits on a ready event nobody will set, and a
    server that couldn't bind looks like a hung test rather than an OSError."""

    async def refuse(*args, **kwargs):
        raise OSError("address already in use")

    monkeypatch.setattr(PgServer, "start_server", refuse)
    with pytest.raises(OSError, match="address already in use"):
        ServerThread(PgServer(session_factory=_one_row_session)).start()


def test_testing_module_imports_without_pytest():
    """pytest is a dev-only dependency: only the fixture plugin may need it."""
    code = textwrap.dedent(
        """
        import sys

        class BlockPytest:
            def find_spec(self, name, path=None, target=None):
                if name == "pytest" or name.startswith("pytest."):
                    raise ImportError("pytest is not installed")
                return None

        sys.meta_path.insert(0, BlockPytest())
        import pg_mimic.testing
        assert "pytest" not in sys.modules
        """
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_plugin_is_registered_as_an_entry_point():
    """How a user's suite gets the fixtures: installed metadata, not an import
    they have to write."""
    registered = {entry.value for entry in importlib.metadata.entry_points(group="pytest11")}
    assert "pg_mimic.pytest_plugin" in registered


def test_fixtures_serve_the_session_factory_they_are_given(pytester):
    pytester.makeconftest(
        """
        import pytest
        from pg_mimic import ResultColumn, Session

        class OneRow(Session):
            async def describe(self, sql, param_oids):
                return [ResultColumn.for_type("n", int)]

            async def query(self, sql, params):
                yield (42,)

        @pytest.fixture
        def pg_mimic_session_factory():
            return OneRow
        """
    )
    pytester.makepyfile(
        """
        import psycopg

        def test_dsn_connects(pg_mimic_dsn):
            with psycopg.connect(pg_mimic_dsn, autocommit=True) as conn:
                assert conn.execute("SELECT n FROM t").fetchall() == [(42,)]

        def test_server_reports_its_port(pg_mimic_server):
            assert pg_mimic_server.port > 0
        """
    )
    # The nested run's tests are sync, and pytest-asyncio warns about a config
    # option no ini file in a pytester tmpdir will ever set -- noise in this run's
    # output, blamed on this test.
    pytester.runpytest("-p", "no:asyncio").assert_outcomes(passed=2)


def test_fixtures_say_so_when_no_session_factory_was_provided(pytester):
    pytester.makepyfile(
        """
        def test_needs_a_session(pg_mimic_dsn):
            pass
        """
    )
    result = pytester.runpytest("-p", "no:asyncio")
    # An error, not a failure: it goes wrong in fixture setup, before the test body.
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*override the pg_mimic_session_factory fixture*"])
