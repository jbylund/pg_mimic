"""Echo server: logs every statement pg_mimic receives to stdout (including
ones the built-in middleware handles, like BEGIN/SET/session functions -- see
the `prepare()` override below), and echoes the raw SQL text back as a
single-column result for anything that isn't otherwise handled.

    python examples/echo.py [--port 5432] [--host 127.0.0.1]
    # or --open-port for any free port, when 5432 is a real PostgreSQL
    psql "host=127.0.0.1 port=5432 user=test dbname=test"

Try a few things from psql and watch this process's stdout:
    select 1;
    begin; select 2; commit;
    select version();
    select * from some_table;
    \\dt
"""

import logging

from _args import example_parser, parse_args, serve

from pg_mimic import ResultColumn, Session

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("echo")


class EchoSession(Session):
    async def init(self, connection):
        await super().init(connection)
        log.info("client connected (pid=%s, user=%s)", connection.pid, connection.username)

    async def close(self):
        log.info("client disconnected")

    async def prepare(self, sql, param_oids):
        # Runs for *every* statement -- even ones the middleware chain (SET,
        # BEGIN/COMMIT/ROLLBACK, static SELECTs, information_schema) will end
        # up handling instead of describe()/query() below. This is the one
        # place you can observe literally everything pg_mimic receives.
        log.info("STATEMENT: %s", sql)
        return await super().prepare(sql, param_oids)

    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("echo", str)]

    async def query(self, sql, params):
        if params:
            log.info("  params: %s", params)
        yield (sql,)


if __name__ == "__main__":
    args = parse_args(example_parser(__doc__))
    # The user and database are what to connect *as*; the address comes from run()'s
    # own line below, which is the only one that knows the port under --open-port.
    logging.info("connect as: user=test dbname=test")
    serve(EchoSession, args)
