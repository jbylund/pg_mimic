"""Strict-xfail tripwires for the sqlglot executor bugs pg_mimic works around.

Every test here asserts what *Postgres* answers and runs against sqlglot directly
rather than through pg_mimic. A bug still outstanding is marked
`xfail(strict=True)`, so it fails today -- the expected outcome -- and the moment
sqlglot fixes it the test XPASSes, which strict turns into a build failure telling
you a workaround is now deletable.

Once that happens the mark comes off and the test stays, as a plain assertion: it
then documents why the sqlglot floor in pyproject.toml is what it is, and fails if
a regression or a careless downgrade takes the fix away.

The point is that a workaround has no natural expiry. Two of them silently
outlived their cause and were noticed only by re-reading the comments: a
`FULL OUTER JOIN` refused for a bug fixed in sqlglot v30.15.0, and an INTERVAL
patch whose stated justification had gone stale. These tests are how the next
one announces itself instead. See
https://github.com/jbylund/pg_mimic/issues/50

Each test names the issue tracking it, and the workaround it justifies where
there is one, so a failure points straight at the code to delete. Two
inventories: bugs we work around are
https://github.com/jbylund/pg_mimic/issues/49, and the ones reaching users
untreated are https://github.com/jbylund/pg_mimic/issues/58

The tests from `test_distinct_keeps_columns_that_share_a_name` down were found by
`tools/fuzz`, which generates random SELECTs and compares the executor against a
real PostgreSQL. Reading them as a group is the honest summary of how far the
executor is from Postgres: it is a good query engine for the shapes sqlglot's own
tests cover, and every one of these is a wrong answer or a refusal for SQL that a
client has every reason to send.

A failure here is never a pg_mimic regression: it means good news upstream.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
from sqlglot.executor import execute

_UPSTREAM_FIXED = "sqlglot fixed this -- remove the workaround named in the test"


def _rows(sql: str, tables: dict, schema: dict) -> list[tuple]:
    return [tuple(row) for row in execute(sql, schema=schema, tables=tables, dialect="postgres").rows]


_NUMBERS = ({"t": [{"a": 1}, {"a": 2}, {"a": 3}]}, {"t": {"a": "INT"}})
_TEXT = ({"q": [{"s": "Bump version"}, {"s": "Add feature"}]}, {"q": {"s": "TEXT"}})


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_offset_is_applied():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: `_take_row_window` / `rows_sliced_here` in tables.py.
    """
    assert _rows("SELECT a FROM t ORDER BY a OFFSET 1", *_NUMBERS) == [(2,), (3,)]
    assert _rows("SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 1", *_NUMBERS) == [(2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_order_by_on_a_set_operation_keeps_the_columns():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: `_take_result_order` / `_sorted_rows` in tables.py.

    The rows come back column-less -- `[(), (), ()]` -- not merely unsorted.
    """
    assert _rows("SELECT a FROM t UNION SELECT a FROM t ORDER BY a", *_NUMBERS) == [(1,), (2,), (3,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_not_in_a_subquery_filters():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: `_rewrite_not_in` in tables.py. `NOT IN (literals)` is fine.
    """
    tables = {"t": [{"a": 1}, {"a": 2}], "u": [{"b": 1}]}
    schema = {"t": {"a": "INT"}, "u": {"b": "INT"}}
    assert _rows("SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)", tables, schema) == [(2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_descending_order_places_nulls_instead_of_raising():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: `_rewrite_null_ordering` in tables.py.

    Ascending coincidentally matches Postgres; descending raises TypeError.

    Fixed upstream but in no release, so the mark comes off when the floor moves:
    https://github.com/jbylund/pg_mimic/issues/109 is the checklist. The daily
    `Upstream sqlglot` job is already red on its `main` leg for this.
    """
    tables, schema = {"n": [{"a": 1}, {"a": None}]}, {"n": {"a": "INT"}}
    assert _rows("SELECT a FROM n ORDER BY a DESC", tables, schema) == [(None,), (1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_a_set_operation_may_read_one_table_twice():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: `canonicalize_table_aliases=True` in `TableSession._plan`.

    The planner keys steps by table name, so the second branch reruns the first's.
    """
    assert _rows("SELECT a FROM t WHERE a = 1 UNION ALL SELECT a FROM t WHERE a = 2", *_NUMBERS) == [(1,), (2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_limit_on_a_parenthesized_query_is_applied():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: `_flatten_parenthesized` in tables.py.
    """
    assert _rows("(SELECT a FROM t) LIMIT 1", *_NUMBERS) == [(1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_a_decimal_cast_is_exact():
    """https://github.com/jbylund/pg_mimic/issues/33

    Workaround: the `_cast` override in tables.py, which is the whole
    reason `_execute` reaches past `sqlglot.executor.execute()` for an `env=`.

    `env.cast` sends every one of `exp.DataType.NUMERIC_TYPES` through `int()`, and
    DECIMAL is in that set: the first of these raises ValueError and the second is 9.
    Every dialect sqlglot targets returns an exact 9.99 for both.
    """
    tables, schema = {"d": [{"n": Decimal("9.99")}]}, {"d": {"n": "DECIMAL"}}
    assert _rows("SELECT CAST('9.99' AS NUMERIC) AS c FROM d", tables, schema) == [(Decimal("9.99"),)]
    assert _rows("SELECT CAST(9.99 AS DECIMAL) AS c FROM d", tables, schema) == [(Decimal("9.99"),)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_tablesample_is_applied():
    """https://github.com/jbylund/pg_mimic/issues/49

    Workaround: refused by `_reject_silently_ignored` in tables.py.
    """
    assert _rows("SELECT a FROM t TABLESAMPLE BERNOULLI (0)", *_NUMBERS) == []


def test_like_is_anchored_and_takes_metacharacters_literally():
    """https://github.com/jbylund/pg_mimic/issues/38

    Fixed in sqlglot v30.17.0 by
    https://redirect.github.com/tobymao/sqlglot/commit/44b73a00, one of the reasons
    the floor is 30.17.0. examples/git_sql.py no longer replaces ENV["LIKE"].

    Postgres' LIKE is fully anchored, and only % and _ are wildcards.
    """
    assert _rows("SELECT s FROM q WHERE s LIKE 'Bump'", *_TEXT) == []
    assert _rows("SELECT s FROM q WHERE s LIKE 'Bump vers.on'", *_TEXT) == []


def test_not_like_excludes_what_like_matches():
    """https://github.com/jbylund/pg_mimic/issues/44

    Fixed in sqlglot v30.17.0 by
    https://redirect.github.com/tobymao/sqlglot/pull/8139. examples/git_sql.py no
    longer needs _unfold_negations; pushdown() keeps its own negate guard.
    """
    assert _rows("SELECT s FROM q WHERE s NOT LIKE 'Bump%'", *_TEXT) == [("Add feature",)]


def test_ilike_exists():
    """https://github.com/jbylund/pg_mimic/issues/38

    Fixed in sqlglot v30.17.0 by
    https://redirect.github.com/tobymao/sqlglot/pull/8144.
    """
    assert _rows("SELECT s FROM q WHERE s ILIKE 'bump%'", *_TEXT) == [("Bump version",)]


def test_length_exists():
    """https://github.com/jbylund/pg_mimic/issues/38

    Not xfail: implemented upstream in
    https://redirect.github.com/tobymao/sqlglot/pull/8145 and released in
    v30.17.0, which is part of why the floor is 30.17.0. Kept as a plain
    assertion so a downgrade or a regression shows up here.
    """
    assert _rows("SELECT length(s) FROM q", *_TEXT) == [(12,), (11,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_concatenation_exists():
    """https://github.com/jbylund/pg_mimic/issues/38

    No workaround to delete -- `||` raises, and pg_mimic reports that as
    `0A000` on an information_schema query rather than answering no rows (#39).

    Split from the `length()` case above because the two were fixed separately:
    implemented upstream in
    https://redirect.github.com/tobymao/sqlglot/pull/8146, merged but not in any
    release as of v30.17.0. When it ships, this mark comes off and the floor moves.
    """
    assert _rows("SELECT s || '!' FROM q", *_TEXT) == [("Bump version!",), ("Add feature!",)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_interval_handles_years_and_months():
    """https://github.com/jbylund/pg_mimic/issues/55

    Workaround: `_interval_delta` in examples/git_sql.py.

    A literal operand is constant-folded before the executor runs, so this needs a
    column to reach ENV["INTERVAL"], which is `timedelta(**{unit: n})` -- and
    timedelta has no years or months.
    """
    tables, schema = {"d": [{"ts": datetime.datetime(2024, 3, 15)}]}, {"d": {"ts": "TIMESTAMP"}}
    assert _rows("SELECT ts - INTERVAL '1 year' FROM d", tables, schema) == [(datetime.datetime(2023, 3, 15),)]
    assert _rows("SELECT ts - INTERVAL '1 month' FROM d", tables, schema) == [(datetime.datetime(2024, 2, 15),)]


def test_typed_division_keeps_a_real_operands_fraction():
    """https://github.com/jbylund/pg_mimic/issues/48

    Fixed in sqlglot v30.17.0 by
    https://redirect.github.com/tobymao/sqlglot/pull/8138.
    """
    assert _rows("SELECT a / 2.0 FROM t", *_NUMBERS)[0] == (0.5,)


_HALVES = [Decimal("2.5"), Decimal("0.5"), Decimal("-2.5"), Decimal("3.5")]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_round_on_numeric_goes_half_away_from_zero():
    """https://github.com/jbylund/pg_mimic/issues/58

    No workaround. ENV["ROUND"] is Python's builtin, which rounds half to even;
    Postgres' `round(numeric)` breaks ties away from zero. Checked against a real
    PostgreSQL 18: 2.5 -> 3, 0.5 -> 1, -2.5 -> -3.
    """
    tables = {"d": [{"n": value} for value in _HALVES]}
    rows = _rows("SELECT round(n) FROM d", tables, {"d": {"n": "DECIMAL"}})
    assert [row[0] for row in rows] == [3, 1, -3, 4]


def test_round_on_double_goes_half_to_even():
    """Not xfail: correct today, and easy to "fix" into a regression.

    Postgres' `round(double precision)` goes through C's rint(), which rounds
    half to *even* -- so Python's builtin already matches it, and only the numeric
    case above is wrong. Verified against PostgreSQL 18, where
    `round(2.5::double precision)` is 2 while `round(2.5::numeric)` is 3.
    """
    tables = {"d": [{"v": float(value)} for value in _HALVES]}
    rows = _rows("SELECT round(v) FROM d", tables, {"d": {"v": "DOUBLE"}})
    assert [row[0] for row in rows] == [2, 0, -2, 4]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_now_is_timezone_aware():
    """https://github.com/jbylund/pg_mimic/issues/58

    ENV["CURRENTTIMESTAMP"] is datetime.now -- the host's local wall clock, with
    no offset -- where Postgres' now() is timestamptz. Comparing it against an
    aware column raises rather than answering, the executor having no timezone
    model at all. Workaround in examples/git_sql.py only; TableSession has none.
    """
    tables = {"d": [{"ts": datetime.datetime(2024, 1, 1, tzinfo=datetime.timezone.utc)}]}
    assert _rows("SELECT ts > CURRENT_TIMESTAMP FROM d", tables, {"d": {"ts": "TIMESTAMPTZ"}}) == [(False,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_date_trunc_is_implementable():
    """https://github.com/jbylund/pg_mimic/issues/58

    The one gap that cannot be closed by adding an ENV entry: the generator emits
    `TIMESTAMPTRUNC(value, MONTH)`, where MONTH resolves to ENV["MONTH"] -- a
    function object, not the string a TIMESTAMPTRUNC implementation could switch
    on. Fixing it means changing what sqlglot's Python generator emits.
    """
    tables = {"d": [{"ts": datetime.datetime(2024, 3, 15)}]}
    assert _rows("SELECT date_trunc('month', ts) FROM d", tables, {"d": {"ts": "TIMESTAMP"}}) == [(datetime.datetime(2024, 3, 1),)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_a_scalar_subquery_in_the_select_list_runs():
    """https://github.com/jbylund/pg_mimic/issues/58

    Emits invalid Python, so the client gets a SyntaxError out of generated code
    it never wrote. One subquery is enough, with or without an outer FROM; a
    subquery in WHERE, an IN (subquery) and a derived table are all fine.
    """
    tables = {"t": [{"a": 1}, {"a": 2}], "u": [{"b": 9}]}
    schema = {"t": {"a": "INT"}, "u": {"b": "INT"}}
    assert _rows("SELECT a, (SELECT max(b) FROM u) FROM t", tables, schema) == [(1, 9), (2, 9)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_a_correlated_scalar_subquery_runs():
    """https://github.com/jbylund/pg_mimic/issues/58

    Its own tripwire because the uncorrelated one above can be worked around
    downstream -- rewritten to a CROSS JOIN, or evaluated once and spliced in as a
    literal -- and this cannot: a correlated subquery has to be evaluated per outer
    row, which is the thing the executor fails at. psql's \\dT sends this form, so
    a fix for the uncorrelated case alone would flip that test and leave \\dT empty.
    """
    tables = {"t": [{"a": 1}, {"a": 20}], "u": [{"b": 9}]}
    schema = {"t": {"a": "INT"}, "u": {"b": "INT"}}
    assert _rows("SELECT a, (SELECT max(b) FROM u WHERE b < t.a) FROM t", tables, schema) == [(1, None), (20, 9)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_an_inline_values_list_can_be_selected_from():
    """https://github.com/jbylund/pg_mimic/issues/58

    `'Values' object has no attribute 'parts'`. psql's \\d foreign-key footer sends
    exactly this shape, inside an IN (...).
    """
    tables, schema = {"t": [{"a": 1}]}, {"t": {"a": "INT"}}
    assert _rows("SELECT * FROM (VALUES ('16384')) AS v", tables, schema) == [("16384",)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_in_takes_a_from_less_subquery():
    """https://github.com/jbylund/pg_mimic/issues/58

    Found underneath the inline VALUES: the obvious rewrite of `IN (SELECT * FROM
    (VALUES (x)))` is `IN (SELECT x)`, and that fails too. `IN` over a real table
    or a literal list is fine, and a FROM-less SELECT on its own is fine -- it is
    the combination.
    """
    tables, schema = {"t": [{"a": 1}]}, {"t": {"a": "INT"}}
    assert _rows("SELECT a FROM t WHERE a IN (SELECT 1)", tables, schema) == [(1,)]


def test_full_outer_join_preserves_unmatched_rows():
    """Not xfail: fixed upstream in v30.15.0 (commit f85ea4c2), which is why the floor
    first moved to 30.16.0 and TableSession no longer refuses it:
    https://github.com/jbylund/pg_mimic/issues/50

    Kept as a plain assertion so that a *regression* upstream is caught too.
    """
    tables = {"x": [{"a": 1}, {"a": 2}], "y": [{"b": 2}, {"b": 3}]}
    schema = {"x": {"a": "INT"}, "y": {"b": "INT"}}
    rows = _rows("SELECT x.a, y.b FROM x FULL OUTER JOIN y ON x.a = y.b", tables, schema)
    assert sorted(rows, key=str) == [(1, None), (2, 2), (None, 3)]


# Everything below was found by tools/fuzz. See the module docstring.

_DUPLICATES = ({"t": [{"a": 1}, {"a": 1}, {"a": 2}]}, {"t": {"a": "INT"}})
_MIXED = ({"m": [{"n": Decimal("2.5"), "v": 2.0}]}, {"m": {"n": "DECIMAL", "v": "DOUBLE"}})


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_distinct_keeps_columns_that_share_a_name():
    """https://github.com/jbylund/pg_mimic/issues/58

    Two output columns with the same name are silently merged into one, so the
    query answers a *narrower* result than it was asked for -- and with DISTINCT
    over a join, a shorter one too: nine rows of two columns become three of one.
    Dropping a column the client's Describe already promised is worse than an
    error, and no workaround in tables.py would catch it, since nothing peeks at
    result rows.

    `SELECT a, a` without DISTINCT is fine, which is what makes this a DISTINCT bug
    rather than a projection one.
    """
    assert _rows("SELECT DISTINCT a, a FROM t", *_NUMBERS) == [(1, 1), (2, 2), (3, 3)]
    rows = _rows("SELECT DISTINCT x.a, y.a FROM t AS x CROSS JOIN t AS y", *_NUMBERS)
    assert len(rows) == 9


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_star_expands_columns_that_share_a_name():
    """https://github.com/jbylund/pg_mimic/issues/58

    The same merge-by-name as above, reached through `*` instead of DISTINCT, and
    the reason `WITH c AS (SELECT 1, 1 FROM t) SELECT * FROM c` answers one column:
    both are named `_col_0`. A CTE or derived table whose columns are expressions
    rather than plain column references is the common way to hit it.
    """
    assert _rows("WITH c AS (SELECT a, a FROM t) SELECT * FROM c", *_NUMBERS) == [(1, 1), (2, 2), (3, 3)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_count_distinct_counts_distinct_values():
    """https://github.com/jbylund/pg_mimic/issues/58

    DISTINCT inside an aggregate is ignored outright -- `count(DISTINCT a)` is
    `count(a)`. A plausible number, never flagged, and wrong whenever the column
    has duplicates, which is the only reason anyone writes it.
    """
    assert _rows("SELECT COUNT(DISTINCT a) FROM t", *_DUPLICATES) == [(2,)]
    assert _rows("SELECT COUNT(DISTINCT 1) FROM t", *_DUPLICATES) == [(1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_casting_to_an_integer_rounds_rather_than_truncates():
    """https://github.com/jbylund/pg_mimic/issues/58

    `env.cast` sends the value through `int()`, which truncates toward zero, where
    Postgres rounds -- and by the same two rules as `round()`: an exact type goes
    half away from zero, a binary float goes through rint() and lands half to even.
    Verified against PostgreSQL 18.

    So `CAST(0.5 AS INT)` is 1 rather than 0, and `CAST(-1.5::double AS INT)` is -2
    rather than -1. Off-by-one in a value, silently.
    """
    exact = {"d": [{"n": Decimal(text)} for text in ("0.5", "-0.5", "2.5")]}
    assert _rows("SELECT CAST(n AS INT) FROM d", exact, {"d": {"n": "DECIMAL"}}) == [(1,), (-1,), (3,)]
    binary = {"d": [{"v": 0.5}, {"v": 2.5}, {"v": -1.5}]}
    assert _rows("SELECT CAST(v AS INT) FROM d", binary, {"d": {"v": "DOUBLE"}}) == [(0,), (2,), (-2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_round_to_a_scale_goes_half_away_from_zero():
    """https://github.com/jbylund/pg_mimic/issues/58

    The two-argument `round()`, split from
    `test_round_on_numeric_goes_half_away_from_zero` above because it is a separate
    code path with a separate symptom: ENV["ROUND"] passes the scale to Python's
    builtin, so 0.25 to one place is 0.2 where Postgres says 0.3.
    """
    assert _rows("SELECT ROUND(n, 1) FROM d", {"d": [{"n": Decimal("0.25")}]}, {"d": {"n": "DECIMAL"}}) == [(Decimal("0.3"),)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_like_honors_a_backslash_escape():
    """https://github.com/jbylund/pg_mimic/issues/58

    Postgres' LIKE takes backslash as its default escape character, so
    `LIKE 'a\\%b'` matches the literal text `a%b` and nothing else. The executor
    treats the backslash as a literal, matches nothing, and answers no rows.

    Distinct from the anchoring and metacharacter bugs fixed in v30.17.0 and
    covered by test_like_is_anchored_and_takes_metacharacters_literally: escaping
    is the part that still differs.
    """
    tables, schema = {"q": [{"s": "a%b"}, {"s": "axb"}]}, {"q": {"s": "TEXT"}}
    assert _rows("SELECT s FROM q WHERE s LIKE 'a\\%b'", tables, schema) == [("a%b",)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_exists_subquery_runs():
    """https://github.com/jbylund/pg_mimic/issues/58

    `EXISTS (subquery)` emits invalid Python, in a WHERE clause or a select list,
    correlated or not -- the client gets a SyntaxError out of generated code it
    never wrote. The same failure mode as the scalar subquery above and probably
    the same cause, but its own tripwire because EXISTS is the more common of the
    two by far: it is how an ORM asks whether a related row exists, and how
    `NOT IN (subquery)` is rewritten before it runs.
    """
    tables, schema = {"t": [{"a": 1}], "u": [{"b": 1}]}, {"t": {"a": "INT"}, "u": {"b": "INT"}}
    assert _rows("SELECT a FROM t WHERE EXISTS (SELECT 1 FROM u)", tables, schema) == [(1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_arithmetic_mixes_numeric_and_double():
    """https://github.com/jbylund/pg_mimic/issues/58

    A NUMERIC column against a DOUBLE one -- or against a float literal -- raises
    `unsupported operand type(s) for *: 'decimal.Decimal' and 'float'`, because the
    executor does Python arithmetic on the storage types and Python refuses that
    pair. Postgres resolves both to double precision and answers 5.

    The one on this list most likely to be the first thing a user hits, since it
    needs no subquery, no join and no unusual function: two numeric columns of
    different types multiplied together is enough.
    """
    assert _rows("SELECT n * v FROM m", *_MIXED) == [(5.0,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_in_a_subquery_is_null_for_a_null_left_operand():
    """https://github.com/jbylund/pg_mimic/issues/58

    `NULL IN (SELECT ...)` is NULL in Postgres, not FALSE -- three-valued logic,
    and the reason `NOT IN (subquery)` excludes a NULL row rather than keeping it.
    The executor answers FALSE, so the negation keeps it, and a `NOT IN` against a
    nullable column returns rows Postgres would not.
    """
    tables, schema = {"t": [{"a": 1}], "u": [{"b": 1}]}, {"t": {"a": "INT"}, "u": {"b": "INT"}}
    assert _rows("SELECT NULL IN (SELECT b FROM u) FROM t", tables, schema) == [(None,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_an_outer_join_pads_rows_a_where_clause_then_removes():
    """https://github.com/jbylund/pg_mimic/issues/58

    The NULL-padded row an outer join adds survives a WHERE clause that tests the
    padded column, so `WHERE x.a = 1` keeps a row whose `x.a` is NULL. Extra rows
    out of a filter that should have removed them, which is the worst shape a
    wrong answer can take: nothing about the result looks suspicious.
    """
    tables = {"x": [{"a": 1}, {"a": 2}], "y": [{"b": 9}]}
    schema = {"x": {"a": "INT"}, "y": {"b": "INT"}}
    assert _rows("SELECT x.a FROM x FULL JOIN y ON NULL WHERE x.a = 1", tables, schema) == [(1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_the_trim_and_reverse_functions_exist():
    """https://github.com/jbylund/pg_mimic/issues/58

    Missing from ENV, like `length()` was before v30.17.0, and fixable the same
    way. Grouped into one test because they are one gap: whichever of them is
    added first, the rest are a line each.

    Worth a tripwire despite being a plain omission because these are not exotic --
    `btrim` is what Postgres compiles a bare `trim(x)` to.
    """
    tables, schema = {"q": [{"s": "  ab  "}]}, {"q": {"s": "TEXT"}}
    assert _rows("SELECT BTRIM(s), LTRIM(s), RTRIM(s), REVERSE(BTRIM(s)) FROM q", tables, schema) == [("ab", "ab  ", "  ab", "ba")]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_extract_takes_a_day_of_week():
    """https://github.com/jbylund/pg_mimic/issues/58

    `EXTRACT(DOW FROM ts)` reaches `datetime.dow`, an attribute datetime does not
    have, so it raises rather than answering. YEAR, MONTH, DAY and HOUR all work,
    which is what makes this a per-field gap rather than a missing EXTRACT.

    2024-03-15 is a Friday, and Postgres numbers Sunday 0, so 5.
    """
    tables, schema = {"d": [{"ts": datetime.datetime(2024, 3, 15)}]}, {"d": {"ts": "TIMESTAMP"}}
    assert _rows("SELECT EXTRACT(DOW FROM ts) FROM d", tables, schema) == [(5,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_dividing_null_is_null():
    """https://github.com/jbylund/pg_mimic/issues/58

    `NULL / 1` raises `int() argument must be a string, a bytes-like object or a
    real number, not 'NoneType'`: integer division coerces with `int()` before
    checking for NULL. Any nullable integer column divided by anything is enough,
    and the other operators handle NULL correctly, so it is division specifically.
    """
    assert _rows("SELECT a / 1 FROM n", {"n": [{"a": None}]}, {"n": {"a": "INT"}}) == [(None,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_is_not_null_answers_a_boolean():
    """https://github.com/jbylund/pg_mimic/issues/58

    `x IS NOT NULL` answers `x` itself when x is not null, rather than TRUE -- the
    generated code is `x` where it should be `x is not None`. It reads as correct
    for as long as the value happens to be truthy, and a column of strings in a
    boolean-typed output column is a wire encoding error rather than a wrong row.

    `IS NULL` is fine; only the negation is wrong.
    """
    assert _rows("SELECT (CASE WHEN a = 1 THEN 'x' END) IS NOT NULL FROM t", *_NUMBERS) == [(True,), (False,), (False,)]
