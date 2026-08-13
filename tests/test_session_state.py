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
from psycopg.pq import TransactionStatus
from wire import SYNC, connect_and_get_backend_key, make_parse, make_query, read_message

from pg_mimic import middleware, settings_catalog
from pg_mimic.testing import serve_in_thread

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


def test_reset_all_restores_every_default(conn, mock_session):
    """RESET ALL restores defaults; it does not blank the settings.

    `extra_float_digits` asserted "" here until pg_mimic knew what its default was,
    which was only ever a description of the gap -- real Postgres answers 1. See #32.
    """
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema")
        cur.execute("SET extra_float_digits = 3")
        cur.execute("RESET ALL")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ('"$user", public',)
        cur.execute("SHOW extra_float_digits")
        assert cur.fetchone() == ("1",)


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
    with serve_in_thread(mock_session.spawn) as server:
        with psycopg.Connection.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("DEALLOCATE ALL")
                assert cur.statusmessage == "DEALLOCATE ALL"
                with pytest.raises(psycopg.Error) as excinfo:
                    cur.execute("DEALLOCATE nosuchstatement")
                assert excinfo.value.sqlstate == "26000"


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


def test_show_of_a_setting_nothing_has_ever_set_raises(conn, mock_session):
    """42704, the same as real Postgres, rather than the empty string that made an
    unknown setting indistinguishable from a blank one (#32). Answered here, not
    forwarded: a SHOW is connection boilerplate however it ends."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UndefinedObject) as excinfo:
            cur.execute("SHOW no_such_setting")
        assert excinfo.value.sqlstate == "42704"
        assert 'unrecognized configuration parameter "no_such_setting"' in str(excinfo.value)
    assert mock_session.queries == []


def test_a_setting_stays_known_once_set(conn, mock_session):
    """Naming a setting is what makes it exist, and nothing that drops the *value*
    unmakes it -- checked against PostgreSQL 18.4 for RESET ALL and DISCARD ALL,
    both of which leave a custom GUC reading as the empty string.

    This once used `SET mytenant`, on a docstring claiming the same measurement.
    18.4 answers that with 42704 (guc.c:1169, `assignable_custom_variable_name`):
    a placeholder GUC needs a *qualified* name, so the behaviour was only ever
    real for a dotted one. Set here through set_config(), since a dotted SET goes
    to the session (#35), and blanked with set_config's NULL, since `RESET
    app.mytenant` goes there too."""
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('app.mytenant', 'acme', false)")
        for forget in ("SELECT set_config('app.mytenant', NULL, false)", "RESET ALL", "DISCARD ALL"):
            cur.execute(forget)
            cur.execute("SHOW app.mytenant")
            assert cur.fetchone() == ("",), f"unknown again after {forget}"


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


def test_a_parameterized_set_is_a_syntax_error(conn, mock_session):
    """SET's value is part of the statement, not something Bind supplies, and
    Postgres answers `SET x TO $1` with a syntax error. Accepted, it would report a
    ParameterStatus whose value is the literal text "$1" -- and psycopg takes
    client_encoding at its word, so every later row fails to decode and the
    connection is unusable with nothing to blame it on."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("SET client_encoding TO %s", ("LATIN1",))
        assert excinfo.value.sqlstate == "42601"
    assert conn.info.parameter_status("client_encoding") == "UTF8"
    # and the connection is still usable, which is the whole point
    assert conn.execute("SHOW client_encoding").fetchone() == ("UTF8",)


def test_session_state_statements_survive_a_multi_statement_batch(conn, mock_session):
    """A batch is split by slicing the original text, not by re-rendering it through
    sqlglot -- which writes `SAVEPOINT a` back as `SAVEPOINT AS a` and `DISCARD ALL`
    as `DISCARD AS ALL`, so none of these were recognised in a batch."""
    with conn.cursor() as cur:
        cur.execute("SET search_path TO myschema; BEGIN; SAVEPOINT a; ROLLBACK TO SAVEPOINT a; RELEASE a; COMMIT")
        cur.execute("SHOW search_path")
        assert cur.fetchone() == ("myschema",)

        cur.execute("DISCARD ALL; SHOW search_path")
        assert cur.nextset()  # past DISCARD ALL's own (empty) result
        assert cur.fetchone() == ('"$user", public',)
    assert mock_session.queries == []


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
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
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


# --- SQL-level prepared statements ----------------------------------------------------
#
# One namespace with two entrances, as in Postgres: SQL PREPARE and the protocol's
# Parse write to the same registry, and either can DEALLOCATE what the other made.
# Every expectation below was checked against a real PostgreSQL 18.


def _tables_conn(server):
    return psycopg.Connection.connect(server.dsn(user="test", dbname="test"), autocommit=True)


@pytest.fixture
def rows_conn():
    from pg_mimic import TableSession
    from pg_mimic.testing import serve_in_thread

    tables = {"t": [{"a": 1}, {"a": 2}, {"a": 3}]}
    with serve_in_thread(lambda: TableSession(tables)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            yield conn


def test_prepare_and_execute_round_trip(rows_conn):
    """The command tag is the prepared statement's, not EXECUTE's: Postgres
    answers `EXECUTE p` of a SELECT with `SELECT n`."""
    with rows_conn.cursor() as cur:
        cur.execute("PREPARE p AS SELECT a FROM t ORDER BY a")
        assert cur.statusmessage == "PREPARE"

        cur.execute("EXECUTE p")
        assert cur.fetchall() == [(1,), (2,), (3,)]
        assert cur.statusmessage == "SELECT 3"


def test_execute_passes_its_arguments(rows_conn):
    with rows_conn.cursor() as cur:
        cur.execute("PREPARE byval (bigint) AS SELECT a FROM t WHERE a = $1")
        cur.execute("EXECUTE byval (2)")
        assert cur.fetchall() == [(2,)]
        assert cur.statusmessage == "SELECT 1"


def test_deallocate_drops_a_sql_prepared_statement(rows_conn):
    """The bug this whole change is for: PREPARE was accepted and its DEALLOCATE
    refused, because the two lived in different places."""
    with rows_conn.cursor() as cur:
        cur.execute("PREPARE gone AS SELECT a FROM t")
        cur.execute("DEALLOCATE gone")
        assert cur.statusmessage == "DEALLOCATE"

        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("EXECUTE gone")
        assert excinfo.value.sqlstate == "26000"


async def test_sql_can_deallocate_a_protocol_level_statement(mock_session):
    """The namespace is shared in both directions -- verified against PostgreSQL 18,
    where SQL can DEALLOCATE a statement the protocol's Parse created.

    Driven over the wire so the statement gets a name of our choosing, rather than
    depending on how psycopg happens to name its own.
    """
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_parse("SELECT 1", statement_name="mine") + SYNC)
        await writer.drain()
        await _drain_until_ready(reader)

        writer.write(make_query('DEALLOCATE "mine"'))
        await writer.drain()
        tags = await _drain_until_ready(reader)
        assert b"E" not in tags, "DEALLOCATE of a Parse-created statement must not error"

        # ... and it is gone: a second one is 26000.
        writer.write(make_query('DEALLOCATE "mine"'))
        await writer.drain()
        assert b"E" in await _drain_until_ready(reader)
        writer.close()


async def _drain_until_ready(reader) -> list[bytes]:
    tags = []
    while True:
        tag, _payload = await read_message(reader)
        tags.append(tag)
        if tag == b"Z":
            return tags


def test_execute_in_the_extended_protocol_describes_the_prepared_query(rows_conn):
    """A client may Parse("EXECUTE p") and Describe before binding, so the rewrite
    has to happen when the statement is resolved, not when it runs."""
    with rows_conn.cursor() as cur:
        cur.execute("PREPARE p AS SELECT a FROM t ORDER BY a")
        cur.execute("EXECUTE p", prepare=True)  # forces Parse/Describe/Bind/Execute
        assert [column.name for column in cur.description] == ["a"]
        assert cur.fetchall() == [(1,), (2,), (3,)]


def test_discard_all_drops_sql_prepared_statements(rows_conn):
    with rows_conn.cursor() as cur:
        cur.execute("PREPARE q AS SELECT a FROM t")
        cur.execute("DISCARD ALL")
        with pytest.raises(psycopg.Error) as excinfo:
            cur.execute("EXECUTE q")
        assert excinfo.value.sqlstate == "26000"


def test_a_session_can_take_prepare_back(mock_session):
    """Dropping the link hands PREPARE and EXECUTE to the session untouched, for a
    session fronting a backend that has its own prepared statements."""
    from pg_mimic import middleware as mw

    mock_session.middleware = tuple(link for link in mw.DEFAULT_MIDDLEWARE if link is not mw.prepared_statements)
    with serve_in_thread(mock_session.spawn) as server:
        with _tables_conn(server) as conn:
            with conn.cursor() as cur:
                cur.execute("PREPARE p AS SELECT 1")
                cur.execute("EXECUTE p")
    assert [sql for sql, _params in mock_session.queries] == ["PREPARE p AS SELECT 1", "EXECUTE p"]


# --- the parameters a real server is born knowing (#32) -------------------------------


def test_an_unmodelled_but_real_guc_reads_its_postgres_default(conn, mock_session):
    """`SHOW work_mem` answered "" before pg_mimic carried the parameter list, then
    42704 once unknown names started erroring. Neither is what a server says: the
    value is a property of PostgreSQL, and pg_settings.json now supplies it."""
    with conn.cursor() as cur:
        for setting, expected in (("work_mem", "4MB"), ("shared_buffers", "128MB"), ("max_connections", "100")):
            cur.execute(f"SHOW {setting}")
            assert cur.fetchone() == (expected,), setting


def test_a_name_that_is_not_a_parameter_still_raises(conn, mock_session):
    """The catalog is what keeps the previous test from swallowing this one: a
    parameter pg_mimic does not model and a parameter that does not exist are only
    different questions if something knows the difference."""
    import psycopg
    import pytest

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UndefinedObject):
            cur.execute("SHOW never.set_at_all")


def test_setting_a_real_guc_then_resetting_returns_the_default_not_a_blank(conn, mock_session):
    """The ordering rule in _setting_value: a catalogued default outranks the
    known-but-blank state, because RESET means different things to the two kinds of
    name. Measured against PostgreSQL 18 -- work_mem reads 4MB, app.x reads ""."""
    with conn.cursor() as cur:
        cur.execute("SET work_mem = '8MB'")
        cur.execute("SHOW work_mem")
        assert cur.fetchone() == ("8MB",)
        cur.execute("RESET work_mem")
        cur.execute("SHOW work_mem")
        assert cur.fetchone() == ("4MB",)


def test_current_setting_missing_ok_is_still_null_for_a_non_parameter(conn, mock_session):
    """The row-level-security probe #32 exists for, still answered NULL -- the
    catalog must not turn every unknown name into a value."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_setting('app.tenant', true)")
        assert cur.fetchone() == (None,)


def test_version_and_port_describe_pg_mimic_not_the_generating_server(conn, mock_session):
    """The catalogue is generated from whichever server it was pointed at, so left to
    it `server_version_num` would report that server's release while `server_version`
    and the startup ParameterStatus report pg_mimic's -- and a client gating a feature
    on the number acts on a version it is not talking to. Same for `port`, where the
    catalogued 5432 is a fact about PostgreSQL's default rather than this listener."""
    with conn.cursor() as cur:
        cur.execute("SHOW server_version")
        version = cur.fetchone()[0]
        cur.execute("SHOW server_version_num")
        assert cur.fetchone() == ("160000",), f"disagrees with server_version {version!r}"
        assert version.startswith("16.0")

        cur.execute("SHOW port")
        assert cur.fetchone() == (str(conn.info.port),)


def test_show_all_lists_the_settings_with_descriptions(conn, mock_session):
    """`SHOW ALL` is the whole table, not a parameter named "all". It read as the
    latter before the catalogue existed -- one bogus empty row -- and became a 42704
    once unknown names started erroring, which is worse for the tools that run it on
    connect."""
    with conn.cursor() as cur:
        cur.execute("SHOW ALL")
        rows = cur.fetchall()
        assert [description.name for description in cur.description] == ["name", "setting", "description"]
        assert len(rows) > 300

        settings = {name: (value, description) for name, value, description in rows}
        assert settings["work_mem"] == ("4MB", "Sets the maximum memory to be used for query workspaces.")

        # what the connection has set shows through, not the default underneath it
        cur.execute("SET search_path TO myschema")
        cur.execute("SHOW ALL")
        assert dict((name, value) for name, value, _ in cur.fetchall())["search_path"] == "myschema"


def test_a_dotted_guc_does_not_become_known_by_setting_it(conn, mock_session):
    """Pins what the README now says rather than what it used to. A dotted name goes
    to the session (#35), so pg_mimic never sees the write -- and the row-level-security
    probe stays NULL afterwards. The docs claimed the opposite."""
    with conn.cursor() as cur:
        cur.execute("SET app.tenant = 'acme'")
        cur.execute("SELECT current_setting('app.tenant', true)")
        assert cur.fetchone() == (None,)


# --- which parameters a session may actually set (#77) --------------------------------

# One parameter per refusable context, with the message PostgreSQL 18.4 refuses it
# with. All five are 55P02 and the wording differs by context -- guc.c picks it in
# set_config_with_handle() -- so this is a table rather than one assertion: a client
# that matches on message text can tell "restart the server" from "not now".
_REFUSED_BY_CONTEXT = {
    "postmaster": {
        "sql": "SET shared_buffers = '64MB'",
        "message": 'parameter "shared_buffers" cannot be changed without restarting the server',
    },
    "sighup": {
        "sql": "SET autovacuum_naptime = 30",
        "message": 'parameter "autovacuum_naptime" cannot be changed now',
    },
    "internal": {
        "sql": "SET server_version = 'x'",
        "message": 'parameter "server_version" cannot be changed',
    },
    "backend": {
        "sql": "SET post_auth_delay = 1",
        "message": 'parameter "post_auth_delay" cannot be set after connection start',
    },
    "superuser_backend": {
        "sql": "SET log_connections = 'all'",
        "message": 'parameter "log_connections" cannot be set after connection start',
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_REFUSED_BY_CONTEXT.values()))),
    argvalues=[[v for k, v in sorted(_REFUSED_BY_CONTEXT[name].items())] for name in sorted(_REFUSED_BY_CONTEXT)],
    ids=sorted(_REFUSED_BY_CONTEXT),
)
def test_a_parameter_a_session_cannot_change_is_refused(conn, mock_session, sql, message):
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CantChangeRuntimeParam) as excinfo:
            cur.execute(sql)
        assert excinfo.value.sqlstate == "55P02"
        assert message in str(excinfo.value)
    assert mock_session.queries == []


def test_resetting_a_parameter_a_session_cannot_change_is_refused_too(conn, mock_session):
    """`RESET x` is `SET x TO DEFAULT`, and 18.4 refuses it identically -- the
    parameter is no more changeable for being changed back."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CantChangeRuntimeParam) as excinfo:
            cur.execute("RESET shared_buffers")
        assert excinfo.value.sqlstate == "55P02"
        assert 'parameter "shared_buffers" cannot be changed without restarting the server' in str(excinfo.value)
    assert mock_session.queries == []


def test_setting_a_name_that_is_not_a_parameter_is_refused(conn, mock_session):
    """The half of #77 that costs the most: every undotted name used to be settable,
    which made `SET` unable to tell a typo from a parameter. 18.4 refuses it at
    guc.c:1169, because an undotted name has no placeholder to create."""
    with conn.cursor() as cur:
        for sql in ("SET not_a_real_guc = 1", "RESET not_a_real_guc"):
            with pytest.raises(psycopg.errors.UndefinedObject) as excinfo:
                cur.execute(sql)
            assert excinfo.value.sqlstate == "42704", sql
            assert 'unrecognized configuration parameter "not_a_real_guc"' in str(excinfo.value), sql
    assert mock_session.queries == []


def test_a_superuser_context_parameter_is_still_accepted(conn, mock_session):
    """A decision, not a reading of the manual (#77). pg_mimic reports
    `is_superuser = off`, so read literally these 48 owe 42501 -- but there is no
    privilege model behind that answer, and clients set `log_*` for their own
    diagnostics against something that keeps no log. Accepting is the cheaper lie."""
    with conn.cursor() as cur:
        cur.execute("SET log_statement = 'all'")
        cur.execute("SHOW log_statement")
        assert cur.fetchone() == ("all",)
    assert mock_session.queries == []


def test_set_config_cannot_set_what_set_refuses(conn, mock_session):
    """The hole the check is placed to close: set_config() names its parameter as a
    string rather than as syntax, so it never passes the SET grammar. Both routes
    meet in _apply_set_config, and 18.4 refuses both the same way."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CantChangeRuntimeParam):
            cur.execute("SELECT set_config('shared_buffers', '1GB', false)")
        with pytest.raises(psycopg.errors.UndefinedObject):
            cur.execute("SELECT set_config('not_a_real_guc', 'x', false)")
    assert mock_session.queries == []


def test_set_local_outside_a_transaction_warns_and_then_still_refuses(conn, mock_session):
    """Both, in that order, measured on 18.4: CheckTransactionBlock warns before the
    GUC machinery has looked the parameter up, and refusing it is still the answer.
    Warning without the error would leave `SET LOCAL shared_buffers` reading as
    accepted."""
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CantChangeRuntimeParam):
            cur.execute("SET LOCAL shared_buffers = '1GB'")
        # the settable one still only warns
        cur.execute("SET LOCAL work_mem = '9MB'")
    assert mock_session.queries == []


def test_reset_all_is_not_refused_by_any_of_them(conn, mock_session):
    """RESET ALL drops what this connection set rather than assigning anything, so
    it has no name to refuse -- and 18.4 takes it without complaint even though 199
    parameters could not be set individually."""
    with conn.cursor() as cur:
        cur.execute("SET work_mem = '8MB'")
        cur.execute("RESET ALL")
        cur.execute("SHOW work_mem")
        assert cur.fetchone() == ("4MB",)
    assert mock_session.queries == []


# What psycopg, asyncpg, pg8000 and SQLAlchemy set on or just after connect. #77 is
# the one change in its cluster that can only *remove* working behaviour, so the
# thing worth pinning is not which parameters are refused but which are still
# accepted: every name here is `user` context, which is why refusing the other 199
# costs no client anything. A parameter arriving in this list with a refusable
# context is a regression in the accept set, whatever the tests below say.
_SENT_BY_REAL_CLIENTS = (
    "application_name",
    "bytea_output",
    "client_encoding",
    "client_min_messages",
    "datestyle",
    "default_transaction_isolation",
    "default_transaction_read_only",
    "extra_float_digits",
    "idle_in_transaction_session_timeout",
    "intervalstyle",
    "jit",
    "lock_timeout",
    "row_security",
    "search_path",
    "standard_conforming_strings",
    "statement_timeout",
    "synchronous_commit",
    "timezone",
    "work_mem",
)


def test_every_parameter_a_client_sends_on_connect_is_still_settable():
    refused = {name: settings_catalog.context(name) for name in _SENT_BY_REAL_CLIENTS}
    refused = {name: context for name, context in refused.items() if context not in middleware._SETTABLE_CONTEXTS}
    assert refused == {}


def test_every_context_in_the_catalogue_is_one_of_the_two_lists():
    """The generator can grow a context the split has never seen -- PostgreSQL could
    add one, or the catalogue could be regenerated against a different release. Then
    `_check_settable` would fall back to a bare "cannot be changed" for it, quietly.
    This is what makes that loud instead."""
    known = middleware._SETTABLE_CONTEXTS | middleware._UNSETTABLE_CONTEXTS.keys()
    unaccounted = {entry["context"] for entry in settings_catalog.SETTINGS.values()} - known
    assert unaccounted == set()
