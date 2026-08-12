"""Connection boilerplate around session settings: the SET/RESET/DISCARD/DEALLOCATE
grammar, and the ParameterStatus messages a reported setting owes the client.

The point of the grammar table is that none of these reach the session. A
statement the middleware doesn't recognise falls through to `query()`, which is
not where a session author expects `DISCARD ALL` (what pgbouncer sends between
pooled clients) or `SET TIME ZONE` to arrive.

Savepoints live in test_transactions.py, alongside the rest of the transaction
state they belong to.
"""

from __future__ import annotations

import psycopg
import pytest
from conftest import ServerThread
from psycopg.pq import TransactionStatus
from wire import connect_and_get_backend_key, make_query, read_message

from pg_mimic import PgServer

# sql -> the command tag real Postgres completes it with (src/include/tcop/cmdtaglist.h).
_ANSWERED_OUTSIDE_A_TRANSACTION = {
    "set_value": {"sql": "SET extra_float_digits = 3", "tag": "SET"},
    "set_to_value": {"sql": "SET extra_float_digits TO 3", "tag": "SET"},
    "set_session_scoped": {"sql": "SET SESSION timezone TO 'UTC'", "tag": "SET"},
    "set_local_scoped": {"sql": "SET LOCAL search_path TO myschema", "tag": "SET"},
    "set_to_default": {"sql": "SET search_path TO DEFAULT", "tag": "SET"},
    "set_time_zone": {"sql": "SET TIME ZONE 'UTC'", "tag": "SET"},
    "set_time_zone_local": {"sql": "SET TIME ZONE LOCAL", "tag": "SET"},
    "set_schema": {"sql": "SET SCHEMA 'myschema'", "tag": "SET"},
    "set_characteristics_read_only": {"sql": "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY", "tag": "SET"},
    "set_characteristics_isolation": {
        "sql": "SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "tag": "SET",
    },
    "reset_one": {"sql": "RESET search_path", "tag": "RESET"},
    "reset_all": {"sql": "RESET ALL", "tag": "RESET"},
    "discard_all": {"sql": "DISCARD ALL", "tag": "DISCARD ALL"},
    "discard_plans": {"sql": "DISCARD PLANS", "tag": "DISCARD PLANS"},
    "discard_sequences": {"sql": "DISCARD SEQUENCES", "tag": "DISCARD SEQUENCES"},
    "discard_temp": {"sql": "DISCARD TEMP", "tag": "DISCARD TEMP"},
    "discard_temporary": {"sql": "DISCARD TEMPORARY", "tag": "DISCARD TEMP"},
    "deallocate_all": {"sql": "DEALLOCATE ALL", "tag": "DEALLOCATE ALL"},
    "show_one": {"sql": "SHOW search_path", "tag": "SHOW"},
    "show_time_zone": {"sql": "SHOW TIME ZONE", "tag": "SHOW"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_ANSWERED_OUTSIDE_A_TRANSACTION.values()))),
    argvalues=[
        [v for k, v in sorted(_ANSWERED_OUTSIDE_A_TRANSACTION[name].items())] for name in sorted(_ANSWERED_OUTSIDE_A_TRANSACTION)
    ],
    ids=sorted(_ANSWERED_OUTSIDE_A_TRANSACTION),
)
def test_connection_boilerplate_is_answered_not_forwarded(conn, mock_session, sql, tag):
    with conn.cursor() as cur:
        cur.execute(sql)
        assert cur.statusmessage == tag
    assert conn.info.transaction_status == TransactionStatus.IDLE
    assert mock_session.queries == [], f"{sql!r} reached the session"


def test_set_time_zone_is_the_timezone_guc(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET TIME ZONE 'America/New_York'")
        cur.execute("SHOW timezone")
        assert cur.fetchone() == ("America/New_York",)
        cur.execute("SHOW TIME ZONE")
        assert cur.fetchone() == ("America/New_York",)

        # LOCAL and DEFAULT both mean "back to the built-in default"
        cur.execute("SET TIME ZONE LOCAL")
        cur.execute("SHOW timezone")
        assert cur.fetchone() == ("UTC",)


def test_set_schema_is_search_path(conn, mock_session):
    """The SQL standard defines SET SCHEMA as SET search_path TO <schema>."""
    with conn.cursor() as cur:
        cur.execute("SET SCHEMA 'myschema'")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ("myschema",)


def test_reset_restores_the_default(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema")
        cur.execute("RESET search_path")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ('"$user", public',)


def test_reset_all_clears_every_setting(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema")
        cur.execute("SET extra_float_digits = 3")
        cur.execute("RESET ALL")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ('"$user", public',)
        cur.execute("SHOW extra_float_digits")
        assert cur.fetchone() == ("",)


def test_set_session_characteristics_sets_the_default_transaction_gucs(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET SESSION CHARACTERISTICS AS TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        cur.execute("SHOW default_transaction_isolation")
        assert cur.fetchone() == ("repeatable read",)
        cur.execute("SHOW default_transaction_read_only")
        assert cur.fetchone() == ("on",)


def test_discard_all_clears_session_vars_and_prepared_statements(conn, mock_session):
    """What pgbouncer sends when it hands a server connection to the next client."""
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema")
        cur.execute("DISCARD ALL")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ('"$user", public',)
    assert mock_session.queries == []


def test_discard_all_is_refused_inside_a_transaction(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("BEGIN")
        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("DISCARD ALL")
        assert excinfo.value.sqlstate == "25001"
        cur.execute("ROLLBACK")

        # the narrower forms are allowed in a transaction block, as in real Postgres
        cur.execute("BEGIN")
        cur.execute("DISCARD PLANS")
        cur.execute("ROLLBACK")


def test_deallocate_drops_a_prepared_statement(mock_session):
    """Protocol-level prepared statements share Postgres's SQL-level namespace, so
    DEALLOCATE has to reach the ones the Connection is holding."""
    server = PgServer(session_factory=lambda: mock_session)
    thread = ServerThread(server)
    port = thread.start()
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=test dbname=test", autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("DEALLOCATE ALL")
                assert cur.statusmessage == "DEALLOCATE ALL"
                with pytest.raises(psycopg.Error) as excinfo:
                    cur.execute("DEALLOCATE nosuchstatement")
                assert excinfo.value.sqlstate == "26000"
    finally:
        thread.stop()


def test_custom_gucs_reach_the_session(conn, mock_session):
    """A dotted name is a custom GUC, and `SET app.tenant_id = 5` before a query is
    the row-level-security/multi-tenancy pattern. Answering it here would swallow
    it: the session would have nowhere to see it, and a session fronting a real
    backend could not forward it, leaving the backend's RLS reading the wrong
    tenant. It stays with the session until `Session.set_parameter()` exists (#35)."""
    with conn.cursor() as cur:
        cur.execute("SET app.tenant_id = 5")
        cur.execute("SET LOCAL app.tenant_id = 5")
        cur.execute("RESET app.tenant_id")
        cur.execute('SET "app.tenant_id" = 5')
    assert [sql for sql, _params in mock_session.queries] == [
        "SET app.tenant_id = 5",
        "SET LOCAL app.tenant_id = 5",
        "RESET app.tenant_id",
        'SET "app.tenant_id" = 5',
    ]


def test_a_quoted_setting_name_is_the_same_setting(conn, mock_session):
    """GUC names are matched case-insensitively however they are spelled, so unlike
    an identifier a quoted one still folds."""
    with conn.cursor() as cur:
        cur.execute('SET "SEARCH_PATH" TO myschema')
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ("myschema",)


def test_set_role_and_session_authorization_pass_through(conn, mock_session):
    """A documented decision, not an oversight: pg_mimic has no role catalog to
    validate against and no privilege model to apply, so these go to the session --
    the only place that can decide what a role means here."""
    with conn.cursor() as cur:
        cur.execute("SET ROLE admin")
        cur.execute("SET ROLE TO admin")
        cur.execute("SET SESSION AUTHORIZATION alice")
        cur.execute("RESET ROLE")
    assert [sql for sql, _params in mock_session.queries] == [
        "SET ROLE admin",
        "SET ROLE TO admin",
        "SET SESSION AUTHORIZATION alice",
        "RESET ROLE",
    ]


# --- ParameterStatus ----------------------------------------------------------------


def test_client_observes_a_client_encoding_change(conn, mock_session):
    """psycopg reads its connection encoding out of the cached ParameterStatus, so
    without one it goes on using the value from the startup burst forever."""
    assert conn.info.parameter_status("client_encoding") == "UTF8"

    with conn.cursor() as cur:
        cur.execute("SET client_encoding TO 'LATIN1'")
    assert conn.info.parameter_status("client_encoding") == "LATIN1"


def test_reported_settings_are_reported_again_when_reset(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema")
        assert conn.info.parameter_status("search_path") == "myschema"

        cur.execute("RESET ALL")
        assert conn.info.parameter_status("search_path") == '"$user", public'


def test_set_config_reports_too(conn, mock_session):
    """Same setting, other spelling -- both go through the one apply-a-setting path."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('application_name', 'via_set_config', false)")
        assert cur.fetchone() == ("via_set_config",)
    assert conn.info.parameter_status("application_name") == "via_set_config"


def test_unreported_settings_get_no_parameter_status(conn, mock_session):
    with conn.cursor() as cur:
        cur.execute("SET extra_float_digits = 3")
    assert conn.info.parameter_status("extra_float_digits") is None


def test_application_name_from_the_startup_packet_is_echoed(dsn, mock_session):
    """DEFAULT_PARAMETER_STATUS can't carry this one: it is per-connection, out of
    the client's own startup packet."""
    with psycopg.Connection.connect(dsn + " application_name=myapp", autocommit=True) as conn:
        assert conn.info.parameter_status("application_name") == "myapp"
        with conn.cursor() as cur:
            cur.execute("SHOW application_name")
            assert cur.fetchone() == ("myapp",)


def test_application_name_is_reported_even_when_unset(conn):
    assert conn.info.parameter_status("application_name") == ""


async def test_parameter_status_arrives_before_ready_for_query(mock_session):
    """Real Postgres reports changed GUCs at the end of the command, immediately
    before ReadyForQuery -- not spliced into the command's own messages."""
    server = PgServer(session_factory=lambda: mock_session)
    thread = ServerThread(server)
    port = thread.start()
    try:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(port)
        writer.write(make_query("SET client_encoding TO 'LATIN1'"))
        await writer.drain()

        tags = []
        while True:
            tag, payload = await read_message(reader)
            tags.append(tag)
            if tag == b"S":
                assert payload == b"client_encoding\x00LATIN1\x00"
            if tag == b"Z":
                break
        assert tags == [b"C", b"S", b"Z"]  # CommandComplete, ParameterStatus, ReadyForQuery

        writer.close()
    finally:
        thread.stop()
