from __future__ import annotations

from pg_mimic import ResultColumn


def test_parameterized_query_receives_real_params(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("echo", str)]
    mock_session.rows = [("ok",)]

    with conn.cursor() as cur:
        cur.execute("SELECT %s, %s", (42, "hello"))
        assert cur.fetchall() == [("ok",)]

    # The session handler must have received the real bound parameter values,
    # not a textually-interpolated SQL string (mysql-mimic's shortcut).
    sql, params = mock_session.queries[-1]
    assert params == ["42", "hello"]
    assert "42" not in sql and "hello" not in sql


def test_prepared_statement_reused_with_different_params(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("v", int)]
    mock_session.rows = [(1,)]

    with conn.cursor() as cur:
        for value in (1, 2, 3):
            cur.execute("SELECT %s", (value,), prepare=True)
            assert cur.fetchall() == [(1,)]

    params_seen = [params for _sql, params in mock_session.queries]
    assert params_seen == [["1"], ["2"], ["3"]]


def test_multiple_execute_calls_stream_the_same_portal(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("n", int)]
    mock_session.rows = [(i,) for i in range(10)]

    with conn.cursor() as cur:
        cur.execute("SELECT n FROM t")
        rows = cur.fetchall()
        assert rows == [(i,) for i in range(10)]

    # query() must only have been invoked once for this one portal, even
    # though psycopg may issue multiple Execute calls to fetch all 10 rows.
    assert len(mock_session.queries) == 1
