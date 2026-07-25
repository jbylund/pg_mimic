"""PgError and the SQLSTATE codes pg_mimic uses internally.

Real Postgres SQLSTATE codes -- reused as-is so real client error handling
(e.g. matching on `error.sqlstate`) behaves the way it would against a real
server. See https://www.postgresql.org/docs/current/errcodes-appendix.html
"""
from __future__ import annotations


class PgError(Exception):
    def __init__(self, sqlstate: str, message: str, **fields: str):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.message = message
        self.fields = fields


SUCCESSFUL_COMPLETION = "00000"
SYNTAX_ERROR = "42601"
UNDEFINED_TABLE = "42P01"
UNDEFINED_COLUMN = "42703"
UNDEFINED_FUNCTION = "42883"
INVALID_AUTHORIZATION_SPECIFICATION = "28000"
INVALID_PASSWORD = "28P01"
IN_FAILED_SQL_TRANSACTION = "25P02"
ACTIVE_SQL_TRANSACTION = "25001"
NO_ACTIVE_SQL_TRANSACTION = "25P01"
INVALID_SQL_STATEMENT_NAME = "26000"  # unknown prepared statement/portal name
FEATURE_NOT_SUPPORTED = "0A000"
PROTOCOL_VIOLATION = "08P01"
INTERNAL_ERROR = "XX000"
