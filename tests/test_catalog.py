"""information_schema emulation, driven by whatever Session.schema() declares."""

from __future__ import annotations

import pytest


def test_information_schema_tables(conn, mock_session):
    async def schema():
        return {"users": {"id": "integer", "name": "text"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables ORDER BY table_name")
        assert cur.fetchall() == [("users",)]
    assert mock_session.queries == []


def test_information_schema_columns(conn, mock_session):
    async def schema():
        return {"users": {"id": "integer", "name": "text"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'users' ORDER BY ordinal_position"
        )
        assert cur.fetchall() == [("id", "integer"), ("name", "text")]


# --- what a catalog query that cannot run should say ---------------------------------
#
# The two halves of #39. An executor failure on an information_schema query is a
# pg_mimic gap, and answering it with no rows is a lie -- that is how #38 stayed
# hidden. The same failure on a pg_catalog query is psql asking after partitions or
# collations a mimic has none of, where empty is what makes \d and \l work at all.


def test_an_unrunnable_information_schema_query_is_an_error_not_no_rows(conn, mock_session):
    """Before #39 an executor failure came back as zero rows and a clean exit status.
    What matters is the *shape* of the answer, so the query needs a construct the
    executor cannot run -- and picking one is less obvious than it looks.

    Two vehicles have already rotted out from under this test: `length()`, which
    sqlglot implemented in
    https://redirect.github.com/tobymao/sqlglot/pull/8145 (released in v30.17.0),
    and `||`, implemented in
    https://redirect.github.com/tobymao/sqlglot/pull/8146. Anything on the #49/#58
    lists will go the same way, since those lists exist to be emptied.

    `pg_partition_ancestors` does not, and *not* because it is obscure. It is a
    Postgres catalog function rather than general SQL, so a generic SQL executor has
    no reason to grow one -- and pg_mimic has no partition tree for it to walk even
    if sqlglot did. It is also the vehicle the pg_catalog half of this pair already
    uses (`test_pg_catalog_stays_lenient_so_psql_keeps_working`), which is where it
    came from.

    The tempting alternatives are all worse, because they are wrong rather than
    fragile. `1/0`, `no_such_func()` and `CAST('users' AS INT)` do each raise in the
    executor -- but Postgres answers them `22012`, `42883` and `22P02`, and
    `0A000 feature_not_supported` means "Postgres does this and pg_mimic cannot".
    Pinning `0A000` to any of them would assert a wrong answer and leave a trap for
    whoever later maps one of those codes properly: they would break a test whose
    name says they broke unrunnable-query handling. A vehicle that never rots would
    only manage it by being permanently wrong.

    `length()` stays asserted below rather than dropped: it is why the sqlglot floor
    is 30.17.0, and a downgrade should surface as this failing rather than as
    introspection quietly going empty again."""
    import psycopg
    import pytest

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            cur.execute("SELECT pg_partition_ancestors('16384') FROM information_schema.tables")

        cur.execute("SELECT table_name, length(table_name) FROM information_schema.tables")
        rows = cur.fetchall()
        assert rows and all(length == len(name) for name, length in rows)


def test_an_unmodelled_information_schema_column_is_an_error(conn, mock_session):
    """This answered no rows until #66, which is what Postgres calls 42703.

    Empty is worse than an error for an ORM: it concludes the table has no such
    column rather than that pg_mimic cannot say. What kept it lenient was the width
    of the model rather than the principle -- #99 took both views to Postgres' full
    width, so a column reaching this branch is one Postgres does not have either.

    The message is Postgres' wording, not sqlglot's `Column 'x' could not be
    resolved. Line: 1, Col: 19` -- clients match on the former, and those line and
    column numbers point into SQL that pg_mimic rewrote before running, which the
    client never sent."""
    import psycopg
    import pytest

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.UndefinedColumn) as excinfo:
            cur.execute("SELECT nosuchcolumn FROM information_schema.tables")
        assert excinfo.value.sqlstate == "42703"
        assert 'column "nosuchcolumn" does not exist' in str(excinfo.value)


def test_pg_catalog_stays_lenient_about_an_unmodelled_column(conn, mock_session):
    r"""The other half of #66, and a deliberate divergence: real Postgres raises
    42703 here too.

    pg_catalog is a chosen slice, and psql asks after columns a mimic has no
    business modelling. Measured across the `\d` family nothing psql asks is
    unmodelled any more -- the failures that remain are sqlglot executor bugs
    (#58) -- so this branch now guards those. Making it strict before they land
    would break `\d` and `\l` outright."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT nosuchcolumn FROM pg_catalog.pg_class")
        assert cur.fetchall() == []


# --- information_schema at Postgres' width --------------------------------------------
#
# #99. `columns` carried 7 of Postgres' 44 and `tables` 4 of its 12, and a column that
# was not declared answered *empty* rather than erroring -- so a client asking for
# `column_default` was told the table has no columns at all, which an ORM reads as an
# empty table rather than as a gap in the mimic.
#
# The shape is now generated from a live server into pg_mimic/information_schema.json;
# the values stay in code, derived from Session.schema() where they can be and NULL
# where they cannot. Expected values throughout this section were read off PostgreSQL
# 18.4 for a table declared with the same types, not reasoned about -- which is how
# is_generated turned out to be 'NEVER' rather than the 'NO' #99 predicted.


_ISSUE_99_COLUMNS = {
    "character_maximum_length": None,
    "column_default": None,
    "datetime_precision": None,
    "is_generated": "NEVER",
    "is_identity": "NO",
    "numeric_precision": 32,
    "numeric_scale": 0,
    "udt_name": "int4",
}


@pytest.mark.parametrize(
    argnames=["column", "expected"],
    argvalues=sorted(_ISSUE_99_COLUMNS.items()),
    ids=sorted(_ISSUE_99_COLUMNS),
)
def test_a_column_issue_99_named_answers_about_the_columns_that_exist(conn, mock_session, column, expected):
    """Each of these came back as no rows at all, for a table that has a column.

    Asserted as a row rather than as `!= []`: a NULL that arrives *in a row* is the
    honest answer, and is the whole difference from the row never arriving."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute(f"SELECT column_name, {column} FROM information_schema.columns")
        assert cur.fetchall() == [("id", expected)]


# What Postgres reports about a column *because of its type*. One declared type per
# case, against every column it moves -- so a type whose facts are wrong fails on the
# facts rather than on whichever query happened to reach it.
_TYPE_DEPENDENT_COLUMNS = {
    "integer": {
        "character_octet_length": None,
        "numeric_precision": 32,
        "numeric_precision_radix": 2,
        "numeric_scale": 0,
        "datetime_precision": None,
        "udt_name": "int4",
    },
    "bigint": {
        "character_octet_length": None,
        "numeric_precision": 64,
        "numeric_precision_radix": 2,
        "numeric_scale": 0,
        "datetime_precision": None,
        "udt_name": "int8",
    },
    "double precision": {
        "character_octet_length": None,
        "numeric_precision": 53,
        # A float has a precision but no scale -- the two are not a pair.
        "numeric_precision_radix": 2,
        "numeric_scale": None,
        "datetime_precision": None,
        "udt_name": "float8",
    },
    "numeric": {
        "character_octet_length": None,
        # The only radix-10 type, and with no typmod there is no precision to report.
        "numeric_precision": None,
        "numeric_precision_radix": 10,
        "numeric_scale": None,
        "datetime_precision": None,
        "udt_name": "numeric",
    },
    "text": {
        # 2^30, Postgres' answer for any character type with no length limit --
        # which is every one pg_mimic serves, since a declared type carries no typmod.
        "character_octet_length": 1073741824,
        "numeric_precision": None,
        "numeric_precision_radix": None,
        "numeric_scale": None,
        "datetime_precision": None,
        "udt_name": "text",
    },
    "character varying": {
        "character_octet_length": 1073741824,
        "numeric_precision": None,
        "numeric_precision_radix": None,
        "numeric_scale": None,
        "datetime_precision": None,
        "udt_name": "varchar",
    },
    "date": {
        "character_octet_length": None,
        "numeric_precision": None,
        "numeric_precision_radix": None,
        "numeric_scale": None,
        # Whole seconds, where the rest of the datetime family is microseconds.
        "datetime_precision": 0,
        "udt_name": "date",
    },
    "timestamptz": {
        "character_octet_length": None,
        "numeric_precision": None,
        "numeric_precision_radix": None,
        "numeric_scale": None,
        "datetime_precision": 6,
        "udt_name": "timestamptz",
    },
    "boolean": {
        "character_octet_length": None,
        "numeric_precision": None,
        "numeric_precision_radix": None,
        "numeric_scale": None,
        "datetime_precision": None,
        "udt_name": "bool",
    },
    "text[]": {
        # Postgres reports none of these for an array, whatever its element type --
        # and names the array type itself, which is what makes udt_name worth having
        # while data_type still says `text[]`.
        "character_octet_length": None,
        "numeric_precision": None,
        "numeric_precision_radix": None,
        "numeric_scale": None,
        "datetime_precision": None,
        "udt_name": "_text",
    },
}


@pytest.mark.parametrize(
    argnames=["declared", "expected"],
    argvalues=[(declared, _TYPE_DEPENDENT_COLUMNS[declared]) for declared in sorted(_TYPE_DEPENDENT_COLUMNS)],
    ids=sorted(_TYPE_DEPENDENT_COLUMNS),
)
def test_the_type_dependent_columns_say_what_postgres_says_for_that_type(conn, mock_session, declared, expected):
    """One column in the table, so each result column is typed from its own value."""

    async def schema():
        return {"t": {"c": declared}}

    mock_session.schema = schema

    names = sorted(expected)
    with conn.cursor() as cur:
        cur.execute(f"SELECT {', '.join(names)} FROM information_schema.columns")
        assert dict(zip(names, cur.fetchone())) == expected


# PostgreSQL 18's ordinal_position order, written out rather than read back from the
# generated JSON: a test that compares the model against itself would pass just as
# happily on a file that had lost half its columns.
_POSTGRES_COLUMN_ORDER = {
    "tables": [
        "table_catalog",
        "table_schema",
        "table_name",
        "table_type",
        "self_referencing_column_name",
        "reference_generation",
        "user_defined_type_catalog",
        "user_defined_type_schema",
        "user_defined_type_name",
        "is_insertable_into",
        "is_typed",
        "commit_action",
    ],
    "columns": [
        "table_catalog",
        "table_schema",
        "table_name",
        "column_name",
        "ordinal_position",
        "column_default",
        "is_nullable",
        "data_type",
        "character_maximum_length",
        "character_octet_length",
        "numeric_precision",
        "numeric_precision_radix",
        "numeric_scale",
        "datetime_precision",
        "interval_type",
        "interval_precision",
        "character_set_catalog",
        "character_set_schema",
        "character_set_name",
        "collation_catalog",
        "collation_schema",
        "collation_name",
        "domain_catalog",
        "domain_schema",
        "domain_name",
        "udt_catalog",
        "udt_schema",
        "udt_name",
        "scope_catalog",
        "scope_schema",
        "scope_name",
        "maximum_cardinality",
        "dtd_identifier",
        "is_self_referencing",
        "is_identity",
        "identity_generation",
        "identity_start",
        "identity_increment",
        "identity_maximum",
        "identity_minimum",
        "identity_cycle",
        "is_generated",
        "generation_expression",
        "is_updatable",
    ],
}


@pytest.mark.parametrize(
    argnames=["view", "expected"],
    argvalues=[(view, _POSTGRES_COLUMN_ORDER[view]) for view in sorted(_POSTGRES_COLUMN_ORDER)],
    ids=sorted(_POSTGRES_COLUMN_ORDER),
)
def test_select_star_returns_postgres_column_order(conn, mock_session, view, expected):
    """A client that unpacks `SELECT *` positionally reads the wrong column otherwise,
    and never finds out. The generator checks the JSON against a live server; this
    checks that the executor still serves it in that order."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute(f"SELECT * FROM information_schema.{view}")
        assert [description.name for description in cur.description] == expected


def test_the_tables_view_carries_the_eight_columns_it_had_been_missing(conn, mock_session):
    """`is_insertable_into` and `is_typed` are what a client actually reads of the
    eight; the other six are NULL for every ordinary table, in Postgres too."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT is_insertable_into, is_typed FROM information_schema.tables")
        assert cur.fetchall() == [("YES", "NO")]

        cur.execute(
            "SELECT self_referencing_column_name, reference_generation, user_defined_type_catalog, "
            "user_defined_type_schema, user_defined_type_name, commit_action FROM information_schema.tables"
        )
        assert cur.fetchall() == [(None,) * 6]


def test_the_generated_shape_is_the_shape_the_catalog_declares():
    """The JSON is the only place the column list lives -- there is no second copy in
    catalog.py to drift from it -- so this pins the count that #99 was about."""
    from pg_mimic.catalog_data import INFORMATION_SCHEMA_SCHEMA

    views = INFORMATION_SCHEMA_SCHEMA["information_schema"]
    assert {view: len(columns) for view, columns in views.items()} == {"tables": 12, "columns": 44}


def test_pg_catalog_stays_lenient_so_psql_keeps_working(conn, mock_session):
    """psql's footer queries trip the executor on every \\d and \\l -- partitions,
    collations, statistics. Raising there would break the command outright, so
    pg_catalog keeps answering empty."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        # The shape psql sends for \d's foreign-key footer, which needs
        # pg_partition_ancestors -- a function the executor does not have.
        cur.execute(
            "SELECT conname FROM pg_catalog.pg_constraint c "
            "WHERE confrelid IN (SELECT pg_catalog.pg_partition_ancestors('16384')) AND contype = 'f'"
        )
        assert cur.fetchall() == []


# --- catalog columns psql asks for ----------------------------------------------------
#
# Each of these used to fail inside the executor and be caught by _empty_result, so
# a whole psql footer section came back blank. Added only where the answer is
# honest: a column on a table pg_mimic keeps empty costs nothing to declare, and
# the three on populated tables are either derived exactly (typarray, from
# arrays.ARRAY_OID) or a constant that is true of a mimic. See #66.


_CATALOG_COLUMNS = {
    "attstorage": "SELECT attstorage FROM pg_catalog.pg_attribute WHERE attname = 'id'",
    "typarray": "SELECT typarray FROM pg_catalog.pg_type WHERE typname = 'text'",
    "stxkind": "SELECT stxkind FROM pg_catalog.pg_statistic_ext",
    "connoinherit": "SELECT connoinherit FROM pg_catalog.pg_constraint",
    "inhdetachpending": "SELECT inhdetachpending FROM pg_catalog.pg_inherits",
    "rolsuper": "SELECT rolsuper FROM pg_catalog.pg_roles",
    "pubname": "SELECT pubname FROM pg_catalog.pg_publication",
    "prattrs": "SELECT prattrs FROM pg_catalog.pg_publication_rel",
}


@pytest.mark.parametrize(
    argnames=["column", "sql"],
    argvalues=sorted(_CATALOG_COLUMNS.items()),
    ids=sorted(_CATALOG_COLUMNS),
)
def test_a_catalog_column_psql_asks_for_can_be_queried(conn, mock_session, column, sql):
    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute(sql)
        cur.fetchall()  # runs rather than failing into an empty result


def test_typarray_points_at_the_real_array_type(conn, mock_session):
    """Derived from arrays.ARRAY_OID rather than invented, so it agrees with the
    OID the wire codecs would actually use for text[]."""
    from pg_mimic import TEXT
    from pg_mimic.arrays import ARRAY_OID

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT typarray FROM pg_catalog.pg_type WHERE typname = 'text'")
        assert cur.fetchone() == (ARRAY_OID[TEXT],)


# --- the declared-type round trip -----------------------------------------------------
#
# TableSession names each column's type for Session.schema(), and the catalog maps
# that name back to an OID; the two directions read different tables, so only a test
# holds them together. They had come apart: no array spelling and no `character` was
# in DECLARED_TYPE_OIDS, so a `tags text[]` column was catalogued as plain text (#43).
#
# Exhaustive over every name TableSession can emit rather than a handful of cases,
# since a handful is exactly what let those two through.


def _names_table_session_emits() -> dict[str, int]:
    from pg_mimic.arrays import ARRAY_OID
    from pg_mimic.tables import _PG_NAME, _pg_type_name

    # Both halves of what _pg_type_name can return: a scalar it has a name for, and
    # the array over one -- which is every scalar, arrays of arrays being no separate
    # type in Postgres.
    emitted = list(_PG_NAME) + [ARRAY_OID[oid] for oid in _PG_NAME if oid in ARRAY_OID]
    return {_pg_type_name(oid): oid for oid in emitted}


_TABLE_SESSION_TYPE_NAMES = _names_table_session_emits()


@pytest.mark.parametrize(
    argnames=["declared", "oid"],
    argvalues=[(name, _TABLE_SESSION_TYPE_NAMES[name]) for name in sorted(_TABLE_SESSION_TYPE_NAMES)],
    ids=sorted(_TABLE_SESSION_TYPE_NAMES),
)
def test_a_declared_type_maps_back_to_the_oid_it_was_named_from(declared, oid):
    from pg_mimic import oid_for_declared_type

    assert oid_for_declared_type(declared) == oid


def test_an_array_column_is_catalogued_as_the_array_type(conn, mock_session):
    """The user-visible half of #43. Any session declaring an array benefits, not
    just TableSession, so it is declared here as the free text a schema() returns."""
    from pg_mimic import TEXT
    from pg_mimic.arrays import ARRAY_OID

    async def schema():
        return {"users": {"id": "integer", "tags": "text[]"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT atttypid FROM pg_catalog.pg_attribute WHERE attname = 'tags'")
        assert cur.fetchone() == (ARRAY_OID[TEXT],)

        # The join psql's \d does not do but a client reading the catalog will:
        # pg_type has the row, and now the column points at it.
        cur.execute(
            "SELECT t.typname FROM pg_catalog.pg_attribute a "
            "JOIN pg_catalog.pg_type t ON t.oid = a.atttypid WHERE a.attname = 'tags'"
        )
        assert cur.fetchone() == ("_text",)


def test_backslash_l_lists_the_connected_database(conn, mock_session):
    """pg_database exists with one row -- the database from the startup packet --
    rather than being absent, which the executor reports as an unhelpful
    'NoneType' object has no attribute 'range_reader'."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT datname FROM pg_catalog.pg_database")
        assert cur.fetchall() == [("test",)]


def test_every_declared_catalog_column_has_a_value_in_every_row():
    """A column in the schema but absent from a row does not read as NULL -- the
    executor raises `KeyError` on the missing key, which surfaces as an
    ExecuteError. That is strictly worse than not declaring the column: on
    information_schema it is a 0A000 to the client rather than an empty result.

    So declaring a column and populating it are one step, and this is the guard.
    """
    from pg_mimic.catalog import _build_information_schema, _build_pg_catalog
    from pg_mimic.declared import resolve

    for schema, tables in (
        _build_pg_catalog(resolve({"t": {"id": "integer", "name": "text"}}), "testdb"),
        _build_information_schema(resolve({"t": {"id": "integer"}})),
    ):
        namespace = next(iter(schema))
        for table, columns in schema[namespace].items():
            for position, row in enumerate(tables[namespace].get(table, [])):
                missing = sorted(set(columns) - set(row))
                assert not missing, f"{table} row {position} declares but does not carry {missing}"


# --- column shape comes from the declared schema, not from row one (#101) -------------


def test_a_column_whose_first_row_is_null_still_describes_as_its_real_type(conn, mock_session):
    """`statement_from_rows` types each column from the first row -- the only place a
    session yielding bare rows can look. The catalog is not that: it builds the
    schema before it builds the rows, and used to drop it here.

    `character_octet_length` is NULL for an integer column and 1073741824 for a text
    one, so with the integer column first this described as text and sent the number
    out as the string '1073741824'."""
    from pg_mimic.types import INT4

    async def schema():
        return {"users": {"id": "integer", "name": "text"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name, character_octet_length FROM information_schema.columns "
            "WHERE table_name = 'users' ORDER BY ordinal_position"
        )
        rows = cur.fetchall()
        assert cur.description[1].type_code == INT4, "typed from row one, which is NULL here"
        assert rows == [("id", None), ("name", 1073741824)], "and the value arrives as a number"


def test_a_session_that_declares_no_schema_can_still_be_introspected(conn, mock_session):
    """`Session.schema()` returns None by default, and the `or {}` meant to guard that
    bound to the wrong branch -- so any session not overriding it crashed every
    catalog query with `'NoneType' object has no attribute 'items'`.

    Nothing caught it because every session in this suite either declares a schema or
    is TableSession, which always does."""

    async def schema():
        return None

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables")
        assert cur.fetchall() == []
        cur.execute("SELECT relname FROM pg_catalog.pg_class")
        assert cur.fetchall() == []
