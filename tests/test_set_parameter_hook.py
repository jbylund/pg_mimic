"""Session.set_parameter(): SET/RESET reach the session already parsed.

#23 closed the SET/RESET/DISCARD grammar holes in the middleware but left one seam
open: a statement the middleware declined to answer arrived at the session as raw
SQL, where an author had to re-parse it to discover it was connection boilerplate
at all. This is the hook that closes it (#35).

The connection stays the source of truth for SHOW -- a bare BaseSession bypasses
the middleware entirely and the connection still owes ParameterStatus on the wire,
so the session is *told* rather than asked.
"""

from __future__ import annotations

import psycopg
import pytest

from pg_mimic import Session, TableSession
from pg_mimic.errors import INVALID_PARAMETER_VALUE, PgError
from pg_mimic.testing import serve_in_thread


class _Recording(TableSession):
    def __init__(self):
        super().__init__({"t": [{"a": 1}]})
        self.told: list[tuple[str, str | None]] = []

    async def set_parameter(self, name, value):
        self.told.append((name, value))


class _Refusing(TableSession):
    def __init__(self):
        super().__init__({"t": [{"a": 1}]})

    async def set_parameter(self, name, value):
        if value == "nope":
            raise PgError(INVALID_PARAMETER_VALUE, f"no such tenant: {value}")


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


def test_set_reaches_the_session(recorded):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET work_mem = '32MB'")
    assert sessions[0].told == [("work_mem", "32MB")]


def test_reset_arrives_as_a_none_value(recorded):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET work_mem = '32MB'")
        cur.execute("RESET work_mem")
    assert sessions[0].told == [("work_mem", "32MB"), ("work_mem", None)]


def test_set_config_goes_through_the_same_funnel(recorded):
    """The reason the funnel exists: refusing `SET x` while allowing
    `set_config('x', ...)` would be a hole in the same wall (#77)."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', '5s', false)")
    assert sessions[0].told == [("statement_timeout", "5s")]


def test_reset_all_is_told_one_name_at_a_time(recorded):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET work_mem = '32MB'")
        cur.execute("SET statement_timeout = '5s'")
        cur.execute("RESET ALL")
    assert sorted(sessions[0].told[2:]) == [("statement_timeout", None), ("work_mem", None)]


def test_the_value_is_the_text_that_was_written(recorded):
    """Not the parsed value: a session forwarding this to a real backend wants to
    send what was asked for. What pg_mimic keeps is parsed, in session_vars."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET row_security = 'tr'")
    assert sessions[0].told == [("row_security", "tr")]
    assert sessions[0].state.session_vars["row_security"] is True


def test_a_session_may_refuse_a_setting():
    with serve_in_thread(_Refusing) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection:
            with connection.cursor() as cur:
                cur.execute("SELECT set_config('app.tenant', 'acme', false)")
            with connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InvalidParameterValue) as excinfo:
                    cur.execute("SELECT set_config('app.tenant', 'nope', false)")
                assert "no such tenant: nope" in str(excinfo.value)
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('app.tenant')")
                assert cur.fetchone()[0] == "acme"


def test_a_refusal_leaves_a_name_that_was_never_set_unknown():
    """The undo puts back the absence too, not just the previous value -- otherwise a
    refused SET leaves the name readable-but-blank, which is a different thing to a
    client probing `current_setting(x, true) IS NULL`."""
    with serve_in_thread(_Refusing) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection:
            with connection.cursor() as cur:
                with pytest.raises(psycopg.errors.InvalidParameterValue):
                    cur.execute("SELECT set_config('app.fresh', 'nope', false)")
            with connection.cursor() as cur:
                cur.execute("SELECT current_setting('app.fresh', true) IS NULL")
                assert cur.fetchone()[0] is True


def test_a_session_that_does_not_override_it_is_unaffected():
    """The default is a no-op, so every session that predates the hook keeps working."""
    with serve_in_thread(lambda: TableSession({"t": [{"a": 1}]})) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection, connection.cursor() as cur:
            cur.execute("SET work_mem = '32MB'")
            cur.execute("SHOW work_mem")
            assert cur.fetchone()[0] == "32MB"


def test_the_hook_is_a_no_op_on_the_base_class():
    assert Session.set_parameter.__doc__ is not None
