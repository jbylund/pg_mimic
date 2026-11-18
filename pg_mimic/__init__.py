from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _installed_version

# middleware is exported because Session.middleware is a public extension point --
# customising the chain needs middleware.DEFAULT_MIDDLEWARE / middleware.static_select.
# catalog rides along for Session.schema()-driven information_schema emulation.
#
# `testing` is deliberately NOT imported here, unlike those two. Nothing in the
# serving path refers to it, so importing it eagerly would make every process that
# embeds a server -- production ones included -- pay for a threading-based test
# harness it never calls. `import pg_mimic.testing` in your tests instead.
#
# errors is exported for its SQLSTATE constants. PgError below is the exception a
# session raises, but `PgError("42P01", ...)` says nothing at the call site --
# `PgError(errors.UNDEFINED_TABLE, ...)` does, so the codes are public surface too.
#
# describe is the machinery behind "column shape from a declared schema, without
# executing" -- what TableSession answers Describe with, and what any session
# declaring its own schema() should reach for rather than reimplement (#88).
from . import catalog, describe, errors, middleware, settings_values
from .arrays import ARRAY_OID
from .auth import (
    AuthPlugin,
    ClearTextPasswordAuthPlugin,
    IdentityProvider,
    Md5PasswordAuthPlugin,
    ScramSha256AuthPlugin,
    SimpleIdentityProvider,
    TrustAuthPlugin,
)

# Table and Schema are what a Session.schema() declares. Public because declaring a
# primary key or a foreign key is something a user does, not something pg_mimic infers.
from .declared import Schema, Table

# The two ways a column's type is named on the way to an OID, exported together
# because they are one pair: `oid_for_declared_type("text[]")` takes the SQL
# spelling a Session.schema() declares, `oid_for_type(list[str])` the Python type
# ResultColumn.for_type reads. A session declaring a schema needs the first to
# describe its own columns, and used to have to write its own (#89).
from .describe import oid_for_declared_type
from .errors import PgError
from .results import ResultColumn
from .server import PgServer
from .session import BaseSession, Portal, Session, Statement, StaticStatement
from .state import SettingValue

# TableSession is headline public API, not a niche extension point: "serve these
# tables" is the entry point most users want before they ever write a Session.
from .tables import TableSession

# OID constants: explicit column declarations need them -- ResultColumn("j", JSONB),
# ResultColumn("tags", ARRAY_OID[TEXT]) -- so they are part of the public surface.
from .types import (
    BOOL,
    BYTEA,
    DATE,
    FLOAT4,
    FLOAT8,
    INT2,
    INT4,
    INT8,
    INTERVAL,
    JSON,
    JSONB,
    NUMERIC,
    TEXT,
    TIME,
    TIMESTAMP,
    TIMESTAMPTZ,
    UUID,
    VARCHAR,
    oid_for_type,
)

try:
    # Single source of truth: pyproject.toml's [project] version, read back from
    # the installed distribution metadata. Hardcoding it here as well meant two
    # strings to bump, and only pyproject's is checked against the tag at release.
    __version__ = _installed_version("pg-mimic")
except PackageNotFoundError:
    # Imported from a source tree that was never installed (no dist-info to read).
    __version__ = "0.0.0.dev0"

__all__ = [
    "PgServer",
    "catalog",
    "describe",
    "errors",
    "middleware",
    "settings_values",
    "BaseSession",
    "Session",
    "TableSession",
    "Schema",
    "Table",
    "Statement",
    "StaticStatement",
    "Portal",
    "ResultColumn",
    "SettingValue",
    "ARRAY_OID",
    "BOOL",
    "BYTEA",
    "DATE",
    "FLOAT4",
    "FLOAT8",
    "INT2",
    "INT4",
    "INT8",
    "INTERVAL",
    "JSON",
    "JSONB",
    "NUMERIC",
    "TEXT",
    "TIME",
    "TIMESTAMP",
    "TIMESTAMPTZ",
    "UUID",
    "VARCHAR",
    "oid_for_declared_type",
    "oid_for_type",
    "PgError",
    "AuthPlugin",
    "TrustAuthPlugin",
    "ClearTextPasswordAuthPlugin",
    "Md5PasswordAuthPlugin",
    "ScramSha256AuthPlugin",
    "IdentityProvider",
    "SimpleIdentityProvider",
    "__version__",
]
