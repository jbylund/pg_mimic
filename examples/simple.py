"""Minimal pg_mimic server: answers every query with the same static rows.

    python examples/simple.py
    psql "host=127.0.0.1 port=5432 user=test dbname=test" -c "select * from anything"
"""
import asyncio

from pg_mimic import PgServer, ResultColumn, Session


class MySession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("a", str), ResultColumn.for_type("b", int)]

    async def query(self, sql, params):
        yield ("hello", 1)
        yield ("world", 2)


async def main():
    server = PgServer(session_factory=MySession)
    await server.start_server(host="127.0.0.1", port=5432)
    print("pg_mimic listening on port 5432")
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
