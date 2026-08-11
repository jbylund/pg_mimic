"""asyncpg's type introspection, answered rather than executed.

test_asyncpg.py drives the real driver; these say which part broke when it does.
The coupling to asyncpg's query is deliberate but narrow, so the marker match is
worth pinning explicitly -- if asyncpg rewrites the query, this is the test that
explains why arrays stopped working.
"""

from __future__ import annotations

import pytest

from pg_mimic import ARRAY_OID, INT8, TEXT
from pg_mimic.typeinfo import _requested_oids, _rows_for, is_typeinfo_query, typeinfo_columns

_OID, _NS, _NAME, _KIND, _BASETYPE, _ELEMTYPE, _ELEMDELIM = range(7)
_DEPTH = 10


def test_matches_asyncpgs_query():
    assert is_typeinfo_query("WITH RECURSIVE typeinfo_tree(oid, ns, name) AS (SELECT 1)")


def test_does_not_match_ordinary_sql():
    """It has to be narrow: matching a session's own query would answer it with
    type metadata."""
    assert not is_typeinfo_query("SELECT * FROM users")
    assert not is_typeinfo_query("SELECT typname FROM pg_catalog.pg_type")


def test_column_shape_matches_what_asyncpg_reads_positionally():
    columns = typeinfo_columns()
    assert [c.name for c in columns] == [
        "oid",
        "ns",
        "name",
        "kind",
        "basetype",
        "elemtype",
        "elemdelim",
        "range_subtype",
        "attrtypoids",
        "attrnames",
        "depth",
        "basetype_name",
        "elemtype_name",
        "range_subtype_name",
    ]


_oid_parsing_testcases = {
    # Array parameters reach a session as a list of text strings.
    "list_of_strings": {"params": [["20", "1016"]], "expected": [20, 1016]},
    "list_of_ints": {"params": [[20, 1016]], "expected": [20, 1016]},
    # A client that declared no parameter type leaves the raw literal instead.
    "array_literal": {"params": ["{20,1016}"], "expected": [20, 1016]},
    "empty_literal": {"params": ["{}"], "expected": []},
    "empty_list": {"params": [[]], "expected": []},
    "no_params": {"params": [], "expected": []},
    "junk_is_skipped": {"params": [["20", "not-an-oid"]], "expected": [20]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_oid_parsing_testcases.values()))),
    argvalues=[[v for k, v in sorted(_oid_parsing_testcases[name].items())] for name in sorted(_oid_parsing_testcases)],
    ids=sorted(_oid_parsing_testcases),
)
def test_requested_oids(expected, params):
    assert _requested_oids(params) == expected


def test_scalar_type_is_described():
    (row,) = _rows_for([INT8])
    assert row[_OID] == INT8
    assert row[_NAME] == "int8"
    assert row[_NS] == "pg_catalog"
    assert row[_KIND] == "b"
    assert row[_ELEMTYPE] is None  # not an array


def test_array_brings_its_element_type_along():
    """asyncpg needs the element to build the array's codec. In the real query the
    recursion walks to it; here it has to be emitted alongside."""
    rows = _rows_for([ARRAY_OID[INT8]])
    by_oid = {row[_OID]: row for row in rows}
    assert set(by_oid) == {ARRAY_OID[INT8], INT8}
    assert by_oid[ARRAY_OID[INT8]][_ELEMTYPE] == INT8


def test_element_types_come_first():
    """The real query ends `ORDER BY depth DESC`, so the deeper rows lead."""
    rows = _rows_for([ARRAY_OID[INT8]])
    assert [row[_DEPTH] for row in rows] == [1, 0]
    assert rows[0][_OID] == INT8


def test_unknown_oids_are_skipped_not_invented():
    assert _rows_for([999999]) == []


def test_duplicate_requests_appear_once():
    rows = _rows_for([INT8, INT8, ARRAY_OID[INT8]])
    assert len([row for row in rows if row[_OID] == INT8]) == 1


def test_shared_element_is_not_repeated():
    rows = _rows_for([ARRAY_OID[INT8], INT8])
    assert len([row for row in rows if row[_OID] == INT8]) == 1


def test_delimiter_is_the_elements():
    """The query reads elemdelim from the element's row, not the array's."""
    rows = _rows_for([ARRAY_OID[TEXT]])
    array_row = next(row for row in rows if row[_OID] == ARRAY_OID[TEXT])
    assert array_row[_ELEMDELIM] == ","


def test_no_domains_ranges_or_composites():
    """pg_mimic has none of those, so the columns describing them are always NULL
    rather than guessed at."""
    for row in _rows_for([ARRAY_OID[INT8], INT8, TEXT]):
        assert row[_BASETYPE] is None  # domains
        assert row[7] is None  # range_subtype
        assert row[8] is None and row[9] is None  # composite attrs
