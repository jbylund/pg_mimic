"""TableSession, through real drivers.

The promise being tested is "no session code": a dict of rows in, a server real
clients can query out, with types the driver reports correctly and a catalog psql
can read. So almost everything here goes through psycopg or asyncpg rather than
calling describe()/query() directly -- a type that is right in a ResultColumn and
wrong on the wire is still wrong.

The load-bearing test is `test_types_come_from_the_schema_not_the_rows`: column
types must come from the declared schema, so a query returning no rows describes
exactly like one returning many. Inferring from row data would pass every other
test in this file.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import asyncpg
import psycopg
import pytest

from pg_mimic import ARRAY_OID, DATE, INT4, INT8, JSONB, NUMERIC, TEXT, TIMESTAMP, VARCHAR, TableSession
from pg_mimic.testing import serve, serve_in_thread
from pg_mimic.types import INET

TABLES = {
    "users": [
        {"id": 1, "name": "alice", "joined": datetime.date(2024, 1, 2), "tags": ["staff"]},
        {"id": 2, "name": "bob", "joined": datetime.date(2024, 3, 4), "tags": []},
    ],
    "orders": [
        {"id": 10, "user_id": 1, "total": Decimal("9.99")},
        {"id": 11, "user_id": 1, "total": Decimal("0.01")},
    ],
    # The three cases inference cannot settle, all declared below: an empty table,
    # an all-NULL column, and a value that is equally an array or a json document.
    "events": [],
    "notes": [{"id": 1, "body": None}],
    "docs": [{"id": 1, "body": {"answer": 42}}],
}

COLUMNS = {
    "events": {"id": int, "at": datetime.datetime},
    "notes": {"body": str},
    "docs": {"body": JSONB},
    "users": {"tags": list[str]},
}


def _session() -> TableSession:
    return TableSession(TABLES, columns=COLUMNS)


@pytest.fixture
def conn():
    with serve_in_thread(_session) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            yield conn


def _types(cursor) -> list[int]:
    return [column.type_code for column in cursor.description]


def _names(cursor) -> list[str]:
    return [column.name for column in cursor.description]


# --- the point of the class ----------------------------------------------------------


def test_a_dict_of_rows_is_a_working_server(conn):
    cursor = conn.execute("SELECT id, name FROM users ORDER BY id")
    assert cursor.fetchall() == [(1, "alice"), (2, "bob")]


def test_select_star_expands_to_the_declared_columns(conn):
    cursor = conn.execute("SELECT * FROM users ORDER BY id")
    assert _names(cursor) == ["id", "name", "joined", "tags"]
    assert cursor.fetchone() == (1, "alice", datetime.date(2024, 1, 2), ["staff"])


def test_declared_types_reach_the_client(conn):
    cursor = conn.execute("SELECT id, name, joined, tags FROM users")
    assert _types(cursor) == [INT8, TEXT, DATE, ARRAY_OID[TEXT]]


def test_types_come_from_the_schema_not_the_rows(conn):
    """The design constraint: no row is consulted to answer describe(), so a
    query matching nothing still declares bigint/text/date. Inferring from the
    first row -- and calling an empty result TEXT, as statement_from_rows must --
    would make these two disagree."""
    empty = conn.execute("SELECT id, name, joined FROM users WHERE id = 999")
    full = conn.execute("SELECT id, name, joined FROM users")
    assert empty.fetchall() == []
    assert _types(empty) == _types(full) == [INT8, TEXT, DATE]


def test_an_empty_table_describes_its_declared_columns(conn):
    cursor = conn.execute("SELECT * FROM events")
    assert cursor.fetchall() == []
    assert _names(cursor) == ["id", "at"]
    assert _types(cursor) == [INT8, TIMESTAMP]


def test_an_all_null_column_carries_its_declared_type(conn):
    cursor = conn.execute("SELECT body FROM notes")
    assert cursor.fetchall() == [(None,)]
    assert _types(cursor) == [TEXT]


def test_expression_types_come_from_sqlglots_annotator(conn):
    """count(*) is bigint because sqlglot types the expression that way, not
    because a row happened to hold an int. A string expression comes back varchar
    where Postgres would say text -- sqlglot's answer, and the same str to every
    client -- while a *declared* column keeps the type it was declared with."""
    assert _types(conn.execute("SELECT count(*) FROM users")) == [INT8]
    assert _types(conn.execute("SELECT sum(total) AS s FROM orders")) == [NUMERIC]
    assert _types(conn.execute("SELECT lower(name) FROM users")) == [VARCHAR]
    assert conn.execute("SELECT count(*) FROM users").fetchone() == (2,)
    assert conn.execute("SELECT lower(name) FROM users ORDER BY id").fetchall() == [("alice",), ("bob",)]


def test_unnameable_expressions_get_postgres_shaped_names(conn):
    assert _names(conn.execute("SELECT count(*) FROM users")) == ["count"]
    assert _names(conn.execute("SELECT id + 1 FROM users")) == ["?column?"]


def test_joins_group_by_and_order_by_are_executed(conn):
    cursor = conn.execute(
        "SELECT u.name, count(*) AS orders, sum(o.total) AS spent "
        "FROM users u JOIN orders o ON o.user_id = u.id GROUP BY u.name ORDER BY u.name"
    )
    assert cursor.fetchall() == [("alice", 2, Decimal("10.00"))]


def test_a_client_ping_is_answered(conn):
    """`SELECT 1` reaches the session (the middleware deliberately passes it), and
    a TableSession can execute it like any other query."""
    assert conn.execute("SELECT 1").fetchone() == (1,)


def test_json_and_array_declarations_are_honoured(conn):
    """The two the type inference refuses to guess between, told apart by the
    declaration alone -- psycopg decodes a jsonb document and a text[] array
    differently, so the client's answer proves which OID was sent."""
    assert _types(conn.execute("SELECT body FROM docs")) == [JSONB]
    assert conn.execute("SELECT body FROM docs").fetchone() == ({"answer": 42},)
    assert conn.execute("SELECT tags FROM users ORDER BY id").fetchall() == [(["staff"],), ([],)]


def test_a_declared_type_overrides_what_the_rows_suggest(conn):
    """int infers to int8; a table whose real column is `integer` says so."""
    with serve_in_thread(lambda: TableSession({"t": [{"n": 1}]}, columns={"t": {"n": INT4}})) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as narrower:
            assert _types(narrower.execute("SELECT n FROM t")) == [INT4]


def test_tuple_rows_need_only_their_names_declared():
    tables = {"users": [(1, "alice"), (2, "bob")]}
    with serve_in_thread(lambda: TableSession(tables, columns={"users": ["id", "name"]})) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            cursor = conn.execute("SELECT * FROM users WHERE name = 'bob'")
            assert cursor.fetchall() == [(2, "bob")]
            assert _types(cursor) == [INT8, TEXT]


def test_a_missing_key_in_a_dict_row_is_null(conn):
    """Rows are the caller's own dicts, and a test fixture often omits a column
    rather than spelling out None."""
    with serve_in_thread(lambda: TableSession({"t": [{"a": 1, "b": "x"}, {"a": 2}]})) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as sparse:
            assert sparse.execute("SELECT a, b FROM t ORDER BY a").fetchall() == [(1, "x"), (2, None)]


# --- parameters ----------------------------------------------------------------------


def test_parameters_are_substituted_into_the_executed_query(conn):
    assert conn.execute("SELECT name FROM users WHERE id = %s", (2,)).fetchall() == [("bob",)]
    assert conn.execute("SELECT id FROM users WHERE name = %s", ("alice",)).fetchall() == [(1,)]
    assert conn.execute("SELECT id FROM users WHERE joined > %s", (datetime.date(2024, 2, 1),)).fetchall() == [(2,)]
    assert conn.execute("SELECT id FROM orders WHERE total > %s", (Decimal("1.00"),)).fetchall() == [(10,)]


def test_a_boolean_parameter_is_converted_rather_than_cast(conn):
    """A bool parameter reaches the session as "f", and sqlglot's executor casts
    that to True (`bool("f")`), so this session converts it itself. The wrong
    answer here is a row, which is why it is worth a test of its own."""
    tables = {"flags": [{"id": 1, "enabled": True}, {"id": 2, "enabled": False}]}
    with serve_in_thread(lambda: TableSession(tables)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as flags:
            assert flags.execute("SELECT id FROM flags WHERE enabled = %s", (False,)).fetchall() == [(2,)]
            assert flags.execute("SELECT id FROM flags WHERE enabled = %s", (True,)).fetchall() == [(1,)]


def test_a_parameter_that_is_not_a_number_for_a_number_column_is_refused(conn):
    """Postgres's own 22P02, rather than an int comparison that quietly matches
    nothing."""
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        conn.execute("SELECT id FROM users WHERE id = %s", ("not-a-number",))


def test_a_parameter_nothing_types_is_reported_as_such(conn):
    """Postgres's own answer to `SELECT $1` with no context: 42P18. Guessing text
    would send the client a column of the wrong type instead."""
    with pytest.raises(psycopg.errors.IndeterminateDatatype):
        conn.execute("SELECT %s", ("x",))


async def test_asyncpg_is_told_the_parameter_types_it_left_to_the_server():
    """asyncpg declares no parameter types and reads them back from
    Describe(Statement), so a session that reports none makes it refuse to send
    any argument at all ("the server expects 0 arguments for this query")."""
    async with serve(_session) as server:
        client = await asyncpg.connect(host="127.0.0.1", port=server.port, user="u", database="d")
        try:
            assert await client.fetchval("SELECT name FROM users WHERE id = $1", 2) == "bob"
            assert await client.fetchval("SELECT id FROM users WHERE name = $1", "alice") == 1
        finally:
            await client.close()


async def test_asyncpg_decodes_the_declared_types_from_binary():
    """asyncpg always asks for binary results and decodes them against the OIDs in
    RowDescription, so a wrong declared type surfaces as a wrong Python value."""
    async with serve(_session) as server:
        client = await asyncpg.connect(host="127.0.0.1", port=server.port, user="u", database="d")
        try:
            row = await client.fetchrow("SELECT id, name, joined, tags FROM users WHERE id = 1")
            assert dict(row) == {"id": 1, "name": "alice", "joined": datetime.date(2024, 1, 2), "tags": ["staff"]}
            assert await client.fetchval("SELECT total FROM orders WHERE id = 10") == Decimal("9.99")
        finally:
            await client.close()


# --- the catalog, for free -----------------------------------------------------------


def test_information_schema_lists_the_tables(conn):
    rows = conn.execute("SELECT table_name FROM information_schema.tables ORDER BY table_name").fetchall()
    assert rows == [("docs",), ("events",), ("notes",), ("orders",), ("users",)]


def test_information_schema_lists_the_columns_with_their_types(conn):
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'users' ORDER BY ordinal_position"
    ).fetchall()
    assert rows == [("id", "bigint"), ("name", "text"), ("joined", "date"), ("tags", "text[]")]


def test_a_declared_only_table_is_still_in_the_catalog(conn):
    """What `\\d events` reads: an empty table has to be as visible as a full one,
    which is the whole reason its columns must be declared."""
    rows = conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'events' ORDER BY ordinal_position"
    ).fetchall()
    assert rows == [("id", "bigint"), ("at", "timestamp")]


# --- refusals ------------------------------------------------------------------------


@pytest.mark.parametrize(
    argnames=["sql"],
    argvalues=[
        ("INSERT INTO users (id, name) VALUES (3, 'carol')",),
        ("UPDATE users SET name = 'carol' WHERE id = 1",),
        ("DELETE FROM users WHERE id = 1",),
    ],
)
def test_writes_are_refused_rather_than_applied(conn, sql):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        conn.execute(sql)
    assert conn.execute("SELECT count(*) FROM users").fetchone() == (2,)


def test_ddl_is_refused(conn):
    with pytest.raises(psycopg.errors.FeatureNotSupported):
        conn.execute("TRUNCATE users")


def test_an_unknown_table_is_reported_as_a_missing_relation(conn):
    """42P01 with Postgres's own wording: what a client's error handling matches
    on, and what qualify()'s "Column 'id' could not be resolved" would not be."""
    with pytest.raises(psycopg.errors.UndefinedTable, match='relation "nosuch" does not exist'):
        conn.execute("SELECT id FROM nosuch")


def test_an_unknown_column_is_reported_as_such(conn):
    with pytest.raises(psycopg.errors.UndefinedColumn):
        conn.execute("SELECT nope FROM users")


def test_a_table_in_another_schema_is_not_this_sessions_table(conn):
    """sqlglot's executor resolves `other.users` against `users` regardless of the
    schema, which would serve one schema's table as another's."""
    with pytest.raises(psycopg.errors.UndefinedTable, match='relation "other.users" does not exist'):
        conn.execute("SELECT id FROM other.users")
    assert conn.execute("SELECT id FROM public.users ORDER BY id").fetchall() == [(1,), (2,)]


def test_offset_is_refused_because_the_executor_ignores_it(conn):
    """The worst kind of gap: the executor parses OFFSET, drops it, and returns a
    full-looking result that is the wrong page. LIMIT alone it does honour."""
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="ignores OFFSET"):
        conn.execute("SELECT id FROM users ORDER BY id LIMIT 1 OFFSET 1")
    assert conn.execute("SELECT id FROM users ORDER BY id LIMIT 1").fetchall() == [(1,)]


def test_what_the_executor_cannot_run_fails_loudly(conn):
    """sqlglot's executor has no recursive CTEs. Answering the non-recursive part
    would look like a result and be wrong."""
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="recursive"):
        conn.execute("WITH RECURSIVE t AS (SELECT 1 AS n UNION ALL SELECT n + 1 FROM t WHERE n < 3) SELECT n FROM t")


def test_an_untypeable_expression_is_refused_not_guessed(conn):
    """A function sqlglot cannot type has no column type either, and TEXT would be
    a guess the client silently believes."""
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="cast"):
        conn.execute("SELECT generate_series(1, 3)")


# --- what has to be declared, refused at construction --------------------------------


def test_an_empty_table_must_declare_its_columns():
    with pytest.raises(ValueError, match="is empty, so its columns cannot be inferred"):
        TableSession({"events": []})


def test_an_all_null_column_must_declare_its_type():
    with pytest.raises(ValueError, match="no non-NULL value to infer a type from"):
        TableSession({"notes": [{"id": 1, "body": None}]})


def test_a_list_value_must_say_array_or_json():
    """The ambiguity ResultColumn.for_type refuses on, reached from the other
    side: there is a value to look at, and it still doesn't settle the type."""
    with pytest.raises(TypeError, match="equally a Postgres array or a json document"):
        TableSession({"users": [{"tags": ["a"]}]})


def test_a_column_of_mixed_types_is_refused():
    with pytest.raises(ValueError, match="more than one type"):
        TableSession({"t": [{"n": 1}, {"n": 2.5}]})


def test_tuple_rows_without_declared_names_are_refused():
    with pytest.raises(ValueError, match="column names must be declared"):
        TableSession({"users": [(1, "alice")]})


def test_a_tuple_row_of_the_wrong_width_is_refused():
    with pytest.raises(ValueError, match="1 value"):
        TableSession({"users": [(1,)]}, columns={"users": ["id", "name"]})


def test_a_dict_row_that_does_not_match_the_declared_names_is_refused():
    """A partial `columns` declaration adds to a dict table's columns rather than
    restricting them, so this only bites where the names came from the declaration
    alone -- a table given as tuples, with a stray dict row among them."""
    with pytest.raises(ValueError, match="do not include"):
        TableSession({"users": [(1, "alice"), {"nickname": "bob"}]}, columns={"users": ["id", "name"]})


def test_columns_naming_a_table_that_was_not_given_is_refused():
    with pytest.raises(ValueError, match="not in tables"):
        TableSession({"users": [{"id": 1}]}, columns={"orders": {"id": int}})


def test_a_type_sqlglot_cannot_be_told_about_is_refused_early():
    """An OID with no sqlglot spelling can't be described to the query engine, so
    no query over the table could work -- said at construction rather than per
    query, where it would look like a query bug."""
    with pytest.raises(ValueError, match="no equivalent in sqlglot's type system"):
        TableSession({"hosts": [{"addr": "10.0.0.1"}]}, columns={"hosts": {"addr": INET}})


def test_bare_list_and_dict_declarations_are_refused_as_ever():
    """types.oid_for_type's refusal, reached through columns=: the declaration has
    to name the element type or the json type."""
    with pytest.raises(TypeError, match="ambiguous"):
        TableSession({"docs": [{"body": {"a": 1}}]}, columns={"docs": {"body": dict}})


# --- the derived schema itself -------------------------------------------------------


async def test_schema_is_derived_from_the_tables():
    assert await _session().schema() == {
        "users": {"id": "bigint", "name": "text", "joined": "date", "tags": "text[]"},
        "orders": {"id": "bigint", "user_id": "bigint", "total": "numeric"},
        "events": {"id": "bigint", "at": "timestamp"},
        "notes": {"id": "bigint", "body": "text"},
        "docs": {"id": "bigint", "body": "jsonb"},
    }
