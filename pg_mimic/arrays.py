"""Postgres array codecs, text and binary.

Arrays are the one place where the wire representation can't be derived from the
Python value alone: `[1, 2, 3]` is a valid `int8[]`, `text[]` or `jsonb`, and the
column's declared OID is what decides. So everything here is keyed on the element
OID, and delegates per element to the scalar codecs in pg_mimic.types.

Layering is types <- arrays <- results: this module imports the scalar codecs,
and nothing here imports back into them at module scope.

Element values are text strings in both directions, matching what a session
already sees for scalar parameters -- an `int8[]` parameter arrives as
`["1", "2", "3"]`, not `[1, 2, 3]`, for the same reason a scalar `int8` arrives
as `"1"`. NULL elements are None, which is unambiguous since a NULL is signalled
structurally in both formats rather than by any string value.
"""

from __future__ import annotations

import struct
from typing import Any, Iterator

from .types import (
    BOOL,
    BPCHAR,
    BYTEA,
    CHAR,
    DATE,
    FLOAT4,
    FLOAT8,
    INT2,
    INT4,
    INT8,
    INTERVAL,
    JSON,
    JSONB,
    NAME,
    NUMERIC,
    OID,
    TEXT,
    TIME,
    TIMESTAMP,
    TIMESTAMPTZ,
    UUID,
    VARCHAR,
    decode_binary_param,
    encode_value,
    encode_value_binary,
)

# *_ARRAY oids, keyed by the scalar oid they hold.
ARRAY_OID: dict[int, int] = {
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

ELEMENT_OID: dict[int, int] = {array_oid: element for element, array_oid in ARRAY_OID.items()}


def is_array_oid(oid: int) -> bool:
    return oid in ELEMENT_OID


def element_oid_of(oid: int) -> int:
    return ELEMENT_OID[oid]


# --- shape handling -----------------------------------------------------------------


def _dimensions(value: list) -> list[int]:
    """Dimension sizes of a (possibly nested) list, verifying it's rectangular.

    Postgres arrays are rectangular by definition -- there is no wire
    representation for a ragged one -- so a ragged Python list has to be rejected
    here rather than silently truncated or padded.
    """
    if not value:
        return []  # `{}`: zero dimensions, not a 1-element dimension of size 0
    dims: list[int] = []
    node: Any = value
    while isinstance(node, list):
        dims.append(len(node))
        node = node[0] if node else None
    _check_rectangular(value, dims, 0)
    return dims


def _check_rectangular(node: Any, dims: list[int], depth: int) -> None:
    if depth >= len(dims):
        if isinstance(node, list):
            raise ValueError("array is deeper in some branches than others")
        return
    if not isinstance(node, list):
        raise ValueError("array is shallower in some branches than others")
    if len(node) != dims[depth]:
        raise ValueError(f"array is ragged: expected {dims[depth]} elements at depth {depth}, got {len(node)}")
    for child in node:
        _check_rectangular(child, dims, depth + 1)


def _flatten(value: Any) -> Iterator[Any]:
    if isinstance(value, list):
        for item in value:
            yield from _flatten(item)
    else:
        yield value


def _reshape(flat: list[Any], dims: list[int]) -> list[Any]:
    if len(dims) <= 1:
        return flat
    step = 1
    for size in dims[1:]:
        step *= size
    return [_reshape(flat[i : i + step], dims[1:]) for i in range(0, len(flat), step)]


# --- binary -------------------------------------------------------------------------


def encode_array_binary(array_oid: int, value: Any) -> bytes:
    """ndim, flags and element OID, then one (size, lower_bound) pair per
    dimension, then the elements in row-major order with -1 lengths for NULL."""
    element_oid = ELEMENT_OID[array_oid]
    if not isinstance(value, list):
        raise ValueError(f"expected a list for array OID {array_oid}, got {type(value).__name__}")

    dims = _dimensions(value)
    flat = list(_flatten(value)) if dims else []
    has_null = any(item is None for item in flat)

    out = [struct.pack("!iiI", len(dims), 1 if has_null else 0, element_oid)]
    for size in dims:
        out.append(struct.pack("!ii", size, 1))  # lower bound is 1 unless a client says otherwise
    for item in flat:
        if item is None:
            out.append(struct.pack("!i", -1))
        else:
            encoded = encode_value_binary(element_oid, item)
            out.append(struct.pack("!i", len(encoded)) + encoded)
    return b"".join(out)


def decode_array_binary(data: bytes) -> list:
    ndim, _flags, element_oid = struct.unpack_from("!iiI", data, 0)
    offset = 12
    if ndim == 0:
        return []
    if ndim < 0:
        raise ValueError(f"negative array dimension count {ndim}")

    dims = []
    for _ in range(ndim):
        size, _lower = struct.unpack_from("!ii", data, offset)
        offset += 8
        dims.append(size)

    total = 1
    for size in dims:
        total *= size

    flat: list[Any] = []
    for _ in range(total):
        (length,) = struct.unpack_from("!i", data, offset)
        offset += 4
        if length == -1:
            flat.append(None)
        else:
            flat.append(decode_binary_param(element_oid, data[offset : offset + length]))
            offset += length
    return _reshape(flat, dims)


# --- text literals ------------------------------------------------------------------

# Characters that force an element to be quoted. Whitespace counts because the
# parser strips it from unquoted tokens, so " a " would not survive unquoted.
_NEEDS_QUOTING = set('{},"\\ \t\n\r\v\f')


def _format_element(element_oid: int, value: Any) -> str:
    if value is None:
        return "NULL"
    text = encode_value(element_oid, value)
    # An unquoted NULL is the null marker, so the *string* "NULL" must be quoted
    # to stay distinguishable from it. Likewise the empty string.
    if text == "" or text.upper() == "NULL" or any(ch in _NEEDS_QUOTING for ch in text):
        return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return text


def format_array_literal(array_oid: int, value: Any) -> str:
    """`{1,2,3}` / `{{1,2},{3,4}}` / `{}`, quoting elements where needed."""
    element_oid = ELEMENT_OID[array_oid]
    if not isinstance(value, list):
        raise ValueError(f"expected a list for array OID {array_oid}, got {type(value).__name__}")
    _dimensions(value)  # reject ragged input the same way the binary path does
    return _format_nested(element_oid, value)


def _format_nested(element_oid: int, value: Any) -> str:
    if isinstance(value, list):
        return "{" + ",".join(_format_nested(element_oid, item) for item in value) + "}"
    return _format_element(element_oid, value)


def parse_array_literal(text: str) -> list:
    """Parse a Postgres array literal into nested lists of str (None for NULL)."""
    body = text.strip()
    # Postgres emits an explicit-bounds prefix like `[1:3]={...}` when the lower
    # bound isn't 1. The bounds don't survive into a Python list either way.
    if body.startswith("["):
        equals = body.find("=")
        if equals == -1:
            raise ValueError(f"malformed array literal: {text!r}")
        body = body[equals + 1 :].lstrip()
    if not body.startswith("{"):
        raise ValueError(f"malformed array literal, expected '{{': {text!r}")

    value, index = _parse_braced(body, 0)
    if body[index:].strip():
        raise ValueError(f"trailing characters after array literal: {text!r}")
    return value


def _parse_braced(text: str, index: int) -> tuple[list, int]:
    if text[index] != "{":
        raise ValueError(f"expected '{{' at position {index}")
    index += 1
    out: list[Any] = []
    index = _skip_space(text, index)
    if index < len(text) and text[index] == "}":
        return out, index + 1  # `{}`

    while True:
        index = _skip_space(text, index)
        if index >= len(text):
            raise ValueError("unterminated array literal")
        if text[index] == "{":
            item, index = _parse_braced(text, index)
        elif text[index] == '"':
            item, index = _parse_quoted(text, index)
        else:
            item, index = _parse_bare(text, index)
        out.append(item)

        index = _skip_space(text, index)
        if index >= len(text):
            raise ValueError("unterminated array literal")
        if text[index] == ",":
            index += 1
            continue
        if text[index] == "}":
            return out, index + 1
        raise ValueError(f"unexpected {text[index]!r} at position {index} in array literal")


def _skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


def _parse_quoted(text: str, index: int) -> tuple[str, int]:
    index += 1  # opening quote
    chars: list[str] = []
    while index < len(text):
        ch = text[index]
        if ch == "\\":
            index += 1
            if index >= len(text):
                raise ValueError("array literal ends with a trailing backslash")
            chars.append(text[index])
        elif ch == '"':
            return "".join(chars), index + 1
        else:
            chars.append(ch)
        index += 1
    raise ValueError("unterminated quoted element in array literal")


def _parse_bare(text: str, index: int) -> tuple[str | None, int]:
    start = index
    while index < len(text) and text[index] not in ",}":
        index += 1
    token = text[start:index].strip()
    # Only an *unquoted* NULL is the null marker; a quoted one is the string.
    return (None if token.upper() == "NULL" else token), index
