"""The per-connection state that middleware and sessions both reach for.

Two things need this: the middleware, which owns SET/RESET, savepoints and
prepared statements, and the session, which may need to read what the middleware
decided -- a `search_path` to resolve against, an `app.tenant_id` to filter by --
or to manage the lot itself, having dropped the middleware chain.

`DISCARD ALL` defines what belongs here, which makes the boundary a fact rather
than a judgement call: the statement resets session variables, prepared
statements, cursors and (by ending the transaction) savepoints, and `reset()`
below is exactly that. What it does *not* reset is what does not belong --
`DISCARD PLANS`, `DISCARD SEQUENCES` and `DISCARD TEMP` are all no-ops in a
mimic, having no plan cache, no sequences and no temp tables. Note that
`DISCARD PLANS` does not drop prepared statements either.

The connection's own wire machinery deliberately stays on Connection: the
stream, `tx_status` (reported in every ReadyForQuery), the pending
ParameterStatus queue and the authentication handshake are protocol concerns a
session has no business touching.

Mutable on purpose. A session that sets `middleware = ()` sees every statement
untouched and does its own bookkeeping, which means writing here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .session import Statement


@dataclass
class SessionState:
    """What a connection knows that outlives a single statement."""

    # Who is connected. Fixed for the life of the connection, so untouched by
    # reset() -- but kept here so an ordinary session never needs the Connection
    # itself to route by tenant or database.
    username: str = ""
    database: str = ""
    application_name: str = ""

    # SET/RESET, and set_config(). Keyed by the folded setting name.
    session_vars: dict[str, str] = field(default_factory=dict)

    # Prepared statements, by name. One namespace for both entrances: the
    # protocol's Parse and SQL-level PREPARE. Postgres shares them -- SQL can
    # DEALLOCATE a statement that Parse created -- so pg_mimic does too.
    statements: dict[str, Statement] = field(default_factory=dict)

    # Open portals, by name, and the open savepoints innermost last.
    portals: dict[str, Any] = field(default_factory=dict)
    savepoints: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """What `DISCARD ALL` does: forget everything but who is connected."""
        self.session_vars.clear()
        self.statements.clear()
        self.portals.clear()
        self.savepoints.clear()
