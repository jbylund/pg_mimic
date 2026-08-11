"""Catalog emulation -- `information_schema` and `pg_catalog` -- built from
whatever `Session.schema()` declares.

A session author describes their tables once, declaratively, and the catalog
answers the introspection SQL that clients, ORMs and `psql` issue on their
behalf. This is the shape the whole middleware chain follows (see
pg_mimic.middleware): the session supplies data, the chain supplies SQL
compatibility.

Both are evaluated with sqlglot's executor against a synthesised in-memory
schema, so ordinary WHERE/ORDER BY/JOIN over those tables works without pg_mimic
implementing any of it. pg_catalog additionally needs `_rewrite_for_executor`,
because psql writes SQL the executor doesn't handle as-is -- see there.

What this deliberately does not attempt: asyncpg's type introspection, which is a
recursive CTE that sqlglot can neither parse nor execute (its executor has no
recursive CTE support at all). That needs answering directly rather than by
executing it, and is a separate problem.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from sqlglot import exp
from sqlglot.executor import env as sqlglot_env
from sqlglot.executor import execute as sqlglot_execute

from .results import ResultColumn
from .session import Statement, StaticStatement
from .types import TEXT, oid_for_type

if TYPE_CHECKING:
    from .connection import Connection


# sqlglot's Python executor knows the `~` operator's node but has no
# implementation for it, and psql filters on `nspname !~ '^pg_'` constantly.
def _regexp_like(this: Any, expression: Any, *_: Any) -> Any:
    if this is None or expression is None:
        return None
    return bool(re.search(str(expression), str(this)))


sqlglot_env.ENV.setdefault("REGEXPLIKE", _regexp_like)


# --- synthesised tables --------------------------------------------------------------

_PUBLIC_OID = 2200
_OWNER_OID = 10
_HEAP_AM_OID = 2
# Real Postgres starts user objects here, and psql only cares that they're distinct.
_FIRST_USER_OID = 16384


def _build_information_schema(user_schema: dict) -> tuple[dict, dict]:
    """user_schema: {table_name: {col_name: type_str}} (Session.schema()'s shape)."""
    tables_rows = []
    columns_rows = []
    for table_name, cols in user_schema.items():
        tables_rows.append(
            {
                "table_catalog": "pg_mimic",
                "table_schema": "public",
                "table_name": table_name,
                "table_type": "BASE TABLE",
            }
        )
        for i, (col_name, col_type) in enumerate(cols.items(), start=1):
            columns_rows.append(
                {
                    "table_catalog": "pg_mimic",
                    "table_schema": "public",
                    "table_name": table_name,
                    "column_name": col_name,
                    "ordinal_position": i,
                    "data_type": col_type,
                    "is_nullable": "YES",
                }
            )

    schema = {
        "information_schema": {
            "tables": {
                "table_catalog": "TEXT",
                "table_schema": "TEXT",
                "table_name": "TEXT",
                "table_type": "TEXT",
            },
            "columns": {
                "table_catalog": "TEXT",
                "table_schema": "TEXT",
                "table_name": "TEXT",
                "column_name": "TEXT",
                "ordinal_position": "INT",
                "data_type": "TEXT",
                "is_nullable": "TEXT",
            },
        }
    }
    tables = {"information_schema": {"tables": tables_rows, "columns": columns_rows}}
    return schema, tables


_PG_CATALOG_SCHEMA = {
    "pg_catalog": {
        "pg_namespace": {"oid": "INT", "nspname": "TEXT", "nspowner": "INT"},
        "pg_class": {
            "oid": "INT",
            "relname": "TEXT",
            "relnamespace": "INT",
            "relkind": "TEXT",
            "relowner": "INT",
            "relam": "INT",
            "reltuples": "INT",
            "relpages": "INT",
            "relhasindex": "BOOLEAN",
            "relpersistence": "TEXT",
            "reltablespace": "INT",
            "relispartition": "BOOLEAN",
            "relrowsecurity": "BOOLEAN",
            "relforcerowsecurity": "BOOLEAN",
            "relreplident": "TEXT",
            "reloftype": "INT",
            "relhastriggers": "BOOLEAN",
            "relchecks": "INT",
            "relhasrules": "BOOLEAN",
            "reltoastrelid": "INT",
        },
        "pg_am": {"oid": "INT", "amname": "TEXT", "amhandler": "TEXT", "amtype": "TEXT"},
        "pg_attribute": {
            "attrelid": "INT",
            "attname": "TEXT",
            "atttypid": "INT",
            "attnum": "INT",
            "attnotnull": "BOOLEAN",
            "atthasdef": "BOOLEAN",
            "attisdropped": "BOOLEAN",
            "attidentity": "TEXT",
            "attgenerated": "TEXT",
            "attcollation": "INT",
            "atttypmod": "INT",
            "attstattarget": "INT",
            "attformattype": "TEXT",
        },
        # Declared but always empty: pg_mimic models no defaults, collations,
        # indexes, constraints, triggers or inheritance. They still need their
        # columns declared, or a query selecting from them fails to resolve rather
        # than returning the no rows it should.
        "pg_attrdef": {"adrelid": "INT", "adnum": "INT", "adbin": "TEXT"},
        "pg_constraint": {
            "oid": "INT",
            "conname": "TEXT",
            "conrelid": "INT",
            "contype": "TEXT",
            "conparentid": "INT",
            "conindid": "INT",
            "confrelid": "INT",
            "condeferrable": "BOOLEAN",
            "condeferred": "BOOLEAN",
            "convalidated": "BOOLEAN",
        },
        "pg_index": {
            "indexrelid": "INT",
            "indrelid": "INT",
            "indisprimary": "BOOLEAN",
            "indisunique": "BOOLEAN",
            "indisclustered": "BOOLEAN",
            "indisvalid": "BOOLEAN",
            "indisreplident": "BOOLEAN",
            "indkey": "TEXT",
        },
        "pg_inherits": {"inhrelid": "INT", "inhparent": "INT", "inhseqno": "INT"},
        "pg_rewrite": {"oid": "INT", "rulename": "TEXT", "ev_class": "INT", "ev_type": "TEXT", "is_instead": "BOOLEAN"},
        "pg_trigger": {
            "oid": "INT",
            "tgname": "TEXT",
            "tgrelid": "INT",
            "tgenabled": "TEXT",
            "tgisinternal": "BOOLEAN",
            "tgconstraint": "INT",
        },
        "pg_policy": {
            "oid": "INT",
            "polname": "TEXT",
            "polrelid": "INT",
            "polcmd": "TEXT",
            "polpermissive": "BOOLEAN",
            "polqual": "TEXT",
            "polwithcheck": "TEXT",
            "polroles": "TEXT",
        },
        "pg_roles": {"oid": "INT", "rolname": "TEXT"},
        "pg_statistic_ext": {"oid": "INT", "stxname": "TEXT", "stxrelid": "INT", "stxnamespace": "INT"},
        "pg_collation": {"oid": "INT", "collname": "TEXT"},
        "pg_type": {
            "oid": "INT",
            "typcollation": "INT",
            "typname": "TEXT",
            "typnamespace": "INT",
            "typtype": "TEXT",
            "typelem": "INT",
            "typbasetype": "INT",
            "typrelid": "INT",
            "typcategory": "TEXT",
            "typdelim": "TEXT",
        },
    }
}


def _pg_type_rows() -> list[dict]:
    """Every scalar pg_mimic can name, plus its array type.

    Built from the same OID tables the wire codecs use, so a type pg_mimic can
    encode is a type the catalog can describe -- rather than a second list that
    drifts from the first.
    """
    from . import types as pg_types
    from .arrays import ARRAY_OID

    rows = []
    seen = set()
    for name in dir(pg_types):
        oid = getattr(pg_types, name)
        if not name.isupper() or not isinstance(oid, int) or oid in seen:
            continue
        seen.add(oid)
        rows.append(
            {
                "oid": oid,
                "typname": name.lower(),
                "typnamespace": 11,  # pg_catalog
                "typtype": "b",  # base type
                "typelem": 0,
                "typbasetype": 0,
                "typrelid": 0,
                "typcategory": "U",
                "typdelim": ",",
                "typcollation": 0,
            }
        )
    for element_oid, array_oid in ARRAY_OID.items():
        if array_oid in seen:
            continue
        seen.add(array_oid)
        rows.append(
            {
                "oid": array_oid,
                "typname": f"_{_typname_for(rows, element_oid)}",
                "typnamespace": 11,
                "typtype": "b",
                "typelem": element_oid,  # what makes it an array
                "typbasetype": 0,
                "typrelid": 0,
                "typcategory": "A",
                "typdelim": ",",
                "typcollation": 0,
            }
        )
    return rows


def pg_type_by_oid() -> dict[int, dict]:
    """The synthesised pg_type rows, keyed by OID.

    Shared with pg_mimic.typeinfo, so the types asyncpg is told about are the same
    ones psql sees and the same ones the wire codecs can actually handle.
    """
    return {row["oid"]: row for row in _pg_type_rows()}


def _typname_for(rows: list[dict], oid: int) -> str:
    for row in rows:
        if row["oid"] == oid:
            return row["typname"]
    return "unknown"


def _build_pg_catalog(user_schema: dict) -> tuple[dict, dict]:
    """The slice of pg_catalog psql's \\d family reads, from Session.schema()."""
    class_rows = []
    attribute_rows = []
    for index, (table_name, cols) in enumerate(user_schema.items()):
        table_oid = _FIRST_USER_OID + index
        class_rows.append(
            {
                "oid": table_oid,
                "relname": table_name,
                "relnamespace": _PUBLIC_OID,
                "relkind": "r",  # ordinary table
                "relowner": _OWNER_OID,
                "relam": _HEAP_AM_OID,
                "reltuples": -1,  # never analysed
                "relpages": 0,
                "relhasindex": False,
                "relpersistence": "p",
                "reltablespace": 0,
                "relispartition": False,
                "relrowsecurity": False,
                "relforcerowsecurity": False,
                "relreplident": "d",
                "reloftype": 0,
                "relhastriggers": False,
                "relchecks": 0,
                "relhasrules": False,
                "reltoastrelid": 0,
            }
        )
        for position, (col_name, col_type) in enumerate(cols.items(), start=1):
            attribute_rows.append(
                {
                    "attrelid": table_oid,
                    "attname": col_name,
                    "atttypid": _oid_for_declared_type(col_type),
                    "attnum": position,
                    "attnotnull": False,
                    "atthasdef": False,
                    "attisdropped": False,
                    "attidentity": "",
                    "attgenerated": "",
                    "attcollation": 0,
                    "atttypmod": -1,
                    "attstattarget": -1,
                    "attformattype": str(col_type),
                }
            )

    tables = {
        "pg_catalog": {
            "pg_namespace": [
                {"oid": _PUBLIC_OID, "nspname": "public", "nspowner": _OWNER_OID},
                {"oid": 11, "nspname": "pg_catalog", "nspowner": _OWNER_OID},
                {"oid": 13000, "nspname": "information_schema", "nspowner": _OWNER_OID},
            ],
            "pg_class": class_rows,
            "pg_am": [{"oid": _HEAP_AM_OID, "amname": "heap", "amhandler": "heap_tableam_handler", "amtype": "t"}],
            "pg_attribute": attribute_rows,
            "pg_type": _pg_type_rows(),
            "pg_attrdef": [],
            "pg_collation": [],
            "pg_constraint": [],
            "pg_index": [],
            "pg_inherits": [],
            "pg_rewrite": [],
            "pg_trigger": [],
            "pg_policy": [],
            "pg_statistic_ext": [],
            "pg_roles": [{"oid": _OWNER_OID, "rolname": "postgres"}],
        }
    }
    return _PG_CATALOG_SCHEMA, tables


_DECLARED_TYPE_OIDS = {
    "integer": "INT4",
    "int": "INT4",
    "int4": "INT4",
    "bigint": "INT8",
    "int8": "INT8",
    "smallint": "INT2",
    "int2": "INT2",
    "text": "TEXT",
    "varchar": "VARCHAR",
    "character varying": "VARCHAR",
    "boolean": "BOOL",
    "bool": "BOOL",
    "real": "FLOAT4",
    "double precision": "FLOAT8",
    "numeric": "NUMERIC",
    "date": "DATE",
    "time": "TIME",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMPTZ",
    "interval": "INTERVAL",
    "uuid": "UUID",
    "json": "JSON",
    "jsonb": "JSONB",
    "bytea": "BYTEA",
}


def _oid_for_declared_type(declared: str) -> int:
    """Session.schema() declares types as free text ("integer", "text"), so map the
    common spellings onto real OIDs and fall back to text for anything else."""
    from . import types as pg_types

    name = _DECLARED_TYPE_OIDS.get(str(declared).strip().lower())
    if name is not None:
        return getattr(pg_types, name)
    return oid_for_type(str)


# --- making psql's SQL executable ----------------------------------------------------

_CATALOG_FUNCTIONS = {
    # Ownership isn't modelled, so every object belongs to the connected user.
    "pg_get_userbyid": lambda connection: exp.Literal.string(connection.username),
    # Everything pg_mimic exposes lives in `public`, which is always on the path.
    "pg_table_is_visible": lambda connection: exp.true(),
    "pg_type_is_visible": lambda connection: exp.true(),
    "pg_function_is_visible": lambda connection: exp.true(),
    # No indexes, constraints or defaults to describe.
    "pg_get_indexdef": lambda connection: exp.Literal.string(""),
    "pg_get_constraintdef": lambda connection: exp.Literal.string(""),
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

# Catalog tables pg_mimic never has rows for: there are no column defaults, no
# non-default collations, and no indexes or constraints to describe.
_ALWAYS_EMPTY_TABLES = {
    "pg_attrdef",
    "pg_collation",
    "pg_constraint",
    "pg_index",
    "pg_inherits",
    "pg_rewrite",
    "pg_trigger",
    "pg_policy",
    "pg_statistic_ext",
}

# Columns the synthesised catalog stores as integers. psql writes OIDs as string
# literals (`WHERE c.oid = '16384'`) and lets Postgres coerce them; sqlglot's
# executor compares 16384 to "16384" and finds them different.
_INT_CATALOG_COLUMNS = {
    column for table in _PG_CATALOG_SCHEMA["pg_catalog"].values() for column, column_type in table.items() if column_type == "INT"
}


def _rewrite_for_executor(connection: Connection, expr: exp.Expression) -> exp.Expression:
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
        column = exp.column("attformattype", table=first.table)
        # Qualified as `pg_catalog.format_type(...)` the call is wrapped in a Dot,
        # and replacing only the inner function leaves `pg_catalog.<column>` behind
        # -- which fails later, in the Sort step, a long way from here.
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
                and column.name in _INT_CATALOG_COLUMNS
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


# --- the statements the middleware hands back ----------------------------------------


def _empty_result(expr: exp.Expression) -> Statement | None:
    """An answer of no rows, with the columns the query asked for.

    Used when a pg_catalog query can't be executed. Falling through to the session
    would be worse than useless: it cannot answer catalog SQL either, and whatever
    shape it does return gets read as the catalog's answer -- psql reports that as
    "column number N is out of range", a long way from the cause.

    Empty is also usually the truthful answer. What defeats the executor here is
    psql's footer sections -- constraints, statistics, partitions -- and pg_mimic
    genuinely has none of those to report.
    """
    selects = getattr(expr, "selects", None)
    if not selects:
        return None
    columns = [ResultColumn(select.alias_or_name or f"column{i}", TEXT) for i, select in enumerate(selects, start=1)]
    return StaticStatement(expr.sql(dialect="postgres"), columns, [])


def _as_statement(expr: exp.Expression, schema: dict, tables: dict) -> Statement | None:
    try:
        result = sqlglot_execute(expr, schema=schema, tables=tables, dialect="postgres")
    except Exception:
        return _empty_result(expr)

    rows = [tuple(row) for row in result.rows]
    if rows:
        columns = [ResultColumn.for_type(name, type(value)) for name, value in zip(result.columns, rows[0])]
    else:
        columns = [ResultColumn(name, TEXT) for name in result.columns]
    return StaticStatement(expr.sql(dialect="postgres"), columns, rows)


async def _user_schema(connection: Connection) -> dict:
    schema_fn = getattr(connection.session, "schema", None)
    return (await schema_fn()) if schema_fn is not None else {} or {}


async def information_schema_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema, tables = _build_information_schema(await _user_schema(connection))
    return _as_statement(expr, schema, tables)


async def pg_catalog_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema, tables = _build_pg_catalog(await _user_schema(connection))
    return _as_statement(_rewrite_for_executor(connection, expr), schema, tables)
