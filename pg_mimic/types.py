"""Primitive wire codec helpers and the Postgres OID / Python-type mapping.

pg_mimic speaks text format only (both directions), matching pg8000's own
client-side posture: it never requests binary either.
"""
from __future__ import annotations

import json
import struct
import uuid
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable

_PG_EPOCH = datetime(2000, 1, 1)
_PG_EPOCH_DATE = date(2000, 1, 1)

_int32 = struct.Struct("!i")
_int16 = struct.Struct("!h")


def pack_int32(value: int) -> bytes:
    return _int32.pack(value)


def unpack_int32(data: bytes, offset: int = 0) -> int:
    return _int32.unpack_from(data, offset)[0]


def pack_int16(value: int) -> bytes:
    return _int16.pack(value)


def unpack_int16(data: bytes, offset: int = 0) -> int:
    return _int16.unpack_from(data, offset)[0]


def read_cstring(data: bytes, offset: int = 0) -> tuple[str, int]:
    """Read a null-terminated string starting at offset. Returns (value, offset_after_nul)."""
    end = data.index(b"\x00", offset)
    return data[offset:end].decode("utf-8"), end + 1


# --- OID constants (postgresql pg_type.oid, stable across server versions) ---
BOOL = 16
BYTEA = 17
CHAR = 18
NAME = 19
INT8 = 20
INT2 = 21
INT4 = 23
TEXT = 25
OID = 26
JSON = 114
FLOAT4 = 700
FLOAT8 = 701
UNKNOWN = 705
MONEY = 790
MACADDR = 829
INET = 869
CIDR = 650
BPCHAR = 1042
VARCHAR = 1043
DATE = 1082
TIME = 1083
TIMESTAMP = 1114
TIMESTAMPTZ = 1184
INTERVAL = 1186
NUMERIC = 1700
UUID = 2950
JSONB = 3802
RECORD = 2249

# *_ARRAY oids, keyed by the scalar oid they hold
ARRAY_OID = {
    BOOL: 1000,
    BYTEA: 1001,
    CHAR: 1002,
    NAME: 1003,
    INT8: 1016,
    INT2: 1005,
    INT4: 1007,
    TEXT: 1009,
    OID: 1028,
    FLOAT4: 1021,
    FLOAT8: 1022,
    VARCHAR: 1015,
    BPCHAR: 1014,
    DATE: 1182,
    TIME: 1183,
    TIMESTAMP: 1115,
    TIMESTAMPTZ: 1185,
    INTERVAL: 1187,
    NUMERIC: 1231,
    UUID: 2951,
    JSON: 199,
    JSONB: 3807,
}


def _bool_out(v: bool) -> str:
    return "t" if v else "f"


def _bytes_out(v: bytes) -> str:
    return "\\x" + v.hex()


def _float_out(v: float) -> str:
    if v != v:
        return "NaN"
    if v == float("inf"):
        return "Infinity"
    if v == float("-inf"):
        return "-Infinity"
    return repr(v)


def _datetime_out(v: datetime) -> str:
    return v.isoformat(sep=" ")


def _timedelta_out(v: timedelta) -> str:
    # Postgres's default IntervalStyle ("postgres"): "N day[s] HH:MM:SS[.ffffff]".
    # Handles the common non-negative-duration case; Python's timedelta
    # normalization makes the general negative case (where days/seconds can
    # have different signs) enough of a corner case to skip for v1.
    hours, remainder = divmod(v.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    time_part = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    if v.microseconds:
        time_part += f".{v.microseconds:06d}"
    if v.days:
        plural = "s" if v.days != 1 else ""
        return f"{v.days} day{plural} {time_part}"
    return time_part


def _json_out(v: Any) -> str:
    return json.dumps(v)


# Exact-type dispatch, checked before the isinstance fallback scan (bool is an
# int subclass, datetime is a date subclass -- order below matters for the scan).
_TEXT_ENCODERS: dict[type, Any] = {
    bool: _bool_out,
    int: str,
    float: _float_out,
    Decimal: str,
    str: str,
    bytes: _bytes_out,
    bytearray: lambda v: _bytes_out(bytes(v)),
    datetime: _datetime_out,
    date: lambda v: v.isoformat(),
    time: lambda v: v.isoformat(),
    timedelta: _timedelta_out,
    uuid.UUID: str,
    dict: _json_out,
    list: _json_out,
}

_PY_TYPE_OID: dict[type, int] = {
    bool: BOOL,
    int: INT8,
    float: FLOAT8,
    Decimal: NUMERIC,
    str: TEXT,
    bytes: BYTEA,
    bytearray: BYTEA,
    datetime: TIMESTAMP,
    date: DATE,
    time: TIME,
    timedelta: INTERVAL,
    uuid.UUID: UUID,
    dict: JSONB,
    list: JSONB,
}


def oid_for_type(py_type: type) -> int:
    """Best-effort Postgres OID for a Python type. Defaults to TEXT for anything unknown."""
    if py_type in _PY_TYPE_OID:
        return _PY_TYPE_OID[py_type]
    for known_type, oid in _PY_TYPE_OID.items():
        if isinstance(py_type, type) and issubclass(py_type, known_type):
            return oid
    return TEXT


# --- Binary parameter decoding ------------------------------------------------------
#
# Real clients (psycopg, JDBC, asyncpg, ...) commonly send parameters in binary
# format for common scalar types even without being asked, choosing the exact
# scalar OID themselves (verified empirically against psycopg3: it always
# includes that OID in the preceding Parse message). pg_mimic decodes these
# into the same canonical text representation `encode_value` produces, so
# Session.query() always sees plain text-format parameter strings regardless
# of what the client put on the wire -- text format only, as far as any
# session author is concerned.


def _decode_bool(data: bytes) -> str:
    return _bool_out(data != b"\x00")


def _decode_int(fmt: str) -> Any:
    packer = struct.Struct("!" + fmt)

    def decode(data: bytes) -> str:
        return str(packer.unpack(data)[0])

    return decode


def _decode_float(fmt: str) -> Any:
    packer = struct.Struct("!" + fmt)

    def decode(data: bytes) -> str:
        return _float_out(packer.unpack(data)[0])

    return decode


def _decode_bytea(data: bytes) -> str:
    return _bytes_out(data)


def _decode_text(data: bytes) -> str:
    return data.decode("utf-8")


def _decode_date(data: bytes) -> str:
    days = struct.unpack("!i", data)[0]
    return (_PG_EPOCH_DATE + timedelta(days=days)).isoformat()


def _decode_timestamp(data: bytes) -> str:
    micros = struct.unpack("!q", data)[0]
    return _datetime_out(_PG_EPOCH + timedelta(microseconds=micros))


def _decode_uuid(data: bytes) -> str:
    return str(uuid.UUID(bytes=data))


_BINARY_DECODERS: dict[int, Callable[[bytes], str]] = {
    BOOL: _decode_bool,
    INT2: _decode_int("h"),
    INT4: _decode_int("i"),
    INT8: _decode_int("q"),
    FLOAT4: _decode_float("f"),
    FLOAT8: _decode_float("d"),
    BYTEA: _decode_bytea,
    TEXT: _decode_text,
    VARCHAR: _decode_text,
    BPCHAR: _decode_text,
    NAME: _decode_text,
    DATE: _decode_date,
    TIMESTAMP: _decode_timestamp,
    TIMESTAMPTZ: _decode_timestamp,
    UUID: _decode_uuid,
}


def decode_binary_param(oid: int, data: bytes) -> str:
    try:
        decoder = _BINARY_DECODERS[oid]
    except KeyError:
        raise ValueError(f"binary format not supported for parameter OID {oid}") from None
    return decoder(data)


def encode_value(value: Any) -> str:
    """Encode a Python value to its Postgres text-format representation.

    Callers are responsible for handling `None` (SQL NULL) before calling this --
    NULL has no text representation, it's signaled by a -1 length in DataRow.
    """
    func = _TEXT_ENCODERS.get(type(value))
    if func is not None:
        return func(value)
    for known_type, encoder in _TEXT_ENCODERS.items():
        if isinstance(value, known_type):
            return encoder(value)
    return str(value)
