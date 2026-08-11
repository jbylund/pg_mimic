"""Binary result format.

Wrong binary encoding produces plausible-looking wrong values rather than an
error, so these round-trip through psycopg with `binary=True` and compare against
the original Python objects -- asserting only that *some* bytes came back would
miss a byte-order or epoch mistake entirely.
"""

from __future__ import annotations

import struct
import uuid
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import psycopg
import pytest
from conftest import MockSession, ServerThread

from pg_mimic import PgServer, ResultColumn
from pg_mimic.results import encode_row, format_code_for
from pg_mimic.types import INTERVAL, JSON, JSONB, NUMERIC, TIMESTAMP, TIMESTAMPTZ, decode_binary_param, encode_value_binary


def _serve(columns, rows):
    session = MockSession()
    session.columns = columns
    session.rows = rows
    server = PgServer(session_factory=lambda: session)
    thread = ServerThread(server)
    return thread, thread.start()


_roundtrip_testcases = {
    "bool_true": {"py_type": bool, "value": True},
    "bool_false": {"py_type": bool, "value": False},
    "int_positive": {"py_type": int, "value": 4242},
    "int_negative": {"py_type": int, "value": -1},
    "int_large": {"py_type": int, "value": 2**62},
    "float": {"py_type": float, "value": 3.141592653589793},
    "float_negative": {"py_type": float, "value": -0.5},
    "text": {"py_type": str, "value": "hello, wörld"},
    "bytea": {"py_type": bytes, "value": b"\x00\x01\xfe\xff"},
    "date": {"py_type": date, "value": date(1999, 12, 31)},
    "timestamp": {"py_type": datetime, "value": datetime(2021, 3, 4, 5, 6, 7, 890123)},
    "uuid": {"py_type": uuid.UUID, "value": uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_roundtrip_testcases.values()))),
    argvalues=[[v for k, v in sorted(_roundtrip_testcases[name].items())] for name in sorted(_roundtrip_testcases)],
    ids=sorted(_roundtrip_testcases),
)
def test_binary_results_round_trip(py_type, value):
    thread, port = _serve([ResultColumn.for_type("c", py_type)], [(value,)])
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            with conn.cursor(binary=True) as cur:
                cur.execute("select c")
                (got,) = cur.fetchone()
    finally:
        thread.stop()
    assert got == value
    assert type(got) is type(value)


def test_binary_and_text_agree():
    """The same row read in either format must produce identical Python values."""
    row = (True, -7, 2.5, "x", b"\xff", date(2001, 1, 1), datetime(2001, 1, 1, 2, 3, 4))
    columns = [ResultColumn.for_type(f"c{i}", type(v)) for i, v in enumerate(row)]
    thread, port = _serve(columns, [row])
    try:
        dsn = f"host=127.0.0.1 port={port} user=u dbname=d"
        with psycopg.Connection.connect(dsn) as conn:
            with conn.cursor(binary=True) as cur:
                cur.execute("select c0, c1, c2, c3, c4, c5, c6")
                binary_row = cur.fetchone()
            with conn.cursor(binary=False) as cur:
                cur.execute("select c0, c1, c2, c3, c4, c5, c6")
                text_row = cur.fetchone()
    finally:
        thread.stop()
    assert binary_row == text_row == row


def test_null_is_null_in_binary():
    """NULL is a -1 length in DataRow, so it has no representation in either format."""
    thread, port = _serve([ResultColumn.for_type("c", int)], [(None,), (1,)])
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            with conn.cursor(binary=True) as cur:
                cur.execute("select c")
                assert cur.fetchall() == [(None,), (1,)]
    finally:
        thread.stop()


def test_unsupported_type_in_binary_is_refused_not_guessed():
    """The whole point of the narrow encoder table: no silently-wrong bytes."""
    thread, port = _serve([ResultColumn.for_type("c", Decimal)], [(Decimal("1.25"),)])
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            with conn.cursor(binary=True) as cur:
                with pytest.raises(psycopg.errors.FeatureNotSupported) as excinfo:
                    cur.execute("select c")
        assert str(NUMERIC) in str(excinfo.value)
    finally:
        thread.stop()


def test_unsupported_type_still_works_in_text():
    """Refusing binary must not narrow what text format can carry."""
    thread, port = _serve([ResultColumn.for_type("c", Decimal)], [(Decimal("1.25"),)])
    try:
        with psycopg.Connection.connect(f"host=127.0.0.1 port={port} user=u dbname=d") as conn:
            with conn.cursor(binary=False) as cur:
                cur.execute("select c")
                assert cur.fetchone() == (Decimal("1.25"),)
    finally:
        thread.stop()


def test_encode_value_binary_rejects_unknown_oid():
    with pytest.raises(ValueError, match="binary format not supported"):
        encode_value_binary(NUMERIC, Decimal("1"))


_format_code_testcases = {
    # Bind sends no codes at all: everything is text.
    "empty_means_text": {"codes": [], "index": 3, "expected": 0},
    # A single code applies to every column, however many there are.
    "single_applies_to_all": {"codes": [1], "index": 7, "expected": 1},
    # Otherwise it's strictly one code per column.
    "per_column": {"codes": [0, 1, 0], "index": 1, "expected": 1},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_format_code_testcases.values()))),
    argvalues=[[v for k, v in sorted(_format_code_testcases[name].items())] for name in sorted(_format_code_testcases)],
    ids=sorted(_format_code_testcases),
)
def test_format_code_for(codes, index, expected):
    assert format_code_for(codes, index) == expected


def test_mixed_per_column_formats():
    """A client may ask for binary on some columns and text on others."""
    columns = [ResultColumn.for_type("a", int), ResultColumn.for_type("b", int)]
    values = encode_row((1, 1), columns, [0, 1])
    assert values == [b"1", b"\x00\x00\x00\x00\x00\x00\x00\x01"]


def test_statement_describe_always_declares_text():
    """Result formats are only chosen at Bind, so a statement-level Describe can
    only honestly say text -- real Postgres does the same. Only the portal-level
    RowDescription, sent after Bind, reflects what the client actually asked for."""
    from pg_mimic.connection import _field_specs

    columns = [ResultColumn.for_type("a", int), ResultColumn.for_type("b", int)]
    assert [f.format_code for f in _field_specs(columns)] == [0, 0]
    assert [f.format_code for f in _field_specs(columns, [1])] == [1, 1]
    assert [f.format_code for f in _field_specs(columns, [0, 1])] == [0, 1]


def _read_both_formats(columns, rows, sql="select c"):
    thread, port = _serve(columns, rows)
    try:
        dsn = f"host=127.0.0.1 port={port} user=u dbname=d"
        out = []
        with psycopg.Connection.connect(dsn) as conn:
            for binary in (False, True):
                with conn.cursor(binary=binary) as cur:
                    cur.execute(sql)
                    out.append(cur.fetchone()[0])
    finally:
        thread.stop()
    return out


def test_timestamptz_preserves_the_instant_in_both_formats():
    aware = datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=-5)))
    text_value, binary_value = _read_both_formats([ResultColumn("c", TIMESTAMPTZ)], [(aware,)])
    assert text_value == binary_value == aware


def test_timestamp_drops_the_offset_identically_in_both_formats():
    """A TIMESTAMP column is wall-clock, so the offset is discarded -- lossy, but
    the two formats have to be lossy the *same* way or they'd disagree."""
    aware = datetime(2021, 3, 4, 5, 6, 7, tzinfo=timezone(timedelta(hours=-5)))
    text_value, binary_value = _read_both_formats([ResultColumn("c", TIMESTAMP)], [(aware,)])
    assert text_value == binary_value == datetime(2021, 3, 4, 5, 6, 7)


_json_testcases = {
    "jsonb_object": {"oid": JSONB, "value": {"a": 1, "b": [1, 2, None], "c": "ü"}},
    "jsonb_array": {"oid": JSONB, "value": [1, {"x": True}]},
    "jsonb_scalar": {"oid": JSONB, "value": {"n": 1.5}},
    "json_object": {"oid": JSON, "value": {"a": 1, "b": [1, 2, None], "c": "ü"}},
    "json_array": {"oid": JSON, "value": [1, {"x": True}]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_json_testcases.values()))),
    argvalues=[[v for k, v in sorted(_json_testcases[name].items())] for name in sorted(_json_testcases)],
    ids=sorted(_json_testcases),
)
def test_json_round_trips_in_both_formats(oid, value):
    text_value, binary_value = _read_both_formats([ResultColumn("c", oid)], [(value,)])
    assert text_value == binary_value == value


def test_jsonb_binary_carries_the_version_byte():
    """jsonb's binary form is a 1-byte version prefix then the JSON text; json's is
    the bare text. Getting the prefix wrong shifts the whole document by a byte."""
    assert encode_value_binary(JSONB, {"a": 1}) == b"\x01" + encode_value_binary(JSON, {"a": 1})
    assert encode_value_binary(JSON, {"a": 1}) == b'{"a": 1}'


def test_jsonb_rejects_unknown_wire_version():
    """A future jsonb version must fail loudly, not have its version byte parsed
    as the first character of the document."""
    with pytest.raises(ValueError, match="unsupported jsonb wire format version"):
        decode_binary_param(JSONB, b"\x02{}")
    with pytest.raises(ValueError, match="unsupported jsonb wire format version"):
        decode_binary_param(JSONB, b"")


def test_json_binary_params_are_decoded_to_text():
    """Session authors see text params regardless of what the client sent."""
    assert decode_binary_param(JSONB, b'\x01{"a": 1}') == '{"a": 1}'
    assert decode_binary_param(JSON, b'{"a": 1}') == '{"a": 1}'


_interval_testcases = {
    "days": {"value": timedelta(days=1)},
    "days_and_time": {"value": timedelta(days=2, hours=3, minutes=4, seconds=5)},
    "time_only": {"value": timedelta(hours=2, minutes=3, seconds=4)},
    "microseconds": {"value": timedelta(microseconds=5)},
    "fractional_second": {"value": timedelta(seconds=1, microseconds=500000)},
    "zero": {"value": timedelta(0)},
    "large": {"value": timedelta(days=400)},
    # Python normalises a negative duration to a negative day count with a
    # non-negative time of day, which is exactly how Postgres keeps the two
    # fields -- so these must survive rather than flipping sign somewhere.
    "negative_hours": {"value": timedelta(hours=-23)},
    "negative_days_and_time": {"value": timedelta(days=-2, hours=-3)},
    "negative_whole_day": {"value": timedelta(days=-1)},
    "negative_with_microseconds": {"value": timedelta(days=-400, microseconds=-1)},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_interval_testcases.values()))),
    argvalues=[[v for k, v in sorted(_interval_testcases[name].items())] for name in sorted(_interval_testcases)],
    ids=sorted(_interval_testcases),
)
def test_interval_round_trips_in_both_formats(value):
    text_value, binary_value = _read_both_formats([ResultColumn.for_type("c", timedelta)], [(value,)])
    assert text_value == binary_value == value


def test_interval_binary_layout():
    """microseconds (int64), days (int32), months (int32) -- in that order. Getting
    the field order wrong still produces a valid-looking interval, just the wrong
    one, so pin the bytes."""
    assert encode_value_binary(INTERVAL, timedelta(days=3)) == struct.pack("!qii", 0, 3, 0)
    assert encode_value_binary(INTERVAL, timedelta(hours=1)) == struct.pack("!qii", 3_600_000_000, 0, 0)
    # A negative duration keeps a non-negative time-of-day against a negative day.
    assert encode_value_binary(INTERVAL, timedelta(hours=-23)) == struct.pack("!qii", 3_600_000_000, -1, 0)


def test_interval_months_are_rendered_as_text():
    """timedelta has no month, so a month-bearing interval parameter can only reach
    the session as text. Postgres's own "N mons" syntax, so it round-trips."""
    assert decode_binary_param(INTERVAL, struct.pack("!qii", 0, 0, 3)) == "3 mons 00:00:00"
    assert decode_binary_param(INTERVAL, struct.pack("!qii", 0, 5, 14)) == "14 mons 5 days 00:00:00"


def test_interval_rejects_non_timedelta():
    with pytest.raises(ValueError, match="expected a timedelta"):
        encode_value_binary(INTERVAL, "1 day")
