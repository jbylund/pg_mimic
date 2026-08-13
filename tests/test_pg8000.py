"""pg8000 compatibility.

Two things make pg8000 worth testing rather than assuming psycopg covers it.

It is the suite's third protocol implementation: psycopg is libpq-based, while
asyncpg and pg8000 each hand-rolled the wire protocol separately. Three
implementations disagree about more than two do.

And it is the strongest exerciser of the *text* path -- it asks for every column
in text format, where asyncpg always asks for binary and psycopg only asks for
binary on request. test_asyncpg.py exists because asyncpg exercises the binary
path far harder than psycopg; this file is the mirror of that argument.
`test_every_column_comes_back_in_text_format` pins the property the rest of the
file depends on, so if a future pg8000 starts requesting binary, that test says
so rather than this file quietly becoming a duplicate of the asyncpg one.

Everything here passed the first time it was written -- this is regression
protection for the text path, not a bug hunt. See #95.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pg8000.dbapi
import pytest
from conftest import MockSession

from pg_mimic import (
    ARRAY_OID,
    INT8,
    JSONB,
    TEXT,
    Md5PasswordAuthPlugin,
    ResultColumn,
    ScramSha256AuthPlugin,
    SimpleIdentityProvider,
)
from pg_mimic.testing import serve_in_thread


def _one(conn, sql, args=()):
    """pg8000's DBAPI defaults `args` to `()`, not None -- passing None reaches
    `len(None)` inside the driver, which is a confusing way to fail a test."""
    cursor = conn.cursor()
    cursor.execute(sql, args)
    return cursor.fetchall()[0][0]


def test_every_column_comes_back_in_text_format(pg8000_conn, mock_session, monkeypatch):
    """The property that earns this file its place in the suite.

    Bind carries the result format codes, and pg8000 sends none at all -- which
    Postgres defines as "everything text" (see results.format_code_for). Spying on
    encode_row is the only place the resolved codes are visible; the client side
    never exposes what it asked for.
    """
    import pg_mimic.connection

    seen = []
    original = pg_mimic.connection.encode_row

    def spy(row, columns, format_codes=None):
        seen.append(format_codes)
        return original(row, columns, format_codes)

    monkeypatch.setattr(pg_mimic.connection, "encode_row", spy)
    mock_session.columns = [ResultColumn.for_type("c", int)]
    mock_session.rows = [(1,)]

    assert _one(pg8000_conn, "select c") == 1
    assert seen, "encode_row was never reached"
    # Empty or None both mean all-text. Binary anywhere would be a 1.
    assert all(not codes for codes in seen), f"pg8000 asked for a non-text format: {seen}"


_scalars = {
    "int8": (int, 42),
    "text": (str, "hello"),
    "bool": (bool, True),
    "float8": (float, 1.5),
    "numeric": (Decimal, Decimal("1.25")),
    "uuid": (uuid.UUID, uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")),
    "date": (date, date(2001, 2, 3)),
    "timestamp": (datetime, datetime(2001, 2, 3, 4, 5, 6)),
    "bytea": (bytes, b"\x00\xff"),
    "interval": (timedelta, timedelta(days=2, hours=3)),
    "interval_negative": (timedelta, timedelta(hours=-23)),
}


@pytest.mark.parametrize(
    argnames=["py_type", "value"],
    argvalues=[_scalars[name] for name in sorted(_scalars)],
    ids=sorted(_scalars),
)
def test_scalar_round_trips(pg8000_conn, mock_session, py_type, value):
    """Every one of these goes out as text, so this is the text encoder's
    round-trip test against a client that didn't write the text encoder."""
    mock_session.columns = [ResultColumn.for_type("c", py_type)]
    mock_session.rows = [(value,)]
    assert _one(pg8000_conn, "select c") == value


def test_null_is_null(pg8000_conn, mock_session):
    """NULL is a -1 length in DataRow, so it has no text representation to get
    wrong -- worth pinning precisely because it bypasses the encoder."""
    mock_session.columns = [ResultColumn.for_type("c", int)]
    mock_session.rows = [(None,)]
    assert _one(pg8000_conn, "select c") is None


@pytest.mark.parametrize(
    argnames=["oid", "value"],
    argvalues=[
        (ARRAY_OID[TEXT], ["a", "b,c", None]),
        (ARRAY_OID[INT8], [1, 2, None]),
    ],
    ids=["text_array", "int8_array"],
)
def test_array_round_trips(pg8000_conn, mock_session, oid, value):
    """Arrays are where the text format actually gets hard: the literal is a
    quoted, comma-delimited string, so an embedded comma and a NULL are the two
    things a hand-rolled parser gets wrong. pg8000 parses these itself."""
    mock_session.columns = [ResultColumn("c", oid)]
    mock_session.rows = [(value,)]
    assert _one(pg8000_conn, "select c") == value


def test_jsonb(pg8000_conn, mock_session):
    """Unlike asyncpg, pg8000 decodes json to a dict without a registered codec,
    so this asserts the parsed value rather than the wire bytes."""
    mock_session.columns = [ResultColumn("c", JSONB)]
    mock_session.rows = [({"k": [1, 2]},)]
    assert _one(pg8000_conn, "select c") == {"k": [1, 2]}


def test_multiple_rows(pg8000_conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("c", int)]
    mock_session.rows = [(1,), (2,), (3,)]
    cursor = pg8000_conn.cursor()
    cursor.execute("select c")
    assert [row[0] for row in cursor.fetchall()] == [1, 2, 3]


def test_cursor_description_names_the_columns(pg8000_conn, mock_session):
    """pg8000 builds `description` straight from RowDescription, so this is that
    message read back by a third parser."""
    mock_session.columns = [ResultColumn.for_type("a", int), ResultColumn.for_type("b", str)]
    mock_session.rows = [(1, "x")]
    cursor = pg8000_conn.cursor()
    cursor.execute("select a, b")
    assert [column[0] for column in cursor.description] == ["a", "b"]


@pytest.mark.parametrize(
    argnames=["sql", "args", "columns", "row"],
    argvalues=[
        ("select %s", (7,), [("c", int)], (7,)),
        ("select %s", ("hi",), [("c", str)], ("hi",)),
        ("select %s, %s", (1, "a"), [("a", int), ("b", str)], (1, "a")),
    ],
    ids=["one_int", "one_text", "two_params"],
)
def test_parameters_use_the_extended_protocol(pg8000_conn, mock_session, sql, args, columns, row):
    """pg8000's `format` paramstyle sends Parse/Bind/Execute rather than a simple
    Query, so any statement with a parameter takes the extended path."""
    mock_session.columns = [ResultColumn.for_type(name, py_type) for name, py_type in columns]
    mock_session.rows = [row]
    cursor = pg8000_conn.cursor()
    cursor.execute(sql, args)
    assert cursor.fetchall()[0] == list(row)


@pytest.mark.parametrize(argnames=["finish"], argvalues=[["commit"], ["rollback"]], ids=["commit", "rollback"])
def test_explicit_transaction(pg8000_conn, mock_session, finish):
    """pg8000 opens its own transaction when autocommit is off, so this is its
    BEGIN and its COMMIT/ROLLBACK rather than SQL the test wrote."""
    pg8000_conn.autocommit = False
    mock_session.columns = [ResultColumn.for_type("c", int)]
    mock_session.rows = [(1,)]
    assert _one(pg8000_conn, "select c") == 1
    getattr(pg8000_conn, finish)()


@pytest.mark.parametrize(
    argnames=["plugin_cls"],
    argvalues=[[ScramSha256AuthPlugin], [Md5PasswordAuthPlugin]],
    ids=["scram_sha_256", "md5"],
)
def test_password_auth(plugin_cls):
    """The handshake driven by a client that isn't psycopg.

    Weaker than it looks for SCRAM: pg_mimic and pg8000 both use `scramp`, so this
    is closer to a self-consistency check than interop. It still covers the
    message sequencing around the exchange, which is pg_mimic's own code.
    """
    identity_provider = SimpleIdentityProvider({"alice": "s3cret"})
    with serve_in_thread(
        lambda: MockSession(),
        auth_plugin_factory=lambda username: plugin_cls(),
        identity_provider=identity_provider,
    ) as server:
        connect = dict(host="127.0.0.1", port=server.port, user="alice", database="test")
        pg8000.dbapi.connect(password="s3cret", **connect).close()
        with pytest.raises(pg8000.dbapi.DatabaseError):
            pg8000.dbapi.connect(password="wrong", **connect).close()
