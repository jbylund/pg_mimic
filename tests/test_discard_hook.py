"""Session.discard(): DISCARD reaches the session already parsed.

The other half of #35. `DISCARD ALL` is what pgbouncer sends between pooled
clients, so every pooled deployment sends it, and a session that has its own state
to drop had no way to hear about it -- the statement was answered entirely by the
connection.

The three narrow forms matter more here than they look: pg_mimic has no plan
cache, no sequences and no temp tables, so it does nothing for them itself. A
session may have all three.
"""

from __future__ import annotations

import psycopg
import pytest

from pg_mimic import TableSession
from pg_mimic.errors import FEATURE_NOT_SUPPORTED, PgError
from pg_mimic.testing import serve_in_thread


class _Recording(TableSession):
    def __init__(self):
        super().__init__({"t": [{"a": 1}]})
        self.seen: list[tuple] = []

    async def discard(self, kind):
        self.seen.append(("discard", kind))

    async def set_parameter(self, name, raw_value, parsed_value):
        self.seen.append(("set", name, raw_value))


class _Refusing(TableSession):
    def __init__(self):
        super().__init__({"t": [{"a": 1}]})

    async def discard(self, kind):
        raise PgError(FEATURE_NOT_SUPPORTED, "this pool does not recycle connections")


@pytest.fixture
def recorded():
    sessions = []

    def factory():
        session = _Recording()
        sessions.append(session)
        return session

    with serve_in_thread(factory) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection:
            yield connection, sessions


_KINDS = {
    "ALL": {"expected": "ALL", "sql": "DISCARD ALL"},
    "PLANS": {"expected": "PLANS", "sql": "DISCARD PLANS"},
    "SEQUENCES": {"expected": "SEQUENCES", "sql": "DISCARD SEQUENCES"},
    "TEMP": {"expected": "TEMP", "sql": "DISCARD TEMP"},
    "TEMPORARY is the same thing as TEMP": {"expected": "TEMP", "sql": "DISCARD TEMPORARY"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_KINDS.values()))),
    argvalues=[[v for k, v in sorted(_KINDS[name].items())] for name in sorted(_KINDS)],
    ids=sorted(_KINDS),
)
def test_every_kind_reaches_the_session(recorded, expected, sql):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute(sql)
    assert ("discard", expected) in sessions[0].seen


def test_the_narrow_forms_reach_it_even_though_the_connection_does_nothing_for_them(recorded):
    """pg_mimic has no plan cache, no sequences and no temp tables. A session may."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("DISCARD PLANS")
        cur.execute("DISCARD SEQUENCES")
        cur.execute("DISCARD TEMP")
    assert sessions[0].seen == [("discard", "PLANS"), ("discard", "SEQUENCES"), ("discard", "TEMP")]


def test_discard_all_also_arrives_as_one_reset_per_setting(recorded):
    """The statement first, then what it does to each setting still held."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET work_mem = '32MB'")
        cur.execute("SET row_security = 'off'")
        sessions[0].seen.clear()
        cur.execute("DISCARD ALL")
    assert sessions[0].seen[0] == ("discard", "ALL")
    assert sorted(sessions[0].seen[1:]) == [("set", "row_security", None), ("set", "work_mem", None)]


def test_a_refusal_leaves_the_connection_as_it_was():
    """Told before anything is cleared, so refusing is atomic without the connection
    having to snapshot itself -- the opposite order from set_parameter, and for that
    reason."""
    with serve_in_thread(_Refusing) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection:
            with connection.cursor() as cur:
                cur.execute("SET work_mem = '32MB'")
            with connection.cursor() as cur:
                with pytest.raises(psycopg.errors.FeatureNotSupported) as excinfo:
                    cur.execute("DISCARD ALL")
                assert "this pool does not recycle connections" in str(excinfo.value)
            with connection.cursor() as cur:
                cur.execute("SHOW work_mem")
                assert cur.fetchone()[0] == "32MB"


def test_discard_all_is_still_refused_inside_a_transaction(recorded):
    """25001 outranks the hook: Postgres refuses the statement before it means
    anything, so the session is never told about one that cannot run."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("BEGIN")
        with pytest.raises(psycopg.errors.ActiveSqlTransaction):
            cur.execute("DISCARD ALL")
        cur.execute("ROLLBACK")
    assert sessions[0].seen == []


def test_the_narrow_forms_are_allowed_inside_a_transaction(recorded):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("DISCARD PLANS")
        cur.execute("COMMIT")
    assert sessions[0].seen == [("discard", "PLANS")]


def test_a_session_that_does_not_override_it_is_unaffected():
    with serve_in_thread(lambda: TableSession({"t": [{"a": 1}]})) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection, connection.cursor() as cur:
            cur.execute("SET work_mem = '32MB'")
            cur.execute("DISCARD ALL")
            cur.execute("SHOW work_mem")
            assert cur.fetchone()[0] == "4MB"
