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


# --- savepoints ----------------------------------------------------------------------
#
# Answered by the middleware, like the rest of transaction control: the transaction
# status reported in ReadyForQuery is connection state, so a session that answered
# SAVEPOINT couldn't set it. Drop `transaction_control` from `Session.middleware` to
# see the whole group -- BEGIN/COMMIT/ROLLBACK and savepoints -- in your own query().

# Each runs against a connection already in `BEGIN; SAVEPOINT sp1`. The tag is
# what real Postgres completes the statement with; the status is what it reports
# afterwards -- `T`, in every case, because none of these ends the transaction.
_SAVEPOINT_GRAMMAR = {
    "savepoint": {"sql": "SAVEPOINT sp2", "tag": "SAVEPOINT"},
    "savepoint_repeating_a_name": {"sql": "SAVEPOINT sp1", "tag": "SAVEPOINT"},
    "savepoint_quoted": {"sql": 'SAVEPOINT "Sp2"', "tag": "SAVEPOINT"},
    "release_savepoint": {"sql": "RELEASE SAVEPOINT sp1", "tag": "RELEASE"},
    "release_bare": {"sql": "RELEASE sp1", "tag": "RELEASE"},
    "rollback_to_savepoint": {"sql": "ROLLBACK TO SAVEPOINT sp1", "tag": "ROLLBACK"},
    "rollback_to_bare": {"sql": "ROLLBACK TO sp1", "tag": "ROLLBACK"},
    "rollback_transaction_to": {"sql": "ROLLBACK TRANSACTION TO SAVEPOINT sp1", "tag": "ROLLBACK"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_SAVEPOINT_GRAMMAR.values()))),
    argvalues=[[v for k, v in sorted(_SAVEPOINT_GRAMMAR[name].items())] for name in sorted(_SAVEPOINT_GRAMMAR)],
    ids=sorted(_SAVEPOINT_GRAMMAR),
)
def test_savepoint_grammar_keeps_the_transaction_open(conn, mock_session, sql, tag):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SAVEPOINT sp1")

        cur.execute(sql)
        assert cur.statusmessage == tag
        assert conn.info.transaction_status == TransactionStatus.INTRANS

        cur.execute("ROLLBACK")
        assert conn.info.transaction_status == TransactionStatus.IDLE
    assert mock_session.queries == [], f"{sql!r} reached the session"


def test_savepoint_outside_a_transaction_is_an_error(conn, mock_session):
    for sql in ("SAVEPOINT sp1", "RELEASE sp1", "ROLLBACK TO SAVEPOINT sp1"):
        with conn.cursor() as cur:
            with pytest.raises(psycopg.Error) as excinfo:
                cur.execute(sql)
            assert excinfo.value.sqlstate == "25P01", sql
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_unknown_savepoint_name_is_an_error(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        for sql in ("RELEASE nosuch", "ROLLBACK TO SAVEPOINT nosuch"):
            with pytest.raises(psycopg.Error) as excinfo:
                cur.execute(sql)
            assert excinfo.value.sqlstate == "3B001", sql
            # the failed statement aborted the transaction; get back to a usable one
            cur.execute("ROLLBACK")
            cur.execute("BEGIN")
        cur.execute("ROLLBACK")


def test_release_drops_the_savepoint_but_rollback_to_keeps_it(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SAVEPOINT sp1")

        # ROLLBACK TO leaves the savepoint in place -- you can roll back to it again
        cur.execute("ROLLBACK TO SAVEPOINT sp1")
        cur.execute("ROLLBACK TO SAVEPOINT sp1")

        cur.execute("RELEASE SAVEPOINT sp1")
        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("ROLLBACK TO SAVEPOINT sp1")
        assert excinfo.value.sqlstate == "3B001"
        cur.execute("ROLLBACK")


def test_release_drops_the_savepoints_nested_inside_it(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SAVEPOINT outer_sp")
        cur.execute("SAVEPOINT inner_sp")
        cur.execute("RELEASE SAVEPOINT outer_sp")

        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("ROLLBACK TO SAVEPOINT inner_sp")
        assert excinfo.value.sqlstate == "3B001"
        cur.execute("ROLLBACK")


def test_committing_forgets_the_savepoints(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SAVEPOINT sp1")
        cur.execute("COMMIT")

        cur.execute("BEGIN")
        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("ROLLBACK TO SAVEPOINT sp1")
        assert excinfo.value.sqlstate == "3B001"
        cur.execute("ROLLBACK")


def test_rollback_to_savepoint_clears_the_failed_transaction_state(conn, mock_session):
    """The whole point of a savepoint: the transaction survives the error. Before
    this, `ROLLBACK TO SAVEPOINT` matched the plain-ROLLBACK regex and reported the
    transaction as over."""
    mock_session.error = RuntimeError("boom")

    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SAVEPOINT sp1")
        with pytest.raises(psycopg.Error):
            cur.execute("SELECT * FROM broken")
        assert conn.info.transaction_status == TransactionStatus.INERROR

        cur.execute("ROLLBACK TO SAVEPOINT sp1")
        assert conn.info.transaction_status == TransactionStatus.INTRANS

        mock_session.error = None
        mock_session.columns = [ResultColumn.for_type("x", int)]
        mock_session.rows = [(1,)]
        cur.execute("SELECT x FROM t")
        assert cur.fetchall() == [(1,)]
        assert conn.info.transaction_status == TransactionStatus.INTRANS

        cur.execute("COMMIT")
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_psycopg_nested_transaction_round_trip(conn, mock_session):
    """psycopg's nested `transaction()` is savepoints all the way down -- it is what
    SQLAlchemy's `begin_nested()` compiles to as well."""
    mock_session.columns = [ResultColumn.for_type("x", int)]
    mock_session.rows = [(1,)]

    with conn.transaction():
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        with conn.transaction():
            assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]

        # an inner block that raises rolls back to its savepoint and no further
        with pytest.raises(ZeroDivisionError):
            with conn.transaction():
                raise ZeroDivisionError
        assert conn.info.transaction_status == TransactionStatus.INTRANS
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]

    assert conn.info.transaction_status == TransactionStatus.IDLE


def test_nested_transaction_recovers_from_a_server_error(conn, mock_session):
    """The failure this all exists for: an error inside the inner block leaves the
    outer transaction usable."""
    mock_session.error = RuntimeError("boom")

    with conn.transaction():
        with pytest.raises(psycopg.Error):
            with conn.transaction():
                conn.execute("SELECT * FROM broken")
        assert conn.info.transaction_status == TransactionStatus.INTRANS

        mock_session.error = None
        mock_session.columns = [ResultColumn.for_type("x", int)]
        mock_session.rows = [(1,)]
        assert conn.execute("SELECT x FROM t").fetchall() == [(1,)]

    assert conn.info.transaction_status == TransactionStatus.IDLE
