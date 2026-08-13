# Serving in-memory tables

`TableSession` is the one path through pg_mimic that needs no session code at all. The
[README](../README.md#serving-in-memory-tables) shows the whole of it in two lines; what follows is
what runs underneath, and where its edges are.

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

## Where the column types come from

pg_mimic treats column shape as a **declared fact**, not something read off result rows (see
[The Session API](./session-api.md)). `TableSession` keeps that: it inspects the tables' Python values
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

## Read-only, on purpose

`INSERT`/`UPDATE`/`DELETE` are refused with `read_only_sql_transaction` (25006) rather than applied. The
tables are your own objects, and mutating them per connection — with no isolation between clients and no
way to roll back — would be a worse lie than a clear error. And anything sqlglot's executor can't run
(recursive CTEs, most of Postgres's function library) is an error too, never an approximate answer: see
[Known limitations](./limitations.md).
