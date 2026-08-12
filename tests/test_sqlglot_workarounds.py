"""Strict-xfail tripwires for the sqlglot executor bugs pg_mimic works around.

Every test here asserts what *Postgres* answers, runs against sqlglot directly
rather than through pg_mimic, and is marked `xfail(strict=True)`. So each one
fails today -- which is the expected outcome -- and the moment sqlglot fixes the
bug the test XPASSes, and strict turns that into a build failure telling you a
workaround is now deletable.

The point is that a workaround has no natural expiry. Two of them silently
outlived their cause and were only noticed by reading the comments (#50): a
`FULL OUTER JOIN` refused for a bug fixed in sqlglot v30.15.0, and an INTERVAL
patch whose stated justification had gone stale. These tests are how the next one
announces itself instead.

Each test names the workaround it justifies, so a failure points straight at the
code to delete. See #49 for the inventory.

A failure here is never a pg_mimic regression: it means good news upstream.
"""

from __future__ import annotations

import datetime

import pytest
from sqlglot.executor import execute

_UPSTREAM_FIXED = "sqlglot fixed this -- remove the workaround named in the test"


def _rows(sql: str, tables: dict, schema: dict) -> list[tuple]:
    return [tuple(row) for row in execute(sql, schema=schema, tables=tables, dialect="postgres").rows]


_NUMBERS = ({"t": [{"a": 1}, {"a": 2}, {"a": 3}]}, {"t": {"a": "INT"}})
_TEXT = ({"q": [{"s": "Bump version"}, {"s": "Add feature"}]}, {"q": {"s": "TEXT"}})


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_offset_is_applied():
    """Workaround: `_take_row_window` / `rows_sliced_here` in tables.py."""
    assert _rows("SELECT a FROM t ORDER BY a OFFSET 1", *_NUMBERS) == [(2,), (3,)]
    assert _rows("SELECT a FROM t ORDER BY a LIMIT 1 OFFSET 1", *_NUMBERS) == [(2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_order_by_on_a_set_operation_keeps_the_columns():
    """Workaround: `_take_result_order` / `_sorted_rows` in tables.py.

    The rows come back column-less -- `[(), (), ()]` -- not merely unsorted.
    """
    assert _rows("SELECT a FROM t UNION SELECT a FROM t ORDER BY a", *_NUMBERS) == [(1,), (2,), (3,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_not_in_a_subquery_filters():
    """Workaround: `_rewrite_not_in` in tables.py. `NOT IN (literals)` is fine."""
    tables = {"t": [{"a": 1}, {"a": 2}], "u": [{"b": 1}]}
    schema = {"t": {"a": "INT"}, "u": {"b": "INT"}}
    assert _rows("SELECT a FROM t WHERE a NOT IN (SELECT b FROM u)", tables, schema) == [(2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_descending_order_places_nulls_instead_of_raising():
    """Workaround: `_rewrite_null_ordering` in tables.py.

    Ascending coincidentally matches Postgres; descending raises TypeError.
    """
    tables, schema = {"n": [{"a": 1}, {"a": None}]}, {"n": {"a": "INT"}}
    assert _rows("SELECT a FROM n ORDER BY a DESC", tables, schema) == [(None,), (1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_a_set_operation_may_read_one_table_twice():
    """Workaround: `canonicalize_table_aliases=True` in `TableSession._plan`.

    The planner keys steps by table name, so the second branch reruns the first's.
    """
    assert _rows("SELECT a FROM t WHERE a = 1 UNION ALL SELECT a FROM t WHERE a = 2", *_NUMBERS) == [(1,), (2,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_limit_on_a_parenthesized_query_is_applied():
    """Workaround: `_flatten_parenthesized` in tables.py."""
    assert _rows("(SELECT a FROM t) LIMIT 1", *_NUMBERS) == [(1,)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_tablesample_is_applied():
    """Workaround: refused by `_reject_silently_ignored` in tables.py."""
    assert _rows("SELECT a FROM t TABLESAMPLE BERNOULLI (0)", *_NUMBERS) == []


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_like_is_anchored_and_takes_metacharacters_literally():
    """Workaround: `catalog_rewrite`'s ENV patching, and see #38.

    Postgres' LIKE is fully anchored, and only % and _ are wildcards.
    """
    assert _rows("SELECT s FROM q WHERE s LIKE 'Bump'", *_TEXT) == []
    assert _rows("SELECT s FROM q WHERE s LIKE 'Bump vers.on'", *_TEXT) == []


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_not_like_excludes_what_like_matches():
    """Workaround: see #44. Fix in flight: tobymao/sqlglot#8139."""
    assert _rows("SELECT s FROM q WHERE s NOT LIKE 'Bump%'", *_TEXT) == [("Add feature",)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_ilike_exists():
    """Workaround: none -- it raises. See #38."""
    assert _rows("SELECT s FROM q WHERE s ILIKE 'bump%'", *_TEXT) == [("Bump version",)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_length_and_concatenation_exist():
    """Workaround: none -- both raise. See #38, and examples/git_sql.py's ENV patch."""
    assert _rows("SELECT length(s) FROM q", *_TEXT) == [(12,), (11,)]
    assert _rows("SELECT s || '!' FROM q", *_TEXT) == [("Bump version!",), ("Add feature!",)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_interval_handles_years_and_months():
    """Workaround: `_interval_delta` in examples/git_sql.py.

    A literal operand is constant-folded before the executor runs, so this needs a
    column to reach ENV["INTERVAL"], which is `timedelta(**{unit: n})` -- and
    timedelta has no years or months.
    """
    tables, schema = {"d": [{"ts": datetime.datetime(2024, 3, 15)}]}, {"d": {"ts": "TIMESTAMP"}}
    assert _rows("SELECT ts - INTERVAL '1 year' FROM d", tables, schema) == [(datetime.datetime(2023, 3, 15),)]
    assert _rows("SELECT ts - INTERVAL '1 month' FROM d", tables, schema) == [(datetime.datetime(2024, 2, 15),)]


@pytest.mark.xfail(strict=True, reason=_UPSTREAM_FIXED)
def test_typed_division_keeps_a_real_operands_fraction():
    """Workaround: none -- see #48. Fix in flight: tobymao/sqlglot#8138."""
    assert _rows("SELECT a / 2.0 FROM t", *_NUMBERS)[0] == (0.5,)


def test_full_outer_join_preserves_unmatched_rows():
    """Not xfail: fixed upstream in v30.15.0 (commit f85ea4c2), which is why
    TableSession's refusal of it is now wrong. See #50.

    Kept as a plain assertion so that a *regression* upstream is caught too.
    """
    tables = {"x": [{"a": 1}, {"a": 2}], "y": [{"b": 2}, {"b": 3}]}
    schema = {"x": {"a": "INT"}, "y": {"b": "INT"}}
    rows = _rows("SELECT x.a, y.b FROM x FULL OUTER JOIN y ON x.a = y.b", tables, schema)
    assert sorted(rows, key=str) == [(1, None), (2, 2), (None, 3)]
