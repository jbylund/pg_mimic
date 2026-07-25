"""Real drivers (verified against psycopg3) send common scalar parameter
types in *binary* format by default, not text -- even though results stay
text-format. pg_mimic decodes these transparently so Session.query() always
sees plain text strings. Each case here pins down one binary decoder in
pg_mimic/types.py (_decode_bool/_decode_int/_decode_float/_decode_bytea/
_decode_date/_decode_timestamp/_decode_uuid)."""
from __future__ import annotations

import datetime
import uuid

import psycopg
import pytest
from psycopg.types.numeric import Int4BinaryDumper

from pg_mimic import ResultColumn

testcases = {
    "bool_true": {"value": True, "expected_param": "t"},
    "bool_false": {"value": False, "expected_param": "f"},
    "int2_small": {"value": 42, "expected_param": "42"},
    "int4_medium": {"value": 100_000, "expected_param": "100000"},
    "int8_large": {"value": 123_456_789_012, "expected_param": "123456789012"},
    "float8": {"value": 3.14, "expected_param": "3.14"},
    "bytea": {"value": b"hello", "expected_param": "\\x68656c6c6f"},
    "date": {"value": datetime.date(2024, 1, 1), "expected_param": "2024-01-01"},
    "timestamp": {"value": datetime.datetime(2024, 1, 1, 12, 30), "expected_param": "2024-01-01 12:30:00"},
    "uuid": {
        "value": uuid.UUID("12345678-1234-5678-1234-567812345678"),
        "expected_param": "12345678-1234-5678-1234-567812345678",
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(testcases.values()))),
    argvalues=[[v for k, v in sorted(testcases[name].items())] for name in sorted(testcases)],
    ids=sorted(testcases),
)
def test_binary_param_decoded_to_canonical_text(conn, mock_session, value, expected_param):
    mock_session.columns = [ResultColumn.for_type("x", str)]
    mock_session.rows = [("ok",)]

    with conn.cursor() as cur:
        cur.execute("SELECT %s", (value,))
        cur.fetchall()

    sql, params = mock_session.queries[-1]
    assert params == [expected_param]


def test_unrecognized_binary_oid_gives_clear_error(conn, mock_session):
    # Decimal/numeric has no binary decoder implemented (real drivers send it
    # as text by default, so this is deliberately unsupported) -- forcing
    # binary format for it should fail cleanly, not corrupt data silently.
    class FakeNumericAsBinaryInt(Int4BinaryDumper):
        # Piggyback on int4's OID-4-byte binary wire shape but relabel it as
        # NUMERIC (oid 1700) purely to exercise pg_mimic's "unknown binary
        # OID" error path deterministically, without relying on some other
        # driver actually doing this by default.
        oid = 1700

    mock_session.columns = [ResultColumn.for_type("x", str)]

    with conn.cursor() as cur:
        cur.adapters.register_dumper(int, FakeNumericAsBinaryInt)
        with pytest.raises(psycopg.errors.FeatureNotSupported):
            cur.execute("SELECT %s", (7,))
