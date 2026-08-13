# Changelog

Notable changes to pg_mimic, newest first. Versions follow
[semantic versioning](https://semver.org/), with the caveat that while the major
version is 0 a minor bump is where breaking changes are allowed to land.

0.1.0–0.1.3 were backfilled from the git log after the fact, so they summarise
what shipped rather than what was written down at the time.

## Unreleased

### Added
- `information_schema.tables` and `information_schema.columns` are served at
  PostgreSQL's full width: 12 columns and 44, up from 4 and 7, in the server's own
  `ordinal_position` order so `SELECT *` lines up positionally. A column pg_mimic
  did not declare answered *empty* rather than erroring, so a client asking for
  `column_default` was told the table has no columns at all — every column a tool
  commonly reads (`column_default`, `character_maximum_length`, `numeric_precision`,
  `numeric_scale`, `datetime_precision`, `udt_name`, `is_identity`, `is_generated`)
  now answers about the columns that exist. What a declared `Session.schema()` can
  settle is derived; the rest is honestly NULL. The shape comes from
  `pg_mimic/information_schema.json`, generated from a live server by
  `tools/generate_information_schema.py`. (#99)

- `PgServer.run(host=..., port=...)` — a blocking entry point for when the server
  is the program. It owns the event loop, logs the listening address through the
  package logger, and returns on SIGINT or SIGTERM rather than raising. The signals
  are handled by the loop, so a connection parked mid-read no longer dies with an
  unretrieved `KeyboardInterrupt` that asyncio prints. The async entry points are unchanged for anyone
  embedding in an existing loop. All six examples use it. (#90)

- Server-to-client push, the first thing pg_mimic sends that a client didn't ask
  for: `Connection.notice()` emits a `NoticeResponse`, so a session can `WARNING`
  or `NOTICE` the way a real backend does — psycopg's `add_notice_handler` and
  asyncpg's `add_log_listener` see them. (#24)
- `LISTEN` / `UNLISTEN <channel>` / `UNLISTEN *` and `NOTIFY <channel>[, payload]`
  / `pg_notify()`, fanned out to every listening connection on the server, so
  event-driven code built on `asyncpg.add_listener` or psycopg's
  `conn.notifies()` can be tested against a fake. `NOTIFY` defers to transaction
  commit and a `ROLLBACK` drops it, as in Postgres — a rolled-back unit of work
  must not have announced itself. `PgServer.notify()` raises the same event from
  outside a session, and `Connection.notify_listeners()` from inside one. (#24)
- `Session.connection`: the documented accessor for the connection a session is
  serving, replacing the private `_connection` attribute as the way to reach
  `notice()`, `notify_listeners()` and the connection's `pid`. (#24)
- `TableSession`: serve a dict of in-memory tables over the wire with no session
  code at all, with column types inferred from the rows. (#30)
- `pg_mimic.testing` is public API: `serve()` and `serve_in_thread()` start a
  server for the duration of a test and hand back a DSN. The pytest plugin ships
  the `pg_mimic_server` / `pg_mimic_dsn` fixtures to any suite that installs
  pg-mimic, with no conftest plumbing. (#28)
- The `COPY` sub-protocol, text and CSV, in both directions —
  `Session.copy_in()` and `Session.copy_out()`. Binary `COPY` is refused with
  `feature_not_supported`, since the binary tuple layout is keyed to a real
  table's column types. (#31)
- `PREPARE` / `EXECUTE` / `DEALLOCATE` are answered by the middleware, against
  the same statement registry the extended query protocol uses. (#62)
- `examples/git_sql.py`: a session that answers SQL over a git repository. (#37)
- `pg_mimic.describe`: the machinery behind "column shape from a declared
  schema, without executing anything" is now a module of its own rather than
  something `TableSession` keeps to itself. `result_columns()` reads the columns
  off a type-annotated query, `size_integer_literals()` gives an integer constant
  the width Postgres gives it (run it *before* the annotator), and
  `oid_for_declared_type()` — exported from `pg_mimic` alongside its Python-type
  neighbour `oid_for_type()` — turns a `Session.schema()` type name (`"integer"`,
  `"text[]"`) into an OID. Any session that declares a schema can answer
  `describe()` with these instead of writing its own. (#88, #89)
- Packaging: the distributions carry a `LICENSE` (MIT) and a PEP 561 `py.typed`
  marker, so a consumer's type checker reads pg_mimic's annotations instead of
  `Any`, and the PyPI page has classifiers and links back to the repository.
  This file is new too. (#26)
- `pg_mimic.errors` is documented as public: its SQLSTATE constants are what you
  raise `PgError` with, and until now only `PgError` itself was exported. (#26)
- `PgServer(max_message_size=...)`, defaulting to 64MiB: a message length is the
  peer's word for how much to buffer, and until now the server acted on any of
  them — a single bogus `Int32` bought a read of up to 2GB. A length over the cap,
  or one that contradicts the header it belongs to (0, negative, or under the four
  bytes it counts for itself), is now a `FATAL` `08P01` and that connection is
  dropped. The startup packet keeps real Postgres's own much smaller ceiling of
  10000 bytes. (#27)
- The startup packet's protocol version is read rather than ignored. A client
  asking for a newer minor version (libpq 18, with `max_protocol_version=latest`,
  asks for 3.2) is answered with `NegotiateProtocolVersion` and connects as 3.0,
  which is also how any `_pq_.` protocol extension it requested is reported back
  — those no longer reach your session as settings. A major version pg_mimic
  doesn't speak is refused with `0A000` rather than misread. (#27)

### Fixed
- `examples/git_sql.py` refused every non-SELECT statement with one sentence —
  "this example serves SELECT only -- a git repo is read-only" — which was only
  true of half of them. `EXPLAIN` writes nothing, so being told the repo is
  read-only sent its author looking for a permission that was never the problem.
  A write is now `25006 read_only_sql_transaction`, Postgres's own code for it;
  anything else is `0A000` naming the statement, with `EXPLAIN` adding why (there
  is no planner behind the example, only sqlglot's executor). A set operation,
  which parses as a query whose first word is SELECT, says that it is the *shape*
  that is uncovered rather than claiming SELECT is unsupported. (#108)

- `examples/git_sql.py` refused to serve a linked worktree or a submodule: it
  looked for a `.git` *directory*, and in both of those `.git` is a file pointing
  at the real one. It asks `git rev-parse` now, so it serves anything git itself
  calls a repository — which is what every collector in the file already assumed.
  (#108)

- `PgServer.run(port=0)` logged `listening on 127.0.0.1:0` instead of the port it
  actually bound. Port 0 means "any free one", and the port the kernel picks is the
  one thing a client cannot guess — so the log line was useless in exactly the case
  it exists for. It now reports `self.port`. (#106)

### Changed
- **Breaking.** `SET` checks whether the parameter exists and whether a session may
  change it, where before it accepted any name at all. A name that is not a
  parameter is `42704 undefined_object`; a parameter outside a session's reach is
  `55P02 cant_change_runtime_param`, with the wording Postgres picks for the
  particular reason — `shared_buffers` "cannot be changed without restarting the
  server", `autovacuum_naptime` "cannot be changed now", `server_version` "cannot be
  changed", `post_auth_delay` "cannot be set after connection start" — because
  clients match on message text. `RESET` and `set_config()` are checked the same
  way; `set_config()` in particular names its parameter as a string rather than as
  syntax, and would otherwise be the way around this.

  199 of the 398 catalogued parameters are refused (`postmaster`, `sighup`,
  `internal`, `backend`, `superuser-backend`). The 48 `superuser` ones are accepted
  by decision rather than by the manual: pg_mimic reports `is_superuser = off`, but
  there is no privilege model behind that answer, and refusing them would break
  clients that set `log_*` for their own diagnostics against a server with no log.
  Everything psycopg, asyncpg, pg8000 and SQLAlchemy set on a connection is `user`
  context, and there is a test pinning that, because this is the one change in this
  area that can only take working behaviour away.

  The visible casualty is invented names: `SET mytenant = 'acme'` used to work and
  now raises, matching PostgreSQL 18.4, where a placeholder parameter needs a
  *qualified* name. Dotted custom GUCs are unaffected — `SET app.tenant = 'acme'`
  still reaches your session, which is what the row-level-security pattern rests
  on. (#77)

- All six examples take `--host`, `--port` and `--open-port`, from one parent parser
  in `examples/_args.py`. Four of them (`simple.py`, `tables.py`,
  `parameterized.py`, `dbapi_proxy.py`) previously called `run()` with no arguments
  and so could only ever listen on 5432 — unusable, without editing the file, on any
  machine that already has PostgreSQL there, which is most machines belonging to
  someone evaluating a Postgres mimic. `--open-port` takes any free port and logs
  which one, and is mutually exclusive with `--port` rather than quietly overriding
  it.

  `examples/git_sql.py` keeps its positional repo path and gains the shared flags;
  its port is now `--port N` rather than a second positional argument. `examples/`
  is not a package and is not shipped (`packages = ["pg_mimic"]`), so `_args.py` is
  plumbing for the examples rather than public API — an example finds it because a
  script's own directory is on `sys.path`. (#106)

- **Breaking.** An `information_schema` query naming a column pg_mimic does not
  model now raises `42703 undefined_column`, as Postgres does, instead of
  answering no rows. Empty was worse than an error for an ORM, which concludes
  the table has no such column rather than that pg_mimic cannot say. Both views
  are served at Postgres' full width, so a column reaching this is one Postgres
  does not have either. `pg_catalog` stays lenient on purpose — psql asks it for
  things a mimic has no business modelling, and raising there breaks `\d` and
  `\l`. (#66)
- **Breaking.** A `session_factory` that returns the *same* `Session` object for
  more than one live connection is now refused at connect time with a `FATAL`,
  rather than accepted and answered wrongly. A session holds per-connection state,
  so sharing one gave every connection the last one's identity and settings —
  connection A reporting connection B's `current_user`, and reading B's
  `search_path`. Returning the same object for the *next* connection is still fine;
  only an overlap is refused. (#84)

- `SET LOCAL` outside a transaction block now emits the `WARNING` real Postgres
  emits (`25P01`, "SET LOCAL can only be used in transaction blocks") instead of
  doing nothing silently, which is all it could do before there was a
  `NoticeResponse` to say it with. (#24)
- Settings are transactional: `SET` rolls back with its transaction and
  `SET LOCAL` reverts at commit, as in Postgres. `RESET` and `DISCARD` are
  handled, and changes to the settings a client tracks emit `ParameterStatus`.
  (#36, #64)
- A static statement produces its rows when it runs rather than when it is
  parsed, so a session that computes them sees the values at execution time.
  (#65)
- `pg_catalog` questions are answered from the rows the session already holds
  instead of falling back to an empty result, which had been silently swallowing
  13 catalog queries. (#68)
- An `information_schema` query the executor cannot run now fails rather than
  answering with zero rows — a wrong answer that looks like a right one is the
  worse failure. (#67)
- Requires `sqlglot>=30.17.0`. Several workarounds for executor bugs were
  deleted because that release fixes them. (#70)
- `TableSession` compares a decimal constant as `numeric`, which is how Postgres
  types one, instead of as the Python float sqlglot's executor read it as.
  `where total = 9.99` finds the `Decimal("9.99")` row it used to miss — and the
  miss was intermittent, since `= 10.00` matched, 10.0 being exactly
  representable in binary. `::numeric` and `cast(... as decimal)`, which raised
  `ValueError` and truncated to an integer respectively, are exact. (#33)
- **Breaking.** `TableSession` sizes an integer constant the way Postgres does:
  `integer` up to 2147483647, `bigint` to 9223372036854775807, `numeric` past
  that, and the width carries through arithmetic (`3000000000 + 0` is bigint).
  Every integer expression used to describe as `int4`, which crashed asyncpg's
  binary decoder outright — `select 3000000000` raised `'i' format requires
  -2147483648 <= number <= 2147483647` — and psycopg only hid it by reading
  results as text. Code reading the OID of a wide integer expression, or
  expecting an `int` from one past the bigint range (now a `Decimal`, as in
  Postgres), has to change. Declared columns are untouched: `columns={"n": INT4}`
  still describes `int4`. (#40)
- **Breaking.** `TableSession` folds identifiers the way Postgres does. A dict key
  is the identifier *as written*, so it is what a **quoted** reference matches,
  while an unquoted reference folds to lower case. Mixed-case keys change
  behaviour in both directions: against `{"userId": 1}`, `SELECT "userId"` now
  works where it used to raise, and `SELECT userId` now raises where it used to
  work. `SELECT *` is unaffected, as are all-lower-case keys. (#41, #42, #75)
- **Breaking.** A configuration parameter that was never set no longer reads as
  the empty string. `SHOW never.set` and `current_setting('never.set')` raise
  `42704`, while `current_setting('never.set', true)` is `NULL` — which is what
  `current_setting('app.tenant', true) IS NULL`, the usual row-level-security
  probe, is actually asking. Code relying on an unknown parameter reading as `''`
  has to change. (#32, #76)
- pg_mimic carries the ~400 parameters a real server is born knowing, generated
  from `pg_settings` by `tools/generate_pg_settings.py`, so `SHOW work_mem`
  answers `4MB` rather than erroring, `RESET` restores a parameter's default
  instead of blanking it, and `SHOW ALL` lists them. Values pg_mimic owns —
  encodings, `search_path`, `server_version` and `port` — still come from the
  connection. (#32, #76)

### Fixed
- Catalog queries describe their columns from the declared catalog schema rather
  than from the first row. A column whose first value was NULL described as `text`,
  so later values in it went out as strings — `character_octet_length` on
  `information_schema.columns` returned `'1073741824'` rather than `1073741824`.
  A session that yields bare rows still types from row one, which is the only place
  it can look. (#101)
- A `Session` that does not override `schema()` no longer crashes every catalog
  query with `'NoneType' object has no attribute 'items'`. The guard against the
  default `None` bound to the wrong branch of a conditional. (#101)

- A column declared as an array (`text[]`) or as `character` is catalogued as
  that type instead of falling back to `text`, so `pg_attribute.atttypid` points
  at the array type a client joins `pg_type` on, and `\d+` reports its storage
  as extended. (#43)
- `examples/git_sql.py` describes `select 3000000000` as `int8` and
  `select 9223372036854775808` as `numeric`, rather than telling a binary client
  `int4` and crashing its decoder (#40), and names an unaliased output column
  `?column?` as Postgres does rather than `_col_0`. It had its own copy of the
  derivation and never got those fixes; it goes through `pg_mimic.describe` now,
  so it cannot fall behind again. (#88)

### Internal

- `SessionState` is extracted and shared by the middleware and the session, so
  there is one place where per-connection state lives. (#61)
- Strict-xfail tripwires assert what real Postgres answers for each sqlglot bug
  pg_mimic works around. When sqlglot fixes one the test XPASSes, which fails the
  build and names the workaround to delete. A daily CI job runs the suite against
  the newest sqlglot and against sqlglot's main branch, so the news arrives
  without anyone touching the repo. (#51, #54, #56, #59, #69)

## 0.1.3 — 2026-08-11

### Added

- `pg_catalog` emulation covering the slice psql's `\d` family reads: `pg_class`,
  `pg_namespace`, `pg_attribute`, `pg_type` and `pg_am`, built from
  `Session.schema()`. Indexes, constraints, triggers, policies and defaults are
  declared but always empty, since pg_mimic models none of them. (#16)
- asyncpg's type introspection query is answered from the same `pg_type` rows.
  It's a recursive CTE that sqlglot can neither parse nor execute, so it is
  matched by shape — narrowly, and it fails loudly rather than quietly if asyncpg
  rewrites it. (#17)

### Internal

- `catalog.py` split three ways, and the result-to-`Statement` conversion shared
  rather than duplicated. (#18)

## 0.1.2 — 2026-08-11

### Added

- Postgres array support, text and binary, via `ARRAY_OID[...]`. (#4)
- Binary format for `numeric`, the time types and `interval`. (#6, #9)
- `set_config()` is handled, and an unknown setting reads as empty rather than
  erroring. (#5)

### Fixed

- `PgServer.close()` no longer hangs when connections are still live. (#10)

## 0.1.1 — 2026-08-11

### Changed

- **Breaking:** table-less `SELECT`s reach the session. The old
  static-SELECT interception did two unrelated jobs, and the second one answered
  queries a session may well have meant to answer itself — `select *` silently
  evaluated to `(1,)` without the session ever seeing it. The chain now keeps
  only what the connection alone can answer (`version()`, `current_user`,
  `current_setting()`, `pg_backend_pid()`); the evaluate-anything behaviour moved
  to `middleware.static_select`, which is no longer in `DEFAULT_MIDDLEWARE`. Add
  it back to `Session.middleware` for the previous behaviour.
- `Session.prepare()`'s interception is an explicit, overridable chain:
  `Session.middleware`, a tuple of async `(MiddlewareContext) -> Statement | None`.
  Extend it, reorder it, put your own link first to override a built-in, or set
  it to `()` to see every statement yourself.

### Fixed

- A binary result-format request in `Bind` was parsed and then dropped, so the
  server always sent text. `RowDescription` now declares the real per-column
  format and `DataRow` carries bytes. Types the binary encoders don't cover raise
  `feature_not_supported` rather than being encoded on a guess — a wrong byte
  order yields a plausible wrong *value*, which is the one failure text format
  cannot produce.
- `__version__` is read from installed distribution metadata instead of being a
  second hardcoded copy of the version, making `pyproject.toml` the only place to
  bump.

## 0.1.0 — 2026-08-11

First release. Simple and extended query protocols, the
`Session`/`Statement`/`Portal` extension points, trust/cleartext/MD5/SCRAM-SHA-256
authentication, and a middleware layer for transactions, `SET`/`SHOW`, static
`SELECT`s and `information_schema`.
