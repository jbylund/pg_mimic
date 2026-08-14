"""Column shape from a declared schema, derived without running the query.

pg_mimic treats column shape as a *declared fact*, known before any row exists
(see pg_mimic.results and pg_mimic.session). A session that declares a `schema()`
already holds everything `describe()` needs: qualify the query against that
schema, let sqlglot's type annotator put a type on every node, then map each
output column's type onto a Postgres OID. No result row is ever consulted, so an
empty table describes exactly like a full one.

Those are the same three steps for every schema-declaring session, and they had
been written twice -- once here in `pg_mimic.tables` and once in
`examples/git_sql.py` -- which is how the two came apart. The example was still
describing `select 3000000000` as int4, the bug that crashes asyncpg's binary
decoder (#40), long after the library copy had learned to size an integer
literal by its value. This module is the one copy (#88).

The recipe used to be written out here, and each caller reassembled it. It is
`pg_mimic.analysis.AnalyzedQuery` now, which owns the order::

    analyzed = AnalyzedQuery(parsed, schema=...)
    columns = result_columns(analyzed.annotated(), param_oids, names=analyzed.column_names())

Two orderings that module keeps, and that anything reaching past it would have to
keep too. Sizing runs before annotation or the width never reaches the columns
derived from it -- and it widens the literal rather than the OID afterwards, so
that `3000000000 + 0` is a bigint sum rather than an int4 one. Names are read off
the query as written, before qualify() rewrites the select list out from under
the question (see `written_column_names`).

`oid_for_declared_type` runs the other direction of the same round trip: the
`schema()` type name a column was declared as, back to the OID it means.
`pg_mimic.types.oid_for_type` is its neighbour, taking a Python type instead.

Low in the layering by design, importing only `arrays`, `catalog_data`, `errors`,
`results` and `types` -- none of which imports a sibling but `types`, and that
one only `arrays` -- so that `tables`, `catalog` and `middleware` can all depend
on it without a cycle, and so can a session written outside the package.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlglot import exp

from . import types as pg_types
from .arrays import ARRAY_OID, element_oid_of, is_array_oid
from .catalog_data import DECLARED_TYPE_OIDS
from .errors import (
    FEATURE_NOT_SUPPORTED,
    INDETERMINATE_DATATYPE,
    SYNTAX_ERROR,
    PgError,
)
from .results import ResultColumn
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

# (OID, the name Session.schema()/psql show, sqlglot's spelling of it, pg_type.typname).
# One table rather than four dicts because these have to agree: a declared type is
# handed to sqlglot by its spelling and comes back out of the annotator, and that
# round trip is only lossless if both directions read the same row here.
#
# typname is the last column because it is a *different* spelling from the second:
# Postgres shows `integer` in a schema but names an unnameable cast `int4`, so
# `SELECT 1::int` is a column called int4 rather than integer (#111).
_TYPES: tuple[tuple[int, str, str, str], ...] = (
    (BOOL, "boolean", "BOOLEAN", "bool"),
    (INT2, "smallint", "SMALLINT", "int2"),
    (INT4, "integer", "INT", "int4"),
    (INT8, "bigint", "BIGINT", "int8"),
    (FLOAT4, "real", "FLOAT", "float4"),
    (FLOAT8, "double precision", "DOUBLE", "float8"),
    (NUMERIC, "numeric", "DECIMAL", "numeric"),
    (TEXT, "text", "TEXT", "text"),
    (VARCHAR, "character varying", "VARCHAR", "varchar"),
    (BPCHAR, "character", "CHAR", "bpchar"),
    (BYTEA, "bytea", "VARBINARY", "bytea"),
    (DATE, "date", "DATE", "date"),
    (TIME, "time", "TIME", "time"),
    (TIMESTAMP, "timestamp", "TIMESTAMP", "timestamp"),
    (TIMESTAMPTZ, "timestamptz", "TIMESTAMPTZ", "timestamptz"),
    (INTERVAL, "interval", "INTERVAL", "interval"),
    (UUID, "uuid", "UUID", "uuid"),
    (JSON, "json", "JSON", "json"),
    (JSONB, "jsonb", "JSONB", "jsonb"),
)

_PG_NAME = {oid: pg_name for oid, pg_name, _, _ in _TYPES}
_SQLGLOT_NAME = {oid: sqlglot_name for oid, _, sqlglot_name, _ in _TYPES}
_TYPNAME = {oid: typname for oid, _, _, typname in _TYPES}
_OID_FOR_SQLGLOT_TYPE = {exp.DataType.build(sqlglot_name).this: oid for oid, _, sqlglot_name, _ in _TYPES}

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

# The widths Postgres sizes an integer constant into: integer, then bigint, then
# numeric. Signed, so that -2147483648 is an integer though 2147483648 is not.
_INT4_RANGE = (-(2**31), 2**31 - 1)
_INT8_RANGE = (-(2**63), 2**63 - 1)

# What Postgres calls an output column it cannot name from the query.
_UNNAMED = "?column?"


# --- declared type names ---------------------------------------------------------------


def oid_for_declared_type(declared: str) -> int:
    """Session.schema() declares types as free text ("integer", "text[]"), so map the
    common spellings onto real OIDs and fall back to text for anything else.

    The counterpart to `pg_mimic.types.oid_for_type`, which answers the same
    question about a Python type. Public because every session declaring a schema
    needs it -- it is how a declared column becomes something describe() can report
    -- and because the alternative was each of them carrying its own half-right
    copy (#89).
    """
    name = str(declared).strip().lower()
    # A trailing `[]` says array, and how many of them says nothing else: Postgres
    # has one array type per element type however many dimensions the declaration
    # spells, so `text[][]` is the same `_text` as `text[]`. Strip them all and
    # resolve the element once.
    element = name
    while element.endswith("[]"):
        element = element[:-2].rstrip()

    type_name = DECLARED_TYPE_OIDS.get(element)
    oid = getattr(pg_types, type_name) if type_name is not None else oid_for_type(str)
    if element == name:
        return oid
    # An element with no array type over it is not a case pg_mimic can reach --
    # everything in DECLARED_TYPE_OIDS has one, as does the text fallback -- so the
    # element OID is only here to keep an unknown from becoming a KeyError.
    return ARRAY_OID.get(oid, oid)


def _pg_type_name(oid: int) -> str:
    """The type name `Session.schema()` declares -- one of the spellings
    pg_mimic.catalog_data knows, or an array over one, so the catalog maps it back to
    this same OID (see oid_for_declared_type, and #43 for when it didn't)."""
    if is_array_oid(oid):
        return f"{_pg_type_name(element_oid_of(oid))}[]"
    return _PG_NAME[oid]  # every OID here passed tables._sqlglot_type_name's check first


# --- sizing literals the way Postgres sizes them ---------------------------------------


def size_integer_literals(expression: exp.Expression) -> None:
    """Give each integer constant the width Postgres gives it.

    Postgres sizes one by its value -- `integer` up to 2147483647, `bigint` to
    9223372036854775807, `numeric` past that -- where sqlglot's annotator types
    every one of them INT. Left alone, `select 3000000000` is described as int4 and
    a binary client refuses to decode it: asyncpg raises "'i' format requires
    -2147483648 <= number <= 2147483647", and psycopg only hides it by reading
    results as text.

    Widening the literal itself rather than the OID afterwards is what makes the
    width carry: `3000000000 + 0` is then bigint, as Postgres has it, rather than
    an int4 sum of an int4. That only works if this runs *before* annotate_types --
    the annotator is what reads the width back off the widened literal.

    Only expression literals -- a declared `integer` column still describes as int4,
    which is the round trip _TYPES exists to keep lossless.
    """
    for literal in list(expression.find_all(exp.Literal)):
        if not literal.is_int:
            continue
        # A negation is sized by what it evaluates to, so the outermost one is the
        # node to wrap: Postgres calls -2147483648 an integer, though 2147483648 on
        # its own is a bigint.
        node, value = literal, int(literal.name)
        while isinstance(node.parent, exp.Neg):
            node, value = node.parent, -value
        if isinstance(node.parent, (exp.Limit, exp.Offset)):
            # A row window is read back off the tree as a count by the caller, which
            # wants the literal it was handed and has no client to describe to.
            continue
        if _INT4_RANGE[0] <= value <= _INT4_RANGE[1]:
            continue
        wider = "BIGINT" if _INT8_RANGE[0] <= value <= _INT8_RANGE[1] else "DECIMAL"
        node.replace(exp.Cast(this=node.copy(), to=exp.DataType.build(wider)))


# --- the annotated query's output columns ----------------------------------------------


def result_columns(expression: exp.Query, param_oids: list[int | None], *, names: Sequence[str]) -> list[ResultColumn]:
    """The columns a type-annotated query projects, named and typed as Postgres does.

    `param_oids` is what Parse declared, which is the only type a bare `$1` in the
    select list can have -- the annotator cannot reach it.

    `names` is `AnalyzedQuery.column_names()`, decided on the query as written,
    which is where Postgres decides them too. Required rather than defaulted:
    reading them back off this tree instead is wrong for any column the query wrote
    a literal for, and wrong quietly -- it answers `3000000000` where Postgres
    answers `?column?`. A caller that has no names has the wrong tree, not a
    naming problem.

    It names the *leading* columns and may be shorter than the select list, which
    is why the zip truncates on purpose. A DISTINCT ON key the query did not select
    is appended to the select list so the rows carry something to deduplicate on,
    after these names were decided and for the executor rather than the client --
    see `tables._Plan.visible_columns`, which trims the same columns off the rows.
    """
    return [ResultColumn(name, _column_oid(select, param_oids)) for name, select in zip(names, expression.selects, strict=False)]


def written_column_names(expression: exp.Query) -> dict[int, str]:
    """What Postgres calls each output column, decided on the query as written.

    Postgres names columns during parse analysis, before anything is resolved. Read
    later than that the question is unanswerable: qualify() names an unaliased
    literal after its own text, so `SELECT 'a'` and `SELECT 'a' AS a` become the
    same tree, and the widening in `size_integer_literals` then hides the literal
    behind a cast that is ours rather than the query's.

    Keyed by node identity because qualify() wraps each projection in a *new* Alias
    while keeping the node underneath, which is how `resolve_column_names` matches
    these back up. So this must run before qualify(), and be resolved before any
    pass that replaces a projection -- `size_integer_literals` does.
    """
    return {id(select): _written_name(select) for select in expression.selects}


def resolve_column_names(qualified: exp.Query, written: dict[int, str]) -> list[str]:
    """One name per output column, pairing `written_column_names` with the qualified tree.

    A projection the query wrote keeps the name decided there. One qualify() made --
    a column that `*` expanded into, or a key added to the select list -- is not in
    the map and is named from this tree, which is already what Postgres calls it.
    Pairing by identity rather than by position is what makes `SELECT *, 1` work,
    where the two select lists are different lengths.
    """
    # Not `written.get(id(...), _qualified_name(select))`: a dict default is evaluated
    # whether or not it is needed, so the fallback would run for every column and be
    # thrown away for all but the few it is for.
    names = []
    for select in qualified.selects:
        name = written.get(id(select.unalias()))
        names.append(_qualified_name(select) if name is None else name)
    return names


def _written_name(select: exp.Expression) -> str:
    """Postgres's rule, on the tree as written: an alias if the query gave one, else
    a name derived from the expression, else `?column?`."""
    return select.alias if isinstance(select, exp.Alias) else _implicit_name(select)


def _implicit_name(node: exp.Expression) -> str:
    """The name an unaliased expression gives itself. Postgres recurses here rather
    than looking the node up: a cast is named after its operand, and only falls back
    to its target type when the operand has no name of its own."""
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Column):
        return node.name
    if isinstance(node, exp.Cast):
        operand = _implicit_name(node.this)
        if operand != _UNNAMED:
            return operand
        # A sub-select operand stops here rather than taking the type name, which is
        # why `(SELECT 1)::text` is ?column? where `1::text` is text.
        if isinstance(node.this, (exp.Subquery, exp.Select)):
            return _UNNAMED
        # typname, not the declared spelling: `1::int` is int4, not integer.
        return _TYPNAME.get(_oid_for_sqlglot_type(node.to), _UNNAMED)
    if isinstance(node, exp.Anonymous):
        return str(node.this).lower()
    if isinstance(node, exp.Func):
        return node.sql_name().lower()
    return _UNNAMED


def _qualified_name(select: exp.Expression) -> str:
    """A column qualify() produced rather than the query -- one that `*` expanded into.

    Its own name is already what Postgres calls it, apart from the `_col_N` qualify()
    labels the unnameable with. `SELECT * FROM (SELECT 1 + 1 FROM t) x` expands to a
    column reference *named* `_col_0`, and that label is qualify's bookkeeping rather
    than a name to pass on to a client -- Postgres calls it `?column?`.
    """
    name = select.alias_or_name
    if name and not name.startswith("_col_"):
        return name
    inner = select.unalias()
    # Only a reference to one of those labels; anything else still names itself.
    return _UNNAMED if isinstance(inner, exp.Column) else _implicit_name(inner)


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
    # Unattributed to any one session: every schema-declaring session derives its
    # columns through here, so naming one of them in the message would be wrong for
    # the rest.
    raise PgError(
        FEATURE_NOT_SUPPORTED,
        f"could not derive a Postgres type for the output column {expression_sql!r}. Column types come from the "
        f"declared schema, not from the rows a query happens to return, so an expression sqlglot cannot type has "
        f"none -- name it with a cast, e.g. {expression_sql}::text.",
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
