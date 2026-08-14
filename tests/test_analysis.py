"""AnalyzedQuery holds one query in several forms, and they have to stay separable.

sqlglot's passes rewrite in place, so the forms share nothing but the parse they
came from. When they did share, `qualified()` answered `SELECT 3000000000` before
`annotated()` had been called and `SELECT CAST(3000000000 AS BIGINT)` after -- the
same accessor, the same instance, a different answer depending on call order. A
container that does that is worse than none, since the whole point is asking for
the form a question is defined against (#111).
"""

from __future__ import annotations

import sqlglot

from pg_mimic.analysis import AnalyzedQuery

_SCHEMA = {"t": {"a": "INT"}}


def _analyzed(sql: str) -> AnalyzedQuery:
    return AnalyzedQuery(sqlglot.parse_one(sql, dialect="postgres"), schema=_SCHEMA)


def _sql(expression) -> str:
    return expression.sql(dialect="postgres")


def test_annotating_does_not_rewrite_the_qualified_tree():
    analyzed = _analyzed("SELECT 3000000000")
    before = _sql(analyzed.qualified())
    analyzed.annotated()
    assert _sql(analyzed.qualified()) == before
    assert "CAST" not in _sql(analyzed.qualified())
    assert "CAST" in _sql(analyzed.annotated())


def test_the_forms_are_distinct_objects():
    analyzed = _analyzed("SELECT a FROM t")
    assert analyzed.raw() is not analyzed.qualified()
    assert analyzed.qualified() is not analyzed.annotated()


def test_qualifying_does_not_rewrite_the_raw_tree():
    analyzed = _analyzed("SELECT * FROM t")
    analyzed.annotated()
    assert _sql(analyzed.raw()) == "SELECT * FROM t"
    assert "*" not in _sql(analyzed.qualified())


def test_each_form_is_derived_once_and_kept():
    analyzed = _analyzed("SELECT a FROM t")
    assert analyzed.qualified() is analyzed.qualified()
    assert analyzed.annotated() is analyzed.annotated()
    assert analyzed.column_names() is analyzed.column_names()


def test_names_come_from_the_query_as_written_whichever_form_is_asked_for_first():
    """column_names() rides on qualified(), so it must not matter whether the caller
    reached it directly or through annotated()."""
    direct = _analyzed("SELECT 'a' AS a, 1")
    direct.column_names()
    through_annotated = _analyzed("SELECT 'a' AS a, 1")
    through_annotated.annotated()
    assert direct.column_names() == ["a", "?column?"]
    assert through_annotated.column_names() == direct.column_names()


def test_a_failure_is_not_reported_as_a_missing_cache_key():
    """The memoization uses a KeyError miss, and qualify() raising is a normal path
    here -- so the real error must not arrive chained behind an internal one."""
    analyzed = _analyzed("SELECT nope FROM t")
    try:
        analyzed.qualified()
    except Exception as error:
        assert not isinstance(error, KeyError)
        assert error.__context__ is None
    else:
        raise AssertionError("expected an unresolvable column to raise")
