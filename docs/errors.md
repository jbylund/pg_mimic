# Reporting errors

Raise `PgError(sqlstate, message)` from anywhere in a session and the client gets a real `ErrorResponse`
carrying that SQLSTATE, so driver-side error handling (`except psycopg.errors.UndefinedTable`,
`e.sqlstate == "42P01"`, an ORM's retry rules) behaves as it would against a real Postgres. Extra
`ErrorResponse` fields can ride along as keyword arguments, keyed by their protocol field byte —
`PgError(UNDEFINED_TABLE, "...", D="a longer explanation")` sets the detail field.

The codes live in `pg_mimic.errors`, which is public: the constants are the whole point of raising the
exception, and `PgError("42P01", ...)` says nothing at the call site.

```python
from pg_mimic import Session
from pg_mimic.errors import PgError, UNDEFINED_TABLE


class MySession(Session):
    async def describe(self, sql, param_oids):
        if "orders" not in sql:
            raise PgError(UNDEFINED_TABLE, 'relation "whatever" does not exist')
        ...
```

Anything else a session raises still reaches the client, as `XX000` (`internal_error`) with the exception's
string — a bug reported rather than a dropped connection. `PgError` is how you say the error was deliberate.
