"""End-to-end against the real psql binary.

pg_catalog exists so that psql works, so the test that matters runs psql. Unit
tests over the same queries can pass while psql still fails -- that happened
repeatedly building this: a query would execute and return rows, and psql would
still report "column number 1 is out of range" because a *different* query in the
same command had quietly fallen through.

Skipped when psql isn't installed, so it's a bonus locally and on any CI image
that has it, not a hard dependency.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from pg_mimic import ResultColumn, Schema, Session, Table
from pg_mimic.testing import serve_in_thread

psql_required = pytest.mark.skipif(shutil.which("psql") is None, reason="psql is not installed")

SCHEMA = {
    "users": {"id": "integer", "name": "text", "email": "text"},
    "orders": {"id": "bigint", "total": "numeric", "placed_at": "timestamp"},
}


class SchemaSession(Session):
    async def schema(self):
        return SCHEMA

    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("x", str)]

    async def query(self, sql, params):
        yield ("row",)


def _psql(command, session=None):
    with serve_in_thread(session or SchemaSession) as server:
        return subprocess.run(
            ["psql", server.dsn(user="test", dbname="test"), "-c", command],
            capture_output=True,
            text=True,
            timeout=20,
        )


@psql_required
def test_list_tables():
    result = _psql("\\dt")
    assert result.stderr.strip() == "", result.stderr
    assert "users" in result.stdout and "orders" in result.stdout
    assert "table" in result.stdout


@psql_required
def test_describe_table_lists_columns_with_types():
    result = _psql("\\d users")
    assert result.stderr.strip() == "", result.stderr
    for line in ("id", "integer", "name", "text", "email"):
        assert line in result.stdout, f"{line!r} missing from:\n{result.stdout}"


@psql_required
def test_describe_table_maps_declared_types():
    result = _psql("\\d orders")
    assert result.stderr.strip() == "", result.stderr
    assert "bigint" in result.stdout
    assert "numeric" in result.stdout


@psql_required
def test_list_schemas():
    result = _psql("\\dn")
    assert result.stderr.strip() == "", result.stderr
    assert "public" in result.stdout


@psql_required
def test_no_section_leaks_a_session_row():
    """psql's footer sections -- publications, constraints, indexes -- must be
    answered as empty rather than falling through to the session, whose reply gets
    printed as though it were catalog data. The publications section did exactly
    that until the middleware matched UNION queries as well as SELECTs.
    """
    result = _psql("\\d users")
    assert "row" not in result.stdout.split("Column")[0]
    assert "Publications:" not in result.stdout


# Each of these listed nothing at all, while the catalog held the rows to answer
# with: pg_database has the connected database, pg_roles the connected user, and
# pg_type 57 types. Empty is the truthful answer for a footer section a mimic has
# none of -- it is not the truthful answer for data we hold. See #66.


@psql_required
def test_list_databases_shows_the_connected_database():
    """\\l needs array_length, which psql spells pg_catalog.array_length -- and a
    schema-qualified call stays an Anonymous node the executor has no name for,
    where the plain spelling would have parsed to ArraySize."""
    result = _psql("\\l")
    assert result.stderr.strip() == "", result.stderr
    assert "test" in result.stdout, result.stdout


@psql_required
def test_list_roles_shows_the_connected_user():
    """\\du selects rolcanlogin and four siblings; one missing column emptied the
    whole listing."""
    result = _psql("\\du")
    assert result.stderr.strip() == "", result.stderr
    assert "postgres" in result.stdout, result.stdout


def test_format_type_reads_the_right_column_for_its_argument(conn, mock_session):
    """psql calls format_type() on pg_attribute for \\d and on pg_type for \\dT, and
    the rendered name lives somewhere different each time. Rewriting both to
    attformattype left \\dT selecting a pg_attribute column from pg_type."""

    async def schema():
        return {"users": {"id": "integer"}}

    mock_session.schema = schema

    with conn.cursor() as cur:
        # the \dT shape: format_type over pg_type
        cur.execute("SELECT pg_catalog.format_type(t.oid, NULL) FROM pg_catalog.pg_type AS t WHERE t.typname = 'text'")
        assert cur.fetchall() == [("text",)]

        # the \d shape: format_type over pg_attribute, still the column's declared type
        cur.execute(
            "SELECT pg_catalog.format_type(a.atttypid, a.atttypmod) FROM pg_catalog.pg_attribute AS a WHERE a.attname = 'id'"
        )
        assert cur.fetchall() == [("integer",)]


# --- a declared primary key ----------------------------------------------------------

_PK_SCHEMA = Schema(
    [
        Table("commits", {"sha": "text", "author": "text"}, primary_key="sha"),
        Table("commit_files", {"sha": "text", "path": "text"}, primary_key=("sha", "path")),
        Table("files", {"path": "text"}),
        Table("odd", {"userId": "integer"}, primary_key="userId"),
    ]
)


class PrimaryKeySession(SchemaSession):
    async def schema(self):
        return _PK_SCHEMA


@psql_required
@pytest.mark.parametrize(
    argnames=["table_name", "footer"],
    argvalues=[
        ["commits", '"commits_pkey" PRIMARY KEY, btree (sha)'],
        ["commit_files", '"commit_files_pkey" PRIMARY KEY, btree (sha, path)'],
        ["odd", '"odd_pkey" PRIMARY KEY, btree ("userId")'],
    ],
    ids=["one column", "composite", "a name that needs quoting"],
)
def test_describe_table_shows_a_declared_primary_key(table_name, footer):
    """Byte-for-byte what PostgreSQL 18 prints for the same declaration, which is the
    assertion that matters: psql composes this footer from four columns across three
    catalog tables, and any one of them missing renders it wrong or not at all."""
    result = _psql(f"\\d {table_name}", PrimaryKeySession)
    assert result.stderr.strip() == "", result.stderr
    assert "Indexes:" in result.stdout
    assert footer in result.stdout


@psql_required
def test_a_table_with_no_primary_key_has_no_indexes_footer():
    result = _psql("\\d files", PrimaryKeySession)
    assert result.stderr.strip() == "", result.stderr
    assert "path" in result.stdout
    assert "Indexes:" not in result.stdout


@psql_required
def test_a_primary_keys_columns_are_reported_not_null():
    result = _psql("\\d commits", PrimaryKeySession)
    assert result.stderr.strip() == "", result.stderr
    assert "not null" in result.stdout


@psql_required
def test_list_indexes_shows_the_key_index():
    result = _psql("\\di", PrimaryKeySession)
    assert result.stderr.strip() == "", result.stderr
    assert "commits_pkey" in result.stdout and "index" in result.stdout


@psql_required
def test_listing_tables_does_not_list_the_key_index():
    """`\\dt` filters on relkind, and an index is a relation -- so the pg_class row an
    index needs for its name must not leak into the table list."""
    result = _psql("\\dt", PrimaryKeySession)
    assert result.stderr.strip() == "", result.stderr
    assert "commits" in result.stdout
    assert "pkey" not in result.stdout


# --- unique constraints --------------------------------------------------------------

_UNIQUE_SCHEMA = Schema(
    [
        Table(
            "u1",
            {"id": "integer", "email": "text", "a": "text", "b": "integer", "oddName": "text"},
            primary_key="id",
            unique=["email", ("a", "b"), "oddName"],
        ),
        Table("only_unique", {"code": "text", "n": "integer"}, unique="code"),
    ]
)


class UniqueSession(SchemaSession):
    async def schema(self):
        return _UNIQUE_SCHEMA


@psql_required
def test_describe_table_shows_declared_unique_constraints():
    """Every line below is what PostgreSQL 18 prints for the same declaration, in this
    order -- psql sorts the footer `indisprimary DESC, relname`."""
    result = _psql("\\d u1", UniqueSession)
    assert result.stderr.strip() == "", result.stderr
    for line in (
        '"u1_pkey" PRIMARY KEY, btree (id)',
        '"u1_a_b_key" UNIQUE CONSTRAINT, btree (a, b)',
        '"u1_email_key" UNIQUE CONSTRAINT, btree (email)',
        '"u1_oddName_key" UNIQUE CONSTRAINT, btree ("oddName")',
    ):
        assert line in result.stdout, result.stdout


@psql_required
def test_a_table_with_only_a_unique_constraint_gets_a_footer():
    result = _psql("\\d only_unique", UniqueSession)
    assert result.stderr.strip() == "", result.stderr
    assert '"only_unique_code_key" UNIQUE CONSTRAINT, btree (code)' in result.stdout
    assert "PRIMARY KEY" not in result.stdout


@psql_required
def test_a_unique_constraint_does_not_make_its_columns_not_null():
    """Only the primary key column is `not null`, so exactly one row says so."""
    result = _psql("\\d u1", UniqueSession)
    assert result.stderr.strip() == "", result.stderr
    assert result.stdout.count("not null") == 1
