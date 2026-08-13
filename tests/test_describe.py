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

import pytest
from conftest import REPO_ROOT

from pg_mimic import TableSession
from pg_mimic.describe import _PG_NAME


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
def test_both_sessions_describe_a_bare_expression_as_postgres_does(measured, sql, git_sql_example):
    git_session = git_sql_example.GitSession(str(REPO_ROOT))
    # Any table will do: none of these queries reads one, and the point is the
    # derivation both sessions share rather than either one's schema.
    table_session = TableSession({"users": [{"id": 1}]})

    columns = _described(git_session, sql)
    assert columns == _described(table_session, sql)

    (name, type_name), *rest = columns
    assert not rest
    reported = {"name": name, "type": type_name}
    assert {key: reported[key] for key in measured} == measured
