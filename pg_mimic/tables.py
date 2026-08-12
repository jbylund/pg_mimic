"""TableSession -- hand it Python rows, get a Postgres server. No session code.

The smallest useful mimic ("serve these tables") otherwise costs three methods
and an OID decision per column, which is a lot of ceremony for the case that
makes pg_mimic reachable at all. sqlglot is already a hard dependency and its
executor already runs table-less SELECTs for `pg_mimic.middleware`; pointing that
same executor at user-supplied tables is a short step, and `Session.schema()`
then falls out of the declared columns for free.

The design decision worth explaining is how `describe()` answers.

pg_mimic treats column shape as a *declared fact*, known before any row exists
(see pg_mimic.results and pg_mimic.session). `session.statement_from_rows` bends
that for engines whose types can only be read off a row, and pays for it by
calling an empty result TEXT. A TableSession genuinely knows its own types up
front, so it derives them properly instead: the tables' Python values are
inspected **once, at construction**, to declare a schema, and every query's
output columns are then derived from *that schema* by annotating the query's
parse tree with sqlglot's type annotator. No result row is ever consulted, an
empty table describes exactly like a full one, and `count(*)` is bigint because
sqlglot says so rather than because a row happened to hold an int.

Where inference cannot settle a type -- an empty table, an all-NULL column, a
`list` that is equally an array or a json document -- it refuses and asks, the
same line `ResultColumn.for_type` draws, except that it refuses at construction
time rather than mid-query. `columns=` is the answer to every one of those.

Read-only, deliberately, in this version: the tables are the caller's own
objects, and a session that quietly mutated them (per connection, with no
transaction isolation and no way to roll back) would be a worse lie than a clear
refusal. INSERT/UPDATE/DELETE get read_only_sql_transaction.

Everything sqlglot's executor cannot do -- recursive CTEs, most of Postgres's
function library -- is reported as an error rather than answered approximately,
because a mimic that returns plausible wrong rows is worse than one that says no.

The same rule governs what it parses and then answers wrongly, which is the more
dangerous half: those don't fail, they answer the wrong question. Where the right
answer is reachable -- by rewriting the query into a shape the executor does get
right (NOT IN, NULL ordering), or by finishing the job on the rows it returns
(OFFSET, DISTINCT ON) -- this session does that. Where it isn't (TABLESAMPLE, FULL
OUTER JOIN), the query is refused.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any, Mapping, Sequence

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.executor import execute as sqlglot_execute
from sqlglot.optimizer.annotate_types import annotate_types
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.qualify import qualify

from .arrays import ARRAY_OID, element_oid_of, is_array_oid
from .errors import (
    FEATURE_NOT_SUPPORTED,
    INDETERMINATE_DATATYPE,
    INVALID_TEXT_REPRESENTATION,
    READ_ONLY_SQL_TRANSACTION,
    SYNTAX_ERROR,
    UNDEFINED_COLUMN,
    UNDEFINED_TABLE,
    PgError,
)
from .results import ResultColumn
from .session import CallbackStatement, Row, Session, Statement
from .types import (
    BOOL,
    BPCHAR,
    BYTEA,
    DATE,
    FLOAT4,
    FLOAT8,
    INT2,
    INT4,
    INT8,
    INTERVAL,
    JSON,
    JSONB,
    NUMERIC,
    TEXT,
    TIME,
    TIMESTAMP,
    TIMESTAMPTZ,
    UUID,
    VARCHAR,
    oid_for_type,
)

# A declared column type: a Python type (`int`, `list[str]`) resolved the way
# `ResultColumn.for_type` resolves it, or a raw OID (`JSONB`) for the cases where
# no Python type says which Postgres type you meant.
DeclaredType = Any
TableRows = Sequence[Mapping[str, Any]] | Sequence[Sequence[Any]]

# (OID, the name Session.schema()/psql show, sqlglot's spelling of it). One table
# rather than three dicts because these have to agree: a declared type is handed
# to sqlglot by its spelling and comes back out of the annotator, and that round
# trip is only lossless if both directions read the same row here.
_TYPES: tuple[tuple[int, str, str], ...] = (
    (BOOL, "boolean", "BOOLEAN"),
    (INT2, "smallint", "SMALLINT"),
    (INT4, "integer", "INT"),
    (INT8, "bigint", "BIGINT"),
    (FLOAT4, "real", "FLOAT"),
    (FLOAT8, "double precision", "DOUBLE"),
    (NUMERIC, "numeric", "DECIMAL"),
    (TEXT, "text", "TEXT"),
    (VARCHAR, "character varying", "VARCHAR"),
    (BPCHAR, "character", "CHAR"),
    (BYTEA, "bytea", "VARBINARY"),
    (DATE, "date", "DATE"),
    (TIME, "time", "TIME"),
    (TIMESTAMP, "timestamp", "TIMESTAMP"),
    (TIMESTAMPTZ, "timestamptz", "TIMESTAMPTZ"),
    (INTERVAL, "interval", "INTERVAL"),
    (UUID, "uuid", "UUID"),
    (JSON, "json", "JSON"),
    (JSONB, "jsonb", "JSONB"),
)

_PG_NAME = {oid: pg_name for oid, pg_name, _ in _TYPES}
_SQLGLOT_NAME = {oid: sqlglot_name for oid, _, sqlglot_name in _TYPES}
_OID_FOR_SQLGLOT_TYPE = {exp.DataType.build(sqlglot_name).this: oid for oid, _, sqlglot_name in _TYPES}

# Spellings only the annotator produces -- no declared column uses them, so they
# belong here rather than in _TYPES, where they would make the round trip above
# ambiguous. Each maps to what Postgres itself would call the expression.
_OID_FOR_SQLGLOT_TYPE.update(
    {
        exp.DataType.Type.BIGDECIMAL: NUMERIC,
        exp.DataType.Type.DATETIME: TIMESTAMP,
        exp.DataType.Type.NCHAR: BPCHAR,
        exp.DataType.Type.NVARCHAR: VARCHAR,
        exp.DataType.Type.BINARY: BYTEA,
    }
)

_UNTYPED = (exp.DataType.Type.UNKNOWN, exp.DataType.Type.NULL)

# Statements whose refusal is about being read-only rather than about coverage.
# Only long-standing sqlglot node names: anything else (TRUNCATE, ALTER, ...)
# lands in the generic "SELECT only" refusal, which is equally true of it.
_WRITES = (exp.Insert, exp.Update, exp.Delete, exp.Merge, exp.Create, exp.Drop)

# qualify() reports an unresolvable name with this phrase. Sniffing its message is
# how a missing column becomes 42703 rather than a generic "not supported"; if
# sqlglot rewords it the error stays correct, just less specific.
_UNRESOLVED = "could not be resolved"


@dataclass(frozen=True)
class _Table:
    """One declared table: its columns in order, typed twice -- once the way
    `Session.schema()` spells a type and once the way sqlglot does -- and its rows
    normalised to dicts keyed by exactly those columns (what sqlglot's executor
    reads, and what makes a missing key an explicit NULL).

    The two sqlglot-facing halves are keyed by the quoted spelling of each column
    (`_quoted`); `pg_types`, which is the catalog's, keeps the name as declared.
    """

    pg_types: dict[str, str]
    sqlglot_types: dict[str, str]
    rows: list[dict[str, Any]]


@dataclass(frozen=True)
class _Plan:
    """A query rewritten into what sqlglot's executor answers correctly, plus the
    work left over for Python.

    Several of the executor's gaps are shapes rather than missing features: a
    predicate whose NULL rule it gets wrong, an ordering whose comparison it
    cannot make. Those become a rewrite of the tree it is handed. The rest --
    taking rows off the front, keeping the first row per key, sorting a result it
    would hand back with its columns missing -- cannot be said to it at all, and
    are done to its output instead. Everything else is still refused rather than
    approximated.
    """

    expression: exp.Query
    # Select positions to keep the first row of, for DISTINCT ON; empty otherwise.
    distinct_on: tuple[int, ...] = ()
    # (output position, descending, nulls first) per ORDER BY term of a set
    # operation or SELECT DISTINCT, which are sorted here rather than by the
    # executor; empty otherwise.
    sort_keys: tuple[tuple[int, bool, bool], ...] = ()
    # How many leading columns the client asked for. Fewer than the query selects
    # when a DISTINCT ON key had to be added to the select list to be deduplicated
    # on; those trailing columns are the executor's business, not the client's.
    visible_columns: int = 0
    # Whether LIMIT/OFFSET are applied to the executor's rows rather than by it.
    rows_sliced_here: bool = False


class TableSession(Session):
    """Serve in-memory tables over the Postgres wire, read-only::

        tables = {
            "users": [{"id": 1, "name": "alice"}, {"id": 2, "name": "bob"}],
            "orders": [{"id": 10, "user_id": 1, "total": Decimal("9.99")}],
        }
        server = PgServer(session_factory=lambda: TableSession(tables))

    Rows are dicts (sqlglot's executor's own shape) or plain tuples, in which case
    their column names have to be declared. SELECT, WHERE, JOIN, GROUP BY, ORDER
    BY, LIMIT, OFFSET, DISTINCT ON and bind parameters are all answered, some of
    them by rewriting the query or finishing it in Python where sqlglot's executor
    would get it wrong. TABLESAMPLE is refused, because it gets that wrong in a way
    nothing here can repair.
    `\\dt`, `\\d users` and `information_schema` come from the derived `schema()`.

    Column types are inferred from the values in the rows, once, at construction.
    Declare the ones inference cannot settle -- an empty table, an all-NULL
    column, a list that could be an array or a json document -- with `columns`,
    which takes a Python type or an OID per column::

        TableSession(
            {"events": [], "docs": [{"body": {"a": 1}}]},
            columns={"events": {"id": int, "at": datetime}, "docs": {"body": JSONB}},
        )

    A `columns` entry may name only some of a table's columns; the rest are still
    inferred. For tuple rows it names all of them, in order, and may be a plain
    list of names if the types are inferrable::

        TableSession({"users": [(1, "alice")]}, columns={"users": ["id", "name"]})

    A dict key is the identifier *as written*, exactly as `CREATE TABLE` reads
    one: `{"users": ...}` answers `FROM users`, `FROM Users` and `FROM "users"`,
    because Postgres folds an unquoted name to lower case, while `{"Users": ...}`
    answers only `FROM "Users"` and reports `FROM Users` as a missing relation.
    Column keys work the same way, so a `{"userId": ...}` row is reachable as
    `"userId"` and not as `userId`. Lower-case keys are the ones that behave the
    way hand-written SQL expects.

    Construction validates and copies the rows, so it is per-connection work
    (`session_factory` runs once per client). At test-data scale that is
    microseconds; a table large enough for it to matter wants a real database.

    What it will not do: write. INSERT/UPDATE/DELETE are refused rather than
    applied to the caller's own lists behind its back, with no isolation between
    connections and no way to undo. And what sqlglot's executor cannot execute --
    recursive CTEs, most of Postgres's functions -- is an error, never an
    approximate answer.
    """

    def __init__(
        self, tables: Mapping[str, TableRows], columns: Mapping[str, Mapping[str, DeclaredType] | Sequence[str]] | None = None
    ):
        declared_columns = dict(columns or {})
        unknown = sorted(set(declared_columns) - set(tables))
        if unknown:
            raise ValueError(f"columns declares table(s) {unknown} that are not in tables ({sorted(tables)})")

        declared = {name: _declare_table(name, rows, declared_columns.get(name)) for name, rows in tables.items()}
        self._schema = {name: table.pg_types for name, table in declared.items()}
        # Quoted, like the column names inside them: sqlglot folds a bare name it is
        # handed, so `{"Users": ...}` would otherwise declare a table called `users`.
        self._rows = {_quoted(name): table.rows for name, table in declared.items()}
        # sqlglot's own view of the same declaration: what qualify() resolves `*`
        # and bare column names against, and what the annotator reads types from.
        self._sqlglot_schema = {_quoted(name): table.sqlglot_types for name, table in declared.items()}

    async def describe(self, sql: str, param_oids: list[int | None]) -> list[ResultColumn] | None:
        plan = self._plan(sql)
        return _result_columns(plan.expression, param_oids)[: plan.visible_columns]

    async def query(self, sql: str, params: list[Any]) -> list[Row]:
        plan = self._plan(sql)
        expression = _bind_parameters(plan.expression, params)
        # After binding, because `LIMIT $1 OFFSET $2` is only a row count once its
        # parameters are literals.
        limit, offset = _take_row_window(expression) if plan.rows_sliced_here else (None, 0)
        try:
            result = sqlglot_execute(expression, schema=self._sqlglot_schema, tables=self._rows, dialect="postgres")
        except Exception as exc:
            # Every Exception, not just SqlglotError: the executor compiles the
            # query to Python and evaluates it, so it also surfaces plain
            # ValueError/TypeError/NameError from code it generated. All of those
            # mean the same thing to the client -- this engine cannot run that SQL
            # -- and saying so beats an internal_error carrying a NameError.
            raise PgError(
                FEATURE_NOT_SUPPORTED,
                f"TableSession could not run this query: {exc}. sqlglot's executor is not a complete SQL engine -- "
                f"it has no recursive CTEs and implements only part of Postgres's function library.",
            ) from None
        rows = [tuple(row) for row in result.rows]
        # In Postgres's order: DISTINCT ON reduces the sorted rows, and only what
        # survives that is what LIMIT counts.
        if plan.distinct_on:
            rows = _first_row_per_key(rows, plan.distinct_on)
        if plan.sort_keys:
            rows = _sorted_rows(rows, plan.sort_keys)
        rows = rows[offset : None if limit is None else offset + limit]
        if plan.visible_columns < len(plan.expression.selects):
            rows = [row[: plan.visible_columns] for row in rows]  # drop the added DISTINCT ON keys
        return rows

    async def schema(self) -> dict:
        return self._schema

    async def prepare(self, sql: str, param_oids: list[int | None]) -> Statement:
        """As Session.prepare(), but answering ParameterDescription properly.

        A client may leave its parameter types to the server (asyncpg always
        does), and then reads the count and types back from
        `Describe(Statement)`. The base Session can only report what Parse
        declared, so a session that infers nothing tells asyncpg the query takes
        no parameters and asyncpg refuses to send any. This session does know the
        schema, so it can say `$1` is bigint.
        """
        statement = await super().prepare(sql, param_oids)
        # Only for a statement that might have a parameter and that is ours to
        # answer: filling these in plans the query, and planning every SET/BEGIN or
        # catalog statement would refuse ones the middleware inside super() answers
        # happily -- those come back as something other than a CallbackStatement.
        if "$" in sql and isinstance(statement, CallbackStatement):
            statement.param_oids = self._parameter_oids(sql, param_oids)
        return statement

    def _parameter_oids(self, sql: str, declared: list[int | None]) -> list[int | None]:
        """Parameter types, taking the client's declaration where it made one and
        filling the rest in from what each parameter is compared against."""
        plan = self._plan(sql).expression
        inferred = {}
        for parameter in plan.find_all(exp.Parameter):
            index = _param_index(parameter) - 1
            inferred[index] = _oid_for_sqlglot_type(_comparison_type(parameter))
        count = max([len(declared), *(index + 1 for index in inferred)], default=0)
        return [declared[i] if i < len(declared) and declared[i] is not None else inferred.get(i) for i in range(count)]

    def _plan(self, sql: str) -> _Plan:
        """The query, qualified and type-annotated against the declared schema, and
        rewritten into what sqlglot's executor answers correctly.

        Everything both describe() and query() need, and nothing either of them
        does alone: names resolved, `*` expanded, a type on every node. Cheap
        enough to redo per call, and stateless -- describe() must not leave
        anything behind for a query() that may never come.
        """
        try:
            expression = sqlglot.parse_one(sql, dialect="postgres")
        except SqlglotError as exc:
            raise PgError(SYNTAX_ERROR, str(exc)) from None

        # Postgres's folding rule -- unquoted names to lower case, quoted ones left
        # alone -- applied before anything below reads a name off the tree. qualify()
        # does it too, but too late for _reject_unknown_tables, which would otherwise
        # match `FROM Users` and `FROM "Users"` against the same declared table.
        expression = normalize_identifiers(expression, dialect="postgres")
        _reject_non_select(sql, expression)
        expression = _flatten_parenthesized(expression)
        _reject_silently_ignored(expression)
        self._reject_unknown_tables(expression)
        try:
            # canonicalize_table_aliases, because the executor keys its plan steps
            # by table name: two branches of a set operation reading the same table
            # collide, and the second silently runs the first one's plan --
            # `SELECT .. FROM t WHERE a UNION ALL SELECT .. FROM t WHERE b` answers
            # `a` twice. Distinct aliases per reference are what keep them apart.
            qualified = qualify(expression, schema=self._sqlglot_schema, dialect="postgres", canonicalize_table_aliases=True)
            # Before annotate_types, so a DISTINCT ON key this adds to the select
            # list is typed like any other column rather than left bare.
            distinct_on, visible_columns = _distinct_on_keys(qualified)
            annotated = annotate_types(qualified, schema=self._sqlglot_schema, dialect="postgres")
        except PgError:
            raise
        except Exception as exc:
            sqlstate = UNDEFINED_COLUMN if _UNRESOLVED in str(exc) else FEATURE_NOT_SUPPORTED
            raise PgError(sqlstate, str(exc)) from None

        _rewrite_not_in(annotated)
        sort_keys = _take_result_order(annotated)
        # Last: it puts a key in front of every ORDER BY term, which the two passes
        # above had to read as the query wrote them.
        _rewrite_null_ordering(annotated)
        return _Plan(
            expression=annotated,
            distinct_on=distinct_on,
            sort_keys=sort_keys,
            visible_columns=visible_columns,
            # An OFFSET the executor would drop, or a DISTINCT ON or ORDER BY whose
            # rows have to be settled here before a LIMIT can count them.
            rows_sliced_here=bool(distinct_on or sort_keys) or annotated.args.get("offset") is not None,
        )

    def _reject_unknown_tables(self, expression: exp.Expression) -> None:
        """Report a table we don't have as Postgres does, before sqlglot can.

        qualify() gets there eventually but blames the column ("Column 'id' could
        not be resolved"), where a client -- or a human -- wants to be told the
        relation is missing, under the SQLSTATE drivers match on.
        """
        cte_names = {cte.alias_or_name for cte in expression.find_all(exp.CTE)}
        for table in expression.find_all(exp.Table):
            # The schema is checked before the name: a TableSession's tables are all
            # in `public`, and sqlglot's executor resolves `other.users` against
            # `users` regardless, which would serve one schema's table as another's.
            if table.db and table.db != "public":
                raise PgError(UNDEFINED_TABLE, f'relation "{table.db}.{table.name}" does not exist')
            # Against `_schema`, the one view of the declaration still keyed by the
            # names as written -- which, the tree having been folded already, is
            # what `table.name` now is.
            if table.name not in self._schema and table.name not in cte_names:
                raise PgError(UNDEFINED_TABLE, f'relation "{table.name}" does not exist')


# --- declaring the tables ------------------------------------------------------------


def _declare_table(name: str, rows: TableRows, declared: Mapping[str, DeclaredType] | Sequence[str] | None) -> _Table:
    column_names = _column_names(name, rows, declared)
    normalised = [_row_as_dict(name, index, row, column_names) for index, row in enumerate(rows)]
    declared_types: Mapping[str, DeclaredType] = declared if isinstance(declared, Mapping) else {}
    oids = {}
    for column in column_names:
        if column in declared_types:
            oids[column] = _declared_oid(name, column, declared_types[column])
        else:
            oids[column] = _inferred_oid(name, column, [row[column] for row in normalised])

    # sqlglot's names first: that is the pass which rejects an OID this session
    # cannot serve at all, and _pg_type_name assumes it already ran.
    sqlglot_types = {column: _sqlglot_type_name(name, column, oid) for column, oid in oids.items()}
    return _Table(
        pg_types={column: _pg_type_name(oid) for column, oid in oids.items()},
        sqlglot_types={_quoted(column): type_name for column, type_name in sqlglot_types.items()},
        rows=[{_quoted(column): value for column, value in row.items()} for row in normalised],
    )


def _quoted(name: str) -> str:
    """A declared name spelled the way SQL has to spell it to mean that exact
    identifier -- `Id` as `"Id"`, and a name with a quote in it escaped.

    Everything sqlglot is handed a name in -- the schema, the executor's tables,
    the query -- goes through the same folding, so an unquoted `Id` would declare a
    column called `id` and put the declaration out of reach of the only reference
    Postgres would resolve to it.
    """
    return exp.to_identifier(name, quoted=True).sql(dialect="postgres")


def _column_names(name: str, rows: TableRows, declared: Mapping[str, DeclaredType] | Sequence[str] | None) -> list[str]:
    """The table's columns, in order: the row keys, then any column named only in
    `declared` (which is how an empty table has columns at all).

    Both a Mapping of declared types and a plain list of names iterate to names,
    so either shape works here.
    """
    declared_names = list(declared or ())
    if any(not isinstance(row, Mapping) for row in rows):
        if not declared_names:
            raise ValueError(
                f'table "{name}" has rows given as tuples, so its column names must be declared: '
                f'columns={{"{name}": ["id", "name"]}}'
            )
        return declared_names

    names = []
    for row in rows:
        for key in row:
            if key not in names:
                names.append(key)
    for declared_name in declared_names:
        if declared_name not in names:
            names.append(declared_name)
    if not names:
        raise ValueError(
            f'table "{name}" is empty, so its columns cannot be inferred -- declare them: '
            f'columns={{"{name}": {{"id": int, "name": str}}}}'
        )
    return names


def _row_as_dict(name: str, index: int, row: Any, column_names: list[str]) -> dict[str, Any]:
    """One row, keyed by every declared column. A key a dict row omits is NULL --
    the executor reads rows positionally against the schema, so a row that is
    simply missing a column has to become an explicit None here."""
    if isinstance(row, Mapping):
        unknown = sorted(set(row) - set(column_names))
        if unknown:
            raise ValueError(
                f'table "{name}" row {index} has column(s) {unknown} that the declared columns {column_names} do not include'
            )
        return {column: row.get(column) for column in column_names}
    values = list(row)
    if len(values) != len(column_names):
        raise ValueError(f'table "{name}" row {index} has {len(values)} value(s) but {len(column_names)} column(s) are declared')
    return dict(zip(column_names, values))


def _declared_oid(name: str, column: str, declared: DeclaredType) -> int:
    """An explicitly declared column type: a raw OID, or a Python type resolved
    exactly as `ResultColumn.for_type` would resolve it."""
    if isinstance(declared, int) and not isinstance(declared, bool):
        return declared
    try:
        return oid_for_type(declared)
    except TypeError as exc:
        raise TypeError(f'column "{name}"."{column}": {exc}') from None


def _inferred_oid(name: str, column: str, values: list[Any]) -> int:
    """The column's type, from the Python values it actually holds.

    Every non-NULL value has to agree. Picking the first and hoping would make
    `[1, 2.5]` an int column that then hands a float to an int8 encoder, and no
    real table could hold both anyway. Nothing to go on -- an empty table, an
    all-NULL column -- is a question, not a default: TEXT would be a guess that
    only shows up as a wrong type in the client, which is the failure mode this
    codebase refuses everywhere else.
    """
    oids = set()
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, dict)):
            # The ambiguity types.oid_for_type refuses on, hit from the other
            # side: here there *is* a value to look at, and it still doesn't say
            # whether the column is a Postgres array or a json document.
            raise TypeError(
                f'column "{name}"."{column}" holds a {type(value).__name__}, which is equally a Postgres array or a json '
                f'document -- declare which: columns={{"{name}": {{"{column}": list[str]}}}} or {{"{column}": JSONB}}'
            )
        oids.add(oid_for_type(type(value)))

    if len(oids) == 1:
        return oids.pop()
    if not oids:
        raise ValueError(
            f'column "{name}"."{column}" has no non-NULL value to infer a type from -- declare it: '
            f'columns={{"{name}": {{"{column}": str}}}}'
        )
    types = sorted({type(value).__name__ for value in values if value is not None})
    raise ValueError(
        f'column "{name}"."{column}" holds values of more than one type ({", ".join(types)}), which no Postgres column '
        f'can -- fix the rows, or declare the type you mean: columns={{"{name}": {{"{column}": str}}}}'
    )


def _pg_type_name(oid: int) -> str:
    """The type name `Session.schema()` declares -- one of the spellings
    pg_mimic.catalog_data knows, or an array over one, so the catalog maps it back to
    this same OID (see catalog._oid_for_declared_type, and #43 for when it didn't)."""
    if is_array_oid(oid):
        return f"{_pg_type_name(element_oid_of(oid))}[]"
    return _PG_NAME[oid]  # every OID here passed _sqlglot_type_name's check first


def _sqlglot_type_name(name: str, column: str, oid: int) -> str:
    if is_array_oid(oid):
        return f"{_sqlglot_type_name(name, column, element_oid_of(oid))}[]"
    sqlglot_name = _SQLGLOT_NAME.get(oid)
    if sqlglot_name is None:
        # Refused at construction rather than on the query that first selects the
        # column: an OID sqlglot cannot be told about is one no query over this
        # table can describe, and finding that out per query would be a mystery.
        raise ValueError(
            f'column "{name}"."{column}" declares OID {oid}, which has no equivalent in sqlglot\'s type system, so '
            f"TableSession cannot describe it to the query engine. Declare one of: {', '.join(sorted(_PG_NAME.values()))} "
            f"(or serve this table from a Session of your own)."
        )
    return sqlglot_name


# --- deriving the answer -------------------------------------------------------------


def _reject_non_select(sql: str, expression: exp.Expression) -> None:
    tag = sql.strip().split(None, 1)[0].upper() if sql.strip() else "statement"
    if isinstance(expression, _WRITES):
        raise PgError(
            READ_ONLY_SQL_TRANSACTION,
            f"cannot execute {tag} in a read-only session -- TableSession serves its tables read-only",
        )
    if not isinstance(expression, exp.Query):
        raise PgError(FEATURE_NOT_SUPPORTED, f"TableSession answers SELECT only, and cannot run {tag}")


def _flatten_parenthesized(expression: exp.Expression) -> exp.Expression:
    """Fold a parenthesized top-level query into the query it wraps.

    `(SELECT ...) LIMIT 1` parses as an exp.Subquery carrying the LIMIT, and every
    check below reads the clauses it cares about -- the LIMIT/OFFSET window, the
    ORDER BY a set operation or a SELECT DISTINCT has to be sorted by here -- off
    the outermost node. Left wrapped they are found on neither node: the executor
    ignores a LIMIT on a Subquery, so `(SELECT a FROM t) LIMIT 1` answers with
    every row.

    Postgres reads the parentheses as grouping, so folding the wrapper's clauses
    onto the query inside is what they mean -- unless the inner query has a row
    window of its own, where the two are genuinely nested and there is nothing to
    fold.
    """
    while isinstance(expression, exp.Subquery) and not expression.alias:
        inner = expression.this
        if not isinstance(inner, exp.Query):
            break
        if inner.args.get("limit") is not None or inner.args.get("offset") is not None:
            raise PgError(
                FEATURE_NOT_SUPPORTED,
                "TableSession applies LIMIT/OFFSET to the rows sqlglot's executor returns, which it can only do for "
                "the whole query -- a parenthesized query with a row window of its own is two of them, and answering "
                "with the wrong rows is worse than refusing.",
            )
        # An inner ORDER BY is only meaningful together with an inner row window,
        # which there is none of here, so an outer one simply replaces it.
        for arg in ("order", "limit", "offset"):
            outer = expression.args.get(arg)
            if outer is not None:
                inner.set(arg, outer)
        expression = inner
    return expression


def _reject_silently_ignored(expression: exp.Expression) -> None:
    """Refuse what sqlglot's executor parses and then answers wrongly.

    These are the dangerous ones: unlike an unimplemented function, they don't
    fail, they return a full result that is quietly the wrong one. What this
    session can rewrite (NOT IN, NULL ordering) or finish by hand (OFFSET,
    DISTINCT ON) is handled in _plan; what is left has no such repair, so the
    answer is no.
    """
    if expression.find(exp.TableSample) is not None:
        raise PgError(
            FEATURE_NOT_SUPPORTED,
            "sqlglot's executor ignores TABLESAMPLE, so TableSession refuses the query rather than answering it "
            "with the wrong rows",
        )

    # OFFSET and DISTINCT ON are finished in Python, which can only reach the rows
    # the executor hands back -- the whole query's. One nested inside a subquery or
    # a set operation's branch would have to be applied to rows this session never
    # sees, so it stays a refusal.
    top_level = (expression.args.get("offset"), expression.args.get("distinct"))
    for node_type, clause in ((exp.Offset, "OFFSET"), (exp.Distinct, "DISTINCT ON")):
        for node in expression.find_all(node_type):
            # Identity, not ==: sqlglot compares expressions structurally, and the
            # nested one is often an exact copy of the outer.
            if any(node is outer for outer in top_level):
                continue
            if node_type is exp.Distinct and not node.args.get("on"):
                continue  # plain SELECT DISTINCT, which the executor does apply
            raise PgError(
                FEATURE_NOT_SUPPORTED,
                f"TableSession applies {clause} to the rows sqlglot's executor returns, which it can only do for the "
                f"whole query -- one inside a subquery or a UNION branch would be ignored, and answering with the "
                f"wrong rows is worse than refusing. Lift it to the outermost SELECT.",
            )


# --- rewriting what the executor gets wrong -------------------------------------------


def _rewrite_null_ordering(expression: exp.Expression) -> None:
    """Sort NULLs where Postgres sorts them.

    The executor orders rows with Python's own comparisons, which have no NULL
    rule: ascending coincidentally matches Postgres (None sorts last) and
    descending raises `'<' not supported between 'int' and 'NoneType'`. Ordering
    on `key IS NULL` first settles the NULLs before any value comparison happens,
    which both places them as Postgres does and keeps the comparison that raises
    from being reached.

    qualify() has already resolved every key's placement to Postgres's own default
    -- NULLS LAST for ASC, NULLS FIRST for DESC -- so `nulls_first` here is what
    the answer has to look like, not just what the query happened to spell out.
    """
    for order in expression.find_all(exp.Order):
        keys: list[exp.Expression] = []
        for ordered in order.expressions:
            nulls_first = bool(ordered.args.get("nulls_first"))
            if nulls_first or ordered.args.get("desc"):
                # Ascending on `IS NULL` puts false (a value) first, so descending
                # is what puts the NULLs first.
                keys.append(exp.Ordered(this=exp.Is(this=ordered.this.copy(), expression=exp.Null()), desc=nulls_first))
            keys.append(ordered)
        order.set("expressions", keys)


def _rewrite_not_in(expression: exp.Expression) -> None:
    """Give `x NOT IN (subquery)` Postgres's answer.

    The executor returns every row for it, filtering nothing -- the one shape here
    where a WHERE clause is not merely approximate but inert. `NOT EXISTS` it does
    run correctly, including correlated, so the filter becomes an anti-join.

    The second half is SQL's NULL rule, which no anti-join carries: if the
    subquery yields a single NULL then `x NOT IN` is unknown for every x and the
    result is empty. That has to be spelled as a COUNT rather than the obvious
    `NOT EXISTS (... IS NULL)`, which is Python the executor cannot compile.

    `IN (subquery)` is left alone -- the executor already answers it correctly,
    NULLs and all.
    """
    negations = [node for node in expression.find_all(exp.Not) if isinstance(node.this, exp.In) and "query" in node.this.args]
    # Innermost first: the rewrite copies the subquery, so a nested NOT IN rewritten
    # after its parent would be a node no longer attached to the tree. Replacing a
    # descendant leaves the ancestors this list holds intact, so this order works
    # and the other does not.
    for index, negation in enumerate(sorted(negations, key=lambda node: node.depth, reverse=True)):
        candidate = negation.this
        subquery = candidate.args["query"]
        inner = subquery.this if isinstance(subquery, exp.Subquery) else subquery
        if len(inner.selects) != 1:
            raise PgError(
                FEATURE_NOT_SUPPORTED,
                "TableSession rewrites NOT IN (subquery) into a NOT EXISTS that sqlglot's executor runs correctly, "
                "which it can only do for a subquery selecting one column.",
            )
        column = inner.selects[0].alias_or_name
        value = candidate.this
        # Distinct aliases per rewrite, for the reason qualify() is asked to
        # canonicalize them: the executor keys its plan steps by name, and two of
        # these sharing one would answer as a single query.
        negation.replace(
            exp.and_(
                exp.Not(this=exp.Exists(this=_anti_join(inner, column, value, f"_not_in_{index}_a"))),
                exp.EQ(this=_null_count(inner, column, f"_not_in_{index}_b"), expression=exp.Literal.number(0)),
            )
        )


def _anti_join(inner: exp.Query, column: str, value: exp.Expression, alias: str) -> exp.Select:
    """`SELECT 1 FROM (subquery) alias WHERE alias.column = value`."""
    return (
        exp.select(exp.Literal.number(1))
        .from_(exp.Subquery(this=inner.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))
        .where(exp.EQ(this=exp.column(column, alias), expression=value.copy()))
    )


def _null_count(inner: exp.Query, column: str, alias: str) -> exp.Subquery:
    """`(SELECT COUNT(*) FROM (subquery) alias WHERE alias.column IS NULL)`."""
    counted = (
        exp.select(exp.Count(this=exp.Star()))
        .from_(exp.Subquery(this=inner.copy(), alias=exp.TableAlias(this=exp.to_identifier(alias))))
        .where(exp.Is(this=exp.column(column, alias), expression=exp.Null()))
    )
    return exp.Subquery(this=counted)


def _distinct_on_keys(expression: exp.Query) -> tuple[tuple[int, ...], int]:
    """The output columns `DISTINCT ON` keeps the first row of, and how many columns
    the client asked for -- taking the clause off the query on the way.

    The executor parses DISTINCT ON and then returns the duplicate rows anyway, and
    it has no window functions to rewrite it into, so this is finished in Python
    instead: keep the first row of each key, which after the executor's own ORDER
    BY is the row Postgres would have kept. Postgres already requires the ORDER BY
    to begin with these expressions, so they are the leading sort keys in any query
    it would accept -- and that requirement is checked here too, so a query it
    rejects is not quietly answered.

    A key the query does not select -- `DISTINCT ON (user_id) page`, which Postgres
    allows -- is appended to the select list so the rows carry something to
    deduplicate on, and dropped again from every row afterwards.
    """
    visible_columns = len(expression.selects)
    distinct = expression.args.get("distinct")
    # A set operation's `distinct` arg is the bool in `UNION [ALL]`, not a clause.
    if not isinstance(distinct, exp.Distinct) or not distinct.args.get("on"):
        return (), visible_columns

    on = distinct.args["on"].expressions
    order = expression.args.get("order")
    if order is not None:
        leading = [ordered.this.sql(dialect="postgres") for ordered in order.expressions[: len(on)]]
        if leading != [key.sql(dialect="postgres") for key in on]:
            raise PgError(SYNTAX_ERROR, "SELECT DISTINCT ON expressions must match initial ORDER BY expressions")

    keys = []
    for key in on:
        position = _output_position(expression, key)
        if position is None:
            position = len(expression.selects)
            expression.select(exp.alias_(key.copy(), f"_distinct_on_{position}"), copy=False)
        keys.append(position)
    expression.set("distinct", None)
    return tuple(keys), visible_columns


def _output_position(expression: exp.Query, key: exp.Expression) -> int | None:
    """Where a DISTINCT ON expression already sits in the select list, or None.

    qualify() writes the key as the output column's name when it resolves to one
    and as the fully qualified expression when it does not, so matching both forms
    is also what tells those two cases apart.
    """
    wanted = key.sql(dialect="postgres")
    for position, select in enumerate(expression.selects):
        named = exp.to_identifier(select.alias_or_name, quoted=True).sql(dialect="postgres")
        if wanted in (select.unalias().sql(dialect="postgres"), named):
            return position
    return None


def _take_result_order(expression: exp.Query) -> tuple[tuple[int, bool, bool], ...]:
    """Strip the ORDER BY off a set operation or a SELECT DISTINCT and return its
    keys, to sort the rows by here instead.

    Asked to sort a UNION the executor returns one empty tuple per row -- the rows
    are there, every column has gone. A SELECT DISTINCT it sorts by the select list
    alone, which the `IS NULL` key _rewrite_null_ordering adds is deliberately not
    part of.

    Both are sortable here for the same reason: Postgres only lets either kind of
    ORDER BY name the output columns, so every key is a position in the result
    rather than an expression that would have to be evaluated to find it.
    """
    order = expression.args.get("order")
    distinct = expression.args.get("distinct")
    sortable_here = isinstance(expression, exp.SetOperation) or isinstance(distinct, exp.Distinct)
    if order is None or not sortable_here:
        return ()

    keys = []
    for ordered in order.expressions:
        position = _output_position(expression, ordered.this)
        if position is None:
            raise PgError(
                UNDEFINED_COLUMN,
                f"for SELECT DISTINCT and for a UNION, EXCEPT or INTERSECT, ORDER BY expressions must appear in "
                f"the select list, and {ordered.this.sql(dialect='postgres')} does not",
            )
        keys.append((position, bool(ordered.args.get("desc")), bool(ordered.args.get("nulls_first"))))
    expression.set("order", None)
    return tuple(keys)


def _sorted_rows(rows: list[Row], keys: tuple[tuple[int, bool, bool], ...]) -> list[Row]:
    """Sort by SQL's rules rather than Python's, which have no answer for NULL."""

    def compare(left: Row, right: Row) -> int:
        for position, descending, nulls_first in keys:
            a, b = left[position], right[position]
            if a is None and b is None:
                continue
            if a is None or b is None:
                first_is_null = a is None
                return -1 if first_is_null == nulls_first else 1
            if a == b:
                continue
            return (-1 if a < b else 1) * (-1 if descending else 1)
        return 0

    return sorted(rows, key=cmp_to_key(compare))


def _take_row_window(expression: exp.Query) -> tuple[int | None, int]:
    """Strip LIMIT/OFFSET off the query and return them, to be applied to the rows.

    Both, not just the OFFSET the executor drops: a LIMIT left in place would be
    counted against the rows before the OFFSET skipped any of them, or before
    DISTINCT ON removed any, and Postgres counts it after both.
    """
    limit = _row_count(expression.args.get("limit"), "LIMIT")
    offset = _row_count(expression.args.get("offset"), "OFFSET")
    expression.set("limit", None)
    expression.set("offset", None)
    return limit, offset or 0


def _row_count(node: exp.Expression | None, clause: str) -> int | None:
    if node is None:
        return None
    value = node.expression
    if isinstance(value, exp.Literal) and value.is_int:
        return int(value.name)
    if isinstance(value, exp.Null) or value is None:
        return None  # LIMIT ALL, which is no limit at all
    raise PgError(
        FEATURE_NOT_SUPPORTED,
        f"TableSession applies {clause} to the rows sqlglot's executor returns and needs a row count to do it, "
        f"but this one is {value.sql(dialect='postgres')!r}.",
    )


def _first_row_per_key(rows: list[Row], keys: tuple[int, ...]) -> list[Row]:
    seen = set()
    kept = []
    for row in rows:
        key = tuple(_hashable(row[k]) for k in keys)
        if key not in seen:
            seen.add(key)
            kept.append(row)
    return kept


def _hashable(value: Any) -> Any:
    """A DISTINCT ON key that can go in a set.

    An array column is a list and a json/jsonb one a dict or a list, neither of
    which hashes, and both nest. Nothing here needs the key to be anything but
    comparable for equality, so tuples stand in -- dicts as their sorted items, so
    that two equal documents produce one key however they were built.
    """
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    return value


def _result_columns(expression: exp.Query, param_oids: list[int | None]) -> list[ResultColumn]:
    return [ResultColumn(_column_name(select), _column_oid(select, param_oids)) for select in expression.selects]


def _column_name(select: exp.Expression) -> str:
    """The output column's name. qualify() has already resolved the easy ones and
    labelled anything unnameable `_col_N`; name those after their function, as
    Postgres does, and fall back to Postgres's own `?column?`."""
    name = select.alias_or_name
    if name and not name.startswith("_col_"):
        return name
    inner = select.unalias()
    if isinstance(inner, exp.Anonymous):
        return str(inner.this).lower()
    if isinstance(inner, exp.Func):
        return inner.sql_name().lower()
    return "?column?"


def _column_oid(select: exp.Expression, param_oids: list[int | None]) -> int:
    oid = _oid_for_sqlglot_type(select.type)
    if oid is not None:
        return oid

    inner = select.unalias()
    if isinstance(inner, exp.Null):
        return TEXT  # an untyped NULL resolves to text in real Postgres too
    if isinstance(inner, exp.Parameter):
        # The one type the annotator can't reach and the protocol can: a bare `$1`
        # in the select list, whose type the client declared in Parse.
        declared = _param_oid(inner, param_oids)
        if declared is not None:
            return declared
        raise PgError(INDETERMINATE_DATATYPE, f"could not determine data type of parameter ${_param_index(inner)}")

    expression_sql = inner.sql(dialect="postgres")
    raise PgError(
        FEATURE_NOT_SUPPORTED,
        f"TableSession could not derive a Postgres type for the output column {expression_sql!r}. Column types come "
        f"from the declared table schema, not from the rows a query happens to return, so an expression sqlglot "
        f"cannot type has none -- name it with a cast, e.g. {expression_sql}::text.",
    )


def _oid_for_sqlglot_type(data_type: exp.DataType | None) -> int | None:
    if data_type is None:
        return None
    # Unwrap array nesting the way types.oid_for_type does: a Postgres array OID
    # carries no dimensionality, that rides in each value.
    element = data_type
    while element.this == exp.DataType.Type.ARRAY:
        if not element.expressions:
            return None
        element = element.expressions[0]
    oid = _OID_FOR_SQLGLOT_TYPE.get(element.this)
    if oid is None:
        return None
    return ARRAY_OID.get(oid) if element is not data_type else oid


def _param_oid(parameter: exp.Parameter, param_oids: list[int | None]) -> int | None:
    index = _param_index(parameter) - 1
    return param_oids[index] if 0 <= index < len(param_oids) else None


def _param_index(parameter: exp.Parameter) -> int:
    try:
        return int(parameter.name)
    except ValueError:
        raise PgError(SYNTAX_ERROR, f"unsupported parameter ${parameter.name}") from None


# --- binding parameters --------------------------------------------------------------


def _bind_parameters(expression: exp.Query, params: list[Any]) -> exp.Query:
    """Substitute the bound values into the query the executor will run.

    sqlglot's executor has no notion of a bind parameter, so `$1` has to become a
    literal, and the values arrive as text (pg_mimic decodes binary parameters to
    the same canonical text) -- so the type to read each one as comes from
    whatever it is being compared against. `WHERE id = $1` then compares numbers
    rather than an int against the string "1".

    Each parameter becomes the literal the same value written inline in the SQL
    would have been, so a parameterised query and a literal one behave alike. The
    exception is the date/time family, where a literal would compare a string to a
    datetime and raise: those become an explicit CAST, which the executor does
    convert.
    """
    bound = expression.copy()
    for parameter in list(bound.find_all(exp.Parameter)):
        index = _param_index(parameter) - 1
        if not 0 <= index < len(params):
            raise PgError(SYNTAX_ERROR, f"there is no parameter ${index + 1}")
        parameter.replace(_literal(params[index], _oid_for_sqlglot_type(_comparison_type(parameter))))
    return bound


def _comparison_type(parameter: exp.Parameter) -> exp.DataType | None:
    """The declared type of whatever this parameter sits next to in the tree.

    Postgres resolves an untyped parameter from its context; this is the slice of
    that which matters here -- a parameter compared, added or matched against a
    column of a known type. With no such neighbour the value stays text, which is
    what it arrived as.
    """
    parent = parameter.parent
    if parent is None:
        return None
    if isinstance(parent, (exp.Limit, exp.Offset)):
        # A row count has no neighbour to be typed from, and left as text it would
        # not be a count at all. Postgres calls it bigint.
        return exp.DataType.build("BIGINT")
    for sibling in _child_expressions(parent):
        if sibling is parameter:
            continue
        data_type = sibling.type
        if data_type is not None and data_type.this not in _UNTYPED:
            return data_type
    return None


def _child_expressions(node: exp.Expression) -> list[exp.Expression]:
    children = []
    for value in node.args.values():
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, exp.Expression):
                children.append(item)
    return children


# Postgres's own spellings of true and false, plus pg_mimic's canonical "t"/"f"
# (what a binary bool parameter is decoded to). Anything else is not a boolean:
# taken as false it would silently answer with the false rows.
_TRUE_TEXTS = {"t", "true", "y", "yes", "on", "1"}
_FALSE_TEXTS = {"f", "false", "n", "no", "off", "0"}

# The types whose text form the executor's CAST converts correctly. Deliberately
# not bool (`bool("f")` is True there) and not numeric (int(), which "1.5" fails
# and "1.00" truncates) -- those are built here instead.
_CAST_FOR = {DATE: "DATE", TIME: "TIME", TIMESTAMP: "TIMESTAMP", TIMESTAMPTZ: "TIMESTAMP"}

_NUMBERS = {INT2, INT4, INT8, FLOAT4, FLOAT8, NUMERIC}


def _literal(value: Any, oid: int | None) -> exp.Expression:
    if value is None:
        return exp.Null()
    if isinstance(value, list):
        # An array parameter arrives as a (possibly nested) list of texts.
        element = element_oid_of(oid) if oid is not None and is_array_oid(oid) else None
        return exp.Array(expressions=[_literal(item, element) for item in value])
    if oid in _NUMBERS:
        return _number_literal(value, oid)
    if oid == BOOL:
        return _bool_literal(value)
    if oid in _CAST_FOR:
        return exp.Cast(this=exp.Literal.string(value), to=exp.DataType.build(_CAST_FOR[oid]))
    return exp.Literal.string(value)


def _bool_literal(value: Any) -> exp.Boolean:
    text = str(value).strip().lower()
    if text in _TRUE_TEXTS:
        return exp.true()
    if text in _FALSE_TEXTS:
        return exp.false()
    raise PgError(INVALID_TEXT_REPRESENTATION, f'invalid input syntax for type boolean: "{value}"')


# What each numeric type's text form may look like. float() is not the test:
# it takes "1.5" and "1_0" for a bigint, where Postgres raises, and a parameter
# quietly rounded or reinterpreted is a query answered with the wrong rows.
_INTEGER_TEXT = re.compile(r"[+-]?[0-9]+\Z")
_DECIMAL_TEXT = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)([eE][+-]?[0-9]+)?\Z")
_INTEGERS = {INT2, INT4, INT8}
_NON_FINITE = {"inf", "infinity", "-inf", "-infinity", "nan"}


def _number_literal(value: Any, oid: int) -> exp.Literal:
    text = str(value).strip()
    if oid not in _INTEGERS and text.lower() in _NON_FINITE:
        # Postgres takes these for float4/float8/numeric; sqlglot's executor has
        # no spelling for them, and an unquoted Infinity reaches it as a name.
        raise PgError(
            FEATURE_NOT_SUPPORTED,
            f'TableSession cannot pass the {_PG_NAME[oid]} value "{value}" to sqlglot\'s executor, which has no '
            f"literal for infinity or NaN.",
        )
    pattern = _INTEGER_TEXT if oid in _INTEGERS else _DECIMAL_TEXT
    if not pattern.match(text):
        raise PgError(INVALID_TEXT_REPRESENTATION, f'invalid input syntax for type {_PG_NAME[oid]}: "{value}"')
    return exp.Literal.number(text)
