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
from typing import TYPE_CHECKING

from sqlglot import exp
from sqlglot.errors import OptimizeError
from sqlglot.executor import execute as sqlglot_execute

from .catalog_data import PG_CATALOG_SCHEMA
from .catalog_rewrite import rewrite_for_executor
from .describe import oid_for_declared_type
from .errors import FEATURE_NOT_SUPPORTED, PgError
from .results import ResultColumn
from .session import Statement, StaticStatement, statement_from_rows
from .types import TEXT

if TYPE_CHECKING:
    from .connection import Connection


logger = logging.getLogger(__name__)

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
    from .arrays import is_array_oid
    from .types import BPCHAR, BYTEA, JSON, JSONB, NUMERIC, TEXT, VARCHAR

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


def _as_statement(expr: exp.Expression, schema: dict, tables: dict, *, strict: bool) -> Statement | None:
    """Run a catalog query, or decide what its failure means. See #39.

    `strict` says whether an executor failure is this connection's problem or the
    client's answer. It is True for information_schema, which users query
    themselves, and False for pg_catalog, which is overwhelmingly psql's own SQL.
    """
    try:
        result = sqlglot_execute(expr, schema=schema, tables=tables, dialect="postgres")
    except OptimizeError as error:
        # A catalog column pg_mimic doesn't model. Empty is *not* the right answer
        # here -- Postgres says 42703, and no rows is the same kind of lie the
        # executor branch below refuses to tell. It stays lenient only because the
        # model is too thin to be strict against: information_schema.columns
        # carries 7 of Postgres's ~44, so raising would turn ordinary ORM
        # introspection into errors overnight. Model the columns first, then make
        # this 42703 -- see #66.
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

    return statement_from_rows(expr.sql(dialect="postgres"), result.columns, [tuple(row) for row in result.rows])


async def _user_schema(connection: Connection) -> dict:
    schema_fn = getattr(connection.session, "schema", None)
    return (await schema_fn()) if schema_fn is not None else {} or {}


async def information_schema_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema, tables = _build_information_schema(await _user_schema(connection))
    return _as_statement(expr, schema, tables, strict=True)


async def pg_catalog_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema, tables = _build_pg_catalog(await _user_schema(connection), connection.state.database)
    return _as_statement(rewrite_for_executor(connection, expr), schema, tables, strict=False)
