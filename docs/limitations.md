# Known limitations

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
  don't, and function coverage is partial. Integer arithmetic does not overflow where Postgres would raise:
  `2147483647 + 1` answers 2147483648 rather than failing with `integer out of range`. That is the
  executor's arithmetic, not pg_mimic's parameter handling — a literal in the SQL and a bind parameter
  behave identically.

  The executor's more dangerous gaps are the clauses it parses and then answers *wrongly* — a wrong answer
  wearing a right answer's clothes. `TableSession` repairs the ones it can, verified against a real
  PostgreSQL: `OFFSET` and `DISTINCT ON` are applied to the rows the executor returns (with `LIMIT`
  counting what survives, as Postgres does); `NOT IN (subquery)`, which the executor never filters on,
  becomes a `NOT EXISTS` carrying SQL's NULL rule; `ORDER BY` places NULLs where Postgres places them
  instead of raising on the comparison; and a `UNION`, `EXCEPT` or `INTERSECT` over the same table runs
  both of its branches rather than the first one twice.

  Numbers are the same story. A decimal constant is compared as `numeric`, the way Postgres types one, so
  `where total = 9.99` finds the `Decimal("9.99")` row instead of missing it because the executor read the
  literal as a float — and `::numeric`, which used to raise, is exact. An integer constant is as wide as
  Postgres makes it, so `select 3000000000` describes as `int8` rather than telling a binary client `int4`
  and crashing its decoder.

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
