"""The core extension point: Statement/Portal, and the Session base classes.

Statement/Portal mirrors Postgres's own internal model (a portal is a bound
instance of a prepared statement), which lets Bind/Execute timing be exact
rather than approximated:

- Statement.describe() answers column shape from the query text and param
  count/types alone -- never param *values*, and never by touching row data.
  It's usable the instant a statement is Parsed, before any Bind.
- Statement.bind() is synchronous, no I/O -- real Bind never executes either.
- Portal.execute() is where real work happens. Its *first* call is what
  actually runs the query (issuing a write, opening a cursor, ...); later
  calls on the same portal keep draining the same row source, so incremental
  fetch (maxRows-limited Execute, PortalSuspended) is exact and never
  re-triggers side effects.

Both the simple query protocol ('Q') and the extended protocol
(Parse/Bind/Describe/Execute/Sync) drive this exact same interface --
simple query just does describe() + bind([]) + execute(0) in one shot.
"""
from __future__ import annotations

import inspect
from abc import ABC, abstractmethod
from typing import Any, AsyncIterable, AsyncIterator, Awaitable, Iterable, Union

from .results import ResultColumn

Row = tuple
RowSource = Union[AsyncIterable[Row], Iterable[Row]]
QueryResult = Union[RowSource, Awaitable[RowSource]]


class Statement(ABC):
    sql: str
    param_oids: list[int | None] = []

    @abstractmethod
    async def describe(self) -> list[ResultColumn] | None:
        """Column shape, or None if this statement produces no rows (NoData)."""

    @abstractmethod
    def bind(self, params: list[str | None]) -> "Portal":
        """Synchronous, no I/O -- just stores the bound parameters."""


class Portal(ABC):
    @abstractmethod
    async def execute(self, max_rows: int) -> tuple[list[Row], bool]:
        """Return (rows, suspended). suspended=True means the row-count limit
        was hit and more rows may remain (0/negative max_rows means "no limit").
        The underlying row source, once started, is never restarted by later
        calls -- only drained further."""


class BaseSession(ABC):
    """Minimal interface a Connection depends on. Implement this directly to
    bypass Session's describe()/query() convenience layer and the middleware
    chain entirely."""

    async def init(self, connection: Any) -> None:
        """Called once, right after authentication succeeds."""

    async def close(self) -> None:
        """Called once, when the connection is closing."""

    @abstractmethod
    async def prepare(self, sql: str, param_oids: list[int | None]) -> Statement:
        """Parse phase: return a Statement for this SQL text. Must not touch
        parameter values (none exist yet) and must not execute anything."""


async def _anext(iterator: AsyncIterator[Row]) -> Row:
    return await iterator.__anext__()


async def _as_async_iterator(source: RowSource) -> AsyncIterator[Row]:
    if hasattr(source, "__aiter__"):
        async for item in source:  # type: ignore[union-attr]
            yield item
    else:
        for item in source:  # type: ignore[union-attr]
            yield item


async def _resolve_row_source(result: QueryResult) -> AsyncIterator[Row]:
    """Session.query() may be a plain `async def` returning a list/iterable
    (a coroutine to await) or an `async def` that `yield`s rows directly (an
    async generator, no await needed/possible). Handle both transparently."""
    if inspect.isasyncgen(result):
        return result
    if inspect.iscoroutine(result):
        resolved = await result
        return _as_async_iterator(resolved)
    return _as_async_iterator(result)  # already a plain iterable


async def drain_rows(row_source: AsyncIterator[Row], max_rows: int) -> tuple[list[Row], bool]:
    """Pull up to max_rows rows (0/negative = unlimited) from an already-started
    async row source. Returns (rows, suspended) -- suspended=True means the
    limit was hit and the source wasn't necessarily exhausted (real Postgres
    semantics: the client must Execute again to find out either way)."""
    rows: list[Row] = []
    count = 0
    suspended = False
    while max_rows <= 0 or count < max_rows:
        try:
            rows.append(await _anext(row_source))
        except StopAsyncIteration:
            break
        count += 1
    else:
        if max_rows > 0:
            suspended = True
    return rows, suspended


class CallbackPortal(Portal):
    def __init__(self, session: "Session", sql: str, params: list[str | None]):
        self._session = session
        self._sql = sql
        self._params = params
        self._row_source: AsyncIterator[Row] | None = None

    async def execute(self, max_rows: int) -> tuple[list[Row], bool]:
        if self._row_source is None:
            self._row_source = await _resolve_row_source(self._session.query(self._sql, self._params))
        return await drain_rows(self._row_source, max_rows)


class CallbackStatement(Statement):
    _UNSET = object()

    def __init__(self, session: "Session", sql: str, param_oids: list[int | None]):
        self._session = session
        self.sql = sql
        self.param_oids = param_oids
        self._columns: list[ResultColumn] | None | object = self._UNSET

    async def describe(self) -> list[ResultColumn] | None:
        if self._columns is self._UNSET:
            self._columns = await self._session.describe(self.sql, self.param_oids)
        return self._columns  # type: ignore[return-value]

    def bind(self, params: list[str | None]) -> Portal:
        return CallbackPortal(self._session, self.sql, params)


class Session(BaseSession):
    """The class most session authors subclass. Override describe()/query()
    (and optionally schema()) instead of hand-writing Statement/Portal."""

    _connection: Any = None

    async def init(self, connection: Any) -> None:
        self._connection = connection

    async def describe(self, sql: str, param_oids: list[int | None]) -> list[ResultColumn] | None:
        return None

    async def query(self, sql: str, params: list[str | None]) -> RowSource:
        raise NotImplementedError

    async def schema(self) -> dict | None:
        """Optional: describe your tables for information_schema/pg_catalog
        emulation. See pg_mimic.catalog."""
        return None

    async def prepare(self, sql: str, param_oids: list[int | None]) -> Statement:
        if self._connection is not None:
            from . import catalog

            middleware_statement = await catalog.resolve(self._connection, sql, param_oids)
            if middleware_statement is not None:
                return middleware_statement
        return CallbackStatement(self, sql, param_oids)
