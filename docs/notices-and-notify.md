# Notices, and LISTEN/NOTIFY

These are the two things a Postgres server sends that the client didn't ask for, and both are reached
through `self.connection` — the documented accessor for the `Connection` a session is serving, available
from `init()` onwards.

A **notice** is Postgres's out-of-band `RAISE NOTICE`/`WARNING` channel: a message that rides along with a
query's response without failing it. Raised while answering, it attaches to that query, before its
`CommandComplete`, exactly as a real backend's does.

```python
class MySession(Session):
    async def query(self, sql, params):
        if "limit" not in sql.lower():
            self.connection.notice("unbounded query, this may be slow", severity="WARNING")
        ...
```

psycopg surfaces those through `conn.add_notice_handler(...)` and asyncpg through
`conn.add_log_listener(...)`, so code that depends on either can be tested against pg_mimic.

**LISTEN/NOTIFY** works between connections on the same server. `LISTEN <channel>`,
`UNLISTEN <channel>`, `UNLISTEN *` and `NOTIFY <channel>[, 'payload']` are handled by the middleware, as is
`pg_notify(channel, payload)`, and a `NOTIFY` reaches every connection on the server listening to that
channel:

```python
with serve_in_thread(MySession) as server:
    with psycopg.connect(server.dsn()) as listener, psycopg.connect(server.dsn()) as writer:
        listener.execute("LISTEN orders")
        writer.execute("NOTIFY orders, '42'")
        for note in listener.notifies(timeout=5, stop_after=1):
            assert (note.channel, note.payload) == ("orders", "42")
```

A session can raise the same event itself with `self.connection.notify_listeners(channel, payload)`, and
code embedding the server can raise one from outside any session with `PgServer.notify(channel, payload)`.
`notify()` writes to client transports, so from another thread it has to be hopped onto the server's loop:
`ServerThread.loop.call_soon_threadsafe(server.notify, "orders", "42")`.

Two behaviours are worth knowing, both matching PostgreSQL 18 rather than being convenient:

- **`NOTIFY` defers to transaction commit.** Inside a transaction it is delivered by `COMMIT` and dropped
  by `ROLLBACK` (and by a `ROLLBACK TO SAVEPOINT` that undoes it); in autocommit it goes out immediately.
  Delivering eagerly would mean a rolled-back unit of work still announced itself — a false positive in
  exactly the event-driven code this feature exists to test. Duplicates within one transaction collapse on
  the (channel, payload) pair, as they do there. `LISTEN` is transactional the same way, and does not start
  receiving until its transaction commits.
- **Async messages land only where the protocol allows.** A `NotificationResponse` is never written into
  the middle of a `DataRow` stream: one raised while a query is running is delivered after that query's
  `CommandComplete`, and one raised while a portal is suspended mid-drain waits for `Sync`. A client
  cannot report a violation of this — it would just mis-parse a row — so the rule is asserted on the raw
  wire in `tests/test_listen_notify.py`, against sequences read off a real PostgreSQL socket.
