from __future__ import annotations

import psycopg

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


# --- a prepared statement must not answer with a stale value -------------------------
#
# StaticStatement used to bake its rows when the statement was built, which is right
# for the simple protocol (it resolves per execution) and wrong for a prepared one:
# parsed once, executed many times, and the setting moves underneath. See #63.


def _prepared_conn(dsn):
    # prepare_threshold=1: Parse/Bind/Execute from the first run, which is what
    # psycopg does of its own accord after five.
    return psycopg.Connection.connect(dsn, autocommit=True, prepare_threshold=1)


def test_a_prepared_show_reads_the_setting_each_time(dsn, mock_session):
    with _prepared_conn(dsn) as conn:
        with conn.cursor() as cur:
            for value in ("first", "second", "third"):
                cur.execute(f"SET search_path TO {value}")
                cur.execute("SHOW search_path")
                assert cur.fetchone() == (value,), f"stale after SET {value}"


def test_a_prepared_current_setting_reads_the_setting_each_time(dsn, mock_session):
    with _prepared_conn(dsn) as conn:
        with conn.cursor() as cur:
            for value in ("a", "b", "c"):
                cur.execute(f"SET search_path TO {value}")
                cur.execute("SELECT current_setting('search_path')")
                assert cur.fetchone() == (value,)


def test_a_prepared_current_setting_notices_a_setting_appearing(dsn, mock_session):
    """#63 through the missing_ok branch (#32): a statement prepared while the
    setting does not exist yet must not go on answering NULL once it does. Twice up
    front, because psycopg prepares on the second execution.

    A *dotted* name, because since #77 it is the only kind that can appear: every
    undotted name either is in the catalogue from the start or can never be set at
    all. Named through set_config() rather than SET for the same reason -- a dotted
    SET belongs to the session (#35)."""
    with _prepared_conn(dsn) as conn:
        with conn.cursor() as cur:
            for _ in range(2):
                cur.execute("SELECT current_setting('app.later', true)")
                assert cur.fetchone() == (None,)

            cur.execute("SELECT set_config('app.later', 'now', false)")
            cur.execute("SELECT current_setting('app.later', true)")
            assert cur.fetchone() == ("now",)


def test_a_prepared_show_follows_a_rollback(dsn, mock_session):
    """The two halves together: settings are transactional (#45) and a prepared
    SHOW reports the current value (#63). Either bug alone hides the other."""
    with _prepared_conn(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO before")
            # Twice up front, so SHOW is already prepared before the transaction --
            # psycopg's threshold prepares on the *second* execution, and a check
            # that parses fresh each time proves nothing about a cached statement.
            cur.execute("SHOW search_path")
            cur.execute("SHOW search_path")
            assert cur.fetchone() == ("before",)

            cur.execute("BEGIN")
            cur.execute("SET search_path TO during")
            cur.execute("SHOW search_path")
            assert cur.fetchone() == ("during",)
            cur.execute("ROLLBACK")
            cur.execute("SHOW search_path")
            assert cur.fetchone() == ("before",)


def test_a_prepared_constant_session_function_still_answers(dsn, mock_session):
    """The lazy path must not break the ones that cannot change."""
    with _prepared_conn(dsn) as conn:
        with conn.cursor() as cur:
            for _ in range(3):
                cur.execute("SELECT current_database()")
                assert cur.fetchone() == ("test",)
