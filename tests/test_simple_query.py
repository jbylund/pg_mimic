from __future__ import annotations

from pg_mimic import ResultColumn


def test_select_static_rows(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("a", str), ResultColumn.for_type("b", int)]
    mock_session.rows = [("x", 1), ("y", 2)]

    with conn.cursor() as cur:
        cur.execute("SELECT a, b FROM t")
        rows = cur.fetchall()
        assert rows == [("x", 1), ("y", 2)]
        assert cur.description[0].name == "a"
        assert cur.description[1].name == "b"


def test_no_rows(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("a", int)]
    mock_session.rows = []

    with conn.cursor() as cur:
        cur.execute("SELECT a FROM t WHERE 1=0")
        assert cur.fetchall() == []


def test_command_complete_no_columns(conn, mock_session):
    mock_session.columns = None
    mock_session.rows = [(), (), ()]  # 3 "affected rows", no result columns

    with conn.cursor() as cur:
        cur.execute("UPDATE t SET x = 1")
        assert cur.rowcount == 3


async def test_async_select(aconn, mock_session):
    mock_session.columns = [ResultColumn.for_type("n", int)]
    mock_session.rows = [(1,), (2,), (3,)]

    async with aconn.cursor() as cur:
        await cur.execute("SELECT n FROM t")
        rows = await cur.fetchall()
        assert rows == [(1,), (2,), (3,)]
