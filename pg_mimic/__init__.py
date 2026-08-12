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
from . import catalog, middleware
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
from .errors import PgError
from .results import ResultColumn
from .server import PgServer
from .session import BaseSession, Portal, Session, Statement, StaticStatement

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
    "middleware",
    "BaseSession",
    "Session",
    "Statement",
    "StaticStatement",
    "Portal",
    "ResultColumn",
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
