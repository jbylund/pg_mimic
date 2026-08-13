"""Minimal pg_mimic server: answers every query with the same static rows.

python examples/simple.py
# or --open-port for any free port, when 5432 is a real PostgreSQL
psql "host=127.0.0.1 port=5432 user=test dbname=test" -c "select * from anything"
"""

from _args import example_parser, parse_args, serve

from pg_mimic import ResultColumn, Session


class MySession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("a", str), ResultColumn.for_type("b", int)]

    async def query(self, sql, params):
        yield ("hello", 1)
        yield ("world", 2)


if __name__ == "__main__":
    serve(MySession, parse_args(example_parser(__doc__)))
