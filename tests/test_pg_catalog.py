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

import pytest
import sqlglot

from pg_mimic.catalog import _build_pg_catalog, _Key, _key_name
from pg_mimic.catalog_rewrite import rewrite_for_executor
from pg_mimic.declared import Schema, Table, resolve

SCHEMA = {"users": {"id": "integer", "name": "text"}, "orders": {"id": "bigint", "total": "numeric"}}


class FakeConnection:
    username = "alice"


def _run(sql, schema=None):
    sqlglot_schema, tables = _build_pg_catalog(resolve(SCHEMA if schema is None else schema))
    from sqlglot.executor import execute

    expr = rewrite_for_executor(FakeConnection(), sqlglot.parse_one(sql, dialect="postgres"))
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

    _schema, tables = _build_pg_catalog(resolve(SCHEMA))
    orders_oid = next(r["oid"] for r in tables["pg_catalog"]["pg_class"] if r["relname"] == "orders")
    attrs = {r["attname"]: r["atttypid"] for r in tables["pg_catalog"]["pg_attribute"] if r["attrelid"] == orders_oid}
    assert attrs == {"id": INT8, "total": NUMERIC}


def test_pg_type_covers_the_types_pg_mimic_can_encode():
    """Built from the same OID tables the wire codecs use, so the catalog can
    describe anything pg_mimic can put on the wire."""
    from pg_mimic import ARRAY_OID, INT8, TEXT

    _schema, tables = _build_pg_catalog(resolve(SCHEMA))
    by_oid = {r["oid"]: r for r in tables["pg_catalog"]["pg_type"]}
    assert by_oid[TEXT]["typname"] == "text"
    assert by_oid[ARRAY_OID[INT8]]["typelem"] == INT8  # what marks it an array
    assert by_oid[ARRAY_OID[TEXT]]["typname"] == "_text"


# --- a declared primary key ----------------------------------------------------------

_PK_SCHEMA = Schema(
    [
        Table("commits", {"sha": "text", "author": "text"}, primary_key="sha"),
        Table("commit_files", {"sha": "text", "path": "text", "adds": "integer"}, primary_key=("sha", "path")),
        Table("files", {"path": "text"}),
        Table("odd", {"userId": "integer"}, primary_key="userId"),
    ]
)

# psql's index-footer query for `\d <table>`, verbatim except the OID literal. The
# LEFT JOIN to pg_constraint is why an index needs a matching constraint row, and
# `c2.relname` is why the index needs a pg_class row of its own.
_INDEX_FOOTER = """
SELECT c2.relname, i.indisprimary, i.indisunique, i.indisclustered, i.indisvalid,
  pg_catalog.pg_get_indexdef(i.indexrelid, 0, true),
  pg_catalog.pg_get_constraintdef(con.oid, true), contype, condeferrable, condeferred,
  i.indisreplident, c2.reltablespace
FROM pg_catalog.pg_class c, pg_catalog.pg_class c2, pg_catalog.pg_index i
  LEFT JOIN pg_catalog.pg_constraint con
    ON (conrelid = i.indrelid AND conindid = i.indexrelid AND contype IN ('p','u','x'))
WHERE c.oid = '{oid}' AND c.oid = i.indrelid AND i.indexrelid = c2.oid
ORDER BY i.indisprimary DESC, c2.relname
"""


def _oid_of(table_name):
    rows = _run(f"SELECT oid FROM pg_catalog.pg_class WHERE relname = '{table_name}' AND relkind = 'r'", _PK_SCHEMA)
    return rows[0][0]


@pytest.mark.parametrize(
    argnames=["table_name", "index_name", "indexed"],
    argvalues=[
        ["commits", "commits_pkey", "(sha)"],
        ["commit_files", "commit_files_pkey", "(sha, path)"],
        ["odd", "odd_pkey", '("userId")'],
    ],
    ids=["one column", "composite, in key order", "a name that needs quoting"],
)
def test_psqls_index_footer_query_answers_for_a_declared_primary_key(table_name, index_name, indexed):
    """The whole point of the row-carried indexdef: psql echoes everything after
    " USING " into the footer, so that substring is what renders `btree (sha)`."""
    rows = _run(_INDEX_FOOTER.format(oid=_oid_of(table_name)), _PK_SCHEMA)
    assert len(rows) == 1
    relname, indisprimary, indisunique = rows[0][0], rows[0][1], rows[0][2]
    indexdef, condef, contype = rows[0][5], rows[0][6], rows[0][7]
    assert (relname, indisprimary, indisunique) == (index_name, True, True)
    assert indexdef.endswith(f" USING btree {indexed}")
    assert (condef, contype) == (f"PRIMARY KEY {indexed}", "p")


def test_a_table_with_no_primary_key_has_no_index_footer():
    assert _run(_INDEX_FOOTER.format(oid=_oid_of("files")), _PK_SCHEMA) == []


def test_declaring_a_primary_key_does_not_renumber_the_tables():
    """Table OIDs are positional and a client may have introspected them, so index and
    constraint OIDs come from their own ranges rather than continuing the table run."""
    keyed = _run("SELECT relname, oid FROM pg_catalog.pg_class WHERE relkind = 'r' ORDER BY oid", _PK_SCHEMA)
    unkeyed = _run(
        "SELECT relname, oid FROM pg_catalog.pg_class WHERE relkind = 'r' ORDER BY oid",
        Schema([Table(name, dict(table.columns)) for name, table in _PK_SCHEMA.tables.items()]),
    )
    assert keyed == unkeyed
    indexes = _run("SELECT oid FROM pg_catalog.pg_class WHERE relkind = 'i'", _PK_SCHEMA)
    assert not {oid for (oid,) in indexes} & {oid for _, oid in keyed}


def test_a_primary_keys_columns_are_not_null_and_the_others_are():
    rows = _run(
        "SELECT a.attname, a.attnotnull FROM pg_catalog.pg_attribute a "
        f"WHERE a.attrelid = '{_oid_of('commit_files')}' ORDER BY a.attnum",
        _PK_SCHEMA,
    )
    assert rows == [("sha", True), ("path", True), ("adds", False)]


def test_conkey_is_the_key_columns_attnums_in_key_order():
    rows = _run("SELECT conname, conkey FROM pg_catalog.pg_constraint ORDER BY conname", _PK_SCHEMA)
    assert dict(rows) == {"commit_files_pkey": "{1,2}", "commits_pkey": "{1}", "odd_pkey": "{1}"}


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
        Table("plain", {"x": "text"}),
    ]
)


def _footer_rows(table_name, schema):
    rows = _run(f"SELECT oid FROM pg_catalog.pg_class WHERE relname = '{table_name}' AND relkind = 'r'", schema)
    return _run(_INDEX_FOOTER.format(oid=rows[0][0]), schema)


def test_psqls_footer_lists_the_primary_key_first_then_unique_by_name():
    """psql orders it `indisprimary DESC, c2.relname`, so this is the order the lines
    come out in -- matched against a real PostgreSQL 18 declaring the same table."""
    rows = _footer_rows("u1", _UNIQUE_SCHEMA)
    assert [(row[0], row[1], row[7]) for row in rows] == [
        ("u1_pkey", True, "p"),
        ("u1_a_b_key", False, "u"),
        ("u1_email_key", False, "u"),
        ("u1_oddName_key", False, "u"),
    ]


def test_a_unique_constraint_renders_as_unique_not_primary_key():
    definitions = {row[0]: (row[5], row[6]) for row in _footer_rows("u1", _UNIQUE_SCHEMA)}
    assert definitions["u1_a_b_key"][0].endswith(" USING btree (a, b)")
    assert definitions["u1_a_b_key"][1] == "UNIQUE (a, b)"
    assert definitions["u1_oddName_key"][1] == 'UNIQUE ("oddName")'
    assert definitions["u1_pkey"][1] == "PRIMARY KEY (id)"


def test_a_unique_index_is_unique_but_not_primary():
    rows = _footer_rows("only_unique", _UNIQUE_SCHEMA)
    assert [(row[0], row[1], row[2]) for row in rows] == [("only_unique_code_key", False, True)]


def test_a_unique_constraints_columns_are_still_nullable():
    """The one substantive difference from a primary key: only a primary key's columns
    are implicitly NOT NULL."""
    oid = _run("SELECT oid FROM pg_catalog.pg_class WHERE relname = 'u1' AND relkind = 'r'", _UNIQUE_SCHEMA)[0][0]
    rows = _run(
        f"SELECT attname, attnotnull FROM pg_catalog.pg_attribute WHERE attrelid = '{oid}' ORDER BY attnum",
        _UNIQUE_SCHEMA,
    )
    assert rows == [("id", True), ("email", False), ("a", False), ("b", False), ("oddName", False)]


def test_a_table_with_only_unique_constraints_still_gets_the_footer():
    """relhasindex gates the whole footer, and a table with no primary key has one."""
    rows = _run("SELECT relname, relhasindex FROM pg_catalog.pg_class WHERE relkind = 'r'", _UNIQUE_SCHEMA)
    assert dict(rows) == {"u1": True, "only_unique": True, "plain": False}


def test_every_key_gets_its_own_index_and_constraint_oid():
    indexes = _run("SELECT indexrelid, indrelid FROM pg_catalog.pg_index", _UNIQUE_SCHEMA)
    constraints = _run("SELECT oid FROM pg_catalog.pg_constraint", _UNIQUE_SCHEMA)
    tables = _run("SELECT oid FROM pg_catalog.pg_class WHERE relkind = 'r'", _UNIQUE_SCHEMA)
    index_oids = {oid for oid, _ in indexes}
    assert len(index_oids) == len(indexes) == 5
    assert len({oid for (oid,) in constraints}) == 5
    assert not index_oids & {oid for (oid,) in tables}


def test_a_key_name_is_truncated_to_postgres_identifier_length():
    """63 bytes is NAMEDATALEN - 1, and reachable with an ordinary long table name."""
    long_table = "t" * 60
    name = _key_name(_Key(long_table, ("column_one", "column_two"), False), taken=set())
    assert len(name) == 63
    assert name.startswith(long_table)


def test_two_keys_that_truncate_to_one_name_are_disambiguated():
    """As Postgres' own ChooseIndexName does. Identical *keys* are refused at
    declaration, so this only fires for different keys with a shared prefix."""
    long_table = "t" * 60
    first = _key_name(_Key(long_table, ("column_one",), False), taken=set())
    second = _key_name(_Key(long_table, ("column_two",), False), taken={first})
    assert first != second
    assert len(second) <= 63
