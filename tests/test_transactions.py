from __future__ import annotations

import psycopg
import pytest
from psycopg.pq import TransactionStatus

from pg_mimic import ResultColumn


def test_begin_commit(conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("a", int)]
    mock_session.rows = [(1,)]

    with conn.cursor() as cur:
        cur.execute("BEGIN")
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        cur.execute("SELECT a FROM t")
        assert cur.fetchall() == [(1,)]
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        cur.execute("COMMIT")
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_begin_rollback(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("ROLLBACK")
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_error_inside_transaction_fails_until_rollback(conn, mock_session):
    mock_session.error = RuntimeError("boom")

    with conn.cursor() as cur:
        cur.execute("BEGIN")
        with pytest.raises(psycopg.Error):
            cur.execute("SELECT * FROM broken")
        assert conn.info.transaction_status == TransactionStatus.INERROR

        # any further statement (other than ROLLBACK) must also fail while aborted
        with pytest.raises(psycopg.errors.InFailedSqlTransaction):
            cur.execute("SELECT 1")

        cur.execute("ROLLBACK")
        assert conn.info.transaction_status == TransactionStatus.IDLE

    # back to normal after rollback
    mock_session.error = None
    mock_session.columns = [ResultColumn.for_type("x", int)]
    mock_session.rows = [(1,)]
    with conn.cursor() as cur:
        cur.execute("SELECT x FROM t")
        assert cur.fetchall() == [(1,)]
