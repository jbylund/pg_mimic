# The Session API

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

Three further parts of the session surface have pages of their own: [bulk data with `COPY`](./copy.md),
[reporting errors](./errors.md), and [notices and LISTEN/NOTIFY](./notices-and-notify.md).

## Declaring column types

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

## Declaring a schema

`schema()` returns a `Schema` of `Table`s. Each `Table` maps a column name to a SQL spelling
Postgres itself uses — `"integer"`, `"character varying"`, `"text[]"`:

```python
from pg_mimic import Schema, Session, Table


class MySession(Session):
    async def schema(self):
        return Schema(
            [
                Table("users", {"id": "integer", "name": "text"}),
                Table("orders", {"id": "integer", "user_id": "integer", "total": "numeric"}),
            ]
        )
```

The nested `{table: {column: type_name}}` dict this returned before is still accepted, so an
existing session needs no change. A `Schema` is what pg_mimic normalises either into, and it is what
`TableSession.schema()` hands back.

Two properties are worth knowing because nothing else will tell you: **the order of the tables and
of each table's columns is meaningful** — it decides table OIDs and `ordinal_position` — and a
`Table` is immutable, since one declared at module scope is shared by every connection.

`information_schema` and the `pg_catalog` slice psql's `\d` reads are built out of the declaration
(see [What's handled automatically](./whats-handled.md)), and so is `TableSession`'s answer to
every `Describe`. A table with no columns is legal, as it is in Postgres.

`oid_for_declared_type()` is how one of those names becomes an OID, and the reason a session that
declares a schema needs no type table of its own. Its neighbour `oid_for_type()` answers the same
question about a *Python* type, which is what `ResultColumn.for_type` reads:

```python
from pg_mimic import oid_for_declared_type, oid_for_type

oid_for_declared_type("integer")  # int4 — the SQL spelling a schema() declares
oid_for_declared_type("text[]")  # text[] — any number of `[]`, arrays carrying no dimensionality
oid_for_type(int)  # int8 — the Python type
```

`pg_mimic.describe` goes one step further and derives a whole query's column shape from the declared
schema, without running it. Three steps, and the order is the part worth remembering:

```python
from sqlglot.optimizer.annotate_types import annotate_types
from pg_mimic.describe import result_columns, size_integer_literals

size_integer_literals(qualified)  # before the annotator, or `select 3000000000` describes as int4
annotated = annotate_types(qualified, schema=my_schema, dialect="postgres")
columns = result_columns(annotated, param_oids)
```

`TableSession` and [`examples/git_sql.py`](../examples/git_sql.py) both answer `describe()` through
this, which is what keeps them agreeing with each other and with Postgres.

## Why two methods instead of one

Postgres's extended query protocol (`Parse`/`Bind`/`Describe`/`Execute`/`Sync`) genuinely separates "what
columns will this return" from "run it and give me rows" — a client can ask `Describe` before ever issuing
`Execute`. `describe()` answers that without touching any row source; `query()` is only ever invoked once
`Execute` actually runs, so side effects happen exactly when a real Postgres server would trigger them, not
earlier.

## Real parameterized queries and prepared statements

Both the simple query protocol (`psql`, one-shot queries) and the extended protocol (parameterized queries,
prepared statement reuse) are fully supported and drive the exact same `Statement`/`Portal` machinery
internally — a prepared statement `Parse`d once can be `Bind`+`Execute`d repeatedly with different
parameters, and large results stream incrementally (`Execute`'s row-limit / `PortalSuspended` semantics)
rather than being forced into memory all at once, as long as `query()` yields lazily.

## Power tier: full Statement/Portal control

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
