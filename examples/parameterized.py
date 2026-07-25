"""Shows real out-of-band bind parameters arriving via the extended query
protocol -- not textually interpolated into the SQL string.

    python examples/parameterized.py
    psql "host=127.0.0.1 port=5432 user=test dbname=test" \\
        -c "select * from items where price > 10" \\
        --set=PGOPTIONS=  # psql itself uses simple query; try psycopg instead:

    python -c "
    import psycopg
    conn = psycopg.connect('host=127.0.0.1 port=5432 user=test dbname=test', autocommit=True)
    print(conn.execute('select * from items where price > %s', (10,)).fetchall())
    "
"""

import asyncio

from pg_mimic import PgServer, ResultColumn, Session

ITEMS = [
    ("widget", 5.0),
    ("gadget", 15.0),
    ("gizmo", 25.0),
]


class ItemsSession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("name", str), ResultColumn.for_type("price", float)]

    async def query(self, sql, params):
        # `params` are real bound values (already decoded to text), e.g. ["10"] --
        # not interpolated into `sql`. A real session would use them to filter/query
        # a backing store; here we just do it in Python for the example.
        min_price = float(params[0]) if params else 0.0
        for name, price in ITEMS:
            if price > min_price:
                yield (name, price)


async def main():
    server = PgServer(session_factory=ItemsSession)
    await server.start_server(host="127.0.0.1", port=5432)
    print("pg_mimic listening on port 5432")
    await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
