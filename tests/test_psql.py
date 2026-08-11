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
from conftest import ServerThread

from pg_mimic import PgServer, ResultColumn, Session

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


def _psql(command):
    server = PgServer(session_factory=SchemaSession)
    thread = ServerThread(server)
    port = thread.start()
    try:
        result = subprocess.run(
            ["psql", f"host=127.0.0.1 port={port} user=test dbname=test", "-c", command],
            capture_output=True,
            text=True,
            timeout=20,
        )
    finally:
        thread.stop()
    return result


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
