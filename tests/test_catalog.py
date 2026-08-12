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
    """`||` is missing from sqlglot's executor (#38). Before this it came back as
    zero rows and a clean exit status.

    `length()` was missing too until sqlglot v30.17.0, which is part of why the floor
    is 30.17.0. It is asserted here rather than dropped, so a downgrade shows up as
    this test failing rather than as introspection quietly going empty again."""
    import psycopg
    import pytest

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            cur.execute("SELECT table_name || '!' FROM information_schema.tables")

        cur.execute("SELECT table_name, length(table_name) FROM information_schema.tables")
        rows = cur.fetchall()
        assert rows and all(length == len(name) for name, length in rows)


def test_an_unmodelled_information_schema_column_is_still_empty(conn, mock_session):
    """Pinning current behaviour, not endorsing it: Postgres answers 42703 here.

    It stays lenient only because the model is too thin to be strict against --
    information_schema.columns carries 7 of Postgres' ~44 -- so raising would turn
    ordinary introspection into errors. See #66; this test flips with it."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        cur.execute("SELECT nosuchcolumn FROM information_schema.tables")
        assert cur.fetchall() == []


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

    for schema, tables in (
        _build_pg_catalog({"t": {"id": "integer", "name": "text"}}, "testdb"),
        _build_information_schema({"t": {"id": "integer"}}),
    ):
        namespace = next(iter(schema))
        for table, columns in schema[namespace].items():
            for position, row in enumerate(tables[namespace].get(table, [])):
                missing = sorted(set(columns) - set(row))
                assert not missing, f"{table} row {position} declares but does not carry {missing}"
