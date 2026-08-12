"""SQL over a git repository, served on the Postgres wire protocol.

    python examples/git_sql.py [repo-path] [port]   # defaults to the cwd, port 5432
    psql "host=127.0.0.1 port=5432 user=me dbname=git"

Needs git 2.37+ (mid-2022) for --since-as-filter; see THE RULE below for why
plain --since is not an option.

Four tables -- commits, commit_files, files, branches -- collected by shelling
out to git, then queried with sqlglot's executor, which pg_mimic already
depends on for its own catalog emulation. Nothing here implements SQL: the
collectors return list[dict] and the executor does the rest, which is why
GROUP BY, HAVING, CTEs, subqueries and self-joins all work without appearing
anywhere below.

    -- which files keep changing together
    SELECT a.path, b.path, count(*) AS n
    FROM commit_files a JOIN commit_files b ON a.sha = b.sha AND a.path < b.path
    GROUP BY 1, 2 ORDER BY n DESC LIMIT 20;

Two parts are worth reading for their own sake.

describe() never runs the query. Column shape comes from sqlglot's type
annotator against SCHEMA and is exact even through aggregates and arithmetic
-- count(*) is int8, insertions / 2.0 is float8 -- so Parse/Describe is
answered without touching git. examples/dbapi_proxy.py has to execute a SELECT
to learn its own column types; a declared schema buys the difference.

WHERE conjuncts are pushed into git's own filters, so `WHERE author_name = 'x'`
becomes `git log --author=x` instead of reading every commit and discarding
most of them. THE RULE above pushdown() is what keeps that safe.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import sqlglot
import sqlglot.executor.env as executor_env
from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.qualify import qualify

from pg_mimic import (
    BOOL,
    FLOAT8,
    INT2,
    INT4,
    INT8,
    NUMERIC,
    TEXT,
    TIMESTAMP,
    PgServer,
    ResultColumn,
    Session,
)
from pg_mimic.errors import FEATURE_NOT_SUPPORTED, PgError

# --- patterns --------------------------------------------------------------------

# git reads its pattern flags as POSIX basic regular expressions, in which `|` is a
# literal rather than alternation -- so a pattern meaning one thing to the executor can
# mean something stricter to git, which silently drops rows. _log_records pins the
# dialect with -E (extended), and everything handed to git is escaped for it here.
_ERE_METACHARACTERS = re.compile(r"([.\[\]{}()*+?^$|\\])")


def _ere_escape(text: str) -> str:
    return _ERE_METACHARACTERS.sub(r"\\\1", text)


def _like_to_regex(pattern: str) -> str:
    """SQL LIKE to a regex, with everything that isn't a wildcard taken literally."""
    return "".join({"%": ".*", "_": "."}.get(char) or _ere_escape(char) for char in pattern)


# --- the executor's function table ---------------------------------------------------

# sqlglot.executor.env.ENV is a plain dict of the functions the executor can call.
# Editing it is process-global (pg_mimic's catalog_rewrite.py already does, for
# REGEXPLIKE), and the three edits below are not the same kind of change.
#
# Filling holes: an unfilled name raises, which _execute reports as a Postgres error.
# date_trunc is deliberately absent -- the executor passes its unit as an env *function*
# rather than a string. EXTRACT(year FROM committed_at) works natively.
executor_env.ENV.setdefault("LENGTH", len)
executor_env.ENV.setdefault("DPIPE", lambda a, b: f"{a}{b}")

# Correcting a wrong answer: the shipped LIKE is `re.match(pattern.replace("_", ".")
# .replace("%", ".*"), value)`, which neither escapes regex metacharacters (so
# `LIKE '%(#1_)%'` matches text with no parenthesis at all, `(...)` having become a
# capture group) nor anchors the end (`LIKE 'Bump'` matches 'Bump version').
executor_env.ENV["LIKE"] = lambda value, pattern: re.fullmatch(_like_to_regex(pattern), value) is not None
executor_env.ENV["ILIKE"] = lambda value, pattern: re.fullmatch(_like_to_regex(pattern), value, re.IGNORECASE) is not None


def _interval_delta(count: str, unit: str) -> timedelta:
    """`interval '90 days'` as a timedelta -- a deliberate *semantic* departure, unlike
    the two patches above, and so the one to weigh before importing this module into a
    larger process. The shipped INTERVAL does `timedelta(**{unit: n})`, so
    `interval '1 year'` raises: timedelta has no years or months. Approximating them
    turns that error into an answer wrong by up to five days, for every session in the
    process (Postgres is calendar-aware -- a month after Jan 31 is Feb 28).

    Both the executor and _constant_datetime's pushdown go through here, so the rows git
    returns and the rows the WHERE keeps cannot disagree about what a year is.
    """
    unit = unit.lower().rstrip("s")
    approximate_days = {"month": 30, "quarter": 91, "year": 365}
    if unit in approximate_days:
        return timedelta(days=float(count) * approximate_days[unit])
    return timedelta(**{f"{unit}s": float(count)})  # raises for units timedelta lacks


executor_env.ENV["INTERVAL"] = _interval_delta


# --- the schema --------------------------------------------------------------------

# One declaration, three jobs: pg_mimic builds information_schema and the pg_catalog
# slice psql's \d reads from it, sqlglot resolves and type-annotates against it, and
# the executor uses it to plan. Timestamps are naive UTC on purpose -- sqlglot's now()
# is naive, and comparing it to an aware datetime raises.
SCHEMA = {
    "commits": {
        "sha": "text",
        "author_name": "text",
        "author_email": "text",
        "committer_name": "text",
        "committed_at": "timestamp",
        "authored_at": "timestamp",
        "subject": "text",
        "insertions": "integer",
        "deletions": "integer",
        "files_changed": "integer",
    },
    "commit_files": {
        "sha": "text",
        "path": "text",
        "insertions": "integer",
        "deletions": "integer",
    },
    "files": {
        "path": "text",
        "ext": "text",
        "size_bytes": "integer",
        "lines": "integer",
    },
    "branches": {
        "name": "text",
        "is_head": "boolean",
        "upstream": "text",
        "last_commit_at": "timestamp",
    },
}

# sqlglot's annotated types -> Postgres OIDs, for describing the columns a query
# projects. SCHEMA's own declared type names route through the same map via
# _declared_oid, so the two readings of SCHEMA cannot drift apart.
_TYPE_OIDS = {
    exp.DataType.Type.TEXT: TEXT,
    exp.DataType.Type.VARCHAR: TEXT,
    exp.DataType.Type.CHAR: TEXT,
    exp.DataType.Type.SMALLINT: INT2,
    exp.DataType.Type.INT: INT4,
    exp.DataType.Type.BIGINT: INT8,
    exp.DataType.Type.BOOLEAN: BOOL,
    exp.DataType.Type.TIMESTAMP: TIMESTAMP,
    exp.DataType.Type.DOUBLE: FLOAT8,
    exp.DataType.Type.FLOAT: FLOAT8,
    exp.DataType.Type.DECIMAL: NUMERIC,
}


def _declared_oid(name: str | None) -> int | None:
    """A SCHEMA type name ("integer") -> OID, through the map describe() already uses."""
    return _TYPE_OIDS.get(exp.DataType.build(name).this) if name else None


# --- pushdown ------------------------------------------------------------------------

# THE RULE: a pushed predicate is a *hint*, never the filter. Every conjunct handled
# here also stays in the WHERE clause the executor evaluates, so git only narrows the
# candidate set and sqlglot alone decides the answer.
#
# That makes over-approximation free, which matters because git's filters are looser
# than they look: --author is an unanchored regex matched against the whole
# "Name <email>" identity string, so --author=joe also matches joe@example.com. Nothing
# here is anchored for that reason -- too permissive costs a few rows the executor then
# discards, too strict drops rows that qualify. Anything not understood below is simply
# not pushed: still correct, only slower.
#
# The rule guards against pushing too *little*. It does not guard against pushing
# something git reads more narrowly than the executor does, and two cases do exactly
# that -- both found by a differential test against the same queries with pushdown
# disabled, neither reasoned away:
#
#   - git reads patterns as POSIX *basic* regular expressions, where `|` is a literal.
#     _ere_escape and the -E in parse_log pin the dialect.
#   - plain --since is unsound (see _time_args). It stops traversal at the first commit
#     older than the cutoff, and commit dates are not monotonic along history (clock
#     skew, rebases, merges of long-lived branches), so qualifying commits go
#     uncollected -- and a re-applied WHERE cannot recover a row never produced.
#     --since-as-filter (git 2.37+) filters without the cutoff. --until is fine:
#     traversal runs newest to oldest, so it only skips before it starts yielding.

_IDENTITY_FLAGS = {
    "author_name": "--author",
    "author_email": "--author",
    "committer_name": "--committer",
    "subject": "--grep",
}


def _literal(node: exp.Expression) -> str | None:
    return node.this if isinstance(node, exp.Literal) and node.is_string else None


def _constant_datetime(node: exp.Expression) -> datetime | None:
    """Evaluate the RHS of a timestamp comparison, if it is knowable without rows.

    Covers the two shapes that actually show up: a date literal, and
    `now() - interval '90 days'` (which sqlglot parses as CURRENT_TIMESTAMP minus an
    Interval). Anything else declines, and the predicate just isn't pushed.
    """
    if isinstance(node, exp.CurrentTimestamp):
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if isinstance(node, exp.Cast):
        return _constant_datetime(node.this)
    if text := _literal(node):
        try:
            return datetime.fromisoformat(text).replace(tzinfo=None)
        except ValueError:
            return None
    if isinstance(node, exp.Sub) and isinstance(node.expression, exp.Interval):
        base = _constant_datetime(node.this)
        if base is None:
            return None
        interval = node.expression
        try:
            return base - _interval_delta(interval.this.this, interval.text("unit"))
        except (AttributeError, TypeError, ValueError):
            return None
    return None


def _conjuncts(expr: exp.Expression) -> list[exp.Expression]:
    where = expr.find(exp.Where)
    if where is None:
        return []
    return list(where.this.flatten()) if isinstance(where.this, exp.And) else [where.this]


def _owned_by(node: exp.Expression, alias: str) -> bool:
    """True if every column in this conjunct belongs to `alias`.

    This is what makes pushdown safe across joins. qualify() has already stamped a
    table alias onto every column, so a predicate mentioning another table -- or two
    tables at once -- is left alone. Narrowing one side of a join by the other side's
    predicate would drop rows the query still needs, and that is the one kind of
    mistake the re-applied WHERE cannot undo.
    """
    columns = list(node.find_all(exp.Column))
    return bool(columns) and all(column.table == alias for column in columns)


def _pushable_pattern(node: exp.Expression) -> str | None:
    """This comparison's right-hand side as an ERE for git, or None if not pushable.

    Every pattern git sees is built here, so the escaping rule lives in one place. A
    user-supplied regex (`subject ~ '...'`) deliberately gets None: Postgres, Python's
    re and git's -E agree on the common constructs but not on all of them, and a pattern
    git reads more strictly than the executor silently loses rows.
    """
    text = _literal(node.expression)
    if text is None:
        return None
    if isinstance(node, exp.EQ):
        return _ere_escape(text)
    return _like_to_regex(text) if isinstance(node, exp.Like) else None


def _identity_args(node: exp.Expression, flag: str) -> list[str]:
    pattern = _pushable_pattern(node)
    if pattern is not None:
        return [f"{flag}={pattern}"]
    if isinstance(node, exp.In):
        # git ORs repeated --author/--committer/--grep flags, which is exactly what IN
        # means. A single unparseable member would make the OR too narrow, so bail on
        # the whole predicate rather than push a partial list.
        values = [_literal(item) for item in node.expressions]
        if values and all(value is not None for value in values):
            return [f"{flag}={_ere_escape(value)}" for value in values]
    return []


def _time_args(node: exp.Expression) -> list[str]:
    when = _constant_datetime(node.expression)
    if when is None:
        return []
    if isinstance(node, (exp.GT, exp.GTE)):
        return [f"--since-as-filter={when.isoformat()}"]  # never plain --since; see above
    if isinstance(node, (exp.LT, exp.LTE)):
        return [f"--until={when.isoformat()}"]
    return []


def _path_args(node: exp.Expression) -> list[str]:
    text = _literal(node.expression)
    if text is None:
        return []
    if isinstance(node, exp.EQ):
        return [f":(literal){text}"]
    # A trailing-% LIKE is the only shape with a clean pathspec equivalent; the rest stay
    # unpushed rather than being approximated into a wrong glob.
    if isinstance(node, exp.Like) and text.endswith("%") and "%" not in text[:-1] and "_" not in text:
        return [f":(glob){text[:-1]}**"]
    return []


_COMPARISONS = (exp.EQ, exp.GT, exp.GTE, exp.LT, exp.LTE, exp.Like, exp.RegexpLike, exp.In)


def pushdown(expr: exp.Expression, table: str, alias: str) -> list[str]:
    """The conjuncts belonging to `alias`, as extra `git log` arguments."""
    args: list[str] = []
    paths: list[str] = []
    for node in _conjuncts(expr):
        # The left side must be the bare column itself, not merely contain one. A
        # conjunct like `upper(author_name) = 'JOE'` mentions author_name but does not
        # constrain it, and pushing --author=JOE for it drops every matching commit --
        # the exact unsoundness the re-applied WHERE cannot repair.
        if not isinstance(node, _COMPARISONS) or not isinstance(node.this, exp.Column):
            continue
        if not _owned_by(node, alias):
            continue
        column = node.this.name
        if column in _IDENTITY_FLAGS:
            args += _identity_args(node, _IDENTITY_FLAGS[column])
        elif column in ("committed_at", "authored_at"):
            args += _time_args(node)
        elif column == "path" and table == "commit_files":
            paths += _path_args(node)
    return args + (["--", *paths] if paths else [])


# --- collectors ----------------------------------------------------------------------

_RECORD, _FIELD = "\x1e", "\x1f"
_LOG_FORMAT = _RECORD + _FIELD.join(["%H", "%an", "%ae", "%cn", "%cI", "%aI", "%s"])


def _git(repo: str, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        raise PgError(FEATURE_NOT_SUPPORTED, f"git failed: {result.stderr.strip()[:200]}")
    return result.stdout


def _naive_utc(stamp: str) -> datetime | None:
    try:
        return datetime.fromisoformat(stamp).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return None


def parse_log(repo: str, filters: list[str]) -> list[tuple[list[str], list[tuple]]]:
    """One `git log --numstat` run, parsed into (header fields, numstat rows).

    commits and commit_files are two projections of this single result, so a query
    joining them spawns git once rather than twice -- on a 30k-commit repo that is the
    difference between 20 seconds and 40. GitSession caches this per filter set.
    """
    args = ["log", "-E", "--numstat", "--no-renames", f"--pretty=format:{_LOG_FORMAT}", *filters]
    records = []
    for record in _git(repo, *args).split(_RECORD):
        header, _, body = record.strip("\n").partition("\n")
        fields = header.split(_FIELD)
        if len(fields) == 7:
            records.append((fields, list(_numstat(body))))
    return records


def _numstat(body: str):
    for line in body.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            # Binary files report "-" for both counts; there is no line count to give.
            added, removed, path = parts
            yield path, (None if added == "-" else int(added)), (None if removed == "-" else int(removed))


def commit_rows(records) -> list[dict]:
    rows = []
    for fields, stats in records:
        sha, author_name, author_email, committer_name, committed, authored, subject = fields
        rows.append(
            {
                "sha": sha,
                "author_name": author_name,
                "author_email": author_email,
                "committer_name": committer_name,
                "committed_at": _naive_utc(committed),
                "authored_at": _naive_utc(authored),
                "subject": subject,
                "insertions": sum(added or 0 for _, added, _ in stats),
                "deletions": sum(removed or 0 for _, _, removed in stats),
                "files_changed": len(stats),
            }
        )
    return rows


def commit_file_rows(records) -> list[dict]:
    return [
        {"sha": fields[0], "path": path, "insertions": added, "deletions": removed}
        for fields, stats in records
        for path, added, removed in stats
    ]


def collect_files(repo: str) -> list[dict]:
    rows = []
    for path in _git(repo, "ls-files", "-z").split("\0"):
        if not path:
            continue
        full = os.path.join(repo, path)
        try:
            size = os.path.getsize(full)
        except OSError:
            continue
        rows.append(
            {
                "path": path,
                "ext": os.path.splitext(path)[1].lstrip(".") or None,
                "size_bytes": size,
                "lines": _count_lines(full, size),
            }
        )
    return rows


def _count_lines(full: str, size: int) -> int | None:
    """None rather than a number for binary files and anything large enough that
    reading it would make an interactive query feel broken."""
    if size > 2_000_000:
        return None
    try:
        with open(full, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    return None if b"\0" in data[:8192] else data.count(b"\n")


def collect_branches(repo: str) -> list[dict]:
    fmt = _FIELD.join(["%(refname:short)", "%(HEAD)", "%(upstream:short)", "%(committerdate:iso-strict)"])
    rows = []
    for line in _git(repo, "for-each-ref", f"--format={fmt}", "refs/heads").splitlines():
        name, head, upstream, committed = line.split(_FIELD)
        rows.append(
            {
                "name": name,
                "is_head": head == "*",
                "upstream": upstream or None,
                "last_commit_at": _naive_utc(committed),
            }
        )
    return rows


# Two projections of one `git log` -- the only tables pushdown applies to, since git log
# is the only source with filters to push into.
LOG_PROJECTIONS = {"commits": commit_rows, "commit_files": commit_file_rows}

# Their own sources, cheap and unfiltered; a predicate against these is simply evaluated
# by the executor.
COLLECTORS = {"files": collect_files, "branches": collect_branches}

TABLE_NAMES = (*LOG_PROJECTIONS, *COLLECTORS)


# --- the session ---------------------------------------------------------------------


class GitSession(Session):
    def __init__(self, repo: str):
        self.repo = repo
        # Both caches live for the connection: psql and BI tools re-introspect
        # constantly, and `git log --numstat` over a large repo is measured in seconds.
        # A long-lived connection therefore sees a snapshot, not live history -- and an
        # unbounded one, which a real implementation would cap.
        self._records: dict[tuple, list] = {}  # filters -> parsed git log
        self._rows: dict[tuple, list[dict]] = {}  # (table, filters) -> projected rows

    async def schema(self) -> dict:
        return SCHEMA

    async def prepare(self, sql, param_oids):
        """Fill in parameter types the client left for the server to work out.

        psql sends literals, but asyncpg and psycopg send Parse with no parameter types
        and expect ParameterDescription to tell them what `$1` is. pg_mimic answers that
        from Statement.param_oids, so the inference belongs here -- and a declared schema
        makes it short: a parameter compared against a column takes that column's type.
        """
        return await super().prepare(sql, self._infer_param_oids(sql, param_oids))

    def _infer_param_oids(self, sql: str, declared: list[int | None]) -> list[int | None]:
        if "$" not in sql:
            # prepare() runs before the middleware chain, so without this every SET,
            # SHOW and psql catalog query would pay for a parse whose result is thrown
            # away. No `$n`, nothing to infer.
            return declared
        try:
            expr = qualify(sqlglot.parse_one(sql, dialect="postgres"), schema=SCHEMA, dialect="postgres")
        except Exception:
            return declared  # unparseable, or not about our tables -- leave it alone
        aliases = _alias_map(expr)
        oids = dict(enumerate(declared))
        for node in expr.find_all(exp.Parameter):
            index = _parameter_index(node)
            if index is not None and oids.get(index) is None:
                oids[index] = _oid_of_compared_column(node, aliases)
        return [oids.get(index) for index in range(max(oids, default=-1) + 1)]

    async def describe(self, sql, param_oids):
        """Column shape without executing anything -- see the module docstring."""
        expr = self._analyze(sql, params=None)
        if not isinstance(expr, exp.Select):
            return None
        expr = annotate_types(expr, schema=SCHEMA)
        return [
            ResultColumn(select.alias_or_name, _TYPE_OIDS.get(select.type.this if select.type else None, TEXT))
            for select in expr.selects
        ]

    async def query(self, sql, params):
        expr = self._analyze(sql, params)
        if not isinstance(expr, exp.Select):
            raise PgError(FEATURE_NOT_SUPPORTED, "this example serves SELECT only -- a git repo is read-only")
        # git, the file reads and the executor are all blocking, and pg_mimic runs one
        # task per connection -- without this hop a slow query freezes every other
        # session on the event loop, including psql's opening handshake.
        result = await asyncio.to_thread(self._execute, expr)
        for row in result.rows:
            yield tuple(row)

    def _analyze(self, sql: str, params: list | None) -> exp.Expression:
        """Parse, qualify every column to a table, then substitute bound parameters.

        Qualifying first is what lets _substitute_params see which column each `$1` is
        measured against, which is the only type information available for it.
        """
        expr = sqlglot.parse_one(sql, dialect="postgres")
        if isinstance(expr, exp.Select):
            expr = qualify(expr, schema=SCHEMA, dialect="postgres")
            _substitute_params(expr, params)
        return expr

    def _execute(self, expr: exp.Select):
        tables = self._tables(expr)  # a PgError from git or an unknown table stands as-is
        try:
            return sqlglot_execute(expr, schema=SCHEMA, tables=tables, dialect="postgres")
        except Exception as error:
            # The executor supports a good deal less SQL than Postgres. Say so, rather
            # than returning an empty result the client reads as "no matching rows".
            raise PgError(FEATURE_NOT_SUPPORTED, f"this query is beyond the demo's executor: {error}") from error

    def _tables(self, expr: exp.Select) -> dict[str, list[dict]]:
        nodes = list(expr.find_all(exp.Table))
        uses = Counter(node.name for node in nodes)
        tables: dict[str, list[dict]] = {}
        for node in nodes:
            name = node.name
            if name in tables:
                continue
            if name in COLLECTORS:
                key = (name, ())
                if key not in self._rows:
                    self._rows[key] = COLLECTORS[name](self.repo)
            elif name in LOG_PROJECTIONS:
                # The executor reads rows by table name, not by alias, so two aliases of
                # one table cannot be given different row sets. Collect unfiltered in
                # that case rather than narrowing both by one alias's predicate -- the
                # self-join in the module docstring is what this protects.
                filters = pushdown(expr, name, node.alias_or_name) if uses[name] == 1 else []
                key = (name, tuple(filters))
                if key not in self._rows:
                    self._rows[key] = LOG_PROJECTIONS[name](self._log_records(filters))
            else:
                raise PgError(FEATURE_NOT_SUPPORTED, f'unknown table "{name}" -- try \\dt')
            tables[name] = self._rows[key]
        return tables

    def _log_records(self, filters: list[str]) -> list:
        key = tuple(filters)
        if key not in self._records:
            self._records[key] = parse_log(self.repo, filters)
        return self._records[key]


def _parameter_index(node: exp.Parameter) -> int | None:
    try:
        return int(node.this.this) - 1
    except (AttributeError, ValueError):
        return None


def _alias_map(expr: exp.Expression) -> dict[str, str]:
    """Table alias -> table name, which qualify() has already stamped onto every column."""
    return {node.alias_or_name: node.name for node in expr.find_all(exp.Table)}


def _oid_of_compared_column(node: exp.Parameter, aliases: dict[str, str]) -> int | None:
    parent = node.parent
    if not isinstance(parent, _COMPARISONS):
        return None
    for side in (parent.this, parent.expression):
        if isinstance(side, exp.Column):
            return _declared_oid(SCHEMA.get(aliases.get(side.table, ""), {}).get(side.name))
    return None


_TEXT_PARAM_COERCERS = {
    INT2: int,
    INT4: int,
    INT8: int,
    FLOAT8: float,
    NUMERIC: float,
    BOOL: lambda text: text.lower() in ("t", "true", "1", "y", "yes", "on"),
    TIMESTAMP: datetime.fromisoformat,
}


def _coerce(value, oid: int | None):
    """Parameters reach a session as strings whichever wire format they arrived in.

    pg_mimic normalises both to text and parses only arrays back out, so `$1` compared
    against an integer column is the string '100' and would be compared against ints.
    The column it is measured against settles the type, and prepare() has already
    located it.
    """
    coerce = _TEXT_PARAM_COERCERS.get(oid)
    if coerce is None or not isinstance(value, str):
        return value
    try:
        return coerce(value)
    except ValueError:
        return value


def _substitute_params(expr: exp.Select, params: list | None) -> None:
    """Replace $1/$2 with the values Bind supplied, or NULL when describing."""
    aliases = _alias_map(expr)
    for node in list(expr.find_all(exp.Parameter)):
        index = _parameter_index(node)
        value = params[index] if params is not None and index is not None and index < len(params) else None
        node.replace(_as_literal(_coerce(value, _oid_of_compared_column(node, aliases))))


def _as_literal(value) -> exp.Expression:
    try:
        # convert() casts datetimes to CAST(... AS TIMESTAMP) for us -- a bare string
        # would be compared against a timestamp column as a string.
        return exp.convert(value)
    except ValueError:
        return exp.Literal.string(str(value))


async def main():
    repo = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else ".")
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 5432
    if not os.path.isdir(os.path.join(repo, ".git")):
        sys.exit(f"{repo} is not a git repository")
    server = PgServer(session_factory=lambda: GitSession(repo))
    await server.start_server(host="127.0.0.1", port=port)
    print(f"pg_mimic serving {repo} on port {port} -- tables: {', '.join(TABLE_NAMES)}")
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
