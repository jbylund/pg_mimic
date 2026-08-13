"""Minimal pg_mimic server: answers every query with the same static rows.

python examples/simple.py
psql "host=127.0.0.1 port=5432 user=test dbname=test" -c "select * from anything"
"""

import logging

from pg_mimic import PgServer, ResultColumn, Session


class MySession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("a", str), ResultColumn.for_type("b", int)]

    async def query(self, sql, params):
        yield ("hello", 1)
        yield ("world", 2)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    PgServer(session_factory=MySession).run()
