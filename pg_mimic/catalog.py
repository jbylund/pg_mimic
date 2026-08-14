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

import logging
import re
from typing import TYPE_CHECKING

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.executor import execute as sqlglot_execute

from . import types as pg_types
from .analysis import AnalyzedQuery
from .arrays import ARRAY_OID, is_array_oid
from .catalog_data import INFORMATION_SCHEMA_SCHEMA, PG_CATALOG_SCHEMA
from .catalog_rewrite import rewrite_for_executor
from .describe import oid_for_declared_type, result_columns
from .errors import FEATURE_NOT_SUPPORTED, UNDEFINED_COLUMN, PgError
from .results import ResultColumn
from .session import Statement, StaticStatement, statement_from_rows
from .types import (
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
    VARCHAR,
)

if TYPE_CHECKING:
    from .connection import Connection


logger = logging.getLogger(__name__)

# --- synthesised tables --------------------------------------------------------------

_PUBLIC_OID = 2200
_OWNER_OID = 10
_HEAP_AM_OID = 2
# Real Postgres starts user objects here, and psql only cares that they're distinct.
_FIRST_USER_OID = 16384


_CATALOG_NAME = "pg_mimic"

# Postgres' character_octet_length for any character type with no length limit --
# 2^30, the largest a varlena can be. pg_mimic declares no typmods, so every
# character column it serves is that one.
_UNBOUNDED_OCTET_LENGTH = 1073741824

# What information_schema.columns reports about a column *of this type*, as opposed
# to about the column itself. Copied from the server rather than reasoned about --
# these are information_schema's own helper functions, answered for each type at
# typmod -1, which is the typmod pg_mimic gives every column:
#
#   SELECT information_schema._pg_char_max_length(oid, -1),
#          information_schema._pg_char_octet_length(oid, -1),
#          information_schema._pg_numeric_precision(oid, -1),
#          information_schema._pg_numeric_precision_radix(oid, -1),
#          information_schema._pg_numeric_scale(oid, -1),
#          information_schema._pg_datetime_precision(oid, -1)
#   FROM pg_type WHERE typname = ...
#
# Keyed by OID rather than by the declared spelling so `int` and `integer` land on
# the same row, and so an array -- whose OID is in neither this table nor pg_mimic's
# scalar list -- comes out all-NULL, which is what Postgres says of an array column.
#
# character_maximum_length is absent from every entry on purpose: without a typmod
# there is no limit to report, so it is NULL for `character varying` too.
_TYPE_FACTS: dict[int, dict[str, int]] = {
    BPCHAR: {"character_octet_length": _UNBOUNDED_OCTET_LENGTH},
    TEXT: {"character_octet_length": _UNBOUNDED_OCTET_LENGTH},
    VARCHAR: {"character_octet_length": _UNBOUNDED_OCTET_LENGTH},
    # Integers carry a scale of 0; the floats and bare `numeric` carry none.
    INT2: {"numeric_precision": 16, "numeric_precision_radix": 2, "numeric_scale": 0},
    INT4: {"numeric_precision": 32, "numeric_precision_radix": 2, "numeric_scale": 0},
    INT8: {"numeric_precision": 64, "numeric_precision_radix": 2, "numeric_scale": 0},
    FLOAT4: {"numeric_precision": 24, "numeric_precision_radix": 2},
    FLOAT8: {"numeric_precision": 53, "numeric_precision_radix": 2},
    # The one radix-10 type, and the only place the radix is worth anything.
    NUMERIC: {"numeric_precision_radix": 10},
    # A date has whole-second resolution, hence 0; everything else defaults to
    # microseconds, which Postgres reports as 6 rather than as NULL.
    DATE: {"datetime_precision": 0},
    TIME: {"datetime_precision": 6},
    TIMESTAMP: {"datetime_precision": 6},
    TIMESTAMPTZ: {"datetime_precision": 6},
    INTERVAL: {"datetime_precision": 6},
}


def _information_schema_row(view: str, **derived) -> dict:
    """One row of `view`: every column it declares, NULL except what was derived.

    Declaring a column and populating it are one step -- a column in the schema but
    absent from a row is not a NULL, it is a `KeyError` out of the executor, which
    reaches the client as a 0A000. So the declared list is what builds the row and
    `derived` fills in the part a session can actually answer. Regenerating
    information_schema.json against a newer release then adds its new columns as
    NULL rather than as an error; see tests/test_catalog.py's guard.
    """
    row = dict.fromkeys(INFORMATION_SCHEMA_SCHEMA["information_schema"][view])
    unknown = sorted(set(derived) - set(row))
    if unknown:
        raise KeyError(f"information_schema.{view} does not declare {unknown}")
    row.update(derived)
    return row


def _build_information_schema(user_schema: dict) -> tuple[dict, dict]:
    """user_schema: {table_name: {col_name: type_str}} (Session.schema()'s shape).

    The columns filled in below are the ones a declared schema genuinely settles,
    plus the handful Postgres answers with a constant for any ordinary table --
    `is_updatable`, `is_generated` and friends, which read as constants here because
    pg_mimic has no views, no generated columns and no identity sequences to make
    them vary. Everything else is honestly NULL rather than absent, which is the
    difference between a query that answers and one that returns no rows at all.
    """
    type_rows = pg_type_by_oid()
    tables_rows = []
    columns_rows = []
    for table_name, cols in user_schema.items():
        tables_rows.append(
            _information_schema_row(
                "tables",
                table_catalog=_CATALOG_NAME,
                table_schema="public",
                table_name=table_name,
                table_type="BASE TABLE",
                # Every table a session declares is an ordinary one, which Postgres
                # reports as insertable and untyped whatever the session then does
                # with a write -- refusing it is TableSession's business, not the
                # catalog's.
                is_insertable_into="YES",
                is_typed="NO",
            )
        )
        for i, (col_name, col_type) in enumerate(cols.items(), start=1):
            oid = oid_for_declared_type(col_type)
            columns_rows.append(
                _information_schema_row(
                    "columns",
                    table_catalog=_CATALOG_NAME,
                    table_schema="public",
                    table_name=table_name,
                    column_name=col_name,
                    ordinal_position=i,
                    # A declared schema names types, not constraints, so nothing here
                    # is NOT NULL -- as pg_catalog already says with attnotnull.
                    is_nullable="YES",
                    data_type=col_type,
                    udt_catalog=_CATALOG_NAME,
                    # The underlying type, named as pg_type names it -- `int4`,
                    # `_text` -- and read from the same rows pg_catalog serves, so
                    # the two halves of the catalog cannot disagree about it.
                    udt_schema="pg_catalog",
                    udt_name=type_rows[oid]["typname"],
                    # Postgres' own identifier for the column's type descriptor,
                    # which for a table column is just its attnum as a string.
                    dtd_identifier=str(i),
                    is_self_referencing="NO",
                    is_identity="NO",
                    # 'NO' rather than NULL even with no identity at all: the view
                    # reads a NULL seqcycle through a CASE whose ELSE is 'NO'.
                    identity_cycle="NO",
                    # 'NEVER', not the 'NO' the yes_or_no columns use -- is_generated
                    # is character_data, and its other value is 'ALWAYS'.
                    is_generated="NEVER",
                    is_updatable="YES",
                    **_TYPE_FACTS.get(oid, {}),
                )
            )

    tables = {"information_schema": {"tables": tables_rows, "columns": columns_rows}}
    return INFORMATION_SCHEMA_SCHEMA, tables


def _pg_type_rows() -> list[dict]:
    """Every scalar pg_mimic can name, plus its array type.

    Built from the same OID tables the wire codecs use, so a type pg_mimic can
    encode is a type the catalog can describe -- rather than a second list that
    drifts from the first.
    """
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
                "typarray": ARRAY_OID.get(oid, 0),
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
                "typarray": 0,
            }
        )
    return rows


def pg_type_by_oid() -> dict[int, dict]:
    """The synthesised pg_type rows, keyed by OID.

    Shared with pg_mimic.typeinfo, so the types asyncpg is told about are the same
    ones psql sees and the same ones the wire codecs can actually handle.
    """
    return {row["oid"]: row for row in _pg_type_rows()}


def _storage_for(oid: int) -> str:
    """Postgres' storage mode for a column of this type.

    pg_mimic stores nothing, so this is derived rather than observed -- but that is
    what psql's \\d+ "Storage" column is reporting anyway: variable-length types are
    TOASTable ('x'), fixed-width ones are laid out plain ('p').
    """
    # Every array is variable-length whatever its element is, so they are all 'x'
    # in Postgres and none of them need listing individually.
    return "x" if is_array_oid(oid) or oid in {TEXT, VARCHAR, BPCHAR, BYTEA, JSON, JSONB, NUMERIC} else "p"


def _typname_for(rows: list[dict], oid: int) -> str:
    for row in rows:
        if row["oid"] == oid:
            return row["typname"]
    return "unknown"


def _build_pg_catalog(user_schema: dict, database: str = "postgres") -> tuple[dict, dict]:
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
                    "atttypid": oid_for_declared_type(col_type),
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
                    "attstorage": _storage_for(oid_for_declared_type(col_type)),
                    # '' is Postgres' own "no explicit compression set".
                    "attcompression": "",
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
            # No privilege model here at all, so claiming superuser would be the more
            # flattering lie. False is the safer one.
            "pg_roles": [
                {
                    "oid": _OWNER_OID,
                    "rolname": "postgres",
                    "rolsuper": False,
                    "rolinherit": True,
                    "rolcreaterole": False,
                    "rolcreatedb": False,
                    "rolcanlogin": True,
                    "rolconnlimit": -1,
                    "rolvaliduntil": None,
                    "rolreplication": False,
                    "rolbypassrls": False,
                }
            ],
            "pg_publication": [],
            "pg_publication_rel": [],
            "pg_publication_namespace": [],
            "pg_database": [
                {
                    "oid": 16385,
                    "datname": database,
                    "datdba": _OWNER_OID,
                    "datcollate": "C",
                    "datctype": "C",
                    "datlocprovider": "c",
                    "daticulocale": None,
                    "daticurules": None,
                    "datacl": None,
                    "datallowconn": True,
                    "datconnlimit": -1,
                    "dattablespace": 0,
                }
            ],
        }
    }
    return PG_CATALOG_SCHEMA, tables


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


def _undefined_column_message(error: OptimizeError) -> str:
    """Postgres' wording for a column that isn't there, from sqlglot's.

    sqlglot says `Column '<name>' could not be resolved. Line: 1, Col: 19`; Postgres
    says `column "<name>" does not exist`. Clients match on the latter, and the line
    and column numbers are sqlglot's view of a statement pg_mimic rewrote before
    running -- they point into text the client never sent.
    """
    match = re.search(r"Column '([^']+)' could not be resolved", str(error))
    if match is None:
        return str(error)
    return f'column "{match.group(1)}" does not exist'


def _declared_columns(expr: exp.Expression, schema: dict) -> list[ResultColumn] | None:
    """Column shape from the catalog's declared schema, or None if it cannot be had.

    `statement_from_rows` types each column from the *first row*, which is the only
    place a session yielding bare rows can look. The catalog is not that: it builds
    the schema before it builds the rows, and then dropped it here -- so a column
    whose first value happened to be NULL described as text, and every later value
    in it went out as a string. `information_schema.columns` made that easy to hit,
    since the type-dependent columns are NULL for the columns that have no such
    property. See #101.

    This is the same route TableSession takes, against the same shared helpers, so
    the catalog now answers the way the rest of the project claims column shape is
    settled: from what is declared, never from what a query happened to return.

    None means fall back. An expression the annotator cannot type is not an error
    here -- first-row inference is what this replaced, and it remains better than
    refusing a query the executor already answered.
    """
    try:
        analyzed = AnalyzedQuery(expr, schema=schema)
        return result_columns(analyzed.annotated(), [], names=analyzed.column_names())
    except Exception:
        return None


def _as_statement(expr: exp.Expression, schema: dict, tables: dict, *, strict: bool) -> Statement | None:
    """Run a catalog query, or decide what its failure means. See #39.

    `strict` says whether an executor failure is this connection's problem or the
    client's answer. It is True for information_schema, which users query
    themselves, and False for pg_catalog, which is overwhelmingly psql's own SQL.
    """
    try:
        result = sqlglot_execute(expr, schema=schema, tables=tables, dialect="postgres")
    except OptimizeError as error:
        # A column the catalog does not model. On information_schema that is the
        # client's own query and Postgres answers 42703; no rows is the same lie the
        # executor branch below refuses to tell, and worse than an error for an ORM,
        # which concludes the table has no such column rather than that pg_mimic
        # cannot say. What kept this lenient was the width of the model rather than
        # the principle -- information_schema carried 7 of Postgres' 44 columns and
        # 4 of its 12, so raising would have turned ordinary introspection into
        # errors overnight. #99 serves both views at full width, which is what makes
        # an unmodelled one unusual enough to refuse.
        if strict:
            raise PgError(UNDEFINED_COLUMN, _undefined_column_message(error)) from None
        # pg_catalog stays lenient, and not merely out of caution: measured across
        # psql's \d family, nothing it asks of pg_catalog is unmodelled any more --
        # the failures that remain are sqlglot executor bugs (#58) reaching the
        # branch below. This one is for the columns of a deliberate slice, where psql
        # asks after things a mimic has no business modelling.
        logger.debug("catalog query answered empty, nothing models it: %s -- %s", error, expr.sql(dialect="postgres"))
        return _empty_result(expr)
    except Exception as error:
        # The executor broke rather than ran out of catalog: a missing entry in its
        # function table reads as `name 'DPIPE' is not defined`. On an
        # information_schema query that is a pg_mimic gap and empty is a lie -- it
        # is how #38 stayed hidden, since `SELECT a || b FROM
        # information_schema.tables` came back as no rows and a clean exit.
        if strict:
            raise PgError(
                FEATURE_NOT_SUPPORTED,
                f"pg_mimic could not run this information_schema query: {error}",
            ) from None
        # pg_catalog, where the same failure is psql asking after partitions,
        # collations and statistics that a mimic has none of. Raising would break
        # \d, \d+ and \l outright, and empty is what makes them work.
        logger.debug(
            "catalog query answered empty after an executor failure: %s -- %s",
            error,
            expr.sql(dialect="postgres"),
        )
        return _empty_result(expr)

    sql = expr.sql(dialect="postgres")
    rows = [tuple(row) for row in result.rows]
    declared = _declared_columns(expr, schema)
    if declared is not None and len(declared) == len(result.columns):
        return StaticStatement(sql, declared, rows)
    return statement_from_rows(sql, result.columns, rows)


async def _user_schema(connection: Connection) -> dict:
    """What the session declares, or nothing -- never None.

    `Session.schema()` returns None by default, and the `or {}` guarding that used
    to bind to the else branch instead of the whole conditional, so a session that
    did not override it crashed every catalog query with `'NoneType' object has no
    attribute 'items'`. Nothing caught it because the sessions in this suite either
    declare a schema or are TableSession, which always does.
    """
    schema_fn = getattr(connection.session, "schema", None)
    return ((await schema_fn()) if schema_fn is not None else {}) or {}


async def information_schema_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema, tables = _build_information_schema(await _user_schema(connection))
    return _as_statement(expr, schema, tables, strict=True)


async def pg_catalog_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema, tables = _build_pg_catalog(await _user_schema(connection), connection.state.database)
    return _as_statement(rewrite_for_executor(connection, expr), schema, tables, strict=False)
