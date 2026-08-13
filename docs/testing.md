# Using it in tests

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
`server_version`, `max_message_size`) and yield the running server, so `server.port` and
`server.dsn(user=..., dbname=...)` give the client its connection details. On the way out the server
is closed and any connection the test left open is dropped rather than waited for — a test that
forgets to close its connection fails on its own assertions, it doesn't hang.

## pytest fixtures

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
expressions; see [What's handled automatically](./whats-handled.md)). The exception is
[`TableSession`](./table-session.md), which does execute the query — against your rows, with
sqlglot's executor, as far as that executor goes. If a test's correctness depends on Postgres actually
executing the query, that test wants a real Postgres.

`pg_mimic.testing` imports without pytest installed — the fixtures live in a separate module that
pytest loads itself — and it is not re-exported from the `pg_mimic` namespace, so nothing that merely
runs a server pays to import it. Ask for it by name: `from pg_mimic.testing import serve_in_thread`.
