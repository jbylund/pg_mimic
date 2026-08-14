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
from typing import Any, AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Sequence

from .results import ResultColumn
from .state import SessionState, SettingValue
from .types import TEXT

Row = tuple
RowSource = AsyncIterable[Row] | Iterable[Row]
QueryResult = RowSource | Awaitable[RowSource]


class Statement(ABC):
    sql: str
    param_oids: list[int | None] = []

    @abstractmethod
    async def describe(self) -> list[ResultColumn] | None:
        """Column shape, or None if this statement produces no rows (NoData)."""

    @abstractmethod
    def bind(self, params: list[str | None]) -> Portal:
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
    def __init__(self, session: Session, sql: str, params: list[str | None]):
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

    def __init__(self, session: Session, sql: str, param_oids: list[int | None]):
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


class StaticStatement(Statement):
    """A Statement whose result is already fully known (SET/SHOW/BEGIN/static
    SELECT/information_schema/...) -- no CallbackStatement/Session involved.

    `rows` may be a list, or a callable returning one, and is normalised to a
    callable here so there is only ever one kind of thing to run. Pass a callable
    whenever the answer can change between Parse and Execute -- `SHOW x` and
    `current_setting` both read state a later statement may move. A list is fixed
    at build time, which is right for the simple protocol (it resolves per
    execution) and wrong for a prepared one, which resolves once and executes many
    times: the client would go on being told what was true at Parse. See #63.
    """

    def __init__(
        self,
        sql: str,
        columns: list[ResultColumn] | None,
        rows: list[Row] | Callable[[], list[Row]],
        on_execute=None,
    ):
        self.sql = sql
        self.param_oids: list[int | None] = []
        self._columns = columns
        self._rows: Callable[[], list[Row]] = rows if callable(rows) else lambda: rows
        self._on_execute = on_execute

    async def describe(self) -> list[ResultColumn] | None:
        return self._columns

    def bind(self, params: list[str | None]) -> Portal:
        return StaticPortal(self._rows, self._on_execute)


class StaticPortal(Portal):
    def __init__(self, rows: Callable[[], list[Row]], on_execute):
        self._rows = rows
        self._on_execute = on_execute
        self._row_source: AsyncIterator[Row] | None = None

    async def execute(self, max_rows: int) -> tuple[list[Row], bool]:
        if self._row_source is None:
            if self._on_execute is not None:
                # Awaited when there is something to await. A middleware effect is
                # ordinarily synchronous -- it writes connection state and returns --
                # but one that has to reach the *session* cannot be, and this is the
                # only place an effect runs. Same shape as _resolve_row_source, for
                # the same reason: the caller should not have to know which it got.
                effect = self._on_execute()
                if inspect.isawaitable(effect):
                    await effect
            # After on_execute, so a statement that both writes and reads back --
            # `SELECT set_config('x', 'y', false)` -- reports the value it just set.
            self._row_source = _rows_as_async_iter(self._rows())
        return await drain_rows(self._row_source, max_rows)


async def _rows_as_async_iter(rows: list[Row]) -> AsyncIterator[Row]:
    for row in rows:
        yield row


def statement_from_rows(
    sql: str,
    column_names: Iterable[str],
    rows: list[Row] | Callable[[], list[Row]],
    on_execute: Any = None,
) -> Statement:
    """A StaticStatement over rows some engine already produced.

    Column types come from the first row, the only place they can -- so with no
    rows there is nothing to infer from and the columns are declared TEXT rather
    than guessed. Shared because that branch is easy to get subtly different in
    each copy.
    """
    # A callable is called once here, because the column types can only come from
    # real values -- and then passed through, so execution re-runs it.
    sample = rows() if callable(rows) else rows
    if sample:
        columns = [ResultColumn.for_type(name, type(value)) for name, value in zip(column_names, sample[0])]
    else:
        columns = [ResultColumn(name, TEXT) for name in column_names]
    return StaticStatement(sql, columns, rows, on_execute)


class Session(BaseSession):
    """The class most session authors subclass. Override describe()/query()
    (and optionally schema()) instead of hand-writing Statement/Portal.

    `middleware` is the chain of client boilerplate answered before your
    describe()/query() is consulted -- transaction control, SET/SHOW, session
    functions and information_schema by default. It's an ordinary class
    attribute, so a subclass can extend it::

        from pg_mimic.middleware import DEFAULT_MIDDLEWARE, static_select

        class MySession(Session):
            middleware = DEFAULT_MIDDLEWARE + (static_select,)

    reorder it, or set it to `()` to see every statement yourself. Ordinary
    queries -- `SELECT 1` included -- always reach your session regardless.
    """

    # None means middleware.DEFAULT_MIDDLEWARE -- a sentinel rather than the tuple
    # itself because that module imports this one, so it can't be imported here at
    # class-definition time. An explicit () means "no middleware at all".
    _connection: Any = None
    middleware: Sequence[Any] | None = None

    #: The connection's session state -- settings, prepared statements, portals,
    #: savepoints, and who is connected. Assigned by the framework before init()
    #: runs, so an override may use it without calling super(). Read what the
    #: middleware decided, or manage it yourself if you set `middleware = ()`.
    #: See pg_mimic.state.
    #:
    #: `session_vars` holds *values*, not the text a client typed, so a setting
    #: comes back ready to use::
    #:
    #:     self.state.session_vars["row_security"]      # True, not "on" or "tr"
    #:     self.state.session_vars["statement_timeout"] # 5000, not "5s"
    #:
    #: An overridden setting only -- one nobody has SET is absent rather than at
    #: its default.
    state: SessionState = None  # type: ignore[assignment]

    async def init(self, connection: Any) -> None:
        self._connection = connection

    @property
    def connection(self) -> Any:
        """The `Connection` this session is answering for -- the way to reach the
        things that belong to the wire rather than to the query.

        Assigned by the framework before `init()` runs, so it is available from
        anywhere a session does its work::

            self.connection.notice("row limit reached", severity="WARNING")
            self.connection.notify_listeners("orders", "42")

        `pid`, `username`, `database` and `startup_params` are the other things
        worth reading off it; `state` is on the session directly. Raises if read
        before the session is bound to a connection, which is a clearer failure
        than the `None` that used to come back from the private attribute.
        """
        if self._connection is None:
            raise RuntimeError("this session is not attached to a connection yet -- available from init() onwards")
        return self._connection

    async def describe(self, sql: str, param_oids: list[int | None]) -> list[ResultColumn] | None:
        return None

    async def query(self, sql: str, params: list[str | None]) -> RowSource:
        raise NotImplementedError

    async def set_parameter(self, name: str, raw_value: str | None, parsed_value: SettingValue | None) -> None:
        """Told about every SET, RESET and set_config() the middleware handled.

        Both spellings, because a session wants different ones for different jobs:
        `raw_value` is the text the client wrote, which is what to forward to a real
        backend, and `parsed_value` is what it means, which is what to act on::

            SET work_mem = '32MB'      raw='32MB'  parsed=32768   (its own unit, kB)
            SET row_security = 'tr'    raw='tr'    parsed=True
            SET client_encoding='utf8' raw='utf8'  parsed='UTF8'
            SET app.tenant = 'acme'    raw='acme'  parsed='acme'  (a custom GUC has
                                                                   no type to parse)
            RESET work_mem             raw=None    parsed=None

        Both are None for a RESET, which is the only thing None means here -- no
        parameter parses to it.

        Handed over rather than read off `self.state.session_vars`, which holds the
        same parsed value by the time this runs. Depending on that would make the
        order the connection happens to do things in part of this contract.

        `pg_mimic.settings_values.parse` and `.render` are the same pair the
        middleware used to produce it, for a session that needs to read a value this
        did not hand it -- one a real backend reported back, say.

        `name` is lowercased. Dotted custom GUCs (`app.tenant_id`) arrive here too,
        which is the point: the alternative was re-parsing raw SQL out of `query()`
        to discover it was connection boilerplate at all.

        The connection has already recorded the change and will send any
        ParameterStatus it owes. Raising `PgError` rejects the setting and undoes
        that record, so a session may refuse one it cannot honour::

            async def set_parameter(self, name, raw_value, parsed_value):
                if name == "app.tenant_id" and not self.may_use(raw_value):
                    raise PgError(INVALID_PARAMETER_VALUE, f"no such tenant: {raw_value}")

        Does nothing by default.
        """

    async def schema(self) -> dict | None:
        """Optional: describe your tables for information_schema/pg_catalog
        emulation. See pg_mimic.catalog."""
        return None

    async def copy_in(self, sql: str, rows: AsyncIterator[Row]) -> int | None:
        """Optional: handle `COPY ... FROM STDIN`.

        `rows` is an async iterator of tuples of `str | None` -- one per line the
        client sent, already split on the delimiter, un-escaped, and with the null
        string turned into None. Framing, the text/CSV format and the CopyData
        message stream are all handled before this is called. Iterate it lazily
        (that is the point of COPY) and return how many rows you stored, or None to
        let pg_mimic report the number it decoded.

        Not implemented by default, and the absence is detected before the server
        invites the client to start sending: a session that silently accepted and
        dropped bulk data would look exactly like a successful load.
        """
        raise NotImplementedError

    async def copy_out(self, sql: str) -> RowSource:
        """Optional: handle `COPY ... TO STDOUT`.

        Yield rows the same way query() does; pg_mimic formats them. There are no
        declared column types here -- the copy sub-protocol has no RowDescription --
        so each value is rendered from its own Python type, and a `list`/`dict`,
        which could equally be an array or a json document, is refused rather than
        guessed at. Yield those already formatted.
        """
        raise NotImplementedError

    async def prepare(self, sql: str, param_oids: list[int | None]) -> Statement:
        if self._connection is not None:
            from .middleware import DEFAULT_MIDDLEWARE, resolve

            chain = DEFAULT_MIDDLEWARE if self.middleware is None else self.middleware
            if chain:
                middleware_statement = await resolve(self._connection, sql, param_oids, chain)
                if middleware_statement is not None:
                    return middleware_statement
        return CallbackStatement(self, sql, param_oids)
