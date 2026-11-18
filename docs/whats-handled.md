# What's handled automatically

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

  A `SET` is checked against the parameter list rather than accepted on sight, so a name that isn't a
  parameter is `42704` and a parameter a session may not change is `55P02` — with the wording
  Postgres picks for *why*, which differs by parameter: `SET shared_buffers` "cannot be changed
  without restarting the server", `SET autovacuum_naptime` "cannot be changed now". 199 of the 398
  are refused (`postmaster`, `sighup`, `internal`, `backend`, `superuser-backend`), and the 199
  accepted cover everything a client library sets on a connection. The 48 `superuser` parameters are
  accepted by decision rather than by the manual: pg_mimic reports `is_superuser = off`, but there is
  no privilege model behind that answer, and refusing them would break clients that set `log_*` for
  their own diagnostics against a server that keeps no log. `set_config()` is checked the same way —
  it names its parameter as a string rather than as syntax, and would otherwise be the way around
  this.
- Session functions: `SELECT version()`, `current_user`, `current_database()`, `current_setting('x')`,
  `set_config()`, `pg_backend_pid()` — things only the connection can answer.

  A setting nothing has ever set does not exist, as in real Postgres: `SHOW never.set` and
  `current_setting('never.set')` raise `42704`, while `current_setting('never.set', true)` is `NULL`
  — which is what `current_setting('app.tenant', true) IS NULL`, the usual row-level-security probe
  for "was this ever set?", is actually asking. A parameter with a built-in default reads that
  default back after a `RESET`, so `SET work_mem = '8MB'; RESET work_mem` is `4MB`, as there. A name
  without one stays known once named and reads blank through `RESET`, `DISCARD ALL` and a rolled-back
  transaction, also as there.

  A *dotted* name is the exception worth knowing about, and since parameters are checked against the
  list it is now the only name that can be introduced at all. `SET app.tenant = 'acme'` is passed
  through to your session rather than answered here, so pg_mimic never sees the write and
  `current_setting('app.tenant', true)` stays `NULL` afterwards. A session that wants the probe to
  answer has to record the value itself until `Session.set_parameter()` lands.
  `set_config('app.tenant', 'acme', false)` is the one route that does register the name here, since
  it passes it as a string rather than as syntax.
- `LISTEN`/`UNLISTEN`/`NOTIFY` and `pg_notify()`, fanned out across the server's connections, with
  `NOTIFY` deferred to transaction commit the way Postgres defers it — see
  [Notices, and LISTEN/NOTIFY](./notices-and-notify.md).
- asyncpg's type introspection, so it can build codecs for array and other non-builtin types.
- `information_schema.tables` and `information_schema.columns` at PostgreSQL's full width — all 12
  columns and all 44, in the server's own order, so `SELECT *` lines up positionally and a query for
  `column_default`, `udt_name` or `numeric_precision` answers about the columns that exist instead of
  returning nothing. Plus the slice of `pg_catalog` psql's `\dt`, `\d <table>`, `\di` and `\dn` read —
  all built from your `Session.schema()` declaration — see
  [the session API](./session-api.md#declaring-a-schema). A declared primary key or unique
  constraint gets the `Indexes:` footer `\d` prints for one, and appears in `pg_index`,
  `pg_constraint` and `\di`. A primary key additionally reports its columns as `not null`.
- Multi-statement simple-query batches (`"BEGIN; INSERT ...; COMMIT;"` sent as one `'Q'` message, e.g. by
  `psql -f script.sql`) are split into individual statements (via sqlglot), each getting its own
  `RowDescription`/`DataRow*`/`CommandComplete` — not silently merged or truncated.
- The `COPY` sub-protocol and its text/CSV formats, so `copy_in()`/`copy_out()` see decoded rows — see
  [Bulk data: `COPY`](./copy.md).

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

The chain lives in [`pg_mimic/middleware.py`](../pg_mimic/middleware.py); the `information_schema`
emulation it delegates to is in [`pg_mimic/catalog.py`](../pg_mimic/catalog.py).
