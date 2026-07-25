from __future__ import annotations

import psycopg
from conftest import ServerThread

from pg_mimic import PgServer, ResultColumn, Session


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
                cur.execute("SELECT 1")
                # a real evaluated 1, not the session's own query() echoing back "SELECT 1" as text
                assert cur.fetchall() == [(1,)]
    finally:
        thread.stop()


def test_select_static_literal(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 AS one, 'hi' AS greeting")
        assert cur.fetchall() == [(1, "hi")]
    # the static SELECT never reached the session's own query() handler
    assert mock_session.queries == []


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
