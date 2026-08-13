# Bulk data: `COPY`

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
