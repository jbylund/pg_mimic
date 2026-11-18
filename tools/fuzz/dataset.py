"""The fixed dataset every generated query runs against.

Small on purpose -- eight rows and six -- because a differential fuzzer finds bugs
with *shapes*, not with volume: a wrong join, a mishandled NULL, a tie broken the
wrong way. Every row here is carrying at least one edge case, and a bigger table
would only make the shrinker slower and the repros harder to read.

The values are chosen so that a plausible-but-wrong implementation produces a
*different* answer rather than an accidentally equal one:

- NULLs in every nullable column, including one all-NULL-ish float and a NULL
  timestamp, so outer joins and aggregates have something to get wrong.
- Duplicates (``n`` is 5 twice, ``s`` is 'abc' twice) so DISTINCT and GROUP BY
  have work to do and ORDER BY has ties.
- Zero and negative zero, so division and sign() are interesting.
- ``d`` holds exact halves (2.5, -2.5, 0.5) because rounding mode is a real
  divergence: Postgres' round(numeric) goes half away from zero and Python's
  builtin goes half to even.
- 'ABC' next to 'abc' for ILIKE and case-sensitive ordering, '' for the
  empty-string-is-not-NULL rule, and 'a%b' so a LIKE pattern that is taken as a
  regex instead of a pattern gives itself away.
- ``u.t_id`` has a value matching nothing (99), a NULL, and two rows pointing at
  the same ``t`` row, so INNER/LEFT/RIGHT/FULL joins all differ from each other.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from typing import Any

Kind = str

NUM: Kind = "num"
TEXT: Kind = "text"
BOOL: Kind = "bool"
TS: Kind = "ts"


class Column:
    def __init__(self, name: str, kind: Kind, pg_type: str, py_type: type, exact: bool = False):
        self.name = name
        self.kind = kind
        self.pg_type = pg_type
        self.py_type = py_type
        # An exact column is one Postgres stores without a binary float: only
        # these are safe to compare for equality rather than within a tolerance.
        self.exact = exact

    def __repr__(self) -> str:
        return f"Column({self.name!r}, {self.kind!r})"


class Table:
    def __init__(self, name: str, columns: list[Column], rows: list[tuple]):
        self.name = name
        self.columns = columns
        self.rows = [dict(zip([column.name for column in columns], row)) for row in rows]

    def of_kind(self, kind: Kind) -> list[Column]:
        return [column for column in self.columns if column.kind == kind]

    @property
    def ddl(self) -> str:
        body = ", ".join(f"{column.name} {column.pg_type}" for column in self.columns)
        return f"CREATE TABLE {self.name} ({body})"

    @property
    def py_types(self) -> dict[str, type]:
        return {column.name: column.py_type for column in self.columns}


T = Table(
    "t",
    [
        Column("id", NUM, "INTEGER", int, exact=True),
        Column("n", NUM, "INTEGER", int, exact=True),
        Column("s", TEXT, "TEXT", str),
        Column("f", NUM, "DOUBLE PRECISION", float),
        Column("d", NUM, "NUMERIC(12,4)", Decimal, exact=True),
        Column("b", BOOL, "BOOLEAN", bool),
        Column("ts", TS, "TIMESTAMP", datetime.datetime),
    ],
    [
        (1, 1, "abc", 1.5, Decimal("2.5"), True, datetime.datetime(2024, 1, 15, 10, 30)),
        (2, None, "ABC", -1.5, Decimal("-2.5"), False, datetime.datetime(2024, 3, 15)),
        (3, 0, "", 0.0, Decimal("0.5"), None, datetime.datetime(2024, 3, 15)),
        (4, 5, None, 2.5, Decimal("9.99"), True, datetime.datetime(2023, 12, 31, 23, 59, 59)),
        (5, 5, "a%b", 0.25, Decimal("0"), False, datetime.datetime(2024, 2, 29, 12, 0)),
        (6, -3, "Bump version", -0.0, Decimal("-0.0050"), True, None),
        (7, None, "zzz", None, None, None, datetime.datetime(2025, 6, 1)),
        (8, 2, "abc", 3.5, Decimal("3.5"), False, datetime.datetime(2024, 1, 15, 10, 30)),
    ],
)

U = Table(
    "u",
    [
        Column("uid", NUM, "INTEGER", int, exact=True),
        Column("t_id", NUM, "INTEGER", int, exact=True),
        Column("us", TEXT, "TEXT", str),
        Column("un", NUM, "NUMERIC(12,4)", Decimal, exact=True),
    ],
    [
        (10, 1, "x", Decimal("100")),
        (11, 1, "y", Decimal("-1")),
        (12, 3, None, Decimal("0")),
        (13, None, "x", Decimal("7")),
        (14, 99, "z", None),
        (15, 8, "x", Decimal("100")),
    ],
)

TABLES = [T, U]


def sqlglot_schema() -> dict[str, dict[str, str]]:
    """What the raw executor wants: a type name per column, as sqlglot spells it."""
    names = {
        "INTEGER": "INT",
        "DOUBLE PRECISION": "DOUBLE",
        "NUMERIC(12,4)": "DECIMAL",
        "TEXT": "TEXT",
        "BOOLEAN": "BOOLEAN",
        "TIMESTAMP": "TIMESTAMP",
    }
    return {table.name: {column.name: names[column.pg_type] for column in table.columns} for table in TABLES}


def rows() -> dict[str, list[dict[str, Any]]]:
    return {table.name: table.rows for table in TABLES}


def declared_columns() -> dict[str, dict[str, type]]:
    """Types handed to TableSession rather than left to inference.

    Inference would get most of these right, but not all -- ``t.f`` is NULL in one
    row and ``t.b`` is NULL in three -- and a fuzzer whose schema drifts with its
    data is a fuzzer whose failures are its own fault.
    """
    return {table.name: table.py_types for table in TABLES}


def load(connection) -> None:
    with connection.cursor() as cursor:
        for table in TABLES:
            cursor.execute(f"DROP TABLE IF EXISTS {table.name}")
            cursor.execute(table.ddl)
            placeholders = ", ".join(["%s"] * len(table.columns))
            values = [tuple(row[column.name] for column in table.columns) for row in table.rows]
            cursor.executemany(f"INSERT INTO {table.name} VALUES ({placeholders})", values)
