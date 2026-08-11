from __future__ import annotations

import psycopg
from psycopg.pq import TransactionStatus

from pg_mimic import ResultColumn, catalog


def test_multiple_static_selects_in_one_batch(conn, mock_session):
    # static_select gives each statement in the batch a distinct result, which is
    # what makes the result-set boundaries observable here.
    mock_session.middleware = catalog.DEFAULT_MIDDLEWARE + (catalog.static_select,)

    with conn.cursor() as cur:
        cur.execute("SELECT 1; SELECT 22;")
        assert cur.fetchall() == [(1,)]
        assert cur.nextset()
        assert cur.fetchall() == [(22,)]
        assert cur.nextset() is None


def test_begin_does_not_swallow_following_statement(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("a", int)]
    mock_session.rows = [(1,)]

    with conn.cursor() as cur:
        cur.execute("BEGIN; SELECT a FROM t;")
        assert cur.statusmessage == "BEGIN"
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        cur.nextset()
        assert cur.fetchall() == [(1,)]
        cur.execute("COMMIT")


def test_set_does_not_absorb_following_statement(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("a", int)]
    mock_session.rows = [(2,)]

    with conn.cursor() as cur:
        cur.execute("SET x = 1; SELECT a FROM t;")
        assert cur.statusmessage == "SET"
        cur.nextset()
        assert cur.fetchall() == [(2,)]

        # the SET value itself must be exactly "1", not "1; SELECT 2"
        cur.execute("SHOW x")
        assert cur.fetchone() == ("1",)


def test_batch_aborts_after_error(conn, mock_session):
    mock_session.error = RuntimeError("boom")

    with conn.cursor() as cur:
        try:
            cur.execute("SELECT a FROM broken; SELECT 1;")
        except psycopg.Error:
            pass

    # the second statement in the batch must never have run
    assert mock_session.queries == [("SELECT a FROM broken", [])]
