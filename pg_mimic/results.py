"""ResultColumn -- the explicit, declared column-shape metadata a Statement's
describe() returns, plus the row-value encoder that uses it (text or binary,
per the format codes the client sent in Bind).

Column shape is never inferred by inspecting row data (no "peek the first row"
trick) -- it's always a declared fact, known before any row is pulled from a
Portal's row source. See pg_mimic.session.Statement/Portal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import encode_value, encode_value_binary, oid_for_type


@dataclass
class ResultColumn:
    name: str
    oid: int

    @classmethod
    def for_type(cls, name: str, py_type: type) -> ResultColumn:
        return cls(name, oid_for_type(py_type))


def format_code_for(format_codes: list[int], index: int) -> int:
    """Resolve a per-column format code from a Bind message's list.

    Postgres allows three shapes: empty (everything text), exactly one (applies to
    every column), or one per column.
    """
    if not format_codes:
        return 0
    if len(format_codes) == 1:
        return format_codes[0]
    return format_codes[index]


def encode_row(row: tuple, columns: list[ResultColumn], format_codes: list[int] | None = None) -> list[bytes | None]:
    """Encode a row to DataRow field values, honouring each column's format code.

    NULL is signalled by a -1 length in DataRow and so has no representation in
    either format -- it stays None here regardless.
    """
    codes = format_codes or []
    values: list[bytes | None] = []
    for index, value in enumerate(row):
        if value is None:
            values.append(None)
        elif format_code_for(codes, index) == 0:
            values.append(encode_value(value).encode("utf-8"))
        else:
            values.append(encode_value_binary(columns[index].oid, value))
    return values
