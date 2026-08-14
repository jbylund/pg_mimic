"""The two sessions that derive columns from a declared schema, held together.

`pg_mimic.tables` and `examples/git_sql.py` both answer "what shape is this
query's result, without running it", and for a while they answered differently:
the example was a copy of the library's derivation that never got #40's integer
sizing, so it described `select 3000000000` as int4 -- the width that crashes
asyncpg's binary decoder -- and called an unaliased output column `_col_0` where
Postgres calls it `?column?` (#88).

Both now go through `pg_mimic.describe`, so the divergence cannot come back
without this failing. Agreeing with each other is only half of it, though: two
copies of one wrong answer agree too. Each case below also carries what
PostgreSQL 18 itself reports, measured rather than reasoned about:

    $ psql -X -d postgres -tAc "select pg_typeof(3000000000), pg_typeof(9223372036854775808)"
    bigint|numeric
    $ psql -X -d postgres -c "select 1 + 1"
     ?column?
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from pg_mimic import TableSession
from pg_mimic.describe import _PG_NAME

_GIT_SQL = Path(__file__).resolve().parent.parent / "examples" / "git_sql.py"


def _git_sql_example():
    """examples/git_sql.py, loaded by path -- `examples` is not an importable package.

    Its ENV edits are rolled back around the import. The example patches
    `sqlglot.executor.env.ENV` at module scope, which is process-global, and
    test_sqlglot_workarounds.py's strict xfails are assertions about the ENV
    sqlglot ships -- one of them starts passing if the example's INTERVAL is left
    in place. describe() never reaches the executor, so nothing here wants those
    edits anyway.

    `examples/` goes on sys.path for the duration because the example imports its
    sibling `_args` for the shared command line. Running it as a script puts that
    directory there automatically; loading it by path does not.
    """
    import sqlglot.executor.env as executor_env

    saved = dict(executor_env.ENV)
    sys.path.insert(0, str(_GIT_SQL.parent))
    try:
        spec = importlib.util.spec_from_file_location("git_sql_example", _GIT_SQL)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(_GIT_SQL.parent))
        executor_env.ENV.clear()
        executor_env.ENV.update(saved)


def _described(session, sql: str) -> list[tuple[str, str]]:
    """One session's answer, as (column name, Postgres type name) -- the two things
    the three divergences were about."""
    columns = asyncio.run(session.describe(sql, []))
    return [(column.name, _PG_NAME[column.oid]) for column in columns]


# `measured` names only the attributes Postgres was actually measured on for that
# query, since each row of the table in #88 is about one of them.
_divergences = {
    "a literal past int4 is bigint": {"measured": {"type": "bigint"}, "sql": "SELECT 3000000000"},
    "a literal past int8 is numeric": {"measured": {"type": "numeric"}, "sql": "SELECT 9223372036854775808"},
    "an unaliased output column is ?column?": {"measured": {"name": "?column?"}, "sql": "SELECT 1 + 1"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_divergences.values()))),
    argvalues=[[value for _, value in sorted(_divergences[name].items())] for name in sorted(_divergences)],
    ids=sorted(_divergences),
)
def test_both_sessions_describe_a_bare_expression_as_postgres_does(measured, sql):
    git_session = _git_sql_example().GitSession(str(_GIT_SQL.parent.parent))
    # Any table will do: none of these queries reads one, and the point is the
    # derivation both sessions share rather than either one's schema.
    table_session = TableSession({"users": [{"id": 1}]})

    columns = _described(git_session, sql)
    assert columns == _described(table_session, sql)

    (name, type_name), *rest = columns
    assert not rest
    reported = {"name": name, "type": type_name}
    assert {key: reported[key] for key in measured} == measured


# Every `expected` below is what PostgreSQL 18.4 puts in RowDescription for that
# query, read through psycopg rather than psql's display (#94).
_literal_names = {
    "an integer literal is not named after itself": {"expected": ["?column?"], "sql": "SELECT 1"},
    "a literal past int4 is not either": {"expected": ["?column?"], "sql": "SELECT 3000000000"},
    "a string literal does not leak its contents": {"expected": ["?column?"], "sql": "SELECT 'abc'"},
    "a float literal": {"expected": ["?column?"], "sql": "SELECT 2.5"},
    "an exponent literal": {"expected": ["?column?"], "sql": "SELECT 1e5"},
    "parentheses do not name a literal either": {"expected": ["?column?"], "sql": "SELECT (1)"},
    "NULL was already right": {"expected": ["?column?"], "sql": "SELECT NULL"},
    "TRUE was already right": {"expected": ["?column?"], "sql": "SELECT TRUE"},
    "a negative literal was already right": {"expected": ["?column?"], "sql": "SELECT -1"},
    "an expression over literals was already right": {"expected": ["?column?"], "sql": "SELECT 1 + 1"},
    "unaliased columns repeat rather than uniquify": {"expected": ["?column?"] * 3, "sql": "SELECT 1, 2, 3"},
    "an alias survives beside unaliased literals": {"expected": ["a", "?column?"], "sql": "SELECT 1 AS a, 2"},
    "an alias unlike the literal's text is kept": {"expected": ["total"], "sql": "SELECT 1 AS total"},
    "duplicate aliases are not uniquified either": {"expected": ["dup", "dup"], "sql": "SELECT 1 AS dup, 2 AS dup"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_literal_names.values()))),
    argvalues=[[value for _, value in sorted(_literal_names[name].items())] for name in sorted(_literal_names)],
    ids=sorted(_literal_names),
)
def test_an_unaliased_literal_is_named_column_not_after_itself(expected, sql):
    session = TableSession({"users": [{"id": 1}]})
    assert [name for name, _ in _described(session, sql)] == expected


def test_an_alias_that_matches_the_literals_own_text_is_kept():
    """`SELECT 'a'` and `SELECT 'a' AS a` are the same tree once qualify() has run,
    and Postgres calls them `?column?` and `a`. Naming reads the query as written,
    which is the only place they still differ (#111)."""
    session = TableSession({"users": [{"id": 1}]})
    assert [name for name, _ in _described(session, "SELECT 'a' AS a")] == ["a"]
    assert [name for name, _ in _described(session, "SELECT 'a'")] == ["?column?"]


# Postgres names a cast after its operand, and only falls back to the target type
# when the operand has no name of its own -- using typname, so `1::int` is `int4`
# rather than `integer`. Measured against PostgreSQL 18.4 (#111).
_cast_names = {
    "a cast over a named column keeps the column's name": {"expected": ["id"], "sql": "SELECT CAST(id AS TEXT) FROM users"},
    "the shorthand does too": {"expected": ["id"], "sql": "SELECT id::text FROM users"},
    "a cast over a literal falls back to the type": {"expected": ["text"], "sql": "SELECT CAST(1 AS TEXT)"},
    "over an expression, likewise": {"expected": ["text"], "sql": "SELECT CAST(1 + 1 AS TEXT)"},
    "over NULL, likewise": {"expected": ["text"], "sql": "SELECT CAST(NULL AS TEXT)"},
    "the fallback is typname, not the declared spelling": {"expected": ["int4"], "sql": "SELECT 1::int"},
    "numeric": {"expected": ["numeric"], "sql": "SELECT 1::numeric"},
    "float8": {"expected": ["float8"], "sql": "SELECT 1::float8"},
    "a sub-select operand stops short of the type name": {"expected": ["?column?"], "sql": "SELECT (SELECT 1)::text"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_cast_names.values()))),
    argvalues=[[value for _, value in sorted(_cast_names[name].items())] for name in sorted(_cast_names)],
    ids=sorted(_cast_names),
)
def test_a_cast_is_named_after_its_operand_then_its_type(expected, sql):
    session = TableSession({"users": [{"id": 1}]})
    assert [name for name, _ in _described(session, sql)] == expected
