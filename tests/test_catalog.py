from __future__ import annotations

import psycopg
from conftest import ServerThread

from pg_mimic import PgServer, ResultColumn, Session, catalog


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
    mock_session.middleware = catalog.DEFAULT_MIDDLEWARE + (catalog.static_select,)

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


def test_information_schema_tables(conn, mock_session):
    async def schema():
        return {"users": {"id": "integer", "name": "text"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables ORDER BY table_name")
        assert cur.fetchall() == [("users",)]
    assert mock_session.queries == []


def test_information_schema_columns(conn, mock_session):
    async def schema():
        return {"users": {"id": "integer", "name": "text"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'users' ORDER BY ordinal_position"
        )
        assert cur.fetchall() == [("id", "integer"), ("name", "text")]


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
        return catalog.StaticStatement("ping", [ResultColumn.for_type("pong", str)], [("pong",)])

    mock_session.middleware = (answer_ping,) + catalog.DEFAULT_MIDDLEWARE

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
        return catalog.StaticStatement(ctx.sql, [ResultColumn.for_type("v", str)], [("mine",)])

    mock_session.middleware = (hijack_show,) + catalog.DEFAULT_MIDDLEWARE

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
