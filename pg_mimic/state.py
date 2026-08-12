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

    # What SHOW returns: the current value of every overridden setting, whichever
    # statement last wrote it. Keyed by the folded setting name.
    session_vars: dict[str, str] = field(default_factory=dict)

    # What a COMMIT leaves behind. `SET` and `RESET` write both dicts; `SET LOCAL`
    # writes only the one above, which is what makes it local -- see
    # commit_transaction(). Not a "local values" dict shadowing a session one:
    # checked against PostgreSQL 18, `SET LOCAL x` followed by `SET x` in the same
    # transaction reads back the *session* value, so the visible value is simply
    # the last write and the two dicts differ by lifetime rather than by scope.
    committed_vars: dict[str, str] = field(default_factory=dict)

    # Every setting name this connection has ever written, whether or not the
    # write survived. Postgres knows its built-in GUCs from birth and learns a
    # custom (dotted) one the first time a session writes it; from then on the
    # name reads back as the empty string rather than erroring, which is what
    # tells `current_setting('app.tenant', true) IS NULL` apart from "set to
    # nothing". Measured on PostgreSQL 18, the knowledge outlives everything that
    # drops the *value*: RESET, RESET ALL, DISCARD ALL, and rollback of the
    # transaction that did the setting. Hence a plain set, untouched by reset()
    # and absent from the scope frames below. A session that owns settings of its
    # own -- or would simply rather answer blanks than errors -- registers them
    # here, and they read back like any other.
    known_settings: set[str] = field(default_factory=set)

    # One frame per open fork point -- the transaction itself, then one per
    # savepoint -- holding both dicts as they stood when it opened. Postgres keeps
    # a stack per setting instead; with a handful of settings, copying the dicts
    # is simpler and the cost is nil.
    _scopes: list[tuple[dict[str, str], dict[str, str]]] = field(default_factory=list)

    # Prepared statements, by name. One namespace for both entrances: the
    # protocol's Parse and SQL-level PREPARE. Postgres shares them -- SQL can
    # DEALLOCATE a statement that Parse created -- so pg_mimic does too.
    statements: dict[str, Statement] = field(default_factory=dict)

    # Open portals, by name, and the open savepoints innermost last.
    portals: dict[str, Any] = field(default_factory=dict)
    savepoints: list[str] = field(default_factory=list)

    def reset(self) -> None:
        """What `DISCARD ALL` does: forget everything but who is connected -- and
        which settings this connection has heard of, which real Postgres keeps."""
        self.session_vars.clear()
        self.committed_vars.clear()
        self._scopes.clear()
        self.statements.clear()
        self.portals.clear()
        self.savepoints.clear()

    # --- settings are transactional; nothing else here is ----------------------------
    #
    # Only the settings take part. Prepared statements deliberately do not: checked
    # against PostgreSQL 18, a PREPARE inside a transaction that rolls back survives
    # it, and a DEALLOCATE inside one stays done. Portals follow cursor lifecycle
    # rather than restore-on-rollback.

    def open_scope(self) -> None:
        """Remember both dicts, for BEGIN and for each SAVEPOINT."""
        self._scopes.append((dict(self.session_vars), dict(self.committed_vars)))

    def discard_scopes(self, depth: int) -> None:
        """Drop frames without restoring: RELEASE keeps the values it found."""
        del self._scopes[depth:]

    def restore_scope(self, depth: int) -> None:
        """Put both dicts back as they stood at `depth`, and drop deeper frames.

        For ROLLBACK (depth 0) and ROLLBACK TO SAVEPOINT. A session-scoped `SET`
        made inside the scope is undone along with a local one -- verified against
        PostgreSQL 18, where both revert.
        """
        if depth >= len(self._scopes):
            return
        session_vars, committed_vars = self._scopes[depth]
        self.session_vars.clear()
        self.session_vars.update(session_vars)
        self.committed_vars.clear()
        self.committed_vars.update(committed_vars)
        del self._scopes[depth:]

    def commit_transaction(self) -> None:
        """What COMMIT leaves: the session-scoped writes, and no local ones."""
        self.session_vars.clear()
        self.session_vars.update(self.committed_vars)
        self._scopes.clear()

    def end_transaction(self) -> None:
        """ROLLBACK: back to how the settings stood before BEGIN."""
        self.restore_scope(0)
        self._scopes.clear()
