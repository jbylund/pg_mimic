"""ResultColumn -- the explicit, declared column-shape metadata a Statement's
describe() returns, plus the row-value text encoder that uses it.

Column shape is never inferred by inspecting row data (no "peek the first row"
trick) -- it's always a declared fact, known before any row is pulled from a
Portal's row source. See pg_mimic.session.Statement/Portal.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import encode_value, oid_for_type


@dataclass
class ResultColumn:
    name: str
    oid: int

    @classmethod
    def for_type(cls, name: str, py_type: type) -> "ResultColumn":
        return cls(name, oid_for_type(py_type))


def encode_row(row: tuple, columns: list[ResultColumn]) -> list[str | None]:
    values: list[str | None] = []
    for value in row:
        values.append(None if value is None else encode_value(value))
    return values
