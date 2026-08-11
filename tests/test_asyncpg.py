"""asyncpg compatibility.

asyncpg always requests binary results, where psycopg only does on request, so
it exercises the binary path far harder -- several bugs in this codebase were
invisible to psycopg for exactly that reason. The known gaps are marked xfail
with strict=True on purpose: when one is implemented, CI fails with
"unexpectedly passing" and points at the marker to remove.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from pg_mimic import ARRAY_OID, BOOL, DATE, FLOAT8, INT2, INT4, INT8, JSONB, TEXT, UUID, VARCHAR, ResultColumn

_scalar_testcases = {
    "int8": {"py_type": int, "value": 42},
    "text": {"py_type": str, "value": "hello"},
    "bool": {"py_type": bool, "value": True},
    "float8": {"py_type": float, "value": 1.5},
    "uuid": {"py_type": uuid.UUID, "value": uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")},
    "date": {"py_type": date, "value": date(2001, 2, 3)},
    "timestamp": {"py_type": datetime, "value": datetime(2001, 2, 3, 4, 5, 6)},
    "bytea": {"py_type": bytes, "value": b"\x00\xff"},
    "interval": {"py_type": timedelta, "value": timedelta(days=2, hours=3)},
    "interval_negative": {"py_type": timedelta, "value": timedelta(hours=-23)},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_scalar_testcases.values()))),
    argvalues=[[v for k, v in sorted(_scalar_testcases[name].items())] for name in sorted(_scalar_testcases)],
    ids=sorted(_scalar_testcases),
)
async def test_scalar_round_trips(apg_conn, mock_session, py_type, value):
    mock_session.columns = [ResultColumn.for_type("c", py_type)]
    mock_session.rows = [(value,)]
    assert await apg_conn.fetchval("select c") == value


async def test_null_is_null(apg_conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("c", int)]
    mock_session.rows = [(None,)]
    assert await apg_conn.fetchval("select c") is None


async def test_text_array(apg_conn, mock_session):
    """text[] is the one array type asyncpg has a built-in codec for -- the rest go
    through its type introspection, which pg_mimic.typeinfo answers."""
    mock_session.columns = [ResultColumn("c", ARRAY_OID[TEXT])]
    mock_session.rows = [(["a", "b,c", None],)]
    assert await apg_conn.fetchval("select c") == ["a", "b,c", None]


async def test_jsonb(apg_conn, mock_session):
    """asyncpg hands json back as text unless a codec is registered, so this is
    asserting the wire bytes are right, not that asyncpg parses them."""
    mock_session.columns = [ResultColumn("c", JSONB)]
    mock_session.rows = [({"a": 1},)]
    assert await apg_conn.fetchval("select c") == '{"a": 1}'


async def test_multiple_columns_and_rows(apg_conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("a", int), ResultColumn.for_type("b", str)]
    mock_session.rows = [(1, "x"), (2, "y")]
    rows = await apg_conn.fetch("select a, b")
    assert [tuple(r) for r in rows] == [(1, "x"), (2, "y")]


async def test_session_settings_preamble(apg_conn, mock_session):
    """The statement asyncpg opens introspection with. Before set_config was
    handled this fell through to the session, which answered with the wrong shape
    and left asyncpg retrying it indefinitely."""
    row = await apg_conn.fetchrow("SELECT current_setting('jit') AS cur, set_config('jit', 'off', false) AS new")
    assert tuple(row) == ("off", "off")
    assert mock_session.queries == []


async def test_transaction(apg_conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("c", int)]
    mock_session.rows = [(1,)]
    async with apg_conn.transaction():
        assert await apg_conn.fetchval("select c") == 1


async def test_numeric_precision_is_exact(apg_conn, mock_session):
    """Postgres numerics are base-10000 digit groups rather than binary floats, so
    a value no float could hold must survive intact."""
    value = Decimal("0.10000000000000000000000001")
    mock_session.columns = [ResultColumn.for_type("c", Decimal)]
    mock_session.rows = [(value,)]
    assert await apg_conn.fetchval("select c") == value


async def test_numeric(apg_conn, mock_session):
    mock_session.columns = [ResultColumn.for_type("c", Decimal)]
    mock_session.rows = [(Decimal("1.25"),)]
    assert await apg_conn.fetchval("select c") == Decimal("1.25")


async def test_time(apg_conn, mock_session):
    from datetime import time as time_cls

    mock_session.columns = [ResultColumn.for_type("c", time_cls)]
    mock_session.rows = [(time_cls(1, 2, 3),)]
    assert await apg_conn.fetchval("select c") == time_cls(1, 2, 3)


async def test_int_array(apg_conn, mock_session):
    mock_session.columns = [ResultColumn("c", ARRAY_OID[INT8])]
    mock_session.rows = [([1, 2, 3],)]
    assert await apg_conn.fetchval("select c") == [1, 2, 3]


_array_testcases = {
    "int2": {"element": INT2, "value": [1, 2]},
    "int4": {"element": INT4, "value": [1, 2]},
    "int8": {"element": INT8, "value": [1, 2]},
    "bool": {"element": BOOL, "value": [True, False]},
    "float8": {"element": FLOAT8, "value": [1.5, -0.25]},
    "text": {"element": TEXT, "value": ["a", "b,c"]},
    "varchar": {"element": VARCHAR, "value": ["a"]},
    "uuid": {"element": UUID, "value": [uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")]},
    "date": {"element": DATE, "value": [date(2001, 2, 3)]},
    "with_null": {"element": INT8, "value": [1, None, 3]},
    "empty": {"element": INT8, "value": []},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_array_testcases.values()))),
    argvalues=[[v for k, v in sorted(_array_testcases[name].items())] for name in sorted(_array_testcases)],
    ids=sorted(_array_testcases),
)
async def test_arrays_of_every_element_type(apg_conn, mock_session, element, value):
    """Each of these sends asyncpg to its type introspection, which only text[]
    used to survive -- the rest died on pg_catalog."""
    mock_session.columns = [ResultColumn("c", ARRAY_OID[element])]
    mock_session.rows = [(value,)]
    assert await apg_conn.fetchval("select c") == value


async def test_nested_array(apg_conn, mock_session):
    mock_session.columns = [ResultColumn("c", ARRAY_OID[INT8])]
    mock_session.rows = [([[1, 2], [3, 4]],)]
    assert await apg_conn.fetchval("select c") == [[1, 2], [3, 4]]
