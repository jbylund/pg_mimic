"""The three things a generated query can be run against.

`postgres` is the oracle. The other two are the same executor seen from either
side of pg_mimic's workaround layer, and running both is what makes a finding
actionable:

- fails in `raw` and in `mimic`  -> an upstream sqlglot bug reaching our users
- fails in `raw` only            -> a workaround in tables.py is doing its job
- fails in `mimic` only          -> the workaround itself is wrong. Ours to fix.

`mimic` deliberately stops short of the wire protocol. TableSession.query() is
where every rewrite in tables.py has already been applied, and going through a
socket per query would cost a hundredfold for no extra coverage -- the encoding
path has its own tests.
"""

from __future__ import annotations

import asyncio
import re
import warnings
from typing import Any

from . import dataset

# The executor compiles a query to Python source and exec()s it, so a query whose
# generated code is questionable -- `NOT(-1 is None)` -- emits a SyntaxWarning
# from a file the user never wrote. Interesting once; at fuzzing volume it is
# thousands of lines of stderr on top of the report.
warnings.filterwarnings("ignore", category=SyntaxWarning)


class Failed(Exception):
    """The engine refused the query. Carries the message, not a traceback."""

    def __init__(self, message: str, sqlstate: str | None = None):
        super().__init__(message)
        self.message = message
        self.sqlstate = sqlstate


_NOISE = [
    (re.compile(r"0x[0-9a-f]+", re.I), "ADDR"),
    (re.compile(r"\(\d+\)"), "(N)"),
    (re.compile(r"'[^']*'"), "'S'"),
    (re.compile(r'"[^"]*"'), '"S"'),
    (re.compile(r"\b\d+\b"), "N"),
    (re.compile(r"\s+"), " "),
]


def signature(message: str) -> str:
    """A message with the query-specific parts taken out, so two failures with the
    same cause land in the same bucket.

    Without this, `name 'DPIPE' is not defined` at step `Sort: _0 (4400334928)`
    and the same failure at step `Sort: _0 (4400339104)` are two findings.
    """
    text = message.split("sqlglot's executor is not a complete SQL engine")[0]
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return text.strip()[:200]


class Postgres:
    name = "postgres"

    def __init__(self, dsn: str, statement_timeout_ms: int = 5000):
        import psycopg

        self._connection = psycopg.Connection.connect(dsn, autocommit=True)
        with self._connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
            cursor.execute(f"SET statement_timeout = {statement_timeout_ms}")
            # Not cosmetic: a server whose default_table_access_method is an
            # extension's -- a developer machine with a table AM under test, which
            # is exactly the kind of machine this gets run on -- fails CREATE TABLE
            # outright, and a non-heap AM could answer a query differently.
            cursor.execute("SET default_table_access_method = 'heap'")
        self._error = psycopg.Error

    def load(self) -> None:
        dataset.load(self._connection)

    def run(self, sql: str) -> list[tuple]:
        try:
            with self._connection.cursor() as cursor:
                cursor.execute(sql)  # type: ignore[arg-type]
                return [tuple(row) for row in cursor.fetchall()]
        except self._error as exc:
            sqlstate = getattr(getattr(exc, "diag", None), "sqlstate", None)
            raise Failed(str(exc).strip().splitlines()[0], sqlstate) from None

    def close(self) -> None:
        self._connection.close()


class RawSqlglot:
    name = "raw"

    def __init__(self) -> None:
        self._schema = dataset.sqlglot_schema()
        self._tables = dataset.rows()

    def run(self, sql: str) -> list[tuple]:
        from sqlglot.executor import execute

        try:
            result = execute(sql, schema=self._schema, tables=self._tables, dialect="postgres")
        except Exception as exc:
            raise Failed(f"{type(exc).__name__}: {exc}") from None
        return [tuple(row) for row in result.rows]


class Mimic:
    name = "mimic"

    def __init__(self) -> None:
        from pg_mimic import TableSession

        self._session = TableSession(dataset.rows(), columns=dataset.declared_columns())
        self._loop = asyncio.new_event_loop()

    def run(self, sql: str) -> list[tuple]:
        from pg_mimic.errors import PgError

        try:
            rows = self._loop.run_until_complete(self._session.query(sql, []))
        except PgError as exc:
            raise Failed(f"{exc.sqlstate}: {exc}", exc.sqlstate) from None
        except Exception as exc:
            raise Failed(f"{type(exc).__name__}: {exc}") from None
        return [tuple(row) for row in rows]

    def close(self) -> None:
        self._loop.close()


def build(names: list[str]) -> list[Any]:
    return [RawSqlglot() if name == "raw" else Mimic() for name in names]
