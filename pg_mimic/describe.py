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

The recipe, and the order matters::

    size_integer_literals(qualified)                    # *before* the annotator
    annotated = annotate_types(qualified, schema=..., dialect="postgres")
    columns = result_columns(annotated, param_oids)

Sizing has to run before annotation or the width never reaches the columns
derived from it -- and it widens the literal rather than the OID afterwards, so
that `3000000000 + 0` is a bigint sum rather than an int4 one.

`oid_for_declared_type` runs the other direction of the same round trip: the
`schema()` type name a column was declared as, back to the OID it means.
`pg_mimic.types.oid_for_type` is its neighbour, taking a Python type instead.

Low in the layering by design, importing only `arrays`, `catalog_data`, `errors`,
`results` and `types` -- none of which imports a sibling but `types`, and that
one only `arrays` -- so that `tables`, `catalog` and `middleware` can all depend
on it without a cycle, and so can a session written outside the package.
"""

from __future__ import annotations

from sqlglot import exp

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

# The widths Postgres sizes an integer constant into: integer, then bigint, then
# numeric. Signed, so that -2147483648 is an integer though 2147483648 is not.
_INT4_RANGE = (-(2**31), 2**31 - 1)
_INT8_RANGE = (-(2**63), 2**63 - 1)


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
    from . import types as pg_types

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


def result_columns(expression: exp.Query, param_oids: list[int | None]) -> list[ResultColumn]:
    """The columns a type-annotated query projects, named and typed as Postgres does.

    `param_oids` is what Parse declared, which is the only type a bare `$1` in the
    select list can have -- the annotator cannot reach it.
    """
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
