"""Deciding whether two result sets are the same answer.

This is where a differential fuzzer is won or lost. Postgres and sqlglot's
executor disagree constantly in ways that are not bugs -- `sum(int)` is a Python
int on one side and a bigint-shaped int on the other, `avg(int)` is a Decimal in
Postgres and a float in the executor -- and a comparison that calls those
failures buries the real findings.

So a difference is graded rather than merely detected:

VALUE   the numbers, strings or timestamps genuinely differ. Always a bug.
COUNT   different number of rows. Always a bug.
EXACT   numerically equal, but one side lost exactness -- a float where Postgres
        returned a Decimal, so 0.1 + 0.2 is 0.30000000000000004 rather than
        0.3. Reported separately because it is real (issue #33) but of a
        different kind, and because it fires often.
TYPE    numerically equal, same exactness, different Python type -- an int where
        Postgres said bool, say. Lowest severity; a client mostly cannot tell,
        since pg_mimic encodes from the *declared* type rather than the value.
ORDER   same multiset, different sequence. Only meaningful under --ordered.

Floats are compared with a relative tolerance because both engines do their own
arithmetic in a different order, and `(a + b) * c` is allowed to land a few ULPs
apart. The tolerance is tight enough that a wrong answer -- integer division
giving 0 where Postgres gives 0.5, a rounding mode giving 2 where Postgres gives
3 -- is never inside it.
"""

from __future__ import annotations

import datetime
import math
from decimal import Decimal

_TOLERANCE = 1e-9

# Ordered worst-first: a single row can produce several kinds of difference and
# the run should be bucketed by the most serious one.
SEVERITY = ["COUNT", "VALUE", "EXACT", "TYPE"]


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def _numerically_equal(left: object, right: object) -> bool:
    left_float, right_float = float(left), float(right)  # type: ignore[arg-type]
    if math.isnan(left_float) and math.isnan(right_float):
        return True
    if left_float == right_float:
        return True
    scale = max(abs(left_float), abs(right_float))
    return abs(left_float - right_float) <= _TOLERANCE * scale


def compare_value(expected: object, actual: object) -> str | None:
    """None if the two are the same answer, else the severity of the difference.

    `expected` is Postgres.
    """
    if expected is None and actual is None:
        return None
    if expected is None or actual is None:
        return "VALUE"

    expected_number, actual_number = _is_number(expected), _is_number(actual)

    # A bool is not a number here, but Postgres' bool against the executor's 1/0
    # is a type difference and not a wrong answer.
    if isinstance(expected, bool) or isinstance(actual, bool):
        if isinstance(expected, bool) and isinstance(actual, bool):
            return None if expected == actual else "VALUE"
        if actual_number or expected_number:
            return "TYPE" if int(expected) == int(actual) else "VALUE"  # type: ignore[call-overload]
        return "VALUE"

    if expected_number and actual_number:
        if not _numerically_equal(expected, actual):
            return "VALUE"
        if isinstance(expected, Decimal) and isinstance(actual, float):
            return "EXACT"
        if type(expected) is not type(actual):
            return "TYPE"
        return None

    if isinstance(expected, str) and isinstance(actual, str):
        return None if expected == actual else "VALUE"

    if isinstance(expected, datetime.datetime) and isinstance(actual, datetime.datetime):
        if expected.replace(tzinfo=None) != actual.replace(tzinfo=None):
            return "VALUE"
        return None if (expected.tzinfo is None) == (actual.tzinfo is None) else "TYPE"

    if isinstance(expected, datetime.date) and isinstance(actual, datetime.datetime):
        return None if actual.date() == expected and actual.time() == datetime.time() else "VALUE"

    return None if expected == actual else "VALUE"


def _sort_key(row: tuple) -> tuple:
    """A canonical order that puts equal-valued rows of unequal Python type together.

    Sorting each side independently is only sound if the key ignores exactly the
    differences compare_value forgives -- otherwise a Decimal row and its float
    twin sort to different positions and every comparison downstream is offset by
    one.
    """
    key = []
    for value in row:
        if value is None:
            key.append((0, 0.0, ""))
        elif isinstance(value, bool):
            key.append((1, float(value), ""))
        elif _is_number(value):
            number = float(value)  # type: ignore[arg-type]
            key.append((1, 0.0 if math.isnan(number) else number, ""))
        elif isinstance(value, str):
            key.append((2, 0.0, value))
        else:
            key.append((3, 0.0, str(value)))
    return tuple(key)


def compare(expected: list[tuple], actual: list[tuple], ordered: bool) -> tuple[str, str] | None:
    """None if the two result sets are the same answer, else (severity, detail)."""
    if len(expected) != len(actual):
        return ("COUNT", f"{len(expected)} rows from postgres, {len(actual)} from the executor")

    if ordered:
        sequence_difference = _first_difference(expected, actual)

    left = sorted(expected, key=_sort_key)
    right = sorted(actual, key=_sort_key)
    multiset_difference = _first_difference(left, right)
    if multiset_difference:
        return multiset_difference

    if ordered and sequence_difference:
        index = next(
            (position for position, (a, b) in enumerate(zip(expected, actual)) if _sort_key(a) != _sort_key(b)),
            0,
        )
        return (
            "ORDER",
            f"same rows, different order: at row {index} postgres has {expected[index]!r}, the executor {actual[index]!r}",
        )
    return None


def _first_difference(expected: list[tuple], actual: list[tuple]) -> tuple[str, str] | None:
    worst: tuple[str, str] | None = None
    for index, (expected_row, actual_row) in enumerate(zip(expected, actual)):
        if len(expected_row) != len(actual_row):
            return ("COUNT", f"row {index} has {len(expected_row)} columns in postgres, {len(actual_row)} in the executor")
        for position, (expected_value, actual_value) in enumerate(zip(expected_row, actual_row)):
            severity = compare_value(expected_value, actual_value)
            if severity is None:
                continue
            detail = (
                f"row {index} column {position}: postgres {expected_value!r} ({type(expected_value).__name__}), "
                f"executor {actual_value!r} ({type(actual_value).__name__})"
            )
            if worst is None or SEVERITY.index(severity) < SEVERITY.index(worst[0]):
                worst = (severity, detail)
    return worst
