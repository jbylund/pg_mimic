# pg_mimic

A pure-Python, asyncio PostgreSQL server protocol emulator — the Postgres analog of
[mysql-mimic](https://github.com/barakalon/mysql-mimic). Embed it in your own process, subclass `Session`
to answer queries with arbitrary Python logic, and get a fully wire-compatible server that real Postgres
clients, drivers, ORMs, and tools (`psql`, `psycopg`, `asyncpg`, JDBC, ...) can connect to.

Useful for testing, proxies/virtual databases, query interception, and fronting non-Postgres backends with
a Postgres-speaking API.

## Install

```bash
pip install pg-mimic
```

## Quick start

```python
from pg_mimic import PgServer, ResultColumn, Session


class MySession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("a", str), ResultColumn.for_type("b", int)]

    async def query(self, sql, params):
        yield ("hello", 1)
        yield ("world", 2)


PgServer(session_factory=MySession).run(port=5432)
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

Every example takes `--host`, `--port` and `--open-port`. Use `--open-port` if you already have a real
PostgreSQL on 5432: it listens on any free port and logs which one.

```console
$ python examples/tables.py --open-port
serving 4 tables read-only
pg_mimic listening on 127.0.0.1:58983
```

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

How much SQL that actually runs, where the column types come from, and why writes are refused are in
[Serving in-memory tables](https://github.com/jbylund/pg_mimic/blob/main/docs/table-session.md).

## What you get for free

Real clients ask a server a great deal that has nothing to do with your data. A middleware chain
handles that on your session's behalf — transactions and savepoints, `SET`/`RESET`/`SHOW`, session
functions like `version()` and `current_setting()`, `LISTEN`/`NOTIFY`, `information_schema` and the
slice of `pg_catalog` that psql's `\d` reads, multi-statement batches, and the `COPY` sub-protocol —
and passes everything else through to you. It is an ordinary class attribute, so none of that is
fixed: [What's handled automatically](https://github.com/jbylund/pg_mimic/blob/main/docs/whats-handled.md)
has the full list and the rules for writing your own.

## Documentation

- [The Session API](https://github.com/jbylund/pg_mimic/blob/main/docs/session-api.md) —
  `describe()`/`query()`, declaring column types and a schema, prepared statements, and the `prepare()`
  power tier.
- [Serving in-memory tables](https://github.com/jbylund/pg_mimic/blob/main/docs/table-session.md) —
  `TableSession` in full: how much SQL it executes, where its column types come from, and why it is
  read-only.
- [Bulk data: `COPY`](https://github.com/jbylund/pg_mimic/blob/main/docs/copy.md) —
  `copy_in()`/`copy_out()`, the text and CSV formats, and the options refused by name.
- [Reporting errors](https://github.com/jbylund/pg_mimic/blob/main/docs/errors.md) — `PgError`,
  SQLSTATEs, and what reaches the client when a session raises anything else.
- [Notices, and LISTEN/NOTIFY](https://github.com/jbylund/pg_mimic/blob/main/docs/notices-and-notify.md)
  — the two things a server sends that the client didn't ask for.
- [Using it in tests](https://github.com/jbylund/pg_mimic/blob/main/docs/testing.md) —
  `serve_in_thread()`, `serve()`, the pytest fixtures, and what this does and doesn't test.
- [Server options](https://github.com/jbylund/pg_mimic/blob/main/docs/server-options.md) —
  authentication mechanisms, message size limits, and protocol version negotiation.
- [What's handled automatically](https://github.com/jbylund/pg_mimic/blob/main/docs/whats-handled.md) —
  the middleware chain in full, and how to extend or replace it.
- [Known limitations](https://github.com/jbylund/pg_mimic/blob/main/docs/limitations.md) — binary
  format coverage, how far `TableSession`'s executor goes, TLS, and `pg_catalog`.

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
