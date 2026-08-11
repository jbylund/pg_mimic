"""information_schema emulation, built from whatever `Session.schema()` declares.

A session author describes their tables once, declaratively, and the catalog
answers the introspection SQL that clients and ORMs issue against
`information_schema.tables` / `.columns` on their behalf. This is the shape the
whole middleware chain follows (see pg_mimic.middleware): the session supplies
data, the chain supplies SQL compatibility.

Evaluated with sqlglot's executor against a synthesised in-memory schema, so
ordinary WHERE/ORDER BY/JOIN over those views works without pg_mimic having to
implement any of it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlglot import exp
from sqlglot.executor import execute as sqlglot_execute

from .results import ResultColumn
from .session import Statement, StaticStatement
from .types import TEXT

if TYPE_CHECKING:
    from .connection import Connection


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


async def information_schema_statement(connection: Connection, expr: exp.Select) -> Statement | None:
    schema_fn = getattr(connection.session, "schema", None)
    user_schema = (await schema_fn()) if schema_fn is not None else None
    sqlglot_schema, sqlglot_tables = _build_information_schema(user_schema or {})

    try:
        result = sqlglot_execute(expr, schema=sqlglot_schema, tables=sqlglot_tables, dialect="postgres")
    except Exception:
        return None

    rows = [tuple(row) for row in result.rows]
    if rows:
        columns = [ResultColumn.for_type(name, type(value)) for name, value in zip(result.columns, rows[0])]
    else:
        columns = [ResultColumn(name, TEXT) for name in result.columns]
    return StaticStatement(expr.sql(dialect="postgres"), columns, rows)
