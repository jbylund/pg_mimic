"""One session per connection, and what happens when a factory ignores that.

`PgServer` calls `session_factory()` once per connection, so a session may hold
per-connection state -- `_connection` and `state` both do. A factory returning the
same object for every connection used to be accepted and quietly wrong: each
connect rebound those attributes, so every connection answered as whichever
attached last. See #84.
"""

from __future__ import annotations

import psycopg
import pytest

from pg_mimic.tables import TableSession
from pg_mimic.testing import serve_in_thread


def _dsn_as(server, user: str) -> str:
    return " ".join(part for part in server.dsn(user=user).split())


def test_a_second_connection_to_a_shared_session_is_refused_at_connect_time():
    """Refused while connecting, not on the first query.

    The claim is taken before authentication and before anything is written, which
    is what makes this an `OperationalError` from `connect()`. Taking it after the
    startup burst also refuses the connection, but only once `ReadyForQuery` has
    gone out -- so the client reports a successful connect and fails later, some
    distance from the cause.
    """
    shared = TableSession({"t": [{"id": 1}]})
    with serve_in_thread(lambda: shared) as server:
        first = psycopg.connect(server.dsn(), autocommit=True)
        try:
            with pytest.raises(psycopg.OperationalError) as excinfo:
                psycopg.connect(server.dsn(), autocommit=True)
            assert "session_factory" in str(excinfo.value), "the error should say how to fix it"
            assert first.execute("SELECT id FROM t").fetchone() == (1,), "the first connection is unharmed"
        finally:
            first.close()


def test_the_same_session_may_serve_the_next_connection():
    """The claim is released on teardown, so only an overlap is refused. A factory
    that hands back one object is fine for a suite that connects, finishes, and
    connects again -- which is most of them."""
    shared = TableSession({"t": [{"id": 1}]})
    with serve_in_thread(lambda: shared) as server:
        for _ in range(3):
            conn = psycopg.connect(server.dsn(), autocommit=True)
            assert conn.execute("SELECT id FROM t").fetchone() == (1,)
            conn.close()


def test_each_connection_answers_as_itself():
    """What the refusal is protecting. Before #84 both connections reported the
    user of whichever attached last, because `current_user` is resolved through the
    session's `_connection`."""
    with serve_in_thread(lambda: TableSession({"t": [{"id": 1}]})) as server:
        alice = psycopg.connect(_dsn_as(server, "alice"), autocommit=True)
        bob = psycopg.connect(_dsn_as(server, "bob"), autocommit=True)
        try:
            assert alice.execute("SELECT current_user").fetchone() == ("alice",)
            assert bob.execute("SELECT current_user").fetchone() == ("bob",)

            # session settings are per connection too, and were not
            alice.execute("SET search_path TO from_alice")
            assert bob.execute("SHOW search_path").fetchone() == ('"$user", public',)
        finally:
            alice.close()
            bob.close()


def test_spawn_keeps_the_subclass_it_was_called_on():
    """`MockSession.spawn()` named its own class, so a subclass spawned a base
    session and lost whatever it overrode -- silently, since the base method
    answers rather than raising. Found while converting the ServerThread call
    sites (#29): the conversion had to route around it."""
    from conftest import MockSession

    class Declaring(MockSession):
        async def prepare(self, sql, param_oids):
            return "overridden"

    clone = Declaring().spawn()
    assert isinstance(clone, Declaring)
    assert type(clone).prepare is Declaring.prepare


def test_spawn_reads_configuration_set_after_it_was_spawned():
    """The reason spawn proxies rather than copies: a test configures the fixture
    after the server is already up, and every connection has to see it."""
    from conftest import MockSession

    template = MockSession()
    clone = template.spawn()
    template.rows = [("late",)]
    assert clone.rows == [("late",)]
