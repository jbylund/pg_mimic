"""pg_catalog emulation, exercised the way psql actually reads it.

The queries here are psql's own, kept verbatim rather than simplified: what makes
this hard is not the joins but the spellings psql uses -- schema-qualified
operators and functions, OIDs written as string literals, correlated subqueries
over tables we have no rows for. A simplified query would pass while psql still
failed.

test_psql.py drives the real binary end-to-end; these are the fast checks that
say *which* part broke.
"""

from __future__ import annotations

import sqlglot

from pg_mimic.catalog import _build_pg_catalog, _rewrite_for_executor

SCHEMA = {"users": {"id": "integer", "name": "text"}, "orders": {"id": "bigint", "total": "numeric"}}


class FakeConnection:
    username = "alice"


def _run(sql, schema=None):
    sqlglot_schema, tables = _build_pg_catalog(SCHEMA if schema is None else schema)
    from sqlglot.executor import execute

    expr = _rewrite_for_executor(FakeConnection(), sqlglot.parse_one(sql, dialect="postgres"))
    result = execute(expr, schema=sqlglot_schema, tables=tables, dialect="postgres")
    return [tuple(row) for row in result.rows]


# psql \dt, verbatim.
_DT = """
SELECT n.nspname as "Schema", c.relname as "Name",
  CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view' WHEN 'm' THEN 'materialized view'
    WHEN 'i' THEN 'index' WHEN 'S' THEN 'sequence' WHEN 't' THEN 'TOAST table'
    WHEN 'f' THEN 'foreign table' WHEN 'p' THEN 'partitioned table'
    WHEN 'I' THEN 'partitioned index' END as "Type",
  pg_catalog.pg_get_userbyid(c.relowner) as "Owner"
FROM pg_catalog.pg_class c
     LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
     LEFT JOIN pg_catalog.pg_am am ON am.oid = c.relam
WHERE c.relkind IN ('r','p','') AND n.nspname <> 'pg_catalog' AND n.nspname !~ '^pg_toast'
ORDER BY 1,2
"""


def test_list_tables():
    assert _run(_DT) == [("public", "orders", "table", "alice"), ("public", "users", "table", "alice")]


def test_list_tables_is_empty_without_a_schema():
    """A session that declares no schema has no tables to list -- not an error."""
    assert _run(_DT, schema={}) == []


def test_owner_comes_from_the_connection():
    """Ownership isn't modelled, so pg_get_userbyid answers with the connected user
    rather than a fixed name."""
    rows = _run(_DT)
    assert {row[3] for row in rows} == {"alice"}


# psql's table lookup, verbatim -- note the OID written as a string literal and the
# schema-qualified operator spelling.
_LOOKUP = """
SELECT c.oid, n.nspname, c.relname
FROM pg_catalog.pg_class c LEFT JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE c.relname OPERATOR(pg_catalog.~) '^(users)$' COLLATE pg_catalog.default
  AND pg_catalog.pg_table_is_visible(c.oid)
ORDER BY 2, 3
"""


def test_table_lookup_by_name():
    rows = _run(_LOOKUP)
    assert len(rows) == 1
    assert rows[0][1:] == ("public", "users")


def test_oid_written_as_a_string_literal_still_matches():
    """psql writes `WHERE c.oid = '16384'` and lets Postgres coerce it. sqlglot's
    executor compares 16384 to "16384" and finds them different, so the literal has
    to be coerced first -- without it psql reports "Did not find any relation"."""
    oid = _run(_LOOKUP)[0][0]
    assert _run(f"SELECT relname FROM pg_catalog.pg_class WHERE oid = '{oid}'") == [("users",)]


# psql's column list, verbatim. Both correlated subqueries select from tables that
# are always empty here; before they were handled, the collation one filtered away
# every row rather than yielding NULL.
_COLUMNS = """
SELECT a.attname,
  pg_catalog.format_type(a.atttypid, a.atttypmod),
  (SELECT pg_catalog.pg_get_expr(d.adbin, d.adrelid, true) FROM pg_catalog.pg_attrdef d
   WHERE d.adrelid = a.attrelid AND d.adnum = a.attnum AND a.atthasdef),
  a.attnotnull,
  (SELECT c.collname FROM pg_catalog.pg_collation c, pg_catalog.pg_type t
   WHERE c.oid = a.attcollation AND t.oid = a.atttypid AND a.attcollation <> t.typcollation) AS attcollation,
  a.attidentity, a.attgenerated
FROM pg_catalog.pg_attribute a
WHERE a.attrelid = '{oid}' AND a.attnum > 0 AND NOT a.attisdropped
ORDER BY a.attnum
"""


def test_column_list_reports_names_and_declared_types():
    oid = _run(_LOOKUP)[0][0]
    rows = _run(_COLUMNS.format(oid=oid))
    assert [(row[0], row[1]) for row in rows] == [("id", "integer"), ("name", "text")]


def test_column_list_keeps_every_column():
    """The correlated collation subquery cross-joins an empty table, so it yields
    no rows -- which used to remove every column from the result instead of
    answering NULL."""
    oid = _run(_LOOKUP)[0][0]
    assert len(_run(_COLUMNS.format(oid=oid))) == 2


def test_column_defaults_and_collations_are_null():
    oid = _run(_LOOKUP)[0][0]
    row = _run(_COLUMNS.format(oid=oid))[0]
    assert row[2] is None  # no default
    assert row[4] is None  # no non-default collation


def test_declared_types_map_to_real_oids():
    """Session.schema() declares types as free text, so "bigint" has to become int8
    rather than silently defaulting to text like an unrecognised spelling."""
    from pg_mimic.types import INT8, NUMERIC

    _schema, tables = _build_pg_catalog(SCHEMA)
    orders_oid = next(r["oid"] for r in tables["pg_catalog"]["pg_class"] if r["relname"] == "orders")
    attrs = {r["attname"]: r["atttypid"] for r in tables["pg_catalog"]["pg_attribute"] if r["attrelid"] == orders_oid}
    assert attrs == {"id": INT8, "total": NUMERIC}


def test_pg_type_covers_the_types_pg_mimic_can_encode():
    """Built from the same OID tables the wire codecs use, so the catalog can
    describe anything pg_mimic can put on the wire."""
    from pg_mimic import ARRAY_OID, INT8, TEXT

    _schema, tables = _build_pg_catalog(SCHEMA)
    by_oid = {r["oid"]: r for r in tables["pg_catalog"]["pg_type"]}
    assert by_oid[TEXT]["typname"] == "text"
    assert by_oid[ARRAY_OID[INT8]]["typelem"] == INT8  # what marks it an array
    assert by_oid[ARRAY_OID[TEXT]]["typname"] == "_text"
