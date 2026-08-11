"""information_schema emulation, driven by whatever Session.schema() declares."""

from __future__ import annotations


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
