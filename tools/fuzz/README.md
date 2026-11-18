# Differential fuzzing against a real PostgreSQL

A bug-finding tool, not a test. It generates random `SELECT`s, runs each against a real
PostgreSQL and against sqlglot's executor, and reports where they disagree — minimised to the
smallest query that still disagrees.

Its *output* is what becomes a test: a finding here turns into a strict-xfail tripwire in
[`tests/test_sqlglot_workarounds.py`](../../tests/test_sqlglot_workarounds.py), which is what runs in
CI and tells us when upstream fixes it. Nothing here runs in CI — it needs a live server, and a
fuzzer that fails the build on a new random seed is a fuzzer people delete.

## Running it

```bash
createdb pg_mimic_fuzz
python -m tools.fuzz --count 4000
```

Roughly 150 queries a second, so a few thousand takes seconds to sweep; shrinking is what takes the
time. Exit status is 1 when anything was found.

| flag | |
|---|---|
| `--dsn` | the oracle. Default `postgres://localhost/pg_mimic_fuzz`. The dataset is dropped and recreated on every run |
| `--target` | `mimic` (default) goes through `TableSession`, so through every rewrite in `tables.py`. `raw` calls `sqlglot.executor.execute` directly |
| `--count`, `--seed` | how many queries, and reproducibly which ones |
| `--ordered` | add a total `ORDER BY` and compare row *order* and `LIMIT`/`OFFSET` too |
| `--only` | severities to report, e.g. `--only VALUE,COUNT` for wrong answers only |
| `--without` | features to switch off, e.g. `--without concat,string_functions` |
| `--json` | write the findings to a file as well |

## Reading the report

Findings are graded, worst first, because Postgres and the executor disagree constantly in ways that
are not bugs and a report that ranks them all equally is a report nobody reads.

| | |
|---|---|
| `COUNT` | different number of rows or columns. Always a bug |
| `VALUE` | genuinely different values. Always a bug |
| `REFUSED` | the executor could not run it. A missing function, or invalid generated Python |
| `ORDER` | same rows, different order. Only under `--ordered` |
| `EXACT` | numerically equal but one side lost exactness — a float where Postgres gave a `Decimal` |
| `TYPE` | equal and equally exact, different Python type. Mostly invisible to a client, since pg_mimic encodes from the *declared* type |

Each finding also says whether it reproduces in raw sqlglot, which is what says whose bug it is:

- **also fails raw sqlglot** — an upstream bug reaching our users.
- **raw sqlglot gets this right** — a workaround in `tables.py` is the thing that broke it. Ours.

## Digging past the top finding

One unimplemented function that the generator reaches often will account for most of the failures
and hide everything underneath it — `BTRIM` was 206 of 217 on the first real run. That is what
`--without` is for: switch off what you have already recorded and run again.

```bash
python -m tools.fuzz --count 4000 --without string_functions,concat,extract --only VALUE,COUNT
```

## What the first run found

Fourteen new upstream bugs, now tripwires in
[`tests/test_sqlglot_workarounds.py`](../../tests/test_sqlglot_workarounds.py) — read that file from
`test_distinct_keeps_columns_that_share_a_name` down. Six are wrong answers rather than refusals,
including three that a client cannot detect: `count(DISTINCT x)` ignoring the DISTINCT,
`SELECT DISTINCT a, a` answering one column instead of two, and an outer join's NULL-padded row
surviving a `WHERE` that tests the padded column.

Four are pg_mimic's own — the executor gets them right and a rewrite in `tables.py` breaks them, so
they are ours to fix and are **not** covered by any test yet:

```sql
-- _rewrite_not_in turns a correct answer into a wrong one. Postgres 7, raw sqlglot 7, TableSession 8.
SELECT count(*) FROM t WHERE NOT t.f IN (SELECT 1 FROM t)
-- ...and makes a working select-list NOT IN raise. Raw sqlglot answers true.
SELECT NOT 0 IN (SELECT uid FROM u) FROM u
-- The exact-decimal CAST rewrite creates a Decimal/float pair Python won't divide. Raw sqlglot answers.
SELECT 1 FROM u WHERE CAST(1 AS DECIMAL) / 0.5 < 1
```

One thing to know before reading a report: **text ordering is locale-dependent and dominates**. Every
`ORDER` finding on the first `--ordered` run was a text sort, because the executor compares strings
by code point and the oracle compared them by its `en_US.UTF-8` collation — a modelling gap, recorded
in [Known limitations](../../docs/limitations.md), not a bug anyone can fix. Create the oracle with
`createdb --locale=C --template=template0 pg_mimic_fuzz` to get it out of the way and see what else is
there.

## Checking a finding against upstream main

Do this before reporting anything: a bug already fixed on main and waiting for a release is not a
finding, and two of the tripwires in this repo are in exactly that state.

Usually you do not have to. [`.github/workflows/upstream-sqlglot.yml`](../../.github/workflows/upstream-sqlglot.yml)
already runs the whole suite against sqlglot's main branch daily — including how to install it, which
has a trap in it — so an `XPASS(strict)` there is the answer for anything with a tripwire. Read that
job's last run first.

Reach for a local main build only for a finding that has no tripwire yet, such as one you are still
minimising. `--target raw` needs nothing but sqlglot and psycopg, and the test file needs nothing but
sqlglot and pytest, so add `--noconftest -o addopts=` to skip this repo's conftest and its dev
dependencies:

```bash
/tmp/mainvenv/bin/python -m pytest tests/test_sqlglot_workarounds.py --noconftest -o addopts= -rX
/tmp/mainvenv/bin/python -m tools.fuzz --target raw --count 2000
```

## How it works

Four pieces, each answering one of the four questions a differential fuzzer has to get right.

[`dataset.py`](dataset.py) — *what to query.* Two small tables where every row carries an edge case:
NULLs, duplicates, ties, zero, exact halves for rounding, `'a%b'` for LIKE, and foreign keys that
match nothing, match twice, or are NULL.

[`generate.py`](generate.py) — *what to ask.* A typed generator, because the hard part of fuzzing SQL
is validity rather than randomness. It knows which of its productions Postgres rejects at runtime
(division by zero, `mod()` on a double, a `FULL JOIN` on a non-hashable condition) and avoids them;
about 1% of what it emits is still invalid, and those samples are discarded.

[`compare.py`](compare.py) — *whether the answers match.* The graded comparison above, with a
relative tolerance on floats, since both engines do the same arithmetic in a different order and are
allowed to land a few ULPs apart.

[`shrink.py`](shrink.py) — *what to report.* Without this the tool is useless: hundreds of failures
that are a dozen bugs, each buried under forty lines of generated arithmetic. The reduction moves are
deliberately reckless — hoist any node to its own child, replace any node with a literal, drop any
clause — because **Postgres is the validity filter**: a variant it refuses is rejected exactly like a
variant that stops reproducing. A variant is kept when it is smaller and fails *the same way*, which
is what stops the shrinker sliding off a subtle wrong-answer bug onto some unrelated missing
function. Minimised queries then de-duplicate themselves, since two seeds that found the same bug
minimise to the same text.
