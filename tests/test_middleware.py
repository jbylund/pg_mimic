"""The middleware chain: which statements it answers, which it passes through,
and how a session author reshapes it.

The information_schema link is exercised in test_catalog.py, alongside the
emulation it delegates to.
"""

from __future__ import annotations

import psycopg
import pytest
from conftest import ServerThread

from pg_mimic import PgServer, ResultColumn, Session, StaticStatement, middleware
from pg_mimic.state import SessionState


class ForgetfulSession(Session):
    """A Session whose init() override forgets to call super().init() --
    must not silently disable the middleware chain (regression test for a
    real bug: Session._connection only got set via Session.init() itself, so
    an override that skipped super() left it None, and Session.prepare()
    fell back to CallbackStatement unconditionally)."""

    async def init(self, connection):
        pass  # deliberately not calling super().init(connection)

    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("sql", str)]

    async def query(self, sql, params):
        yield (sql,)


def test_middleware_works_even_if_session_init_skips_super():
    server = PgServer(session_factory=ForgetfulSession)
    thread = ServerThread(server)
    port = thread.start()
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=test dbname=test", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                # the connection's real database, not the session's own query()
                # echoing the SQL text back -- which is what a dead chain would give
                assert cur.fetchall() == [("test",)]
    finally:
        thread.stop()


def test_plain_select_reaches_the_session_by_default(conn, mock_session):
    """`SELECT 1` is a query with whatever meaning the session gives it, so the
    default chain passes it through rather than evaluating it. Only statements
    that ask about connection state get answered on the session's behalf."""
    mock_session.columns = [ResultColumn.for_type("n", int)]
    mock_session.rows = [(99,)]

    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS one, 'hi' AS greeting")
        assert cur.fetchall() == [(99,)]
    assert mock_session.queries == [("SELECT 1 AS one, 'hi' AS greeting", [])]


def test_static_select_middleware_can_be_opted_into(conn, mock_session):
    """The old evaluate-every-table-less-SELECT behaviour, available on request."""
    mock_session.middleware = middleware.DEFAULT_MIDDLEWARE + (middleware.static_select,)

    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS one, 'hi' AS greeting")
        assert cur.fetchall() == [(1, "hi")]
    assert mock_session.queries == []


def test_middleware_can_be_disabled_entirely(conn, mock_session):
    """An empty chain means the session sees every statement, boilerplate included."""
    mock_session.middleware = ()
    mock_session.columns = [ResultColumn.for_type("sql", str)]
    mock_session.rows = [("whatever",)]

    with conn.cursor() as cur:
        cur.execute("SELECT current_database()")
        assert cur.fetchall() == [("whatever",)]
    assert mock_session.queries == [("SELECT current_database()", [])]


def test_select_version_and_current_database(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SELECT version()")
        (version,) = cur.fetchone()
        assert "pg_mimic" in version

        cur.execute("SELECT current_database()")
        assert cur.fetchone() == ("test",)


def test_set_and_show(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ("myschema",)


def test_show_server_version(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        (value,) = cur.fetchone()
        assert "pg_mimic" in value


def test_select_star_reaches_the_session(conn, mock_session):
    """`select *` was previously evaluated to a bare (1,) by static-select and
    never reached the session -- surprising, and the reason static_select is no
    longer in the default chain."""
    mock_session.columns = [ResultColumn.for_type("a", int)]
    mock_session.rows = [(7,)]

    with conn.cursor() as cur:
        cur.execute("select *")
        assert cur.fetchall() == [(7,)]
    assert mock_session.queries == [("select *", [])]


def test_custom_middleware_can_be_added(conn, mock_session):
    """A middleware is just an async callable returning a Statement or None."""

    async def answer_ping(ctx):
        if ctx.sql.lower() != "ping":
            return None
        return StaticStatement("ping", [ResultColumn.for_type("pong", str)], [("pong",)])

    mock_session.middleware = (answer_ping,) + middleware.DEFAULT_MIDDLEWARE

    with conn.cursor() as cur:
        cur.execute("ping")
        assert cur.fetchall() == [("pong",)]
    assert mock_session.queries == []


def test_middleware_order_decides_who_answers(conn, mock_session):
    """First link to return a Statement wins, so putting your own ahead of the
    defaults lets you override built-in behaviour rather than only extend it."""

    async def hijack_show(ctx):
        if not ctx.sql.lower().startswith("show "):
            return None
        return StaticStatement(ctx.sql, [ResultColumn.for_type("v", str)], [("mine",)])

    mock_session.middleware = (hijack_show,) + middleware.DEFAULT_MIDDLEWARE

    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        assert cur.fetchall() == [("mine",)]


def test_session_functions_still_answered_by_default(conn, mock_session):
    """The boilerplate half of the split: these only the connection can answer,
    so they must not fall through to the session."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_user")
        assert cur.fetchone() == ("test",)
    assert mock_session.queries == []


def test_set_config_sets_and_returns_the_value(conn, mock_session):
    """asyncpg opens with `SELECT current_setting('jit'), set_config('jit','off',false)`;
    before set_config was handled that fell through to the session, which answered
    with the wrong shape and left the client retrying."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('jit') AS cur, set_config('jit', 'off', false) AS new")
        assert cur.fetchone() == ("off", "off")

        cur.execute("SELECT set_config('search_path', 'myschema', false)")
        assert cur.fetchone() == ("myschema",)
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ("myschema",)
    assert mock_session.queries == []


def test_set_config_null_resets_the_setting(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('search_path', 'myschema', false)")
        cur.execute("SELECT set_config('search_path', NULL, false)")
        assert cur.fetchone() == ("",)
        cur.execute("SHOW search_path")
        # back to the built-in default rather than the value that was set
        assert cur.fetchone() == ('"$user", public',)


def test_current_setting_of_an_unknown_setting_raises(conn, mock_session):
    """A name nothing has ever set doesn't exist, and real Postgres says so rather
    than reading as empty (#32). Still answered here rather than falling through:
    a fall-through is what made a client retry against a session that can't answer
    connection questions."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UndefinedObject) as excinfo:
            cur.execute("SELECT current_setting('some.unknown.guc')")
        assert excinfo.value.sqlstate == "42704"
        assert 'unrecognized configuration parameter "some.unknown.guc"' in str(excinfo.value)
    assert mock_session.queries == []


def test_current_setting_missing_ok_is_null_for_an_unknown_setting(conn, mock_session):
    """`current_setting('app.tenant', true) IS NULL` is how row-level-security code
    asks "was this ever set?". Answering it with the empty string sends the caller
    down the wrong branch, silently -- the bug in #32."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.tenant', true) IS NULL")
        assert cur.fetchone() == (True,)

        cur.execute("SELECT current_setting('app.tenant', true)")
        assert cur.fetchone() == (None,)

        # A quoted flag is the same flag: the argument is boolean, so Postgres
        # coerces the literal.
        cur.execute("SELECT current_setting('app.tenant', 't') IS NULL")
        assert cur.fetchone() == (True,)
    assert mock_session.queries == []


def test_current_setting_missing_ok_false_still_raises(conn, mock_session):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UndefinedObject) as excinfo:
            cur.execute("SELECT current_setting('app.tenant', false)")
        assert excinfo.value.sqlstate == "42704"
    assert mock_session.queries == []


def test_current_setting_with_a_null_flag_is_null(conn, mock_session):
    """`current_setting(text, bool)` is strict, so a NULL flag makes the call NULL
    without the setting being looked at -- true even of a setting that exists."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('search_path', NULL) IS NULL")
        assert cur.fetchone() == (True,)
    assert mock_session.queries == []


def test_current_setting_of_a_setting_that_was_set_is_never_null(conn, mock_session):
    """The other half of the probe: setting a name is what makes it exist, and it
    goes on existing after a RESET blanks it -- measured against PostgreSQL 18,
    where a custom GUC stays known for the life of the session."""
    with conn.cursor() as cur:
        cur.execute("SET mytenant TO 'acme'")
        cur.execute("SELECT current_setting('mytenant', true)")
        assert cur.fetchone() == ("acme",)

        cur.execute("RESET mytenant")
        cur.execute("SELECT current_setting('mytenant', true) IS NULL, current_setting('mytenant')")
        assert cur.fetchone() == (False, "")
    assert mock_session.queries == []


def test_a_dotted_custom_guc_is_unknown_here_because_the_session_owns_it(conn, mock_session):
    """The boundary this change runs into: a dotted name's SET goes to the session
    (test_custom_gucs_reach_the_session), so the connection genuinely has not seen
    it and says so. Reading it back as empty is what #32 is about -- an RLS probe
    told "set, to nothing" takes the wrong branch in silence, where NULL and 42704
    both fail where someone will look. Once `Session.set_parameter()` lands (#35)
    the session can register the name and these become answers."""
    with conn.cursor() as cur:
        cur.execute("SET app.tenant_id = 5")
        with pytest.raises(psycopg.errors.UndefinedObject):
            cur.execute("SHOW app.tenant_id")
        cur.execute("SELECT current_setting('app.tenant_id', true) IS NULL")
        assert cur.fetchone() == (True,)


def test_current_setting_with_an_unreadable_flag_falls_through(conn, mock_session):
    """A flag that isn't a literal isn't ours to evaluate -- the same rule
    set_config() follows for a value it can't read."""
    mock_session.columns = [ResultColumn.for_type("v", str)]
    mock_session.rows = [("from the session",)]
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.tenant', 'maybe')")
        assert cur.fetchall() == [("from the session",)]
    assert [sql for sql, _params in mock_session.queries] == ["SELECT current_setting('app.tenant', 'maybe')"]


async def test_set_config_applies_at_execute_not_parse():
    """set_config writes session state, so its effect is deferred to Execute like a
    SET statement's -- Parse must not change anything."""

    class FakeConnection:
        def __init__(self):
            # The real thing: SessionState needs no socket, so a fake connection
            # carries the same state object a live one does.
            self.state = SessionState(username="u", database="d")
            self.database = "d"
            self.username = "u"
            self.pid = 1
            self.reported = {}

        def report_parameter(self, name, value):
            self.reported[name] = value

    connection = FakeConnection()
    ctx = middleware.MiddlewareContext(connection, "SELECT set_config('a', 'b', false)", [])

    statement = await middleware.session_functions(ctx)
    assert statement is not None
    assert connection.state.session_vars == {}, "Parse must not have applied it yet"

    portal = statement.bind([])
    assert connection.state.session_vars == {}, "Bind must not have applied it either"

    await portal.execute(0)
    assert connection.state.session_vars == {"a": "b"}


def test_a_session_reads_the_state_the_middleware_owns():
    """The point of the shared state: SET is answered by the middleware, and the
    session can still see what it decided without a hook and without reaching
    into the Connection."""

    class Peeking(Session):
        seen: str | None = None

        async def describe(self, sql, param_oids):
            return [ResultColumn.for_type("search_path", str)]

        async def query(self, sql, params):
            yield (self.state.session_vars.get("search_path", ""),)

    server = PgServer(session_factory=Peeking)
    thread = ServerThread(server)
    port = thread.start()
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=test dbname=test", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SET search_path TO myschema")  # answered by the middleware
                cur.execute("SELECT whatever")  # reaches the session, which reads it back
                assert cur.fetchall() == [("myschema",)]
    finally:
        thread.stop()


def test_state_is_populated_even_if_session_init_skips_super():
    """Assigned by the framework rather than delivered through init(), for the
    same reason _connection is -- see ForgetfulSession above."""

    class Forgetful(Session):
        async def init(self, connection):
            pass

        async def describe(self, sql, param_oids):
            return [ResultColumn.for_type("who", str)]

        async def query(self, sql, params):
            yield (f"{self.state.username}/{self.state.database}",)

    server = PgServer(session_factory=Forgetful)
    thread = ServerThread(server)
    port = thread.start()
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=alice dbname=shop", autocommit=True) as conn:
            assert conn.execute("SELECT anything").fetchall() == [("alice/shop",)]
    finally:
        thread.stop()
