"""asyncpg's type introspection, answered directly rather than executed.

asyncpg opens by asking what the types in a result actually are, with a query it
builds around a `typeinfo_tree` recursive CTE. Unlike psql's catalog SQL, that one
cannot go through pg_mimic.catalog's execute-it-with-sqlglot route at all:

- sqlglot cannot parse it (it fails on the trailing `::regtype::text` casts), so
  the statement never even becomes an expression tree, and
- sqlglot's executor has no recursive CTE support whatsoever -- `WITH RECURSIVE
  t(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM t WHERE n < 3)` fails too.

So this matches the query by shape and answers it from the same `pg_type` rows the
catalog is built from. That is more brittle than executing SQL -- it is coupled to
a query asyncpg could rewrite in any release -- and the trade is deliberate: the
alternative is asyncpg not being able to read any array column except `text[]`.
The coupling is narrow (a marker string) and loud when it breaks (asyncpg errors
rather than misreads), and tests/test_asyncpg.py drives the real driver.

The query is parameterised -- `ti.oid = any($1::oid[])` -- so asyncpg names the
OIDs it wants, and the answer depends on values that only exist at Bind. Hence a
Statement whose rows are computed in bind() rather than a StaticStatement.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .results import ResultColumn
from .session import Portal, Row, Statement, drain_rows
from .types import INT4, OID, TEXT

if TYPE_CHECKING:
    from .connection import Connection

# The CTE asyncpg names its introspection tree. Distinctive enough to match on,
# and it is the thing that would change if asyncpg rewrote the query -- so a
# rewrite turns into "no longer matched" rather than "matched and misanswered".
_MARKER = "typeinfo_tree"

# The columns the CTE declares, in order. asyncpg reads them positionally.
_COLUMNS = (
    ("oid", OID),
    ("ns", TEXT),
    ("name", TEXT),
    ("kind", TEXT),
    ("basetype", OID),
    ("elemtype", OID),
    ("elemdelim", TEXT),
    ("range_subtype", OID),
    ("attrtypoids", TEXT),
    ("attrnames", TEXT),
    ("depth", INT4),
)

# ...plus the three the outer SELECT adds with `::regtype::text`.
_TRAILING_COLUMNS = (("basetype_name", TEXT), ("elemtype_name", TEXT), ("range_subtype_name", TEXT))


def is_typeinfo_query(sql: str) -> bool:
    return _MARKER in sql


def typeinfo_columns() -> list[ResultColumn]:
    return [ResultColumn(name, oid) for name, oid in (*_COLUMNS, *_TRAILING_COLUMNS)]


def _requested_oids(params: list[Any]) -> list[int]:
    """The OIDs from `$1::oid[]`.

    Array parameters reach a session as a list of text strings (see pg_mimic.arrays),
    so this is a list of digit strings rather than ints. A client that sent the
    parameter without a declared type leaves it as the raw literal instead, which
    is why the brace form is handled too.
    """
    if not params:
        return []
    value = params[0]
    if isinstance(value, str):
        value = [part for part in value.strip("{}").split(",") if part]
    if not isinstance(value, list):
        return []

    oids = []
    for item in value:
        try:
            oids.append(int(str(item).strip('"')))
        except ValueError:
            continue
    return oids


def _rows_for(oids: list[int]) -> list[Row]:
    """One row per requested type, plus its element type where it has one.

    asyncpg needs the element to build an array's codec, which in the real query is
    what the recursion walks to -- so the rows it would have reached at depth 1 have
    to be here too, and the real query's `ORDER BY depth DESC` puts them first.
    """
    from .catalog import pg_type_by_oid

    by_oid = pg_type_by_oid()
    seen: set[int] = set()
    at_depth: list[tuple[int, dict]] = []

    for oid in oids:
        row = by_oid.get(oid)
        if row is None or oid in seen:
            continue
        seen.add(oid)
        at_depth.append((0, row))
        element = row["typelem"]
        if element and element not in seen:
            element_row = by_oid.get(element)
            if element_row is not None:
                seen.add(element)
                at_depth.append((1, element_row))

    at_depth.sort(key=lambda pair: -pair[0])  # deepest first, as the query orders
    return [_row(depth, row, by_oid) for depth, row in at_depth]


def _row(depth: int, row: dict, by_oid: dict[int, dict]) -> Row:
    element_oid = row["typelem"] or None
    element = by_oid.get(element_oid) if element_oid else None
    return (
        row["oid"],
        "pg_catalog",
        row["typname"],
        row["typtype"],
        None,  # basetype: only domains have one, and pg_mimic has no domains
        element_oid,
        (element or row)["typdelim"],  # the *element's* delimiter, per the query
        None,  # range_subtype: no range types
        None,  # attrtypoids: composites only
        None,  # attrnames: composites only
        depth,
        None,  # basetype_name
        by_oid[element_oid]["typname"] if element_oid in by_oid else None,
        None,  # range_subtype_name
    )


class TypeInfoStatement(Statement):
    """Shape is known at Parse; the rows depend on the OIDs bound at Bind."""

    def __init__(self, sql: str):
        self.sql = sql
        # asyncpg sends the OID list as oid[]; saying so means the binary parameter
        # is decoded to a list rather than refused for want of a declared type.
        self.param_oids: list[int | None] = [_OID_ARRAY]

    async def describe(self) -> list[ResultColumn] | None:
        return typeinfo_columns()

    def bind(self, params: list[Any]) -> Portal:
        return TypeInfoPortal(_rows_for(_requested_oids(params)))


class TypeInfoPortal(Portal):
    def __init__(self, rows: list[Row]):
        self._rows = rows
        self._source: Any = None

    async def execute(self, max_rows: int) -> tuple[list[Row], bool]:
        if self._source is None:
            self._source = _as_async_iter(self._rows)
        return await drain_rows(self._source, max_rows)


async def _as_async_iter(rows: list[Row]):
    for row in rows:
        yield row


def typeinfo_statement(connection: Connection, sql: str) -> Statement:
    return TypeInfoStatement(sql)


def _oid_array_oid() -> int:
    from .arrays import ARRAY_OID

    return ARRAY_OID[OID]


_OID_ARRAY = _oid_array_oid()
