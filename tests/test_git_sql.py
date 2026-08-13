"""What examples/git_sql.py refuses, and for which of its two reasons.

The example answers one shape of statement -- a single SELECT -- and used to
refuse everything else with the same sentence: "this example serves SELECT only
-- a git repo is read-only". True of INSERT, and misleading about everything
else: EXPLAIN neither reads nor writes a row, so blaming the repo's
read-only-ness for it sends its author looking for a permission to grant when
the real answer is that there is no planner here to print a plan.

So the two reasons are now two refusals, with the SQLSTATE that goes with each:
a write is 25006 (read_only_sql_transaction), what Postgres itself reports for
INSERT on a read-only standby, and anything else is 0A000 (feature_not_
supported) naming the statement it could not run.
"""

from __future__ import annotations

import pytest
from conftest import REPO_ROOT

from pg_mimic.errors import FEATURE_NOT_SUPPORTED, READ_ONLY_SQL_TRANSACTION, PgError


async def _refusal(session, sql: str) -> PgError:
    with pytest.raises(PgError) as raised:
        async for _ in session.query(sql, []):
            pass
    return raised.value


_refusals = {
    "EXPLAIN is not a write": {
        "sql": "EXPLAIN SELECT committer_name FROM commits",
        "message": "runs SELECT only, and cannot run EXPLAIN -- there is no planner behind this",
        "sqlstate": FEATURE_NOT_SUPPORTED,
    },
    "VACUUM is not a write either": {
        "sql": "VACUUM commits",
        "message": "runs SELECT only, and cannot run VACUUM",
        "sqlstate": FEATURE_NOT_SUPPORTED,
    },
    "INSERT is what read-only refuses": {
        "sql": "INSERT INTO commits (sha) VALUES ('abc')",
        "message": "cannot execute INSERT -- this example serves a git repo read-only",
        "sqlstate": READ_ONLY_SQL_TRANSACTION,
    },
    "DELETE is too": {
        "sql": "DELETE FROM commits",
        "message": "cannot execute DELETE -- this example serves a git repo read-only",
        "sqlstate": READ_ONLY_SQL_TRANSACTION,
    },
    # A set operation parses as a Query that is not a Select, and its first word is
    # SELECT -- so the statement-naming refusal would read "cannot run SELECT".
    "a set operation says which shape is missing": {
        "sql": "SELECT sha FROM commits UNION SELECT sha FROM commits",
        "message": "runs a single SELECT -- a set operation or a parenthesized query is not covered",
        "sqlstate": FEATURE_NOT_SUPPORTED,
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_refusals.values()))),
    argvalues=[[value for _, value in sorted(_refusals[name].items())] for name in sorted(_refusals)],
    ids=sorted(_refusals),
)
async def test_a_refusal_gives_the_reason_that_is_true_of_the_statement(message, sql, sqlstate, git_sql_example):
    error = await _refusal(git_sql_example.GitSession(str(REPO_ROOT)), sql)

    assert error.sqlstate == sqlstate
    assert message in error.message
    if sqlstate != READ_ONLY_SQL_TRANSACTION:
        # The whole point of the split: a statement that would write nothing is
        # never told that the thing it is reading cannot be written to.
        assert "read-only" not in error.message
