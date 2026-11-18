"""Making psql's catalog SQL executable by sqlglot.

Everything here is about how psql *writes* SQL, not what it asks for: the queries
are ordinary joins over pg_catalog, and what defeats the executor is the spelling.
Each rewrite below notes the failure it prevents -- all found by running the real
binary and reading what came back.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlglot import exp
from sqlglot.executor import env as sqlglot_env

from .catalog_data import INTEGER_COLUMNS

if TYPE_CHECKING:
    from .connection import Connection


# sqlglot's Python executor knows the `~` operator's node but has no
# implementation for it, and psql filters on `nspname !~ '^pg_'` constantly.
def _array_length(this: Any, *_: Any) -> Any:
    """NULL for a NULL array, as in Postgres -- an empty one has length NULL too."""
    if this is None:
        return None
    return len(this) or None


def _regexp_like(this: Any, expression: Any, *_: Any) -> Any:
    if this is None or expression is None:
        return None
    return bool(re.search(str(expression), str(this)))


sqlglot_env.ENV.setdefault("REGEXPLIKE", _regexp_like)
# array_length(anyarray, int) -- psql's \\l asks it of datacl to decide whether to
# print "(none)" for access privileges. Without it the whole database list comes
# back empty, which is a lie: pg_mimic does know its own database.
sqlglot_env.ENV.setdefault("ARRAYSIZE", _array_length)


_CATALOG_FUNCTIONS = {
    # Ownership isn't modelled, so every object belongs to the connected user.
    "pg_get_userbyid": lambda connection: exp.Literal.string(connection.username),
    # Everything pg_mimic exposes lives in `public`, which is always on the path.
    "pg_table_is_visible": lambda connection: exp.true(),
    "pg_type_is_visible": lambda connection: exp.true(),
    "pg_function_is_visible": lambda connection: exp.true(),
    # pg_get_indexdef and pg_get_constraintdef are *not* here: their answer depends on
    # the row, so they are rewritten into a reference to the column carrying it, the
    # same way format_type is. No column defaults to describe.
    "pg_get_expr": lambda connection: exp.Literal.string(""),
    "pg_get_partkeydef": lambda connection: exp.null(),
    "pg_get_viewdef": lambda connection: exp.Literal.string(""),
    "pg_relation_size": lambda connection: exp.Literal.number(0),
    "pg_total_relation_size": lambda connection: exp.Literal.number(0),
    "pg_size_pretty": lambda connection: exp.Literal.string("0 bytes"),
    "pg_encoding_to_char": lambda connection: exp.Literal.string("UTF8"),
    "array_to_string": lambda connection: exp.Literal.string(""),
    "obj_description": lambda connection: exp.null(),
    "col_description": lambda connection: exp.null(),
    "shobj_description": lambda connection: exp.null(),
}

_REGEX_OPERATORS = {"~", "!~", "~*", "!~*"}

# Catalog functions whose answer depends on the row rather than being constant, and the
# column each one's answer travels on. See the rewrite in rewrite_for_executor.
_ROW_DEPENDENT_FUNCTIONS = {"pg_get_indexdef": "indexdef", "pg_get_constraintdef": "condef"}

# Catalog tables pg_mimic never has rows for: there are no column defaults and no
# non-default collations. pg_index and pg_constraint left this set in #127 -- a declared
# primary key puts rows in both, and psql reads them in a JOIN rather than in one of the
# correlated subqueries this substitution exists for.
_ALWAYS_EMPTY_TABLES = {
    "pg_attrdef",
    "pg_collation",
    "pg_inherits",
    "pg_rewrite",
    "pg_trigger",
    "pg_policy",
    "pg_statistic_ext",
}


# Columns the synthesised catalog stores as integers. psql writes OIDs as string
# literals (`WHERE c.oid = '16384'`) and lets Postgres coerce them; sqlglot's
# executor compares 16384 to "16384" and finds them different.
def rewrite_for_executor(connection: Connection, expr: exp.Expression) -> exp.Expression:
    """Rewrite psql's catalog SQL into something sqlglot's executor can run.

    Four things it can't take as written:

    - `pg_catalog.pg_get_userbyid(...)` parses as Dot(pg_catalog, Anonymous(...)),
      so the whole Dot has to be replaced. Replacing only the inner function leaves
      `pg_catalog.'postgres'` behind, which fails much later and misleadingly.
    - `x OPERATOR(pg_catalog.~) 'pat'` is psql's schema-qualified operator spelling;
      the executor only knows the node a plain `x ~ 'pat'` produces.
    - `COLLATE pg_catalog.default` makes the optimizer try to resolve `default` as
      a column of a table called `pg_catalog`.
    - Catalog functions pg_mimic has no data for, answered with a fixed value.
    """
    expr = expr.copy()

    for node in list(expr.find_all(exp.Dot)):
        inner = node.expression
        if isinstance(inner, exp.Anonymous):
            replacement = _CATALOG_FUNCTIONS.get(str(inner.this).lower())
            if replacement is not None:
                node.replace(replacement(connection))
                continue
            # `pg_catalog.array_length(x, 1)` stays an Anonymous inside the Dot, so
            # it generates a bare lowercase `array_length(...)` the executor has no
            # name for -- where the unqualified spelling would have parsed to
            # exp.ArraySize and generated ARRAYSIZE. Rebuild the node psql would
            # have got had it not qualified the call. Without this the whole of
            # \l comes back empty.
            if str(inner.this).lower() in ("array_length", "array_upper") and inner.expressions:
                node.replace(exp.ArraySize(this=inner.expressions[0]))

    for node in list(expr.find_all(exp.Anonymous)):
        replacement = _CATALOG_FUNCTIONS.get(str(node.this).lower())
        if replacement is not None:
            node.replace(replacement(connection))

    for node in list(expr.find_all(exp.Operator)):
        operator = str(node.args.get("operator", "")).split(".")[-1]
        if operator in _REGEX_OPERATORS:
            like = exp.RegexpLike(this=node.this, expression=node.expression)
            node.replace(exp.Not(this=like) if operator.startswith("!") else like)

    for node in list(expr.find_all(exp.Collate)):
        node.replace(node.this)

    # `x::pg_catalog.regtype` and friends: a schema-qualified type name compiles to
    # invalid Python, and the reg* types render an OID as a name, which needs a
    # catalog lookup pg_mimic has no reason to model. Drop the cast and keep the
    # value -- these appear in branches psql does not display for our data.
    # format_type(a.atttypid, a.atttypmod) renders a column's type name, which
    # depends on the row, so it cannot be answered with a constant like the other
    # catalog functions. The rendered name travels on pg_attribute instead, and the
    # call becomes a reference to it.
    for node in list(expr.find_all(exp.Anonymous)):
        if str(node.this).lower() != "format_type" or not node.expressions:
            continue
        first = node.expressions[0]
        if not isinstance(first, exp.Column):
            continue
        # Which column carries the rendered name depends on where the OID came
        # from. psql asks this of pg_attribute for \d (a column's type) and of
        # pg_type for \dT (the type itself), and the answer lives in a different
        # place each time -- rewriting both to attformattype left \dT selecting a
        # pg_attribute column from pg_type, so the whole type listing came back
        # empty.
        rendered = "attformattype" if first.name.lower() == "atttypid" else "typname"
        column = exp.column(rendered, table=first.table)
        # Qualified as `pg_catalog.format_type(...)` the call is wrapped in a Dot,
        # and replacing only the inner function leaves `pg_catalog.<column>` behind
        # -- which fails later, in the Sort step, a long way from here.
        parent = node.parent
        (parent if isinstance(parent, exp.Dot) else node).replace(column)

    # pg_get_indexdef(i.indexrelid, 0, true) and pg_get_constraintdef(con.oid, true)
    # depend on the row for the same reason format_type does, so they get the same
    # treatment: the rendered text travels on the row and the call becomes a reference
    # to it. psql echoes everything after " USING " in the indexdef straight into the
    # `Indexes:` footer, which is where `btree (sha)` comes from -- a constant "" there
    # would render an index with no columns.
    for node in list(expr.find_all(exp.Anonymous)):
        rendered = _ROW_DEPENDENT_FUNCTIONS.get(str(node.this).lower())
        if rendered is None or not node.expressions:
            continue
        first = node.expressions[0]
        if not isinstance(first, exp.Column):
            continue
        # The argument says which relation the OID came from, so the replacement carries
        # the same qualifier -- `con.oid` becomes `con.condef`. An unqualified `oid`
        # becomes an unqualified `condef`, which qualify() then resolves, since only
        # pg_constraint has that column.
        column = exp.column(rendered, table=first.table)
        parent = node.parent
        (parent if isinstance(parent, exp.Dot) else node).replace(column)

    # psql reads column defaults and collations with correlated scalar subqueries,
    # which sqlglot's executor cannot run at all. Both select from tables that are
    # always empty here -- pg_mimic models neither -- so the answer is NULL either
    # way, and saying so directly is the only way to get the rest of the row.
    for node in list(expr.find_all(exp.Subquery)):
        tables = {table.name.lower() for table in node.find_all(exp.Table)}
        # Any empty table is enough: psql cross-joins them (`FROM pg_collation c,
        # pg_type t`), and a cross join with an empty side has no rows. Requiring
        # every table to be empty missed that, and the subquery then filtered away
        # every row of the outer query rather than yielding NULL.
        if tables & _ALWAYS_EMPTY_TABLES:
            node.replace(exp.null())

    for predicate in list(expr.find_all(exp.Binary)):
        for column, literal in ((predicate.this, predicate.expression), (predicate.expression, predicate.this)):
            if (
                isinstance(column, exp.Column)
                and column.name in INTEGER_COLUMNS
                and isinstance(literal, exp.Literal)
                and literal.is_string
                and str(literal.this).lstrip("-").isdigit()
            ):
                literal.replace(exp.Literal.number(str(literal.this)))

    for node in list(expr.find_all(exp.Cast)):
        target = node.to.sql(dialect="postgres").lower() if node.to else ""
        if "." in target or target.startswith("reg"):
            node.replace(node.this)

    return expr
