"""Serves in-memory tables with no session code at all -- TableSession does the
describe()/query()/schema() work, so real SQL over Python rows just works.

    python examples/tables.py
    # or --open-port for any free port, when 5432 is a real PostgreSQL
    psql "host=127.0.0.1 port=5432 user=test dbname=test" -c "\\dt"
    psql "host=127.0.0.1 port=5432 user=test dbname=test" \\
        -c "select u.name, sum(o.total) as spent
              from users u join orders o on o.user_id = u.id
             group by u.name order by spent desc"

Read-only: an INSERT is refused rather than applied to the dicts below.
"""

import datetime
import logging
from decimal import Decimal

from _args import example_parser, parse_args, serve

from pg_mimic import JSONB, TableSession

TABLES = {
    "users": [
        {"id": 1, "name": "alice", "joined": datetime.date(2024, 1, 2), "tags": ["staff"]},
        {"id": 2, "name": "bob", "joined": datetime.date(2024, 3, 4), "tags": []},
    ],
    "orders": [
        {"id": 10, "user_id": 1, "total": Decimal("9.99")},
        {"id": 11, "user_id": 2, "total": Decimal("24.50")},
        {"id": 12, "user_id": 1, "total": Decimal("5.00")},
    ],
    # An empty table has no values to infer from, and a list is equally a Postgres
    # array or a json document -- both declared below rather than guessed at.
    "sessions": [],
    "settings": [{"user_id": 1, "prefs": {"theme": "dark"}}],
}

COLUMNS = {
    "sessions": {"id": int, "user_id": int, "started_at": datetime.datetime},
    "settings": {"prefs": JSONB},
    "users": {"tags": list[str]},
}


if __name__ == "__main__":
    args = parse_args(example_parser(__doc__))
    logging.info("serving %d tables read-only", len(TABLES))
    # A session per connection, so the factory builds one rather than sharing it.
    serve(lambda: TableSession(TABLES, columns=COLUMNS), args)
