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
- [The Session API](#the-session-api)
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
[`examples/dbapi_proxy.py`](examples/dbapi_proxy.py) (fronting a real sqlite3 database).

## The Session API

Override two methods:

- `describe(sql, param_oids) -> list[ResultColumn] | None` — declares column shape (names + Postgres type
  OIDs). Return `None` for statements that produce no rows (`INSERT`/`UPDATE`/`DELETE` without `RETURNING`,
  etc). Column shape is always a **declared fact**, never inferred by peeking at row data.
- `query(sql, params) -> AsyncIterator[tuple]` — the actual rows, as an async generator (or a coroutine
  returning any iterable). `params` are the real out-of-band bind parameter values as decoded text strings
  (not textually interpolated into `sql`) — pg_mimic decodes them itself even when a client sends them in
  binary format (psycopg, JDBC, and most other drivers do this by default for common scalar types).

Optionally override `schema()` to describe your tables for `information_schema` introspection (see below).

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
- `SET`/`SHOW` (session variables).
- Session functions: `SELECT version()`, `current_user`, `current_database()`, `current_setting('x')`,
  `pg_backend_pid()` — things only the connection can answer.
- `information_schema.tables` / `information_schema.columns`, built from your `Session.schema()`.
- Multi-statement simple-query batches (`"BEGIN; INSERT ...; COMMIT;"` sent as one `'Q'` message, e.g. by
  `psql -f script.sql`) are split into individual statements (via sqlglot), each getting its own
  `RowDescription`/`DataRow*`/`CommandComplete` — not silently merged or truncated.

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

- **Binary format covers common scalars only.** Text is the default in both directions. Binary is
  supported, in either direction, for `bool`, the int and float widths, `bytea`, the string types,
  `date`, `timestamp`/`timestamptz`, `uuid` and `json`/`jsonb` — the set real clients actually ask
  for. Anything else (notably `numeric`, `interval` and arrays) is refused with a clear
  `feature_not_supported` error rather than encoded on a guess, since a wrong byte order or epoch
  offset would surface as a plausible-looking wrong *value* instead of a failure. Text format
  carries those types fine; only the binary path is narrow.
- **No TLS.** `SSLRequest` is answered correctly (`'N'`, continue in plaintext) so opportunistic clients
  still connect, but there's no `ssl.SSLContext` upgrade path yet.
- **`pg_catalog` is not emulated** (only `information_schema`) — tools that specifically query
  `pg_catalog.pg_class` etc. won't see your schema.
- **`Describe(Statement)` before any `Bind`** can only answer accurately for statement shapes pg_mimic (or
  your `describe()`) can determine without real parameter values — inherent to being a planner-less mimic,
  not something a workaround can fully close. In practice this rarely matters: most drivers (including
  psycopg's default flow) `Describe` the *portal*, after `Bind`, which is always exact.

## Development

```bash
pip install -e ".[dev]"
pytest
```
