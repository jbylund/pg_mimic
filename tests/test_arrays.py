"""Postgres arrays, text and binary.

Round-trips go through psycopg in both wire formats and compare against the
original Python values: a wrong dimension header or a mis-quoted element yields a
plausible-looking wrong *value* rather than an error, so asserting that something
came back would miss the bugs that matter.
"""

from __future__ import annotations

import uuid
from datetime import date

import psycopg
import pytest
from conftest import MockSession, ServerThread

from pg_mimic import ARRAY_OID, INT8, TEXT, PgServer, ResultColumn, Session
from pg_mimic.arrays import format_array_literal, parse_array_literal
from pg_mimic.types import decode_binary_param, encode_value_binary, oid_for_type


def _read_both_formats(columns, rows, sql="select c"):
    session = MockSession()
    session.columns = columns
    session.rows = rows
    server = PgServer(session_factory=lambda: session)
    thread = ServerThread(server)
    port = thread.start()
    try:
        out = []
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            for binary in (False, True):
                with conn.cursor(binary=binary) as cur:
                    cur.execute(sql)
                    out.append(cur.fetchone()[0])
    finally:
        thread.stop()
    return out


_roundtrip_testcases = {
    "int8": {"py_type": list[int], "value": [1, 2, 3]},
    "int8_negative": {"py_type": list[int], "value": [-1, 0, 2**40]},
    "int8_with_null": {"py_type": list[int], "value": [1, None, 3]},
    "int8_empty": {"py_type": list[int], "value": []},
    "text": {"py_type": list[str], "value": ["a", "b"]},
    "text_needing_quotes": {"py_type": list[str], "value": ["a,b", 'say "hi"', "back\\slash", "{braced}", " padded "]},
    "text_null_lookalike": {"py_type": list[str], "value": ["NULL", "null", ""]},
    "text_with_null": {"py_type": list[str], "value": ["a", None]},
    "bool": {"py_type": list[bool], "value": [True, False, None]},
    "float": {"py_type": list[float], "value": [1.5, -0.25]},
    "date": {"py_type": list[date], "value": [date(2001, 2, 3), date(1999, 12, 31)]},
    "uuid": {"py_type": list[uuid.UUID], "value": [uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")]},
    "two_dimensional": {"py_type": list[list[int]], "value": [[1, 2], [3, 4]]},
    "three_dimensional": {"py_type": list[list[list[int]]], "value": [[[1], [2]], [[3], [4]]]},
    "two_dimensional_text": {"py_type": list[list[str]], "value": [["a,b", "c"], ["d", None]]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_roundtrip_testcases.values()))),
    argvalues=[[v for k, v in sorted(_roundtrip_testcases[name].items())] for name in sorted(_roundtrip_testcases)],
    ids=sorted(_roundtrip_testcases),
)
def test_arrays_round_trip_in_both_formats(py_type, value):
    text_value, binary_value = _read_both_formats([ResultColumn.for_type("c", py_type)], [(value,)])
    assert text_value == binary_value == value


def test_null_array_is_still_null():
    """A NULL array is a -1 length in DataRow -- distinct from an empty array."""
    text_value, binary_value = _read_both_formats([ResultColumn.for_type("c", list[int])], [(None,)])
    assert text_value is binary_value is None


def test_ragged_arrays_are_rejected():
    """Postgres arrays are rectangular by definition; there is no wire form for a
    ragged one, so it has to be refused rather than silently reshaped."""
    with pytest.raises(ValueError, match="ragged"):
        encode_value_binary(ARRAY_OID[INT8], [[1, 2], [3]])
    with pytest.raises(ValueError, match="ragged"):
        format_array_literal(ARRAY_OID[INT8], [[1, 2], [3]])


_declaration_testcases = {
    "list_str": {"py_type": list[str], "expected": ARRAY_OID[TEXT]},
    "list_int": {"py_type": list[int], "expected": ARRAY_OID[INT8]},
    # Array OIDs say nothing about dimensionality -- it rides in each value's
    # dimension header -- so nesting does not change the declared type.
    "list_list_int": {"py_type": list[list[int]], "expected": ARRAY_OID[INT8]},
    "list_list_list_int": {"py_type": list[list[list[int]]], "expected": ARRAY_OID[INT8]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_declaration_testcases.values()))),
    argvalues=[[v for k, v in sorted(_declaration_testcases[name].items())] for name in sorted(_declaration_testcases)],
    ids=sorted(_declaration_testcases),
)
def test_declaration_maps_to_array_oid(py_type, expected):
    assert oid_for_type(py_type) == expected


_literal_testcases = {
    "plain": {"literal": "{1,2,3}", "expected": ["1", "2", "3"]},
    "empty": {"literal": "{}", "expected": []},
    "unquoted_null_is_none": {"literal": "{1,NULL,3}", "expected": ["1", None, "3"]},
    "lowercase_null_is_none": {"literal": "{null}", "expected": [None]},
    # A *quoted* NULL is the four-character string, not the null marker.
    "quoted_null_is_a_string": {"literal": '{"NULL"}', "expected": ["NULL"]},
    "empty_string": {"literal": '{""}', "expected": [""]},
    "comma_inside_quotes": {"literal": '{"a,b",c}', "expected": ["a,b", "c"]},
    "escaped_quote": {"literal": '{"say \\"hi\\""}', "expected": ['say "hi"']},
    "escaped_backslash": {"literal": '{"back\\\\slash"}', "expected": ["back\\slash"]},
    "braces_inside_quotes": {"literal": '{"{not nested}"}', "expected": ["{not nested}"]},
    "nested": {"literal": "{{1,2},{3,4}}", "expected": [["1", "2"], ["3", "4"]]},
    "whitespace_between_elements": {"literal": "{ 1 , 2 }", "expected": ["1", "2"]},
    # Postgres prefixes explicit bounds when the lower bound isn't 1; a Python
    # list can't carry them either way.
    "explicit_bounds_prefix": {"literal": "[2:4]={1,2,3}", "expected": ["1", "2", "3"]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_literal_testcases.values()))),
    argvalues=[[v for k, v in sorted(_literal_testcases[name].items())] for name in sorted(_literal_testcases)],
    ids=sorted(_literal_testcases),
)
def test_parse_array_literal(literal, expected):
    assert parse_array_literal(literal) == expected


_malformed_testcases = {
    "unterminated": {"literal": "{1,2"},
    "not_an_array": {"literal": "1,2,3"},
    "unterminated_quote": {"literal": '{"abc}'},
    "trailing_junk": {"literal": "{1,2} extra"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_malformed_testcases.values()))),
    argvalues=[[v for k, v in sorted(_malformed_testcases[name].items())] for name in sorted(_malformed_testcases)],
    ids=sorted(_malformed_testcases),
)
def test_malformed_literals_are_rejected(literal):
    with pytest.raises(ValueError):
        parse_array_literal(literal)


def test_literal_round_trips_through_the_parser():
    """Quoting and parsing are inverses -- the property that actually matters, since
    the two halves are used by opposite ends of the connection."""
    awkward = ["a,b", 'say "hi"', "back\\slash", "{braced}", " padded ", "NULL", "", None]
    literal = format_array_literal(ARRAY_OID[TEXT], awkward)
    assert parse_array_literal(literal) == awkward


def test_binary_elements_decode_to_text_like_scalar_params():
    """An int8[] parameter arrives as ["1","2"], for the same reason a scalar int8
    parameter arrives as "1" -- the session's contract is text either way."""
    encoded = encode_value_binary(ARRAY_OID[INT8], [1, 2, None])
    assert decode_binary_param(ARRAY_OID[INT8], encoded) == ["1", "2", None]


def _send_param(value, binary=False, session=None):
    session = session or MockSession()
    session.columns = [ResultColumn.for_type("x", str)]
    session.rows = [("ok",)]
    server = PgServer(session_factory=lambda: session)
    thread = ServerThread(server)
    port = thread.start()
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            with conn.cursor(binary=binary) as cur:
                cur.execute("select x from t where a = %s", (value,))
                cur.fetchall()
    finally:
        thread.stop()
    return session.queries[-1][1]


@pytest.mark.parametrize(argnames=["binary"], argvalues=[[False], [True]], ids=["text", "binary"])
def test_array_param_reaches_the_session_as_a_list(binary):
    """The point of parsing on the way in: the session gets the same Python shape
    whichever wire format the client picked."""
    assert _send_param([1, 2, 3], binary=binary) == [["1", "2", "3"]]


def test_nested_array_param_keeps_its_shape():
    assert _send_param([[1, 2], [3, 4]]) == [[["1", "2"], ["3", "4"]]]


def test_text_array_param_without_a_declared_oid_stays_text():
    """psycopg sends text[] parameters with OID 0, leaving the type to the server.
    With no declared OID there is no way to tell the literal `{a,b}` from a string
    that merely looks like one, so it is left alone rather than guessed at.

    Binary parameters are unaffected -- those already require a known OID.
    """
    assert _send_param(["a", "b"]) == ["{a,b}"]


def test_session_can_declare_param_oids_the_client_omitted():
    """The way out of the above: parameter decoding reads the Statement's
    param_oids, so a session that knows its own SQL can fill in what the client
    left unspecified."""

    class DeclaringSession(MockSession):
        async def prepare(self, sql, param_oids):
            statement = await super().prepare(sql, param_oids)
            statement.param_oids = [ARRAY_OID[TEXT] if oid is None else oid for oid in param_oids]
            return statement

    assert _send_param(["a", "b"], session=DeclaringSession()) == [["a", "b"]]


class _EchoSession(Session):
    """`select %s` -- hands the parameter straight back as an array column."""

    def __init__(self, oid):
        self.oid = oid
        self.seen = None

    async def describe(self, sql, param_oids):
        return [ResultColumn("echo", self.oid)]

    async def query(self, sql, params):
        self.seen = params[0]
        yield (params[0],)


def _echo(value, oid, binary=False):
    session = _EchoSession(oid)
    server = PgServer(session_factory=lambda: session)
    thread = ServerThread(server)
    port = thread.start()
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            with conn.cursor(binary=binary) as cur:
                cur.execute("select %s", (value,))
                return cur.fetchone()[0], session.seen
    finally:
        thread.stop()


_echo_testcases = {
    "int_text": {"value": [1, 2, 3], "oid": ARRAY_OID[INT8], "binary": False},
    "int_binary": {"value": [1, 2, 3], "oid": ARRAY_OID[INT8], "binary": True},
    "nested_int": {"value": [[1, 2], [3, 4]], "oid": ARRAY_OID[INT8], "binary": False},
    # psycopg sends text[] with OID 0, so these exercise the undeclared-type path.
    "text_undeclared_oid": {"value": ["a", "b"], "oid": ARRAY_OID[TEXT], "binary": False},
    "text_needing_quotes": {"value": ["a,b", 'q"x', None], "oid": ARRAY_OID[TEXT], "binary": False},
    "empty": {"value": [], "oid": ARRAY_OID[INT8], "binary": False},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_echo_testcases.values()))),
    argvalues=[[v for k, v in sorted(_echo_testcases[name].items())] for name in sorted(_echo_testcases)],
    ids=sorted(_echo_testcases),
)
def test_array_param_echoes_back_unchanged(binary, oid, value):
    """A client gets back exactly the array it sent.

    This holds even when the client declares no parameter type (psycopg does that
    for text[]): pg_mimic passes the literal through untouched rather than guessing
    at it, and an untouched array literal is still one the client can parse. The
    undeclared case costs the *session* a parsed list, not the client its value.
    """
    got, _seen = _echo(value, oid, binary=binary)
    assert got == value


def test_undeclared_param_costs_the_session_not_the_client():
    """The visible edge of the OID-0 limitation: same wire result either way, but
    the session sees a literal string instead of a list."""
    got_declared, seen_declared = _echo([1, 2, 3], ARRAY_OID[INT8])
    got_undeclared, seen_undeclared = _echo(["a", "b"], ARRAY_OID[TEXT])

    assert got_declared == [1, 2, 3] and seen_declared == ["1", "2", "3"]
    assert got_undeclared == ["a", "b"] and seen_undeclared == "{a,b}"
