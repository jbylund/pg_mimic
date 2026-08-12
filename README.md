# pg_mimic

A pure-Python, asyncio PostgreSQL server protocol emulator — the Postgres analog of
[mysql-mimic](https://github.com/barakalon/mysql-mimic). Embed it in your own process, subclass `Session`
to answer queries with arbitrary Python logic, and get a fully wire-compatible server that real Postgres
clients, drivers, ORMs, and tools (`psql`, `psycopg`, `asyncpg`, JDBC, ...) can connect to.

Useful for testing, proxies/virtual databases, query interception, and fronting non-Postgres backends with
a Postgres-speaking API.

## Contents

- [Install](#install)
- [Quick start](#quick-start)
- [Serving in-memory tables](#serving-in-memory-tables)
- [The Session API](#the-session-api)
- [Using it in tests](#using-it-in-tests)
- [Authentication](#authentication)
- [What's handled automatically](#whats-handled-automatically)
- [Known limitations](#known-limitations)
- [Development](#development)

## Install

```bash
pip install pg-mimic
```

## Quick start

```python
import asyncio
from pg_mimic import PgServer, ResultColumn, Session


class MySession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("a", str), ResultColumn.for_type("b", int)]

    async def query(self, sql, params):
        yield ("hello", 1)
        yield ("world", 2)


async def main():
    server = PgServer(session_factory=MySession)
    await server.start_server(host="127.0.0.1", port=5432)
    await server.serve_forever()


asyncio.run(main())
```

```bash
$ psql "host=127.0.0.1 port=5432 user=test dbname=test" -c "select * from anything"
   a   | b
-------+---
 hello | 1
 world | 2
(2 rows)
```

More examples: [`examples/simple.py`](examples/simple.py), [`examples/echo.py`](examples/echo.py) (logs every
statement it receives -- good for poking at the server interactively with `psql` and watching what comes
through), [`examples/parameterized.py`](examples/parameterized.py) (real bind parameters),
[`examples/tables.py`](examples/tables.py) (in-memory tables, no session code),
[`examples/dbapi_proxy.py`](examples/dbapi_proxy.py) (fronting a real sqlite3 database),
[`examples/git_sql.py`](examples/git_sql.py) (a git repository as four queryable tables -- the longest
example, and the one that shows a declared `schema()` paying for itself: `\d` and `information_schema`
for free, exact `describe()` without executing, and inferred parameter types).

## Serving in-memory tables

If all you want is "serve these tables", you don't need a `Session` at all. `TableSession` takes Python
rows and answers real SQL over them:

```python
from decimal import Decimal
from pg_mimic import PgServer, TableSession

tables = {
    "users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}],
    "orders": [{"id": 10, "user_id": 1, "total": Decimal("9.99")}],
}
server = PgServer(session_factory=lambda: TableSession(tables))
```

```bash
$ psql "host=127.0.0.1 port=5432 user=test dbname=test" \
    -c "select u.name, sum(o.total) as spent from users u
          join orders o on o.user_id = u.id group by u.name"
 name  | spent
-------+-------
 alice |  9.99
(1 row)
```

`SELECT`, `WHERE`, `JOIN`, `GROUP BY`, `ORDER BY`, `LIMIT`, subqueries and bind parameters are executed by
[sqlglot](https://github.com/tobymao/sqlglot)'s executor — already a hard dependency, and already what
answers table-less `SELECT`s elsewhere in pg_mimic. `schema()` is derived from the tables, so `\dt`,
`\d users` and `information_schema` work with no extra code, and a client's `SELECT 1` ping is answered
too. Rows may be dicts (the executor's native shape) or tuples, in which case declare their names:
`TableSession({"users": [(1, "alice")]}, columns={"users": ["id", "name"]})`.

A dict key is the identifier **as written**, exactly as `CREATE TABLE` reads one. Postgres folds an
unquoted name to lower case and preserves a quoted one, so `{"users": ...}` answers `FROM users`,
`FROM Users` and `FROM "users"`, while `{"Users": ...}` answers only `FROM "Users"` and reports
`FROM Users` as a missing relation. Column keys work the same way — a `{"userId": ...}` row is
`SELECT "userId"`, not `SELECT userId`. Lower-case keys are the ones that behave the way hand-written
SQL expects.

### Where the column types come from

pg_mimic treats column shape as a **declared fact**, not something read off result rows (see
[The Session API](#the-session-api)). `TableSession` keeps that: it inspects the tables' Python values
**once, at construction**, to declare a schema, and every query's output columns are then derived from
*that* schema, by annotating the query's parse tree with sqlglot's type annotator. Nothing peeks at the
rows a query returns. So a query matching nothing describes exactly like one matching everything, an
empty table is not silently `text`, and `count(*)` is `bigint` because sqlglot types it that way.

Where inference genuinely can't settle a type it asks rather than guesses — the same line
`ResultColumn.for_type` draws, except that it draws it at construction time, so you find out when you build
the session and not on some later query:

```python
from datetime import datetime
from pg_mimic import JSONB, TableSession

TableSession(
    {"events": [], "notes": [{"body": None}], "docs": [{"body": {"a": 1}}], "users": [{"tags": ["staff"]}]},
    columns={
        "events": {"id": int, "at": datetime},  # empty table: nothing to infer from
        "notes": {"body": str},  # every value is NULL
        "docs": {"body": JSONB},  # a dict is a json document...
        "users": {"tags": list[str]},  # ...and a list is equally an array or json
    },
)
```

A `columns` entry may name just the columns you need to pin down; the rest are still inferred. Values are
either a Python type (resolved exactly as `ResultColumn.for_type` resolves it) or an OID.

### Read-only, on purpose

`INSERT`/`UPDATE`/`DELETE` are refused with `read_only_sql_transaction` (25006) rather than applied. The
tables are your own objects, and mutating them per connection — with no isolation between clients and no
way to roll back — would be a worse lie than a clear error. And anything sqlglot's executor can't run
(recursive CTEs, most of Postgres's function library) is an error too, never an approximate answer: see
[Known limitations](#known-limitations).

## The Session API

Override two methods:

- `describe(sql, param_oids) -> list[ResultColumn] | None` — declares column shape (names + Postgres type
  OIDs). Return `None` for statements that produce no rows (`INSERT`/`UPDATE`/`DELETE` without `RETURNING`,
  etc). Column shape is always a **declared fact**, never inferred by peeking at row data.
- `query(sql, params) -> AsyncIterator[tuple]` — the actual rows, as an async generator (or a coroutine
  returning any iterable). `params` are the real out-of-band bind parameter values as decoded text strings
  (not textually interpolated into `sql`) — pg_mimic decodes them itself even when a client sends them in
  binary format (psycopg, JDBC, and most other drivers do this by default for common scalar types).
  An array parameter arrives as a (possibly nested) `list` of those strings — `["1", "2"]` for an
  `int8[]`, for the same reason a scalar `int8` arrives as `"1"`. NULL elements are `None`.

Optionally override `schema()` to describe your tables for `information_schema` introspection (see below).

### Declaring column types

`ResultColumn.for_type(name, py_type)` infers the Postgres type where that is unambiguous, and refuses
where it isn't. `ResultColumn(name, oid)` always works if you'd rather be explicit:

```python
from pg_mimic import ResultColumn, ARRAY_OID, JSONB, TEXT, NUMERIC

ResultColumn.for_type("n", int)  # int8
ResultColumn.for_type("tags", list[str])  # text[]
ResultColumn.for_type("grid", list[list[int]])  # int8[], 2-dimensional per value
ResultColumn("doc", JSONB)  # json, named explicitly
ResultColumn("tags", ARRAY_OID[TEXT])  # same as list[str]
```

Bare `list` and `dict` **raise**: a list is equally a Postgres array or a json document, and since column
shape is declared before any row exists there is nothing to inspect that would settle it. Say which you
mean. Nesting doesn't change the declared type — `list[int]` and `list[list[int]]` are both `int8[]`,
because a Postgres array OID carries no dimensionality; that rides in each value.

Arrays work in both wire formats, and must be rectangular — a ragged list is rejected rather than
silently reshaped, since Postgres has no wire representation for one.

### Bulk data: `COPY`

`COPY ... FROM STDIN` and `COPY ... TO STDOUT` are the standard bulk path — `psql`'s `\copy`,
`psycopg`'s `cursor.copy()`, `asyncpg`'s `copy_to_table()`/`copy_from_query()`, and most ETL tooling.
pg_mimic handles the sub-protocol (`CopyInResponse`/`CopyOutResponse`, the `CopyData` stream,
`CopyDone`/`CopyFail`, the `COPY <n>` command tag) and the text and CSV formats, so a session only
ever sees rows:

```python
class LoaderSession(Session):
    async def copy_in(self, sql, rows):
        # rows: an async iterator of tuples of `str | None`, already split on the
        # delimiter, un-escaped, with the null string turned into None
        count = 0
        async for row in rows:
            await self.store(row)
            count += 1
        return count  # or None, to report however many pg_mimic decoded

    async def copy_out(self, sql):
        yield (1, "alice")
        yield (2, None)  # None is NULL: `\N` in text format, an empty field in CSV
```

Iterate `rows` lazily — that's the point of `COPY`, and nothing is buffered on the way in. Both hooks
are optional, and a missing one is refused with a `feature_not_supported` error *before* the server
invites the client to start sending: a session that silently accepted and dropped bulk data would
look exactly like a successful load.

Options are read from the statement — `FORMAT text` (the default: tab-separated, `\N` for NULL, the
documented backslash escapes), `FORMAT csv`, `DELIMITER`, `NULL`, `HEADER`, and CSV's
`QUOTE`/`ESCAPE` — in both the modern `WITH (...)` syntax and the legacy bare-keyword one, since real
clients write both. An option pg_mimic doesn't implement (`FREEZE`, `FORCE_QUOTE`, `HEADER MATCH`, a
non-UTF-8 `ENCODING`) is **refused by name** rather than ignored, because silently dropping one would
change the bytes on the wire.

Two things a planner-less mimic can't know, and refuses rather than invents:

- `HEADER` on the way out needs column names, which only the statement (`COPY t (a, b) TO STDOUT`) or
  `Session.schema()` can supply.
- The copy sub-protocol has no `RowDescription`, so there are no declared column types outbound: each
  value is rendered from its own Python type, and a `list`/`dict` — equally an array or a json
  document — is refused. Yield those already formatted.

A `COPY` from a server-side file (`COPY t FROM '/path'`, `COPY t TO PROGRAM ...`) isn't a protocol
exchange at all, so it falls through to your `describe()`/`query()` like any other statement. A
session that overrides `prepare()` bypasses the middleware chain and so bypasses this too — call
`pg_mimic.copy.copy_statement(self, sql)` to get the same `Statement` back.

### Why two methods instead of one

Postgres's extended query protocol (`Parse`/`Bind`/`Describe`/`Execute`/`Sync`) genuinely separates "what
columns will this return" from "run it and give me rows" — a client can ask `Describe` before ever issuing
`Execute`. `describe()` answers that without touching any row source; `query()` is only ever invoked once
`Execute` actually runs, so side effects happen exactly when a real Postgres server would trigger them, not
earlier.

### Real parameterized queries and prepared statements

Both the simple query protocol (`psql`, one-shot queries) and the extended protocol (parameterized queries,
prepared statement reuse) are fully supported and drive the exact same `Statement`/`Portal` machinery
internally — a prepared statement `Parse`d once can be `Bind`+`Execute`d repeatedly with different
parameters, and large results stream incrementally (`Execute`'s row-limit / `PortalSuspended` semantics)
rather than being forced into memory all at once, as long as `query()` yields lazily.

### Power tier: full Statement/Portal control

For sessions that front a real backing connection (see `examples/dbapi_proxy.py`), override `prepare()`
directly instead of `describe()`/`query()`:

```python
from pg_mimic import Statement, Portal


class MyStatement(Statement):
    async def describe(self): ...
    def bind(self, params) -> Portal: ...


class MySession(Session):
    async def prepare(self, sql, param_oids) -> Statement:
        return MyStatement(...)
```

This maps 1:1 onto a real database driver's own prepare/bind/execute stages with no translation overhead.

### Reporting errors

Raise `PgError(sqlstate, message)` from anywhere in a session and the client gets a real `ErrorResponse`
carrying that SQLSTATE, so driver-side error handling (`except psycopg.errors.UndefinedTable`,
`e.sqlstate == "42P01"`, an ORM's retry rules) behaves as it would against a real Postgres. Extra
`ErrorResponse` fields can ride along as keyword arguments, keyed by their protocol field byte —
`PgError(UNDEFINED_TABLE, "...", D="a longer explanation")` sets the detail field.

The codes live in `pg_mimic.errors`, which is public: the constants are the whole point of raising the
exception, and `PgError("42P01", ...)` says nothing at the call site.

```python
from pg_mimic import Session
from pg_mimic.errors import PgError, UNDEFINED_TABLE


class MySession(Session):
    async def describe(self, sql, param_oids):
        if "orders" not in sql:
            raise PgError(UNDEFINED_TABLE, 'relation "whatever" does not exist')
        ...
```

Anything else a session raises still reaches the client, as `XX000` (`internal_error`) with the exception's
string — a bug reported rather than a dropped connection. `PgError` is how you say the error was deliberate.

## Using it in tests

`pg_mimic.testing` stands a server up on an ephemeral port for the duration of a test, so code under
test can connect to a real Postgres endpoint — its own driver, its own connection string — with no
database process anywhere. Here is a complete test:

```python
import psycopg
from pg_mimic import ResultColumn, Session
from pg_mimic.testing import serve_in_thread


class OrdersSession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("id", int), ResultColumn.for_type("total", int)]

    async def query(self, sql, params):
        yield (1, 250)


def test_report_totals_orders():
    with serve_in_thread(OrdersSession) as server:
        with psycopg.connect(server.dsn(), autocommit=True) as conn:
            assert conn.execute("SELECT id, total FROM orders").fetchall() == [(1, 250)]
```

`serve_in_thread()` runs the server on **its own event loop in its own thread**, and that is the
point rather than an implementation detail. A blocking client — sync `psycopg`, a DBAPI driver,
anything that doesn't `await` — occupies the thread it runs on. Give it a server on that same event
loop and the test deadlocks: nothing is left to accept the connection or answer the query while the
client sits waiting for a reply. A separate thread makes the client's blocking irrelevant, which is
also how an embedded mimic really runs in production. Use it from sync tests, and from async tests
that call a blocking client anyway.

For an async client, `serve()` runs the server on the current loop, which is safe because an awaiting
client hands control back:

```python
import asyncpg
from pg_mimic.testing import serve


async def test_async_client():
    async with serve(OrdersSession) as server:
        conn = await asyncpg.connect(host="127.0.0.1", port=server.port, user="u", database="d")
        assert await conn.fetch("SELECT id, total FROM orders")
        await conn.close()
```

Both take the same keyword arguments as `PgServer` (`auth_plugin_factory`, `identity_provider`,
`server_version`) and yield the running server, so `server.port` and
`server.dsn(user=..., dbname=...)` give the client its connection details. On the way out the server
is closed and any connection the test left open is dropped rather than waited for — a test that
forgets to close its connection fails on its own assertions, it doesn't hang.

### pytest fixtures

Installing pg-mimic registers a pytest plugin. Point `pg_mimic_session_factory` at your session and
`pg_mimic_server` / `pg_mimic_dsn` follow from it:

```python
# conftest.py
import pytest


@pytest.fixture
def pg_mimic_session_factory():
    return OrdersSession


# test_orders.py
def test_report_totals_orders(pg_mimic_dsn):
    with psycopg.connect(pg_mimic_dsn, autocommit=True) as conn:
        assert conn.execute("SELECT id, total FROM orders").fetchall() == [(1, 250)]
```

`pg_mimic_server` is the threaded server, so the same fixture works in sync and async tests. Override
`pg_mimic_server_kwargs` to return extra `PgServer` arguments. There is deliberately no default
session: a placeholder that answered queries would turn "you forgot to point this at your session"
into a test asserting against rows nobody wrote, so the fixture fails with a message saying what to
override.

What this is and isn't: the server answers exactly what your `Session` answers, so it tests *your
code's* SQL, drivers, connection handling and error paths — not Postgres's semantics. pg_mimic has no
planner and no storage, so `SELECT` won't filter, join or aggregate anything your session didn't
already compute (`Session.middleware` can be extended with `static_select` to evaluate table-less
expressions; see [What's handled automatically](#whats-handled-automatically)). The exception is
[`TableSession`](#serving-in-memory-tables), which does execute the query — against your rows, with
sqlglot's executor, as far as that executor goes. If a test's correctness depends on Postgres actually
executing the query, that test wants a real Postgres.

`pg_mimic.testing` imports without pytest installed — the fixtures live in a separate module that
pytest loads itself — and it is not re-exported from the `pg_mimic` namespace, so nothing that merely
runs a server pays to import it. Ask for it by name: `from pg_mimic.testing import serve_in_thread`.

## Authentication

Four pluggable mechanisms, matching real Postgres's `pg_hba.conf` methods:

```python
from pg_mimic import PgServer, TrustAuthPlugin, Md5PasswordAuthPlugin, ScramSha256AuthPlugin, SimpleIdentityProvider

# trust (default): any username/password accepted
server = PgServer(session_factory=MySession)

# password-protected
server = PgServer(
    session_factory=MySession,
    auth_plugin_factory=lambda username: ScramSha256AuthPlugin(),  # or Md5PasswordAuthPlugin / ClearTextPasswordAuthPlugin
    identity_provider=SimpleIdentityProvider({"alice": "s3cret"}),
)
```

## What's handled automatically

A small middleware chain (mirroring mysql-mimic's design) answers the boilerplate every client sends,
before it ever reaches your `Session`, so real clients/ORMs/`psql` work without you implementing any of it:

- Transaction control: `BEGIN`/`START TRANSACTION`, `COMMIT`/`END`, `ROLLBACK` — including the
  failed-transaction state (`25P02`) real Postgres enters after an error until `ROLLBACK`.
- Savepoints: `SAVEPOINT`, `RELEASE [SAVEPOINT]`, `ROLLBACK TO [SAVEPOINT]`, tracked as a real stack
  (an unknown name is `3B001`). `ROLLBACK TO` clears the failed-transaction state *without* ending
  the transaction, which is what psycopg's nested `transaction()` and SQLAlchemy's `begin_nested()`
  are built on.
- `SET`/`RESET`/`SHOW` (session variables), in the spellings clients actually send: `SET TIME ZONE`,
  `SET SCHEMA`, `SET SESSION CHARACTERISTICS AS TRANSACTION`, `RESET ALL`, and the
  `DISCARD`/`DEALLOCATE` resets a pooler like pgbouncer sends between clients. Changing one of the
  `GUC_REPORT` settings sends the `ParameterStatus` real Postgres would, so a client's cached
  `client_encoding` (psycopg's `conn.info.encoding`), `application_name` and friends stay current —
  and `application_name` from your startup packet is echoed back in the initial burst.

  `SET ROLE` and `SET SESSION AUTHORIZATION` are the deliberate exceptions: they change
  *authorization*, and pg_mimic has no role catalog to validate against and no privilege model to
  apply, so they fall through to your session rather than being accepted on a promise nothing here
  can keep.
- Session functions: `SELECT version()`, `current_user`, `current_database()`, `current_setting('x')`,
  `set_config()`, `pg_backend_pid()` — things only the connection can answer.
- asyncpg's type introspection, so it can build codecs for array and other non-builtin types.
- `information_schema.tables` / `information_schema.columns`, and the slice of `pg_catalog` psql's
  `\dt`, `\d <table>` and `\dn` read — both built from your `Session.schema()`.
- Multi-statement simple-query batches (`"BEGIN; INSERT ...; COMMIT;"` sent as one `'Q'` message, e.g. by
  `psql -f script.sql`) are split into individual statements (via sqlglot), each getting its own
  `RowDescription`/`DataRow*`/`CommandComplete` — not silently merged or truncated.
- The `COPY` sub-protocol and its text/CSV formats, so `copy_in()`/`copy_out()` see decoded rows — see
  [Bulk data: `COPY`](#bulk-data-copy).

Everything else — `SELECT 1` and `select *` included — falls through to your `describe()`/`query()`
(or `prepare()`). The line is *state you've already declared*: the chain answers questions about the
connection on your behalf, but an ordinary query means whatever your session says it means.

The chain is a plain class attribute, so you can extend, reorder, or switch it off entirely:

```python
from pg_mimic import Session
from pg_mimic.middleware import DEFAULT_MIDDLEWARE, static_select


class MySession(Session):
    # evaluate table-less SELECTs like `SELECT 1` with sqlglot instead of
    # passing them through (the pre-0.1.1 default)
    middleware = DEFAULT_MIDDLEWARE + (static_select,)


class RawSession(Session):
    middleware = ()  # see every statement yourself, boilerplate included
```

A middleware is just `async (MiddlewareContext) -> Statement | None` — return `None` to pass the
statement along. Put your own first to override a built-in rather than only add to it, and use
`StaticStatement(sql, columns, rows)` to answer with a fixed result:

```python
from pg_mimic import ResultColumn, StaticStatement


async def answer_ping(ctx):
    if ctx.sql.lower() != "ping":
        return None  # not mine -- pass it along
    return StaticStatement("ping", [ResultColumn.for_type("pong", str)], [("pong",)])
```

The chain lives in [`pg_mimic/middleware.py`](pg_mimic/middleware.py); the `information_schema`
emulation it delegates to is in [`pg_mimic/catalog.py`](pg_mimic/catalog.py).

## Known limitations

- **Binary format covers common scalars and their arrays.** Text is the default in both directions.
  Binary is supported, in either direction, for `bool`, the int and float widths, `bytea`, the string
  types, `numeric`, `date`, `time`, `timestamp`/`timestamptz`, `interval`, `uuid`, `json`/`jsonb`,
  and arrays of any of those. Anything else (`money`, the network types, `timetz`) is refused with a
  clear `feature_not_supported` error rather than encoded on a guess, since a wrong
  byte order or epoch offset would surface as a plausible-looking wrong *value* instead of a failure.
  Text format carries those types fine; only the binary path is narrow.

  Note that a client requesting binary results gets that error rather than a silent downgrade to text.
  Degrading looks tempting but isn't safe: asyncpg ignores the per-column format in `RowDescription`
  and decodes according to its own request regardless, so text bytes reach a binary parser. psycopg
  reads the field but assumes one format for the whole row.
- **A text-format array parameter with no declared type OID reaches your session as a string.** Some
  clients (psycopg, for `text[]`) send array parameters with OID 0 and let the server infer the type.
  With nothing declared, `{a,b}` can't be told apart from a string that merely looks like an array, so
  pg_mimic passes it through untouched rather than guessing.

  This costs the session a parsed list, not the client its value: an untouched array literal is still
  a valid one, so a client that sends an array and reads it back gets exactly what it sent either way.
  Binary parameters are unaffected — those already require a known OID. A session that knows its own
  SQL can close the gap by setting `param_oids` on the `Statement` it returns from `prepare()`.
- **`TableSession` executes as much SQL as sqlglot's executor does, which is not all of Postgres.**
  Joins, grouping, ordering, subqueries and the common functions work; recursive CTEs and window functions
  don't, function coverage is partial, and numeric comparisons go through Python floats — so a value no
  float represents exactly compares wrong at the boundary (`where total = 9.99` misses a `Decimal("9.99")`
  row). That is the executor's arithmetic, not pg_mimic's parameter handling: a literal in the SQL and a
  bind parameter behave identically, and neither is exact.

  The executor's more dangerous gaps are the clauses it parses and then answers *wrongly* — a wrong answer
  wearing a right answer's clothes. `TableSession` repairs the ones it can, verified against a real
  PostgreSQL: `OFFSET` and `DISTINCT ON` are applied to the rows the executor returns (with `LIMIT`
  counting what survives, as Postgres does); `NOT IN (subquery)`, which the executor never filters on,
  becomes a `NOT EXISTS` carrying SQL's NULL rule; `ORDER BY` places NULLs where Postgres places them
  instead of raising on the comparison; and a `UNION`, `EXCEPT` or `INTERSECT` over the same table runs
  both of its branches rather than the first one twice.

  `TABLESAMPLE` is refused outright, because the executor ignores it and nothing here can repair that.
  An `OFFSET` or `DISTINCT ON` nested inside a subquery is refused for the same reason: the
  repair reaches the query's own rows, and one buried in a subquery would be silently ignored as before.

  Everything it can't run is an error, not an approximate answer, and a column type it can't derive is an
  error too rather than a `text` guess — including a bind parameter nothing in the query types, which is
  Postgres's own `42P18`. If a test needs Postgres's exact evaluation semantics, it wants a real Postgres;
  `TableSession` is for "my code should see these rows over a real wire".
- **No TLS.** `SSLRequest` is answered correctly (`'N'`, continue in plaintext) so opportunistic clients
  still connect, but there's no `ssl.SSLContext` upgrade path yet.
- **`pg_catalog` is emulated only as far as psql's `\d` family needs.** `pg_class`, `pg_namespace`,
  `pg_attribute`, `pg_type` and `pg_am` are built from your `Session.schema()`; indexes, constraints,
  triggers, policies and defaults are declared but always empty, since pg_mimic models none of them.
  A catalog query that can't be answered returns no rows rather than falling through to your session,
  which would otherwise reply with a shape the client reads as catalog data.

  asyncpg's type introspection is handled separately, in `pg_mimic.typeinfo`: it's a recursive CTE
  that sqlglot can neither parse nor execute, so it's matched by shape and answered from the same
  `pg_type` rows. That's coupled to a query asyncpg could rewrite in any release — narrowly, and it
  fails loudly rather than quietly if it does.
- **`Describe(Statement)` before any `Bind`** can only answer accurately for statement shapes pg_mimic (or
  your `describe()`) can determine without real parameter values — inherent to being a planner-less mimic,
  not something a workaround can fully close. In practice this rarely matters: most drivers (including
  psycopg's default flow) `Describe` the *portal*, after `Bind`, which is always exact.
- **Binary `COPY` is refused.** `COPY ... WITH (FORMAT binary)` gets a `feature_not_supported` error,
  for the same reason binary result encoding refuses types it can't encode: the binary copy format is
  a tuple layout keyed to a table's declared column types, and pg_mimic has no table. Text and CSV
  `COPY` cover the same ground. asyncpg's `copy_records_to_table()` always asks for binary, so that
  one method is unavailable; its `copy_to_table(source=..., format="csv")` and `copy_from_query()`
  work.
- **No `CopyBothResponse`.** That message exists only for streaming replication, which pg_mimic
  doesn't emulate — there is no WAL behind it to stream.

## Development

```bash
pip install -e ".[dev]"
pytest
```

CI enforces `ruff check` and `ruff format --check`. To run them on commit instead of finding out from a
red build:

```bash
pip install pre-commit && pre-commit install
```

Released versions are listed in [CHANGELOG.md](CHANGELOG.md). Licensed under the MIT
[LICENSE](LICENSE).
