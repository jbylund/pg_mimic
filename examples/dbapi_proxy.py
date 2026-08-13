"""Fronts a real (if much simpler) database: proxies every query to an
in-memory sqlite3 database, translating the incoming Postgres-dialect SQL to
sqlite's dialect via sqlglot before running it.

    python examples/dbapi_proxy.py
    psql "host=127.0.0.1 port=5432 user=test dbname=test" -c "select * from users"
"""

import logging

import sqlglot
from sqlglot import exp

from pg_mimic import PgServer, ResultColumn, Session
from pg_mimic.types import TEXT


class SqliteSession(Session):
    def __init__(self):
        import sqlite3

        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
        self.conn.executemany("INSERT INTO users (name) VALUES (?)", [("alice",), ("bob",)])
        self.conn.commit()

    def _translate(self, sql: str) -> str:
        return sqlglot.transpile(sql, read="postgres", write="sqlite")[0]

    async def describe(self, sql, param_oids):
        # sqlite3's cursor.description is only populated by actually running the
        # query -- there's no real planner to ask, same limitation pg_mimic
        # itself has for arbitrary session-defined queries. Safe to do here
        # since it's a plain SELECT (no side effects); skipped for DML so an
        # INSERT/UPDATE/DELETE doesn't run twice (once in describe(), once in
        # query()).
        if not isinstance(sqlglot.parse_one(sql, dialect="postgres"), exp.Select):
            return None
        cursor = self.conn.execute(self._translate(sql), params_for_describe(param_oids))
        if cursor.description is None:
            return None
        # sqlite3's cursor.description doesn't carry real column types (only
        # names), so every column is declared TEXT here -- clients will see
        # e.g. integer ids as strings. A real proxy would map sqlite's
        # storage classes (or its own known schema) to proper Postgres OIDs.
        return [ResultColumn(col[0], TEXT) for col in cursor.description]

    async def query(self, sql, params):
        cursor = self.conn.execute(self._translate(sql), params or [])
        for row in cursor.fetchall():
            yield tuple(row)
        self.conn.commit()


def params_for_describe(param_oids: list[int | None]) -> list[None]:
    # We don't have real values yet at describe()-time (only OID hints) --
    # sqlite doesn't care about param values for column shape, just count.
    return [None] * len(param_oids)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info("backed by an in-memory sqlite3 db")
    PgServer(session_factory=SqliteSession).run()
