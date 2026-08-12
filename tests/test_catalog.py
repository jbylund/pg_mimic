"""information_schema emulation, driven by whatever Session.schema() declares."""

from __future__ import annotations


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
    """`||` and `length()` are missing from sqlglot's executor (#38). Before this,
    both came back as zero rows and a clean exit status."""
    import psycopg
    import pytest

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        for sql in (
            "SELECT table_name || '!' FROM information_schema.tables",
            "SELECT length(table_name) FROM information_schema.tables",
        ):
            with pytest.raises(psycopg.errors.FeatureNotSupported):
                cur.execute(sql)


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
