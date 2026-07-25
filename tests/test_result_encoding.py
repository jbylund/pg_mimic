"""Result-row text encoding for the less-common Python types
(pg_mimic/types.py's _TEXT_ENCODERS) -- the existing test suite only ever
returns str/int rows, so bool/bytes/float-specials/date/datetime/timedelta/
dict/list/Decimal/UUID encoding had zero coverage despite being documented,
implemented behavior."""

from __future__ import annotations

import datetime
import math
import uuid
from decimal import Decimal

import pytest

from pg_mimic import ResultColumn


def _select_one(conn, mock_session, value, py_type):
    mock_session.columns = [ResultColumn.for_type("x", py_type)]
    mock_session.rows = [(value,)]
    with conn.cursor() as cur:
        cur.execute("SELECT x FROM t")
        return cur.fetchone()[0]


def test_bool(conn, mock_session):
    assert _select_one(conn, mock_session, True, bool) is True
    assert _select_one(conn, mock_session, False, bool) is False


def test_bytes(conn, mock_session):
    assert _select_one(conn, mock_session, b"hello", bytes) == b"hello"


def test_float(conn, mock_session):
    assert _select_one(conn, mock_session, 3.14, float) == 3.14


@pytest.mark.parametrize(
    argnames=["value", "check"],
    argvalues=[
        (float("nan"), math.isnan),
        (float("inf"), math.isinf),
        (float("-inf"), lambda v: math.isinf(v) and v < 0),
    ],
    ids=["nan", "inf", "neg_inf"],
)
def test_float_special_values(conn, mock_session, value, check):
    assert check(_select_one(conn, mock_session, value, float))


def test_date(conn, mock_session):
    assert _select_one(conn, mock_session, datetime.date(2024, 1, 1), datetime.date) == datetime.date(2024, 1, 1)


def test_datetime(conn, mock_session):
    value = datetime.datetime(2024, 1, 1, 12, 30)
    assert _select_one(conn, mock_session, value, datetime.datetime) == value


def test_timedelta(conn, mock_session):
    value = datetime.timedelta(days=1, seconds=3661, microseconds=42)
    assert _select_one(conn, mock_session, value, datetime.timedelta) == value


def test_decimal(conn, mock_session):
    assert _select_one(conn, mock_session, Decimal("1.23"), Decimal) == Decimal("1.23")


def test_uuid(conn, mock_session):
    value = uuid.UUID("12345678-1234-5678-1234-567812345678")
    assert _select_one(conn, mock_session, value, uuid.UUID) == value


def test_dict_as_jsonb(conn, mock_session):
    assert _select_one(conn, mock_session, {"a": 1, "b": [1, 2]}, dict) == {"a": 1, "b": [1, 2]}


def test_list_as_jsonb(conn, mock_session):
    assert _select_one(conn, mock_session, [1, 2, 3], list) == [1, 2, 3]
