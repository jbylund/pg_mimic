# catalog is exported because Session.middleware is a public extension point --
# customising the chain needs catalog.DEFAULT_MIDDLEWARE / catalog.static_select.
from . import catalog
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
from .session import BaseSession, Portal, Session, Statement

__version__ = "0.1.1"

__all__ = [
    "PgServer",
    "catalog",
    "BaseSession",
    "Session",
    "Statement",
    "Portal",
    "ResultColumn",
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
