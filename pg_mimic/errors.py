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
UNDEFINED_OBJECT = "42704"  # here: a configuration parameter no one has ever heard of
INVALID_TEXT_REPRESENTATION = "22P02"  # e.g. "abc" bound to an integer parameter
INDETERMINATE_DATATYPE = "42P18"  # a parameter whose type nothing in the query settles
INVALID_AUTHORIZATION_SPECIFICATION = "28000"
INVALID_PASSWORD = "28P01"
IN_FAILED_SQL_TRANSACTION = "25P02"
READ_ONLY_SQL_TRANSACTION = "25006"
ACTIVE_SQL_TRANSACTION = "25001"
NO_ACTIVE_SQL_TRANSACTION = "25P01"
INVALID_SQL_STATEMENT_NAME = "26000"  # unknown prepared statement/portal name
INVALID_SAVEPOINT_SPECIFICATION = "3B001"  # RELEASE/ROLLBACK TO of a savepoint that isn't open
INVALID_PARAMETER_VALUE = "22023"
CANT_CHANGE_RUNTIME_PARAM = "55P02"  # a real parameter, but not one a session may set
FEATURE_NOT_SUPPORTED = "0A000"
PROTOCOL_VIOLATION = "08P01"
QUERY_CANCELED = "57014"
INTERNAL_ERROR = "XX000"


class ProtocolViolation(PgError):
    """A frame that the framing layer itself refused -- a length that can't be
    true, a protocol version we don't speak.

    Its own class because it is not recoverable the way a failed statement is.
    An ordinary PgError leaves the connection in a known place (the client sends
    Sync and carries on); a frame we would not read leaves the byte stream at an
    offset neither side agrees on, so there is nothing to carry on with. The
    connection gets a FATAL report and is dropped.
    """

    def __init__(self, message: str, sqlstate: str = PROTOCOL_VIOLATION):
        super().__init__(sqlstate, message)
