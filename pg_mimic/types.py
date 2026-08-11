"""Primitive wire codec helpers and the Postgres OID / Python-type mapping.

Text format is the default in both directions, matching pg8000's own client-side
posture: it never requests binary either. Binary is supported for the common
scalar types where a client asks for it -- inbound via `decode_binary_param`,
outbound via `encode_value_binary` -- and refused explicitly otherwise.
"""

from __future__ import annotations

import json
import struct
import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, get_args, get_origin

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


def _decimal_out(v: Decimal) -> str:
    # Plain notation, never exponent notation: Postgres renders numeric that way,
    # and str(Decimal) does not -- str(Decimal("1E+10")) is "1E+10" where Postgres
    # says 10000000000. That difference would also put the text and binary paths
    # at odds, since the binary format has no exponent to carry.
    return format(v, "f")


def _json_out(v: Any) -> str:
    return json.dumps(v)


# Exact-type dispatch, checked before the isinstance fallback scan (bool is an
# int subclass, datetime is a date subclass -- order below matters for the scan).
_TEXT_ENCODERS: dict[type, Any] = {
    bool: _bool_out,
    int: str,
    float: _float_out,
    Decimal: _decimal_out,
    str: str,
    bytes: _bytes_out,
    bytearray: lambda v: _bytes_out(bytes(v)),
    datetime: _datetime_out,
    date: lambda v: v.isoformat(),
    time: lambda v: v.isoformat(),
    timedelta: _timedelta_out,
    uuid.UUID: str,
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
}


def oid_for_type(py_type: Any) -> int:
    """Postgres OID for a Python type, inferred only where it is unambiguous.

    Scalars infer from `_PY_TYPE_OID`, and `list[T]` infers to T's array type
    (`list[str]` -> text[], `list[list[int]]` -> int8[], since an array OID says
    nothing about dimensionality). Anything unrecognised falls back to TEXT, as
    before.

    Bare `list` and `dict` raise instead: a list could equally be an array or a
    json document, and column shape is declared before any row exists, so there
    is nothing to inspect that would settle it. Name the type you mean --
    `ResultColumn("c", JSONB)` or `list[str]`.
    """
    if get_origin(py_type) is list:
        from .arrays import ARRAY_OID

        # Unwrap nesting rather than recursing: list[list[int]] is a 2-dimensional
        # int8[], and Postgres array OIDs say nothing about dimensionality -- it's
        # carried per value, in the dimension header.
        element = py_type
        while get_origin(element) is list:
            args = get_args(element)
            if len(args) != 1:
                raise TypeError(f"{py_type!r} must name exactly one element type, e.g. list[str]")
            element = args[0]

        element_oid = oid_for_type(element)
        try:
            return ARRAY_OID[element_oid]
        except KeyError:
            raise TypeError(f"no Postgres array type for element {element!r} (OID {element_oid})") from None

    if py_type is list or py_type is dict:
        raise TypeError(
            f"{py_type.__name__} is ambiguous: it could be a Postgres array or a json document. "
            f"Use list[str] (or list[int], ...) for an array, or declare json explicitly with "
            f'ResultColumn("name", JSONB).'
        )

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


def _decode_numeric(data: bytes) -> str:
    ndigits, weight, sign, dscale = struct.unpack_from("!HhHH", data, 0)
    if sign == _NUMERIC_NAN:
        return "NaN"
    if sign == _NUMERIC_PINF:
        return "Infinity"
    if sign == _NUMERIC_NINF:
        return "-Infinity"

    groups = struct.unpack_from(f"!{ndigits}H", data, 8) if ndigits else ()
    text = "".join(f"{group:04d}" for group in groups) or "0"
    # `weight` counts base-10000 groups before the point, so the value is the digit
    # string scaled by that many groups of four decimal places.
    value = Decimal(text).scaleb((weight + 1 - ndigits) * 4)
    if sign == _NUMERIC_NEG:
        value = -value
    return f"{value:.{dscale}f}"


def _decode_time(data: bytes) -> str:
    micros = struct.unpack("!q", data)[0]
    seconds, micros = divmod(micros, 1_000_000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return time(hours, minutes, seconds, micros).isoformat()


def _decode_interval(data: bytes) -> str:
    micros, days, months = struct.unpack("!qii", data)
    time_part = _timedelta_out(timedelta(days=days, microseconds=micros))
    if not months:
        return time_part
    # timedelta has no month, so a month-bearing interval can only be handed to the
    # session as text. "N mons" is Postgres's own input syntax, so it round-trips.
    return f"{months} mons {time_part}"


def _decode_uuid(data: bytes) -> str:
    return str(uuid.UUID(bytes=data))


# json's binary representation is just its text; jsonb prefixes a format version
# byte. Only version 1 has ever existed -- refuse anything else rather than
# treating a future version's first byte as part of the document.
_JSONB_VERSION = 1


def _decode_json(data: bytes) -> str:
    return data.decode("utf-8")


def _decode_jsonb(data: bytes) -> str:
    if not data or data[0] != _JSONB_VERSION:
        raise ValueError(f"unsupported jsonb wire format version {data[0] if data else 'missing'}")
    return data[1:].decode("utf-8")


_BINARY_DECODERS: dict[int, Callable[[bytes], str]] = {
    BOOL: _decode_bool,
    INT2: _decode_int("h"),
    INT4: _decode_int("i"),
    OID: _decode_int("I"),
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
    INTERVAL: _decode_interval,
    NUMERIC: _decode_numeric,
    TIME: _decode_time,
    UUID: _decode_uuid,
    JSON: _decode_json,
    JSONB: _decode_jsonb,
}


def decode_binary_param(oid: int, data: bytes) -> Any:
    """Binary parameter -> the canonical text the session would have seen anyway,
    except for arrays, which become a (possibly nested) list of those texts."""
    decoder = _BINARY_DECODERS.get(oid)
    if decoder is not None:
        return decoder(data)

    from .arrays import decode_array_binary, is_array_oid

    if is_array_oid(oid):
        return decode_array_binary(data)
    raise ValueError(f"binary format not supported for parameter OID {oid}")


def decode_text_param(oid: int, text: str) -> Any:
    """Text parameter -> what the session sees. A plain string, except for arrays,
    which are parsed so that a session gets the same Python shape regardless of
    which wire format the client happened to choose."""
    from .arrays import is_array_oid, parse_array_literal

    if oid is not None and is_array_oid(oid):
        return parse_array_literal(text)
    return text


def encode_value(oid: int, value: Any) -> str:
    """Encode a Python value to its Postgres text-format representation.

    The OID is consulted only where the Python type doesn't settle the wire form:
    a `list` is an array literal for an array OID and a json document otherwise.
    Everything else dispatches on the value's own type, so an unrecognised OID
    still encodes sensibly rather than failing.

    Callers are responsible for handling `None` (SQL NULL) before calling this --
    NULL has no text representation, it's signaled by a -1 length in DataRow.
    """
    if isinstance(value, list):
        from .arrays import format_array_literal, is_array_oid

        if is_array_oid(oid):
            return format_array_literal(oid, value)

    func = _TEXT_ENCODERS.get(type(value))
    if func is not None:
        return func(value)
    for known_type, encoder in _TEXT_ENCODERS.items():
        if isinstance(value, known_type):
            return encoder(value)
    if oid in (JSON, JSONB):
        return _json_out(value)
    return str(value)


# --- Binary result encoding ---------------------------------------------------------
#
# The inverse of the binary parameter decoders above, keyed the same way (by the
# column's declared OID rather than the Python type, since the OID is what the
# client will parse against). Deliberately narrower than the text encoders: a
# column whose OID has no entry here is refused outright by `encode_value_binary`,
# because a wrong byte order or epoch offset produces a plausible-looking wrong
# value rather than a visible failure -- exactly the class of bug text format
# can't have.


def _encode_bool_bin(v: Any) -> bytes:
    return b"\x01" if v else b"\x00"


def _encode_int_bin(fmt: str) -> Callable[[Any], bytes]:
    packer = struct.Struct("!" + fmt)

    def encode(v: Any) -> bytes:
        return packer.pack(int(v))

    return encode


def _encode_float_bin(fmt: str) -> Callable[[Any], bytes]:
    packer = struct.Struct("!" + fmt)

    def encode(v: Any) -> bytes:
        return packer.pack(float(v))

    return encode


def _encode_bytea_bin(v: Any) -> bytes:
    return bytes(v)


def _encode_text_bin(v: Any) -> bytes:
    # Text and binary format are the same bytes for string types; the format code
    # only tells the client not to expect any further decoding.
    return v.encode("utf-8") if isinstance(v, str) else encode_value(TEXT, v).encode("utf-8")


def _encode_date_bin(v: Any) -> bytes:
    if isinstance(v, datetime):
        v = v.date()
    return struct.pack("!i", (v - _PG_EPOCH_DATE).days)


def _micros_since_epoch(v: datetime) -> bytes:
    delta = v - _PG_EPOCH
    return struct.pack("!q", delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds)


def _encode_timestamp_bin(v: Any) -> bytes:
    """TIMESTAMP (without time zone): the wall clock, offset discarded.

    Matches both Postgres's own semantics and what the text encoder effectively
    produces for this OID -- psycopg parses a text timestamp against the declared
    TIMESTAMP type and drops any offset, so converting to UTC here instead would
    make the two formats disagree.
    """
    if not isinstance(v, datetime):
        v = datetime(v.year, v.month, v.day)
    return _micros_since_epoch(v.replace(tzinfo=None))


def _encode_timestamptz_bin(v: Any) -> bytes:
    """TIMESTAMPTZ: the UTC instant. A naive value is taken as already UTC."""
    if not isinstance(v, datetime):
        v = datetime(v.year, v.month, v.day)
    if v.tzinfo is not None:
        v = v.astimezone(timezone.utc).replace(tzinfo=None)
    return _micros_since_epoch(v)


# Postgres numerics are stored as base-10000 digit groups, not binary floats, so
# the decimal value is exact on the wire. The header is ndigits/weight/sign/dscale:
# weight is the base-10000 exponent of the first group, and dscale is the *display*
# scale, which is what keeps Decimal("1.50") from coming back as 1.5.
_NUMERIC_POS = 0x0000
_NUMERIC_NEG = 0x4000
_NUMERIC_NAN = 0xC000
_NUMERIC_PINF = 0xD000
_NUMERIC_NINF = 0xF000
_NBASE = 10000


def _encode_numeric_bin(v: Any) -> bytes:
    if not isinstance(v, Decimal):
        v = Decimal(str(v))
    if v.is_nan():
        return struct.pack("!HHHH", 0, 0, _NUMERIC_NAN, 0)
    if v.is_infinite():
        sign = _NUMERIC_PINF if v > 0 else _NUMERIC_NINF
        return struct.pack("!HHHH", 0, 0, sign, 0)

    negative, digits, exponent = v.as_tuple()
    dscale = -exponent if exponent < 0 else 0
    text = "".join(map(str, digits))

    if exponent >= 0:
        integer_part, fraction_part = text + "0" * exponent, ""
    else:
        text = text.rjust(-exponent + 1, "0")  # ensure at least one integer digit
        integer_part, fraction_part = text[:exponent], text[exponent:]

    # Group into base-10000 digits, aligned outwards from the decimal point.
    integer_part = integer_part.rjust((len(integer_part) + 3) // 4 * 4, "0")
    fraction_part = fraction_part.ljust((len(fraction_part) + 3) // 4 * 4, "0")
    groups = [int(integer_part[i : i + 4]) for i in range(0, len(integer_part), 4)]
    weight = len(groups) - 1
    groups += [int(fraction_part[i : i + 4]) for i in range(0, len(fraction_part), 4)]

    # Postgres stores only the significant groups; leading ones shift the weight.
    while groups and groups[0] == 0:
        groups.pop(0)
        weight -= 1
    while groups and groups[-1] == 0:
        groups.pop()
    if not groups:
        weight = 0  # zero has no digits at all

    sign = _NUMERIC_NEG if negative else _NUMERIC_POS
    return struct.pack("!HhHH", len(groups), weight, sign, dscale) + b"".join(struct.pack("!H", d) for d in groups)


def _encode_time_bin(v: Any) -> bytes:
    """Microseconds since midnight."""
    if not isinstance(v, time):
        raise ValueError(f"expected a time, got {type(v).__name__}")
    micros = ((v.hour * 60 + v.minute) * 60 + v.second) * 1_000_000 + v.microsecond
    return struct.pack("!q", micros)


def _encode_interval_bin(v: Any) -> bytes:
    """microseconds (int64), days (int32), months (int32).

    Postgres keeps those three fields independently signed rather than reducing
    them to one duration, because a month is not a fixed number of days. Python's
    timedelta normalises to a non-negative time-of-day with a possibly negative
    day count, which is exactly that shape for the two fields it has -- so
    `timedelta(hours=-23)` becomes days=-1 with +1h of microseconds, and means the
    same thing on both sides. timedelta cannot express months, so months is always
    zero on the way out.
    """
    if not isinstance(v, timedelta):
        raise ValueError(f"expected a timedelta for an interval, got {type(v).__name__}")
    micros = v.seconds * 1_000_000 + v.microseconds
    return struct.pack("!qii", micros, v.days, 0)


def _encode_uuid_bin(v: Any) -> bytes:
    return (v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))).bytes


def _encode_json_bin(v: Any) -> bytes:
    # encode_value renders dicts/lists as JSON for a json OID and passes through a
    # str that's assumed to be JSON already, so binary is that same text.
    return encode_value(JSON, v).encode("utf-8")


def _encode_jsonb_bin(v: Any) -> bytes:
    return bytes([_JSONB_VERSION]) + _encode_json_bin(v)


_BINARY_ENCODERS: dict[int, Callable[[Any], bytes]] = {
    BOOL: _encode_bool_bin,
    INT2: _encode_int_bin("h"),
    INT4: _encode_int_bin("i"),
    OID: _encode_int_bin("I"),
    INT8: _encode_int_bin("q"),
    FLOAT4: _encode_float_bin("f"),
    FLOAT8: _encode_float_bin("d"),
    BYTEA: _encode_bytea_bin,
    TEXT: _encode_text_bin,
    VARCHAR: _encode_text_bin,
    BPCHAR: _encode_text_bin,
    NAME: _encode_text_bin,
    DATE: _encode_date_bin,
    TIMESTAMP: _encode_timestamp_bin,
    TIMESTAMPTZ: _encode_timestamptz_bin,
    INTERVAL: _encode_interval_bin,
    NUMERIC: _encode_numeric_bin,
    TIME: _encode_time_bin,
    UUID: _encode_uuid_bin,
    JSON: _encode_json_bin,
    JSONB: _encode_jsonb_bin,
}


def encode_value_binary(oid: int, value: Any) -> bytes:
    """Encode a Python value to Postgres binary format for a column of `oid`.

    Raises ValueError for any OID without a known binary representation, so an
    unsupported type surfaces as a protocol error rather than as wrong bytes.
    """
    encoder = _BINARY_ENCODERS.get(oid)
    if encoder is not None:
        return encoder(value)

    from .arrays import encode_array_binary, is_array_oid

    if is_array_oid(oid):
        return encode_array_binary(oid, value)
    raise ValueError(f"binary format not supported for result column OID {oid}")
