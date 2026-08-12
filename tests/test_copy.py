"""The COPY sub-protocol, from both ends.

The unit tests cover the two things that have no second source of truth -- the
option grammar and the text/CSV codecs -- and everything else is driven through a
real client, because copy mode is where a plausible-looking implementation fails
in ways no unit test sees: a client blocks forever waiting for a CopyInResponse,
or keeps sending data the server has stopped reading.

The wire-level tests (see wire.py) drive the cases no client library will produce
on request: a CopyFail, a stray message in the middle of copy-in, and a cancel
arriving while the server is parked waiting for the next CopyData.
"""

from __future__ import annotations

import asyncio
import io
import shutil
import subprocess

import asyncpg
import psycopg
import pytest
import pytest_asyncio
from conftest import ServerThread
from wire import (
    COPY_DONE,
    FLUSH,
    SYNC,
    TARGET_PORTAL,
    connect_and_get_backend_key,
    make_bind,
    make_cancel_request,
    make_copy_data,
    make_copy_fail,
    make_describe,
    make_execute,
    make_parse,
    make_query,
    parse_error_fields,
    read_message,
)

from pg_mimic import PgError, PgServer, ResultColumn, Session
from pg_mimic.copy import CopyEncoder, CopyInDecoder, parse_copy
from pg_mimic.errors import UNDEFINED_TABLE
from pg_mimic.testing import serve_in_thread

psql_required = pytest.mark.skipif(shutil.which("psql") is None, reason="psql is not installed")

SCHEMA = {"people": {"id": "integer", "name": "text"}}


class CopySession(Session):
    """A session that implements both copy hooks, and records what it saw.

    `loaded` is what copy_in() was handed -- tuples of decoded text, exactly the
    shape a session author is promised -- so a test can assert on the decoding
    without knowing anything about CopyData framing.
    """

    def __init__(self):
        self.loaded: list[tuple] = []
        self.out_rows: list[tuple] = []
        self.rows: list[tuple] = []
        self.columns: list[ResultColumn] | None = None
        self.error: Exception | None = None
        self.stop_after: int | None = None
        self.reported_count: int | None = None

    async def schema(self):
        return SCHEMA

    async def describe(self, sql, param_oids):
        return self.columns

    async def query(self, sql, params):
        if self.error is not None:
            raise self.error
        for row in self.rows:
            yield row

    async def copy_in(self, sql, rows):
        async for row in rows:
            self.loaded.append(row)
            if self.stop_after is not None and len(self.loaded) >= self.stop_after:
                break
        return self.reported_count

    async def copy_out(self, sql):
        if self.error is not None:
            raise self.error
        for row in self.out_rows:
            yield row


class ReadOnlySession(Session):
    """Implements neither hook -- the default state of every existing session."""

    async def describe(self, sql, param_oids):
        return None

    async def query(self, sql, params):
        return []


@pytest.fixture
def copy_session():
    return CopySession()


@pytest.fixture
def copy_server(copy_session):
    with serve_in_thread(lambda: copy_session) as server:
        yield server


@pytest.fixture
def copy_dsn(copy_server):
    return copy_server.dsn(user="test", dbname="test")


@pytest.fixture
def copy_conn(copy_dsn):
    with psycopg.Connection.connect(copy_dsn, autocommit=True) as conn:
        yield conn


@pytest_asyncio.fixture
async def copy_apg_conn(copy_server):
    conn = await asyncpg.connect(host="127.0.0.1", port=copy_server.port, user="test", database="test")
    try:
        yield conn
    finally:
        await conn.close()


# --- option grammar -----------------------------------------------------------------

_option_testcases = {
    "text_defaults": {"sql": "COPY t FROM STDIN", "expected": ("text", "\t", "\\N", False)},
    "csv_defaults": {"sql": "COPY t FROM STDIN WITH (FORMAT csv)", "expected": ("csv", ",", "", False)},
    # asyncpg writes the parenthesised list without the optional WITH, which is
    # exactly the form sqlglot cannot parse.
    "csv_without_with": {"sql": "COPY t FROM STDIN (FORMAT csv)", "expected": ("csv", ",", "", False)},
    # ... and psql's \copy forwards the legacy bare-keyword syntax verbatim.
    "legacy_csv_header": {"sql": "COPY t TO STDOUT WITH CSV HEADER", "expected": ("csv", ",", "", True)},
    "legacy_delimiter": {"sql": "COPY t FROM STDIN WITH DELIMITER '|'", "expected": ("text", "|", "\\N", False)},
    "explicit_null": {"sql": "COPY t FROM STDIN WITH (NULL 'nil')", "expected": ("text", "\t", "nil", False)},
    "header_false": {"sql": "COPY t TO STDOUT WITH (FORMAT csv, HEADER false)", "expected": ("csv", ",", "", False)},
    "trailing_semicolon": {"sql": "COPY t TO STDOUT WITH (FORMAT csv);", "expected": ("csv", ",", "", False)},
    # The legacy AS noise word, which psql's `\copy ... with delimiter as '|'`
    # forwards verbatim -- refusing it refuses the legacy form outright.
    "legacy_delimiter_as": {"sql": "COPY t FROM STDIN WITH DELIMITER AS '|'", "expected": ("text", "|", "\\N", False)},
    "legacy_null_as": {"sql": "COPY t FROM STDIN WITH NULL AS 'nil'", "expected": ("text", "\t", "nil", False)},
    # A dollar-quoted constant is a string constant everywhere a literal is taken,
    # option values included -- real Postgres runs this one.
    "dollar_quoted_delimiter": {"sql": "COPY t FROM STDIN WITH (DELIMITER $$|$$)", "expected": ("text", "|", "\\N", False)},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_option_testcases.values()))),
    argvalues=[[v for k, v in sorted(_option_testcases[name].items())] for name in sorted(_option_testcases)],
    ids=sorted(_option_testcases),
)
def test_option_parsing(sql, expected):
    options = parse_copy(sql).options
    assert (options.format, options.delimiter, options.null_string, options.header) == expected


_refusal_testcases = {
    "format_binary": {"sql": "COPY t FROM STDIN WITH (FORMAT binary)", "sqlstate": "0A000"},
    "legacy_binary": {"sql": "COPY t FROM STDIN BINARY", "sqlstate": "0A000"},
    "force_quote": {"sql": "COPY t TO STDOUT WITH (FORMAT csv, FORCE_QUOTE *)", "sqlstate": "0A000"},
    "legacy_force_quote": {"sql": "COPY t TO STDOUT WITH CSV FORCE QUOTE *", "sqlstate": "0A000"},
    "freeze": {"sql": "COPY t FROM STDIN WITH (FREEZE)", "sqlstate": "0A000"},
    "header_match": {"sql": "COPY t FROM STDIN WITH (FORMAT csv, HEADER MATCH)", "sqlstate": "0A000"},
    "quote_in_text_mode": {"sql": "COPY t FROM STDIN WITH (QUOTE '~')", "sqlstate": "0A000"},
    "latin1": {"sql": "COPY t FROM STDIN WITH (ENCODING 'LATIN1')", "sqlstate": "0A000"},
    "unknown_format": {"sql": "COPY t FROM STDIN WITH (FORMAT jsonl)", "sqlstate": "42601"},
    "unknown_option": {"sql": "COPY t FROM STDIN WITH (BOGUS 1)", "sqlstate": "42601"},
    "multi_char_delimiter": {"sql": "COPY t FROM STDIN WITH (DELIMITER 'ab')", "sqlstate": "22023"},
    # Everything past the closing ')' used to be dropped on the floor. A PG12+ row
    # filter silently ignored copies every row instead of the ones asked for, and
    # an option in the tail escapes being refused by name.
    "where_clause_after_options": {"sql": "COPY t FROM STDIN WITH (FORMAT csv) WHERE id > 100", "sqlstate": "42601"},
    "option_after_options": {"sql": "COPY t FROM STDIN WITH (FORMAT csv) FORCE_NOT_NULL (a)", "sqlstate": "42601"},
    "delimiter_as_without_a_value": {"sql": "COPY t FROM STDIN WITH DELIMITER AS", "sqlstate": "42601"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_refusal_testcases.values()))),
    argvalues=[[v for k, v in sorted(_refusal_testcases[name].items())] for name in sorted(_refusal_testcases)],
    ids=sorted(_refusal_testcases),
)
def test_unsupported_options_are_refused(sql, sqlstate):
    """Refused at parse time, which is what lets the error reach the client before
    a CopyInResponse invites it to start sending."""
    with pytest.raises(PgError) as excinfo:
        parse_copy(sql)
    assert excinfo.value.sqlstate == sqlstate


_not_copy_testcases = {
    "select": "SELECT 1",
    # A server-side file read has no client protocol at all, so it belongs to the
    # session like any other statement pg_mimic doesn't model.
    "server_side_file": "COPY t FROM '/tmp/x.csv'",
    "server_side_program": "COPY t TO PROGRAM 'gzip > /tmp/x.gz'",
    # STDIN has to be a word of its own, not the start of one.
    "stdin_is_only_a_prefix": "COPY t FROM STDINX",
    "no_target": "COPY FROM STDIN",
    # An unterminated literal leaves the scanner with no way to tell which words are
    # inside it. Handing the statement to the session is the harmless way to be wrong
    # about a COPY; claiming it and asking the client to start sending is not.
    "unterminated_literal": "COPY (SELECT 'oops) TO STDOUT",
}


@pytest.mark.parametrize(
    argnames=["sql"],
    argvalues=[[sql] for sql in _not_copy_testcases.values()],
    ids=list(_not_copy_testcases),
)
def test_not_a_protocol_copy(sql):
    assert parse_copy(sql) is None


def test_copy_from_a_query_has_no_table_or_columns():
    parsed = parse_copy("COPY (SELECT a FROM t WHERE b = 1) TO STDOUT")
    assert (parsed.direction, parsed.table, parsed.columns) == ("out", None, None)


_endpoint_testcases = {
    "plain_from_stdin": {"sql": "COPY t FROM STDIN", "expected": ("in", "t")},
    "plain_to_stdout": {"sql": "COPY t TO STDOUT", "expected": ("out", "t")},
    "trailing_semicolon": {"sql": "COPY t FROM STDIN;", "expected": ("in", "t")},
    "across_lines": {"sql": "COPY t\n  FROM STDIN\n", "expected": ("in", "t")},
    # The query's own FROM is not the statement's endpoint.
    "subquery_from": {"sql": "COPY (SELECT a FROM t) TO STDOUT", "expected": ("out", None)},
    "nested_parens": {"sql": "COPY (SELECT a FROM (VALUES ('from stdin')) v(a)) TO STDOUT", "expected": ("out", None)},
    # ... and neither is a FROM STDIN a row happens to spell. Read as flat text this
    # is a copy-in whose target stops mid-literal, which is the worse half of the
    # error: the client has been told to start sending before anything downstream
    # notices. Real Postgres runs it as a copy-out of the query.
    "literal_holding_from_stdin": {
        "sql": "COPY (SELECT note FROM t WHERE note = 'copied from stdin yesterday') TO STDOUT",
        "expected": ("out", None),
    },
    "literal_holding_to_stdout": {"sql": "COPY (SELECT 'send it to stdout now') TO STDOUT", "expected": ("out", None)},
    "escaped_quote_in_the_literal": {"sql": "COPY (SELECT 'it''s from stdin') TO STDOUT", "expected": ("out", None)},
    # A dollar-quoted body is a literal too, apostrophes and all -- and an apostrophe
    # that ends no literal is exactly what loses a scanner its place.
    "dollar_quoted": {"sql": "COPY (SELECT $$ from stdin $$) TO STDOUT", "expected": ("out", None)},
    "tagged_dollar_quote": {"sql": "COPY (SELECT $q$it's from stdin$q$) TO STDOUT", "expected": ("out", None)},
    # The same apostrophe, in the one other place Postgres lets it appear unpaired.
    "apostrophe_in_a_quoted_name": {"sql": 'COPY "it\'s" FROM STDIN', "expected": ("in", "it's")},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_endpoint_testcases.values()))),
    argvalues=[[v for k, v in sorted(_endpoint_testcases[name].items())] for name in sorted(_endpoint_testcases)],
    ids=sorted(_endpoint_testcases),
)
def test_the_endpoint_is_found_outside_literals_and_the_query(sql, expected):
    """Which endpoint a COPY names is the classification the whole module hangs off,
    so it is scanned with the tokenizer rather than matched against flat text."""
    parsed = parse_copy(sql)
    assert (parsed.direction, parsed.table) == expected


_target_testcases = {
    "bare": {"sql": "COPY t (a, b) FROM STDIN", "expected": ("t", ["a", "b"])},
    "no_columns": {"sql": "COPY t FROM STDIN", "expected": ("t", None)},
    "unquoted_folds_to_lower": {"sql": "COPY People (Id) FROM STDIN", "expected": ("people", ["id"])},
    "quoted_keeps_its_case": {"sql": 'COPY "People" ("Id") FROM STDIN', "expected": ("People", ["Id"])},
    # A quoted identifier may hold a space, which is where a `[^\s(]+` name pattern
    # gave up -- taking the column list, spelled out right there, down with it.
    "quoted_with_a_space": {"sql": 'COPY "my table" (id, name) FROM STDIN', "expected": ("my table", ["id", "name"])},
    # Split outside the quotes, or this declares three columns for the two it names
    # and the CopyInResponse arity is wrong.
    "comma_inside_a_column_name": {"sql": 'COPY t ("a,b", c) FROM STDIN', "expected": ("t", ["a,b", "c"])},
    "schema_qualified": {"sql": "COPY myschema.t (a) FROM STDIN", "expected": ("t", ["a"])},
    "dot_inside_a_quoted_name": {"sql": 'COPY "we.ird" (a) FROM STDIN', "expected": ("we.ird", ["a"])},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_target_testcases.values()))),
    argvalues=[[v for k, v in sorted(_target_testcases[name].items())] for name in sorted(_target_testcases)],
    ids=sorted(_target_testcases),
)
def test_target_parsing(sql, expected):
    parsed = parse_copy(sql)
    assert (parsed.table, parsed.columns) == expected


def test_a_header_names_the_schema_table_however_it_was_spelled():
    """schema() keys are the session author's own spelling and pg_mimic has no
    catalog to fold them against, so a quoted name matches one exactly and an
    unquoted one -- already folded to lower case -- matches case-insensitively."""

    class Schema(Session):
        async def schema(self):
            return {"People": {"id": "bigint", "name": "text"}}

        async def copy_out(self, parsed):
            yield (1, "alice")

    with serve_in_thread(Schema) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            for sql in ('COPY "People" TO STDOUT WITH (FORMAT csv, HEADER)', "COPY People TO STDOUT WITH (FORMAT csv, HEADER)"):
                with conn.cursor().copy(sql) as copy:
                    assert b"".join(copy).decode() == "id,name\n1,alice\n", sql


# --- codecs -------------------------------------------------------------------------


def _options(sql):
    return parse_copy(sql).options


def test_text_encoding_escapes_and_nulls():
    encoded = CopyEncoder(_options("COPY t TO STDOUT")).row((1, "a\tb", None, "back\\slash", "line\nbreak"))
    assert encoded == b"1\ta\\tb\t\\N\tback\\\\slash\tline\\nbreak\n"


def test_csv_encoding_quotes_only_what_needs_it():
    encoded = CopyEncoder(_options("COPY t TO STDOUT (FORMAT csv)")).row((1, "a,b", 'say "hi"', None, "", "plain"))
    # An empty string is quoted so it doesn't read back as NULL; NULL is the bare
    # null string, which for CSV is the empty field.
    assert encoded == b'1,"a,b","say ""hi""",,"",plain\n'


def test_text_decoding_round_trips_the_escape_set():
    decoder = CopyInDecoder(_options("COPY t FROM STDIN"))
    rows = decoder.feed(b"a\\tb\ta\\nb\t\\N\t\\\\N\t\\x41\t\\101\t\\q\n")
    assert rows + decoder.finish() == [("a\tb", "a\nb", None, "\\N", "A", "A", "q")]


def test_text_decoding_stops_at_the_end_of_data_marker():
    decoder = CopyInDecoder(_options("COPY t FROM STDIN"))
    assert decoder.feed(b"1\n\\.\n2\n") + decoder.finish() == [("1",)]


def test_a_row_split_across_messages_is_one_row():
    """Neither a row boundary nor a character boundary lines up with a CopyData
    boundary, so both have to survive being cut mid-way."""
    decoder = CopyInDecoder(_options("COPY t FROM STDIN"))
    payload = "1\tsnöw\n2\tx\n".encode()
    # Cut inside the two bytes of "ö", so the split is mid-character as well as
    # mid-row.
    assert decoder.feed(payload[:5]) == []
    assert decoder.feed(payload[5:]) == [("1", "snöw"), ("2", "x")]
    assert decoder.finish() == []


def test_csv_decoding_distinguishes_quoted_empty_from_null():
    decoder = CopyInDecoder(_options("COPY t FROM STDIN (FORMAT csv)"))
    rows = decoder.feed(b'1,"a,b","say ""hi""",,""\n')
    assert rows + decoder.finish() == [("1", "a,b", 'say "hi"', None, "")]


def test_csv_field_may_contain_the_record_separator():
    decoder = CopyInDecoder(_options("COPY t FROM STDIN (FORMAT csv)"))
    rows = decoder.feed(b'1,"multi\nline"\n2,x')
    assert rows == [("1", "multi\nline")]
    assert decoder.finish() == [("2", "x")]  # a last record with no newline is still one


def test_csv_header_line_is_skipped_and_crlf_tolerated():
    decoder = CopyInDecoder(_options("COPY t FROM STDIN (FORMAT csv, HEADER)"))
    assert decoder.feed(b"id,name\r\n1,alice\r\n") + decoder.finish() == [("1", "alice")]


# --- psycopg ------------------------------------------------------------------------


def test_psycopg_copy_in_text(copy_conn, copy_session):
    with copy_conn.cursor() as cur:
        with cur.copy("COPY people (id, name) FROM STDIN") as copy:
            copy.write_row((1, "alice"))
            copy.write_row((2, None))
        assert cur.rowcount == 2
    assert copy_session.loaded == [("1", "alice"), ("2", None)]


def test_psycopg_copy_in_csv(copy_conn, copy_session):
    with copy_conn.cursor() as cur:
        with cur.copy("COPY people (id, name) FROM STDIN WITH (FORMAT csv)") as copy:
            copy.write(b'1,"has, comma"\n2,\n')
    assert copy_session.loaded == [("1", "has, comma"), ("2", None)]


def test_psycopg_copy_out_text(copy_conn, copy_session):
    copy_session.out_rows = [(1, "alice"), (2, None)]
    with copy_conn.cursor() as cur:
        with cur.copy("COPY people TO STDOUT") as copy:
            assert list(copy.rows()) == [("1", "alice"), ("2", None)]
        assert cur.rowcount == 2


def test_psycopg_copy_out_of_a_query_that_talks_about_stdin(copy_conn, copy_session):
    """The whole cost of misreading the endpoint, through a real client: psycopg
    asked to copy *out* would be sitting in copy-in mode with a CopyInResponse in
    hand, and there is no way back from that once the invitation has gone out."""
    copy_session.out_rows = [("copied from stdin yesterday",)]
    with copy_conn.cursor() as cur:
        with cur.copy("COPY (SELECT note FROM people WHERE note = 'copied from stdin yesterday') TO STDOUT") as copy:
            assert list(copy.rows()) == [("copied from stdin yesterday",)]


def test_psycopg_copy_out_csv_with_header_from_the_declared_schema(copy_conn, copy_session):
    """No column list in the statement, so the header can only come from
    Session.schema() -- the one other place column names honestly exist."""
    copy_session.out_rows = [(1, "alice")]
    with copy_conn.cursor() as cur:
        with cur.copy("COPY people TO STDOUT WITH (FORMAT csv, HEADER)") as copy:
            assert b"".join(bytes(chunk) for chunk in copy) == b"id,name\n1,alice\n"


def test_copy_out_header_is_refused_when_nothing_can_name_the_columns(copy_conn, copy_session):
    copy_session.out_rows = [(1,)]
    with copy_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            with cur.copy("COPY (SELECT 1) TO STDOUT WITH (FORMAT csv, HEADER)"):
                pass


def test_copy_out_refuses_a_list_it_cannot_type(copy_conn, copy_session):
    """COPY carries no column types, so an array and a json document are the same
    Python value with no way to tell them apart. Refused, not guessed."""
    copy_session.out_rows = [(["a", "b"],)]
    with copy_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            with cur.copy("COPY people TO STDOUT") as copy:
                copy.read()


def test_copy_out_accepts_a_coroutine_returning_rows():
    """copy_out() may be a plain `async def` returning any iterable, not only an
    async generator -- the same two shapes query() accepts."""

    class ListSession(Session):
        async def copy_out(self, sql):
            return [(1, "alice"), (2, "bob")]

    with serve_in_thread(ListSession) as server:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            with conn.cursor().copy("COPY people TO STDOUT") as copy:
                assert list(copy.rows()) == [("1", "alice"), ("2", "bob")]


def test_binary_copy_is_refused_over_the_wire(copy_conn):
    with copy_conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported) as excinfo:
            with cur.copy("COPY people FROM STDIN WITH (FORMAT binary)"):
                pass
    assert "binary" in str(excinfo.value)


def test_a_session_without_the_hook_is_refused_before_any_data_moves():
    with serve_in_thread(ReadOnlySession) as server:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            with pytest.raises(psycopg.errors.FeatureNotSupported) as excinfo:
                with conn.cursor().copy("COPY people FROM STDIN"):
                    pass
    assert "copy_in" in str(excinfo.value)


def test_copy_in_a_failed_transaction_is_refused(copy_dsn, copy_session):
    copy_session.error = PgError(UNDEFINED_TABLE, 'relation "nope" does not exist')
    with psycopg.connect(copy_dsn) as conn:
        with pytest.raises(psycopg.errors.UndefinedTable):
            conn.execute("SELECT * FROM nope")
        with pytest.raises(psycopg.errors.InFailedSqlTransaction):
            with conn.cursor().copy("COPY people FROM STDIN") as copy:
                copy.write_row((1, "alice"))
        conn.rollback()
    assert copy_session.loaded == []


def test_the_connection_survives_a_copy_the_session_abandoned(copy_conn, copy_session):
    """A session that stops reading leaves the client mid-copy. The remaining
    messages have to be consumed anyway, or the next statement reads them as
    though they were commands."""
    copy_session.stop_after = 1
    with copy_conn.cursor() as cur:
        with cur.copy("COPY people FROM STDIN") as copy:
            for index in range(50):
                copy.write_row((index, "x"))
    assert copy_session.loaded == [("0", "x")]

    copy_session.columns = [ResultColumn.for_type("c", int)]
    copy_session.rows = [(7,)]
    assert copy_conn.execute("SELECT c").fetchall() == [(7,)]


def test_a_session_may_report_its_own_row_count(copy_conn, copy_session):
    """CommandComplete says what the session stored, not what pg_mimic decoded --
    a session that filters rows would otherwise have to lie."""
    copy_session.reported_count = 1
    with copy_conn.cursor() as cur:
        with cur.copy("COPY people FROM STDIN") as copy:
            copy.write_row((1, "alice"))
            copy.write_row((2, "bob"))
        assert cur.rowcount == 1


# --- asyncpg ------------------------------------------------------------------------


async def test_asyncpg_copy_records_to_table_refuses_binary(copy_apg_conn, copy_session):
    """asyncpg's copy_records_to_table always asks for `FORMAT binary`. Refusing it
    by name is the whole point: the alternative is a guessed tuple layout that
    loads plausible-looking wrong rows."""
    # asyncpg learns the column types from a `SELECT ... LIMIT 1` before it sends
    # the COPY, so the session has to answer that with a shape before the binary
    # refusal is the thing being tested.
    copy_session.columns = [ResultColumn.for_type("id", int), ResultColumn.for_type("name", str)]
    with pytest.raises(asyncpg.exceptions.FeatureNotSupportedError):
        await copy_apg_conn.copy_records_to_table("people", records=[(1, "alice")], columns=["id", "name"])
    assert copy_session.loaded == []


async def test_asyncpg_copy_to_table_from_csv(copy_apg_conn, copy_session):
    """copy_to_table's csv path, which is asyncpg's text-format bulk load. Note the
    statement it builds puts the options in parentheses with no WITH."""
    source = io.BytesIO(b"1,alice\n2,\n")
    status = await copy_apg_conn.copy_to_table("people", source=source, columns=["id", "name"], format="csv")
    assert status == "COPY 2"
    assert copy_session.loaded == [("1", "alice"), ("2", None)]


async def test_asyncpg_copy_from_query(copy_apg_conn, copy_session):
    copy_session.out_rows = [(1, "alice"), (2, None)]
    output = io.BytesIO()
    status = await copy_apg_conn.copy_from_query("SELECT id, name FROM people", output=output)
    assert status == "COPY 2"
    assert output.getvalue() == b"1\talice\n2\t\\N\n"


# --- psql ---------------------------------------------------------------------------


def _psql(session, command):
    server = PgServer(session_factory=lambda: session)
    thread = ServerThread(server)
    port = thread.start()
    try:
        return subprocess.run(
            ["psql", f"host=127.0.0.1 port={port} user=test dbname=test", "-c", command],
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        thread.stop()


@psql_required
def test_psql_copy_from_a_local_file(tmp_path, copy_session):
    source = tmp_path / "people.tsv"
    source.write_text("1\talice\n2\t\\N\n")
    result = _psql(copy_session, f"\\copy people (id, name) from '{source}'")
    assert result.stderr.strip() == "", result.stderr
    assert "COPY 2" in result.stdout
    assert copy_session.loaded == [("1", "alice"), ("2", None)]


@psql_required
def test_psql_copy_to_a_local_csv_file(tmp_path, copy_session):
    destination = tmp_path / "people.csv"
    copy_session.out_rows = [(1, "alice"), (2, None)]
    result = _psql(copy_session, f"\\copy people (id, name) to '{destination}' with (format csv, header)")
    assert result.stderr.strip() == "", result.stderr
    assert destination.read_text() == "id,name\n1,alice\n2,\n"


# --- the cases only the raw protocol produces ---------------------------------------


async def _start_copy_in(port, sql="COPY people FROM STDIN"):
    reader, writer, pid, secret = await connect_and_get_backend_key(port)
    writer.write(make_query(sql))
    await writer.drain()
    tag, _ = await asyncio.wait_for(read_message(reader), timeout=2)
    assert tag == b"G", tag  # CopyInResponse: the server is now in copy mode
    return reader, writer, pid, secret


async def _drain_to_ready(reader):
    """Every message up to ReadyForQuery, so a test can assert on what the server
    said without leaving the connection part-way through a response."""
    seen = []
    while True:
        tag, payload = await asyncio.wait_for(read_message(reader), timeout=2)
        seen.append((tag, payload))
        if tag == b"Z":
            return seen


async def test_copy_in_through_the_extended_protocol(copy_server, copy_session):
    """Parse/Bind/Describe/Execute rather than a simple query. Bind produces a
    portal that is never executed as one, Describe answers NoData (COPY sends no
    RowDescription), and the copy starts at Execute -- so it's the Sync after
    CopyDone that closes the exchange, not the Execute."""
    reader, writer, _, _ = await connect_and_get_backend_key(copy_server.port)
    writer.write(make_parse("COPY people (id, name) FROM STDIN"))
    writer.write(make_bind())
    writer.write(make_describe(TARGET_PORTAL))
    writer.write(make_execute())
    await writer.drain()
    assert [tag for tag, _ in [await asyncio.wait_for(read_message(reader), timeout=2) for _ in range(4)]] == [
        b"1",  # ParseComplete
        b"2",  # BindComplete
        b"n",  # NoData
        b"G",  # CopyInResponse
    ]

    writer.write(make_copy_data(b"1\talice\n"))
    writer.write(COPY_DONE)
    writer.write(SYNC)
    await writer.drain()
    seen = await _drain_to_ready(reader)
    assert [tag for tag, _ in seen] == [b"C", b"Z"]
    assert seen[0][1] == b"COPY 1\x00"
    assert copy_session.loaded == [("1", "alice")]
    writer.close()
    await writer.wait_closed()


async def test_copy_fail_ends_the_copy_with_the_client_s_reason(copy_server, copy_session):
    reader, writer, _, _ = await _start_copy_in(copy_server.port)
    writer.write(make_copy_data(b"1\talice\n"))
    writer.write(make_copy_fail("changed my mind"))
    await writer.drain()

    seen = await _drain_to_ready(reader)
    errors = [parse_error_fields(payload) for tag, payload in seen if tag == b"E"]
    assert errors[0]["C"] == "57014"
    assert errors[0]["M"] == "COPY from stdin failed: changed my mind"
    writer.close()
    await writer.wait_closed()


async def test_a_stray_message_during_copy_in_is_a_protocol_violation(copy_server):
    reader, writer, _, _ = await _start_copy_in(copy_server.port)
    writer.write(make_copy_data(b"1\talice\n"))
    writer.write(make_query("SELECT 1"))  # not CopyData/CopyDone/CopyFail
    await writer.drain()

    seen = await _drain_to_ready(reader)
    errors = [parse_error_fields(payload) for tag, payload in seen if tag == b"E"]
    assert errors[0]["C"] == "08P01"
    assert "during COPY from stdin" in errors[0]["M"]
    writer.close()
    await writer.wait_closed()


async def test_flush_and_sync_are_ignored_during_copy_in(copy_server, copy_session):
    """Real Postgres tolerates both here rather than treating them as the protocol
    violation any other message type is -- and must not answer the Sync, which
    would put a ReadyForQuery ahead of the CommandComplete still to come."""
    reader, writer, _, _ = await _start_copy_in(copy_server.port)
    writer.write(FLUSH)
    writer.write(make_copy_data(b"1\talice\n"))
    writer.write(SYNC)
    writer.write(COPY_DONE)
    await writer.drain()

    seen = await _drain_to_ready(reader)
    assert [tag for tag, _ in seen] == [b"C", b"Z"]
    assert seen[0][1] == b"COPY 1\x00"
    assert copy_session.loaded == [("1", "alice")]
    writer.close()
    await writer.wait_closed()


async def test_a_cancel_mid_copy_leaves_a_usable_connection(copy_server, copy_session):
    """The server spends most of a copy-in parked on the next CopyData, so that is
    where a cancel lands. Afterwards the client is still mid-copy and its remaining
    messages have to be dropped, not answered."""
    reader, writer, pid, secret = await _start_copy_in(copy_server.port)
    writer.write(make_copy_data(b"1\talice\n"))
    await writer.drain()
    await asyncio.sleep(0.1)  # let the server consume it and park on the next read

    _, cancel_writer = await asyncio.open_connection("127.0.0.1", copy_server.port)
    cancel_writer.write(make_cancel_request(pid, secret))
    await cancel_writer.drain()
    cancel_writer.close()
    await cancel_writer.wait_closed()

    seen = await _drain_to_ready(reader)
    assert [parse_error_fields(payload)["C"] for tag, payload in seen if tag == b"E"] == ["57014"]

    # The client hasn't noticed yet, so it finishes the copy it started.
    writer.write(make_copy_data(b"2\tbob\n"))
    writer.write(COPY_DONE)
    copy_session.columns = [ResultColumn.for_type("c", int)]
    copy_session.rows = [(7,)]
    writer.write(make_query("SELECT c"))
    await writer.drain()
    assert [tag for tag, _ in await _drain_to_ready(reader)] == [b"T", b"D", b"C", b"Z"]
    writer.close()
    await writer.wait_closed()
