# Changelog

Notable changes to pg_mimic, newest first. Versions follow
[semantic versioning](https://semver.org/), with the caveat that while the major
version is 0 a minor bump is where breaking changes are allowed to land.

0.1.0–0.1.3 were backfilled from the git log after the fact, so they summarise
what shipped rather than what was written down at the time.

## Unreleased

### Added

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
- Packaging: the distributions carry a `LICENSE` (MIT) and a PEP 561 `py.typed`
  marker, so a consumer's type checker reads pg_mimic's annotations instead of
  `Any`, and the PyPI page has classifiers and links back to the repository.
  This file is new too. (#26)
- `pg_mimic.errors` is documented as public: its SQLSTATE constants are what you
  raise `PgError` with, and until now only `PgError` itself was exported. (#26)

### Changed

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

- A column declared as an array (`text[]`) or as `character` is catalogued as
  that type instead of falling back to `text`, so `pg_attribute.atttypid` points
  at the array type a client joins `pg_type` on, and `\d+` reports its storage
  as extended. (#43)

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
