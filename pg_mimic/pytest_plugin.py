"""pytest fixtures for pg_mimic, registered through a `pytest11` entry point.

Split from `pg_mimic.testing` so that module stays importable with pytest absent
-- pytest is a dev-only dependency here, and a library that only needs the
context managers should not acquire a test runner to get them. This module is
loaded by pytest itself, so it is the one place pytest is guaranteed present.

Everything hangs off one fixture the user overrides::

    # conftest.py
    import pytest

    @pytest.fixture
    def pg_mimic_session_factory():
        return MySession

    def test_it(pg_mimic_dsn):
        with psycopg.connect(pg_mimic_dsn) as conn:
            ...
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from .server import PgServer, SessionFactory
from .testing import serve_in_thread


@pytest.fixture
def pg_mimic_session_factory() -> SessionFactory:
    """The session the server serves -- override this in your own conftest.

    Fails rather than serving some default session: a placeholder that answered
    queries would turn "you forgot to point this at your session" into a test
    asserting against rows nobody wrote.
    """
    pytest.fail(
        "the pg_mimic fixtures need a session to serve: override the pg_mimic_session_factory "
        "fixture in your conftest.py, returning a zero-argument callable that produces a "
        "pg_mimic Session."
    )


@pytest.fixture
def pg_mimic_server_kwargs() -> dict[str, Any]:
    """Extra `PgServer(...)` arguments -- `auth_plugin_factory`,
    `identity_provider`, `server_version`. Override to return a dict."""
    return {}


@pytest.fixture
def pg_mimic_server(pg_mimic_session_factory: SessionFactory, pg_mimic_server_kwargs: dict[str, Any]) -> Iterator[PgServer]:
    """A running server on an ephemeral port, torn down after the test.

    Threaded rather than run on the test's own event loop, so one fixture serves
    sync and async tests alike -- a blocking client in a sync test would
    otherwise deadlock. See `pg_mimic.testing`.
    """
    with serve_in_thread(pg_mimic_session_factory, **pg_mimic_server_kwargs) as server:
        yield server


@pytest.fixture
def pg_mimic_dsn(pg_mimic_server: PgServer) -> str:
    """A libpq connection string for `pg_mimic_server`, for clients that take one."""
    return pg_mimic_server.dsn()
