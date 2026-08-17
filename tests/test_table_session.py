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
from sqlglot.executor import execute as sqlglot_execute

import pg_mimic.tables as tables_module
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


@pytest.mark.parametrize(
    argnames=["text"],
    argvalues=[("1.5",), ("1_0",), ("0x10",), ("inf",), ("nan",)],
)
def test_a_parameter_that_is_not_an_integer_for_an_integer_column_is_refused(conn, text):
    """Postgres raises 22P02 for every one of these against an integer column.
    float() takes them all, and the query then answers with no rows -- or with the
    rows for a number the client never sent."""
    with pytest.raises(psycopg.errors.InvalidTextRepresentation):
        conn.execute("SELECT id FROM users WHERE id = %s::bigint", (text,))


def test_a_parameter_that_is_not_a_boolean_is_refused(conn):
    """Read as false it would return the false rows, which is an answer to a query
    nobody wrote. Postgres raises 22P02."""
    flags = {"flags": [{"id": 1, "enabled": True}, {"id": 2, "enabled": False}]}
    with serve_in_thread(lambda: TableSession(flags)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as flag_conn:
            with pytest.raises(psycopg.errors.InvalidTextRepresentation):
                flag_conn.execute("SELECT id FROM flags WHERE enabled = %s::boolean", ("banana",))


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


# --- numbers ---------------------------------------------------------------------------
#
# Two ways a number went wrong, both measured against a real PostgreSQL 18: a decimal
# constant compared as a float, which missed rows it should have matched, and every
# integer expression described as int4 however wide it was.

PRICES = {"prices": [{"id": 1, "amount": Decimal("9.99")}, {"id": 2, "amount": Decimal("10.00")}]}


@pytest.fixture
def prices():
    with serve_in_thread(lambda: TableSession(PRICES)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            yield conn


# Postgres types an unadorned decimal constant as numeric and compares it exactly, so
# it matches each of these rows. Only `= 10.00` matched here before, 10.0 being the
# one value of the two a binary float represents exactly -- which is what made the
# bug intermittent rather than a flat failure.
_exact_testcases = {
    "a_value_no_float_represents": {"literal": "9.99", "expected": 1},
    "a_value_float_gets_right": {"literal": "10.00", "expected": 2},
    "trailing_zeros_do_not_count": {"literal": "9.990", "expected": 1},
    "a_cast_written_out": {"literal": "'9.99'::numeric", "expected": 1},
    "a_cast_of_a_whole_number": {"literal": "CAST(10 AS DECIMAL)", "expected": 2},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_exact_testcases.values()))),
    argvalues=[[v for k, v in sorted(_exact_testcases[name].items())] for name in sorted(_exact_testcases)],
    ids=sorted(_exact_testcases),
)
def test_a_decimal_comparison_is_exact(prices, literal, expected):
    assert prices.execute(f"SELECT id FROM prices WHERE amount = {literal}").fetchall() == [(expected,)]


def test_a_decimal_comparison_is_exact_the_other_way_round_too(prices):
    """The literal on the left, and an inequality rather than an equality -- the
    rewrite reads a comparison's operands rather than assuming which side is which."""
    assert prices.execute("SELECT id FROM prices WHERE 9.99 >= amount").fetchall() == [(1,)]
    assert prices.execute("SELECT id FROM prices WHERE amount BETWEEN 9.98 AND 9.999").fetchall() == [(1,)]
    assert prices.execute("SELECT id FROM prices WHERE amount IN (9.99, 1.11)").fetchall() == [(1,)]


def test_a_numeric_cast_is_answered_rather_than_raising(prices):
    """sqlglot's executor sends every DECIMAL cast through `int()`, so `::numeric`
    raised ValueError and `CAST(9.99 AS DECIMAL)` was 9. Postgres gives an exact 9.99
    for both, and the CAST override in tables.py makes them exact here."""
    assert prices.execute("SELECT '9.99'::numeric").fetchall() == [(Decimal("9.99"),)]
    assert prices.execute("SELECT CAST(9.99 AS DECIMAL)").fetchall() == [(Decimal("9.99"),)]
    assert prices.execute("SELECT CAST(amount AS NUMERIC) FROM prices WHERE id = 1").fetchall() == [(Decimal("9.99"),)]


def test_decimal_arithmetic_stays_exact(prices):
    """The reason the fix is a numeric cast rather than a float on both sides: a
    column describe() calls NUMERIC has to multiply and sum as numeric too."""
    cursor = prices.execute("SELECT amount * 2 FROM prices WHERE amount = 9.99")
    assert _types(cursor) == [NUMERIC]
    assert cursor.fetchall() == [(Decimal("19.98"),)]
    assert prices.execute("SELECT sum(amount) FROM prices").fetchall() == [(Decimal("19.99"),)]


def test_a_decimal_literal_against_a_double_precision_column_is_left_alone(conn):
    """`Decimal * float` raises TypeError in Python, so making every decimal literal
    a Decimal would break a float8 column where it works today. The rewrite is scoped
    to comparisons whose other side is already numeric; this one is not."""
    tables = {"readings": [{"v": 9.99}, {"v": 2.5}]}
    with serve_in_thread(lambda: TableSession(tables)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as floats:
            assert floats.execute("SELECT v FROM readings WHERE v = 9.99").fetchall() == [(9.99,)]
            assert floats.execute("SELECT v * 1.5 FROM readings WHERE v = 2.5").fetchall() == [(3.75,)]


def test_a_numeric_parameter_is_exact_too(prices):
    """A bind parameter becomes a literal for the executor, so it had the identical
    problem -- and the promise is that a parameterised query answers like a literal
    one."""
    assert prices.execute("SELECT id FROM prices WHERE amount = %s", (Decimal("9.99"),)).fetchall() == [(1,)]
    assert prices.execute("SELECT id FROM prices WHERE amount = %s", (Decimal("10.00"),)).fetchall() == [(2,)]


# What Postgres calls each of these, read off `pg_typeof`. sqlglot's annotator types
# every integer literal INT, so all of them described as int4 until the literal was
# sized ahead of it.
_width_testcases = {
    "int4_max": {"expr": "2147483647", "type_name": "int4", "expected": 2147483647},
    "one_past_int4_max": {"expr": "2147483648", "type_name": "int8", "expected": 2147483648},
    # A negation is sized by what it evaluates to, however many of them there are.
    "int4_min": {"expr": "-2147483648", "type_name": "int4", "expected": -2147483648},
    "negated_back_out_of_int4": {"expr": "- -2147483648", "type_name": "int8", "expected": 2147483648},
    "int8_max": {"expr": "9223372036854775807", "type_name": "int8", "expected": 9223372036854775807},
    "int8_min": {"expr": "-9223372036854775808", "type_name": "int8", "expected": -9223372036854775808},
    "past_int8_max": {"expr": "9223372036854775808", "type_name": "numeric", "expected": Decimal("9223372036854775808")},
    "width_carries_through_arithmetic": {"expr": "3000000000 + 0", "type_name": "int8", "expected": 3000000000},
    "narrow_arithmetic_stays_narrow": {"expr": "1 + 1", "type_name": "int4", "expected": 2},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_width_testcases.values()))),
    argvalues=[[v for k, v in sorted(_width_testcases[name].items())] for name in sorted(_width_testcases)],
    ids=sorted(_width_testcases),
)
async def test_an_integer_literal_is_as_wide_as_postgres_makes_it(expr, type_name, expected):
    """Through asyncpg, because psycopg reads results as text and cannot see this at
    all: a bigint declared int4 crashes asyncpg's binary decoder with "'i' format
    requires -2147483648 <= number <= 2147483647" rather than returning a wrong
    value."""
    async with serve(_session) as server:
        client = await asyncpg.connect(host="127.0.0.1", port=server.port, user="u", database="d")
        try:
            statement = await client.prepare(f"SELECT {expr}")
            assert [attribute.type.name for attribute in statement.get_attributes()] == [type_name]
            assert await statement.fetchval() == expected
        finally:
            await client.close()


async def test_a_declared_integer_column_still_describes_as_int4():
    """The round trip sizing the literals must not disturb: a table whose real column
    is `integer` says so, where `int` infers to bigint as it does everywhere else in
    pg_mimic. Remapping INT to INT8 in _TYPES is the one-line fix that breaks this."""
    tables = {"counters": [{"narrow": 1, "wide": 1}]}
    columns = {"counters": {"narrow": INT4, "wide": int}}
    async with serve(lambda: TableSession(tables, columns=columns)) as server:
        client = await asyncpg.connect(host="127.0.0.1", port=server.port, user="u", database="d")
        try:
            statement = await client.prepare("SELECT narrow, wide FROM counters")
            assert [attribute.type.name for attribute in statement.get_attributes()] == ["int4", "int8"]
        finally:
            await client.close()


async def test_the_hand_built_executor_answers_what_sqlglots_own_does():
    """TableSession runs the executor itself rather than calling
    `sqlglot.executor.execute()`, which takes no `env=` -- and the env is where the
    exact numeric CAST lives. That means replicating the public function's body, so
    this is the tripwire for the day those internals move: same query, same rows,
    both ways. A drift shows up here rather than as wrong rows.
    """
    session = _session()
    sql = "SELECT u.name, count(*) AS n FROM users AS u JOIN orders AS o ON o.user_id = u.id GROUP BY u.name"
    expression = session._plan(sql).expression
    ours = [tuple(row) for row in tables_module._execute(expression, session._sqlglot_schema, session._rows).rows]
    theirs = [tuple(row) for row in sqlglot_execute(expression, schema=session._sqlglot_schema, tables=session._rows).rows]
    assert ours == theirs == [("alice", 2)]


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


# --- identifier folding --------------------------------------------------------------
#
# A dict key is the identifier as written, so it is what a *quoted* reference
# matches, and an unquoted reference folds to lower case before anything looks it
# up. Every case below was run against PostgreSQL 18 first.

MIXED_CASE = {"Users": [{"Id": 1, "userName": "alice"}]}


@pytest.fixture
def mixed_conn():
    with serve_in_thread(lambda: TableSession(MIXED_CASE)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            yield conn


@pytest.mark.parametrize(argnames=["name"], argvalues=[("users",), ("Users",), ("USERS",), ('"users"',)])
def test_a_lower_case_table_is_found_however_the_query_capitalises_it(conn, name):
    """The case that will actually be hit: SQL written with capitalised table
    names, which Postgres folds down to the lower-case relation."""
    assert conn.execute(f"SELECT count(*) FROM {name}").fetchone() == (2,)


def test_a_quoted_name_does_not_reach_a_lower_case_table(conn):
    """`FROM "Users"` is a different relation from `users`, and Postgres says so
    rather than resolving it -- 42P01, not a column complaint from further in."""
    with pytest.raises(psycopg.errors.UndefinedTable, match='relation "Users" does not exist'):
        conn.execute('SELECT id FROM "Users"')


@pytest.mark.parametrize(argnames=["column"], argvalues=[("name",), ("Name",), ("NAME",), ('"name"',)])
def test_a_lower_case_column_is_found_however_the_query_capitalises_it(conn, column):
    assert conn.execute(f"SELECT {column} FROM users ORDER BY id").fetchall() == [("alice",), ("bob",)]


def test_a_quoted_name_does_not_reach_a_lower_case_column(conn):
    with pytest.raises(psycopg.errors.UndefinedColumn):
        conn.execute('SELECT "Name" FROM users')


def test_a_mixed_case_table_is_reached_by_its_quoted_name(mixed_conn):
    """#42: the key `Users` is what `CREATE TABLE "Users"` makes, so the quoted
    reference is the one that finds it -- and finds its mixed-case columns too."""
    cursor = mixed_conn.execute('SELECT "Id", "userName" FROM "Users"')
    assert cursor.fetchall() == [(1, "alice")]
    assert _names(cursor) == ["Id", "userName"]


def test_star_over_a_mixed_case_table_keeps_the_declared_names(mixed_conn):
    """Not `could not derive a Postgres type for the output column '*'`: the star
    expands against a schema whose columns the query can actually name."""
    cursor = mixed_conn.execute('SELECT * FROM "Users"')
    assert cursor.fetchall() == [(1, "alice")]
    assert _names(cursor) == ["Id", "userName"]


def test_an_unquoted_reference_does_not_reach_a_mixed_case_table(mixed_conn):
    """It folds to `users`, which is not what was declared -- and the error names
    the folded relation, as Postgres's does."""
    with pytest.raises(psycopg.errors.UndefinedTable, match='relation "users" does not exist'):
        mixed_conn.execute('SELECT "Id" FROM Users')


def test_an_unquoted_reference_does_not_reach_a_mixed_case_column(mixed_conn):
    with pytest.raises(psycopg.errors.UndefinedColumn):
        mixed_conn.execute('SELECT Id FROM "Users"')


def test_an_output_column_name_folds_like_any_other_identifier(conn):
    cursor = conn.execute('SELECT id AS Alias, name AS "Kept" FROM users')
    assert _names(cursor) == ["alias", "Kept"]


def test_the_catalog_reports_the_declared_names(mixed_conn):
    """Whatever the keys say, verbatim: `\\d` and information_schema describe the
    table that was declared, not a folded version of it."""
    rows = mixed_conn.execute("SELECT table_name FROM information_schema.tables").fetchall()
    assert rows == [("Users",)]
    rows = mixed_conn.execute(
        "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'Users' ORDER BY ordinal_position"
    ).fetchall()
    assert rows == [("Id", "bigint"), ("userName", "text")]


# --- the derived schema itself -------------------------------------------------------


async def test_schema_is_derived_from_the_tables():
    """A Schema since #126, where this asserted the nested dict it returned before.

    `column_types()` is the same declaration in the old shape, so this stays an
    assertion about the derived *types* rather than about the container.
    """
    schema = await _session().schema()
    assert schema.column_types() == {
        "users": {"id": "bigint", "name": "text", "joined": "date", "tags": "text[]"},
        "orders": {"id": "bigint", "user_id": "bigint", "total": "numeric"},
        "events": {"id": "bigint", "at": "timestamp"},
        "notes": {"id": "bigint", "body": "text"},
        "docs": {"id": "bigint", "body": "jsonb"},
    }
    # Order is load-bearing -- it is what decides each table's OID.
    assert list(schema.tables) == ["users", "orders", "events", "notes", "docs"]


# --- what the executor answers wrongly, repaired --------------------------------------
#
# sqlglot's executor is not a complete engine, and its gaps are not all failures:
# several clauses it parses and then answers wrongly, which is the dangerous kind.
# Where the right answer is reachable -- by rewriting the query into a shape it does
# get right, or by finishing the job on the rows it returns -- TableSession does
# that, and the tests below are the evidence. Every expected value here was taken
# from a real PostgreSQL 18 running the same query over the same rows.

SEMANTICS = {
    "items": [{"id": i, "label": c} for i, c in zip(range(1, 6), "abcde")],
    # Two NULLs, so ordering has to place a group rather than a single row.
    "scores": [
        {"name": "ana", "score": 3},
        {"name": "bo", "score": None},
        {"name": "cy", "score": 1},
        {"name": "di", "score": None},
    ],
    "visits": [
        {"user_id": 1, "at": 10, "page": "home"},
        {"user_id": 1, "at": 20, "page": "docs"},
        {"user_id": 2, "at": 15, "page": "home"},
        {"user_id": 2, "at": 5, "page": "pricing"},
        {"user_id": 3, "at": 7, "page": "home"},
    ],
    "blocked": [{"user_id": 2}],
    "blocked_with_null": [{"user_id": 2}, {"user_id": None}],
}


@pytest.fixture
def semantics():
    with serve_in_thread(lambda: TableSession(SEMANTICS)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            yield conn


def test_offset_pages_through_the_rows(semantics):
    """The executor drops OFFSET and returns the first page for every page asked
    for, so this is applied to the rows it returns instead -- along with the LIMIT,
    which it would otherwise have counted before any row was skipped."""
    assert semantics.execute("SELECT id FROM items ORDER BY id LIMIT 2 OFFSET 1").fetchall() == [(2,), (3,)]
    assert semantics.execute("SELECT id FROM items ORDER BY id OFFSET 3").fetchall() == [(4,), (5,)]
    assert semantics.execute("SELECT id FROM items ORDER BY id LIMIT 2 OFFSET 9").fetchall() == []
    assert semantics.execute("SELECT id FROM items ORDER BY id LIMIT 2").fetchall() == [(1,), (2,)]


def test_parentheses_round_the_whole_query_do_not_hide_its_clauses(semantics):
    """`(SELECT ...) LIMIT 1` parses as a Subquery carrying the LIMIT, which the
    executor ignores and which is not where the top-level clauses are looked for --
    so every row came back where one was asked for."""
    assert semantics.execute("(SELECT id FROM items) LIMIT 2").fetchall() == [(1,), (2,)]
    assert semantics.execute("(SELECT id FROM items ORDER BY id DESC) LIMIT 2").fetchall() == [(5,), (4,)]
    assert semantics.execute("(SELECT id FROM items ORDER BY id) OFFSET 3").fetchall() == [(4,), (5,)]
    # The two forms whose ORDER BY is sorted here rather than by the executor.
    union = "(SELECT id FROM items UNION SELECT id FROM items) ORDER BY id DESC LIMIT 2"
    assert semantics.execute(union).fetchall() == [(5,), (4,)]
    distinct = "(SELECT DISTINCT id FROM items ORDER BY id DESC) LIMIT 2"
    assert semantics.execute(distinct).fetchall() == [(5,), (4,)]


def test_a_parenthesized_query_with_its_own_row_window_is_refused(semantics):
    """Two windows, one of them nested -- there is nothing to fold, and the inner
    one would be applied to rows this session never sees."""
    with pytest.raises(psycopg.errors.FeatureNotSupported):
        semantics.execute("(SELECT id FROM items LIMIT 3) LIMIT 1")


def test_distinct_on_a_json_column_is_answered_rather_than_crashing(conn):
    """The DISTINCT ON key goes in a set, and a json document is a dict, which does
    not hash -- it used to reach the client as an internal_error."""
    assert conn.execute("SELECT DISTINCT ON (body) id FROM docs ORDER BY body, id").fetchall() == [(1,)]


def test_offset_takes_a_bind_parameter(semantics):
    """A paging query is the reason OFFSET exists, and a client writes its page
    size as a parameter -- which is only a row count once it has been bound."""
    assert semantics.execute("SELECT id FROM items ORDER BY id LIMIT %s OFFSET %s", (2, 2)).fetchall() == [(3,), (4,)]


def test_descending_order_puts_nulls_where_postgres_puts_them(semantics):
    """Postgres sorts NULLS FIRST descending. The executor sorts with Python's own
    comparisons, which raise TypeError on None rather than ordering it, so the
    query is rewritten to settle the NULLs before any value is compared."""
    rows = semantics.execute("SELECT name, score FROM scores ORDER BY score DESC, name").fetchall()
    assert rows == [("bo", None), ("di", None), ("ana", 3), ("cy", 1)]


def test_ascending_order_still_puts_nulls_last(semantics):
    """The case the executor already got right, which the rewrite must not disturb."""
    rows = semantics.execute("SELECT name, score FROM scores ORDER BY score ASC, name").fetchall()
    assert rows == [("cy", 1), ("ana", 3), ("bo", None), ("di", None)]


def test_nulls_first_and_nulls_last_are_honoured(semantics):
    """Spelled out, against the default for each direction, so the rewrite is
    reading the query rather than the direction."""
    ascending = semantics.execute("SELECT name FROM scores ORDER BY score ASC NULLS FIRST, name").fetchall()
    assert ascending == [("bo",), ("di",), ("cy",), ("ana",)]
    descending = semantics.execute("SELECT name FROM scores ORDER BY score DESC NULLS LAST, name").fetchall()
    assert descending == [("ana",), ("cy",), ("bo",), ("di",)]


def test_not_in_a_subquery_filters(semantics):
    """The executor returns every row for `NOT IN (subquery)` -- a WHERE clause
    that is not approximate but inert. It runs NOT EXISTS correctly, so that is
    what the query becomes."""
    rows = semantics.execute("SELECT id FROM items WHERE id NOT IN (SELECT user_id FROM blocked) ORDER BY id")
    assert rows.fetchall() == [(1,), (3,), (4,), (5,)]


def test_not_in_a_subquery_holding_null_matches_nothing(semantics):
    """SQL's rule, which no anti-join carries on its own: one NULL in the subquery
    makes the predicate unknown for every row, and the result is empty."""
    rows = semantics.execute("SELECT id FROM items WHERE id NOT IN (SELECT user_id FROM blocked_with_null)")
    assert rows.fetchall() == []


def test_in_a_subquery_is_left_alone(semantics):
    """Only the negated form is broken; the positive one the executor gets right,
    NULL in the subquery and all."""
    rows = semantics.execute("SELECT id FROM items WHERE id IN (SELECT user_id FROM blocked_with_null)")
    assert rows.fetchall() == [(2,)]


def test_several_not_ins_in_one_query_stay_separate(semantics):
    """Each rewrite brings its subquery in as a derived table, and the executor
    keys its plan steps by name -- so two of them sharing an alias would answer as
    one. The nested case also has to be rewritten innermost first, or the inner
    NOT IN is a node the outer one's copy has already left behind."""
    both = semantics.execute(
        "SELECT id FROM items WHERE id NOT IN (SELECT user_id FROM blocked) "
        "AND id NOT IN (SELECT user_id FROM visits WHERE at > 8) ORDER BY id"
    )
    assert both.fetchall() == [(3,), (4,), (5,)]
    nested = semantics.execute(
        "SELECT id FROM items WHERE id NOT IN "
        "(SELECT user_id FROM visits WHERE user_id NOT IN (SELECT user_id FROM blocked)) ORDER BY id"
    )
    assert nested.fetchall() == [(2,), (4,), (5,)]


def test_not_in_a_list_is_left_alone(semantics):
    assert semantics.execute("SELECT id FROM items WHERE id NOT IN (2, 3) ORDER BY id").fetchall() == [(1,), (4,), (5,)]


def test_distinct_on_keeps_the_first_row_per_key(semantics):
    """Parsed and then ignored by the executor, which has no window functions to
    rewrite it with either -- so the first row per key is taken from the rows it
    returns, which its ORDER BY has already put in Postgres's order."""
    rows = semantics.execute("SELECT DISTINCT ON (user_id) user_id, at, page FROM visits ORDER BY user_id, at DESC").fetchall()
    assert rows == [(1, 20, "docs"), (2, 15, "home"), (3, 7, "home")]


def test_distinct_on_expands_star_and_takes_several_keys(semantics):
    assert semantics.execute("SELECT DISTINCT ON (user_id) * FROM visits ORDER BY user_id, at").fetchall() == [
        (1, 10, "home"),
        (2, 5, "pricing"),
        (3, 7, "home"),
    ]
    assert semantics.execute(
        "SELECT DISTINCT ON (user_id, page) user_id, page FROM visits ORDER BY user_id, page, at"
    ).fetchall() == [(1, "docs"), (1, "home"), (2, "home"), (2, "pricing"), (3, "home")]


def test_distinct_on_a_key_that_is_not_selected(semantics):
    """Postgres allows it, so the key is added to the select list to deduplicate on
    and dropped from every row again -- including from what describe() reports,
    which is what the client sizes its result by."""
    cursor = semantics.execute("SELECT DISTINCT ON (user_id) page FROM visits ORDER BY user_id, at DESC")
    assert _names(cursor) == ["page"]
    assert cursor.fetchall() == [("docs",), ("home",), ("home",)]


def test_limit_counts_the_rows_distinct_on_left(semantics):
    """Postgres deduplicates first and limits what survives. A LIMIT the executor
    applied itself would count the duplicates instead."""
    sql = "SELECT DISTINCT ON (user_id) user_id, at FROM visits ORDER BY user_id, at DESC"
    assert semantics.execute(f"{sql} LIMIT 2").fetchall() == [(1, 20), (2, 15)]
    assert semantics.execute(f"{sql} LIMIT 2 OFFSET 1").fetchall() == [(2, 15), (3, 7)]


def test_both_branches_of_a_set_operation_run(semantics):
    """The executor keys its plan steps by table name, so two branches over the
    same table collided and the second silently ran the first one's plan --
    answering `id = 1` twice. Distinct aliases per table reference keep them apart."""
    rows = semantics.execute("SELECT id FROM items WHERE id = 1 UNION ALL SELECT id FROM items WHERE id = 3")
    assert sorted(rows.fetchall()) == [(1,), (3,)]
    assert semantics.execute("SELECT id FROM items WHERE id < 3 EXCEPT SELECT id FROM items WHERE id < 2").fetchall() == [(2,)]
    assert semantics.execute("SELECT id FROM items WHERE id < 3 INTERSECT SELECT id FROM items WHERE id > 1").fetchall() == [(2,)]


def test_a_set_operation_can_be_ordered(semantics):
    """Asked to sort a UNION the executor returns one empty tuple per row -- the
    rows are all there and every column has gone. Postgres only lets such an ORDER
    BY name output columns, so the sort is done here, on the result."""
    assert semantics.execute(
        "SELECT id FROM items WHERE id < 3 UNION SELECT id FROM items WHERE id > 3 ORDER BY id"
    ).fetchall() == [(1,), (2,), (4,), (5,)]
    assert semantics.execute(
        "SELECT label FROM items WHERE id < 3 UNION ALL SELECT label FROM items WHERE id > 3 ORDER BY label DESC"
    ).fetchall() == [("e",), ("d",), ("b",), ("a",)]


def test_an_ordered_set_operation_places_nulls_and_pages(semantics):
    rows = semantics.execute(
        "SELECT name, score FROM scores UNION ALL SELECT name, score FROM scores "
        "ORDER BY score DESC NULLS LAST, name LIMIT 3 OFFSET 1"
    ).fetchall()
    assert rows == [("ana", 3), ("cy", 1), ("cy", 1)]


# --- what cannot be repaired, refused loudly ------------------------------------------


def test_a_full_outer_join_keeps_the_unmatched_rows():
    """Refused until sqlglot 30.15.0, which stopped running it as an inner join --
    the reason pyproject pins that floor.

    Its own tables, so that each side has a row the other cannot match: an inner
    join would answer with only `(2, 2)`, and a left join would miss `(None, 9)`.
    """
    tables = {"lhs": [{"a": 1}, {"a": 2}], "rhs": [{"b": 2}, {"b": 9}]}
    with serve_in_thread(lambda: TableSession(tables)) as server:
        with psycopg.Connection.connect(server.dsn(), autocommit=True) as conn:
            rows = conn.execute(
                "SELECT l.a, r.b FROM lhs l FULL OUTER JOIN rhs r ON l.a = r.b ORDER BY l.a NULLS LAST, r.b"
            ).fetchall()
    assert rows == [(1, None), (2, 2), (None, 9)]


def test_tablesample_is_refused(semantics):
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="ignores TABLESAMPLE"):
        semantics.execute("SELECT id FROM items TABLESAMPLE BERNOULLI (50)")


def test_an_offset_the_result_cannot_carry_is_refused(semantics):
    """OFFSET is applied to the rows the executor returns, which reaches the whole
    query's rows and nothing else. One inside a subquery would be silently dropped
    as before, so it stays refused rather than half-supported."""
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="Lift it to the outermost SELECT"):
        semantics.execute("SELECT id FROM (SELECT id FROM items ORDER BY id OFFSET 2) AS page")


def test_a_distinct_on_the_result_cannot_carry_is_refused(semantics):
    with pytest.raises(psycopg.errors.FeatureNotSupported, match="Lift it to the outermost SELECT"):
        semantics.execute("SELECT user_id FROM (SELECT DISTINCT ON (user_id) user_id FROM visits ORDER BY user_id) AS first_visits")


def test_distinct_on_that_postgres_itself_rejects_is_rejected(semantics):
    """Postgres requires the ORDER BY to begin with the DISTINCT ON expressions.
    Answering a query it would have refused is its own kind of wrong."""
    with pytest.raises(psycopg.errors.SyntaxError, match="must match initial ORDER BY"):
        semantics.execute("SELECT DISTINCT ON (label) id FROM items ORDER BY id")


def test_plain_select_distinct_is_untouched(semantics):
    """The executor applies a plain DISTINCT correctly; only DISTINCT ON is
    finished by hand, so the refusals above must not catch this."""
    assert semantics.execute("SELECT DISTINCT page FROM visits ORDER BY page").fetchall() == [
        ("docs",),
        ("home",),
        ("pricing",),
    ]


def test_a_select_distinct_can_be_ordered_descending(semantics):
    """A SELECT DISTINCT is sorted by its select list alone, which the `IS NULL`
    key that places NULLs is deliberately not part of -- so adding that key left
    the rows ascending. Ordered here instead, for the same reason a UNION is."""
    assert semantics.execute("SELECT DISTINCT user_id FROM visits ORDER BY user_id DESC").fetchall() == [
        (3,),
        (2,),
        (1,),
    ]
    assert semantics.execute("SELECT DISTINCT score FROM scores ORDER BY score DESC").fetchall() == [
        (None,),
        (3,),
        (1,),
    ]
