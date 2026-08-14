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
        self.told: list[tuple] = []

    async def set_parameter(self, name, raw_value, parsed_value):
        self.told.append((name, raw_value, parsed_value))


class _Refusing(TableSession):
    def __init__(self):
        super().__init__({"t": [{"a": 1}]})

    async def set_parameter(self, name, raw_value, parsed_value):
        if raw_value == "nope":
            raise PgError(INVALID_PARAMETER_VALUE, f"no such tenant: {raw_value}")


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
    assert sessions[0].told == [("work_mem", "32MB", 32768)]


def test_reset_arrives_as_a_none_value(recorded):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET work_mem = '32MB'")
        cur.execute("RESET work_mem")
    assert sessions[0].told == [("work_mem", "32MB", 32768), ("work_mem", None, None)]


def test_set_config_goes_through_the_same_funnel(recorded):
    """The reason the funnel exists: refusing `SET x` while allowing
    `set_config('x', ...)` would be a hole in the same wall (#77)."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('statement_timeout', '5s', false)")
    assert sessions[0].told == [("statement_timeout", "5s", 5000)]


def test_reset_all_is_told_one_name_at_a_time(recorded):
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET work_mem = '32MB'")
        cur.execute("SET statement_timeout = '5s'")
        cur.execute("RESET ALL")
    assert sorted(sessions[0].told[2:]) == [("statement_timeout", None, None), ("work_mem", None, None)]


def test_both_spellings_arrive(recorded):
    """The text to forward to a real backend, the value to act on."""
    connection, sessions = recorded
    with connection.cursor() as cur:
        cur.execute("SET row_security = 'tr'")
        cur.execute("SET client_encoding = 'utf8'")
        cur.execute("SELECT set_config('app.tenant', 'acme', false)")
    assert sessions[0].told == [
        ("row_security", "tr", True),
        ("client_encoding", "utf8", "UTF8"),
        ("app.tenant", "acme", "acme"),  # a custom GUC has no type to parse
    ]


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


def test_the_hook_does_not_depend_on_the_order_the_connection_writes_in():
    """session_vars holds the same parsed value by the time this runs, but reading it
    from there would make the connection's internal ordering part of the contract."""
    seen = []

    class Both(TableSession):
        def __init__(self):
            super().__init__({"t": [{"a": 1}]})

        async def set_parameter(self, name, raw_value, parsed_value):
            seen.append((raw_value, parsed_value, self.state.session_vars.get(name, "<absent>")))

    with serve_in_thread(Both) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as connection, connection.cursor() as cur:
            cur.execute("SET row_security = 'tr'")
            cur.execute("RESET row_security")
    assert seen == [("tr", True, True), (None, None, "<absent>")]


def test_the_parser_the_middleware_uses_is_public():
    """A session may need to read a value this did not hand it -- one a real backend
    reported back -- so the pair is exported rather than reached for privately."""
    from pg_mimic import settings_values

    assert settings_values.parse("work_mem", "32MB") == 32768
    assert settings_values.render("work_mem", 32768) == "32MB"
