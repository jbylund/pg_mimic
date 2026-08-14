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


def test_a_redundant_begin_does_not_forget_the_savepoints(conn, mock_session):
    """A BEGIN inside an open transaction block starts nothing -- Postgres warns
    "there is already a transaction in progress" and carries on with the first, so
    the savepoints taken in it are still live. Clearing them on every BEGIN made
    the second one unreachable."""
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SAVEPOINT sp1")
        cur.execute("BEGIN")
        cur.execute("ROLLBACK TO SAVEPOINT sp1")
        cur.execute("RELEASE sp1")
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


# --- settings are transactional ------------------------------------------------------
#
# Every expectation below was checked against a real PostgreSQL 18, including the
# two that are not obvious: a SET LOCAL followed by a plain SET reads back the
# *session* value (so this is not a local-shadows-session lookup), and a SET made
# inside a savepoint is undone by ROLLBACK TO just as a local one is.


def _search_path(cur) -> str:
    cur.execute("SHOW search_path")
    return cur.fetchone()[0]


@pytest.fixture
def guc_conn(dsn, mock_session):
    # prepare_threshold=1: every statement here goes through Parse/Bind/Execute, so
    # these check the transactional rules on the *prepared* path too. Before #63 a
    # prepared SHOW answered with the value it had when it was parsed, which would
    # have masked the lot.
    with psycopg.Connection.connect(dsn, autocommit=True, prepare_threshold=1) as conn:
        conn.execute("SET search_path TO base")
        yield conn


_GUC_TRANSACTION_CASES = {
    "set_rolled_back": {"steps": ["BEGIN", "SET search_path TO x", "ROLLBACK"], "expected": "base"},
    "set_committed": {"steps": ["BEGIN", "SET search_path TO x", "COMMIT"], "expected": "x"},
    "local_reverts_at_commit": {"steps": ["BEGIN", "SET LOCAL search_path TO x", "COMMIT"], "expected": "base"},
    "local_reverts_at_rollback": {"steps": ["BEGIN", "SET LOCAL search_path TO x", "ROLLBACK"], "expected": "base"},
    # The case that rules out "local shadows session": the later SET wins, and survives.
    "local_then_session": {
        "steps": ["BEGIN", "SET LOCAL search_path TO l", "SET search_path TO s", "COMMIT"],
        "expected": "s",
    },
    "session_then_local": {
        "steps": ["BEGIN", "SET search_path TO s", "SET LOCAL search_path TO l", "COMMIT"],
        "expected": "s",
    },
    "set_inside_a_savepoint_is_rolled_back": {
        "steps": ["BEGIN", "SAVEPOINT sp", "SET search_path TO x", "ROLLBACK TO sp", "COMMIT"],
        "expected": "base",
    },
    "local_reverts_to_the_savepoints_value": {
        "steps": ["BEGIN", "SET LOCAL search_path TO a", "SAVEPOINT sp", "SET LOCAL search_path TO b", "ROLLBACK TO sp"],
        "expected": "a",
    },
    "release_keeps_what_the_scope_set": {
        "steps": ["BEGIN", "SAVEPOINT sp", "SET search_path TO x", "RELEASE sp", "COMMIT"],
        "expected": "x",
    },
    "nested_savepoints": {
        "steps": [
            "BEGIN",
            "SET search_path TO one",
            "SAVEPOINT a",
            "SET search_path TO two",
            "SAVEPOINT b",
            "SET search_path TO three",
            "ROLLBACK TO a",
            "COMMIT",
        ],
        "expected": "one",
    },
    # Postgres warns "SET LOCAL can only be used in transaction blocks" and does
    # nothing. We have no NoticeResponse to warn with yet (#24); the nothing is here.
    "local_outside_a_transaction": {"steps": ["SET LOCAL search_path TO nope"], "expected": "base"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_GUC_TRANSACTION_CASES.values()))),
    argvalues=[[v for k, v in sorted(_GUC_TRANSACTION_CASES[name].items())] for name in sorted(_GUC_TRANSACTION_CASES)],
    ids=sorted(_GUC_TRANSACTION_CASES),
)
def test_settings_follow_the_transaction(guc_conn, expected, steps):
    with guc_conn.cursor() as cur:
        for step in steps:
            cur.execute(step)
        assert _search_path(cur) == expected


def test_a_rolled_back_set_leaves_the_setting_known_but_blank(guc_conn):
    """The value is transactional; the setting's *existence* is not. Verified
    against PostgreSQL 18: a custom GUC first set inside a transaction that rolls
    back reads back as the empty string afterwards, not as an unrecognised
    parameter -- so `current_setting(name, true)` is not NULL either.

    A dotted name through set_config(), because that is what 18.4 accepts and what
    since #77 still reaches this state -- `SET mytenant` is 42704 there, undotted
    names having no placeholder to create."""
    with guc_conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("SELECT set_config('app.mytenant', 'acme', false)")
        cur.execute("ROLLBACK")

        cur.execute("SHOW app.mytenant")
        assert cur.fetchone() == ("",)
        cur.execute("SELECT current_setting('app.mytenant', true) IS NULL")
        assert cur.fetchone() == (False,)


def test_a_rolled_back_setting_is_reported_to_the_client(dsn, mock_session):
    """A reported GUC that reverts owes the client a ParameterStatus just as one that
    changes does -- otherwise the client goes on acting on the value the rolled-back
    transaction set.

    DateStyle rather than client_encoding as the vehicle: that one may only ever be
    UTF8 here, so it cannot change and would prove nothing (#116)."""
    with psycopg.Connection.connect(dsn, autocommit=True, prepare_threshold=1) as conn:
        assert conn.info.parameter_status("DateStyle") == "ISO, MDY"
        conn.execute("BEGIN")
        conn.execute("SET DateStyle TO 'ISO, DMY'")
        assert conn.info.parameter_status("DateStyle") == "ISO, DMY"
        conn.execute("ROLLBACK")
        assert conn.info.parameter_status("DateStyle") == "ISO, MDY"


def test_prepared_statements_are_not_transactional(guc_conn):
    """Settings are; prepared statements are not. Verified against PostgreSQL 18,
    where a PREPARE inside a rolled-back transaction survives it."""
    with guc_conn.cursor() as cur:
        cur.execute("BEGIN")
        cur.execute("PREPARE survivor AS SELECT 1")
        cur.execute("ROLLBACK")
        cur.execute("DEALLOCATE survivor")  # still there: no error
