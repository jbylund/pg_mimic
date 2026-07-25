from __future__ import annotations

import psycopg
import pytest

from conftest import MockSession, ServerThread
from pg_mimic import (
    ClearTextPasswordAuthPlugin,
    Md5PasswordAuthPlugin,
    PgServer,
    ScramSha256AuthPlugin,
    SimpleIdentityProvider,
    TrustAuthPlugin,
)


def _start(auth_plugin_factory, identity_provider=None):
    session = MockSession()
    server = PgServer(
        session_factory=lambda: session,
        auth_plugin_factory=auth_plugin_factory,
        identity_provider=identity_provider,
    )
    thread = ServerThread(server)
    port = thread.start()
    return thread, port


def test_trust_accepts_any_password():
    thread, port = _start(lambda username: TrustAuthPlugin())
    try:
        with psycopg.Connection.connect(
            f"host=127.0.0.1 port={port} user=test dbname=test password=whatever-i-want"
        ):
            pass
    finally:
        thread.stop()


@pytest.mark.parametrize(
    argnames=["plugin_cls"],
    argvalues=[[ClearTextPasswordAuthPlugin], [Md5PasswordAuthPlugin], [ScramSha256AuthPlugin]],
    ids=["cleartext", "md5", "scram_sha_256"],
)
def test_password_auth_accepts_correct_password(plugin_cls):
    identity_provider = SimpleIdentityProvider({"alice": "s3cret"})
    thread, port = _start(lambda username: plugin_cls(), identity_provider)
    try:
        with psycopg.Connection.connect(
            f"host=127.0.0.1 port={port} user=alice dbname=test password=s3cret"
        ):
            pass
    finally:
        thread.stop()


@pytest.mark.parametrize(
    argnames=["plugin_cls"],
    argvalues=[[ClearTextPasswordAuthPlugin], [Md5PasswordAuthPlugin], [ScramSha256AuthPlugin]],
    ids=["cleartext", "md5", "scram_sha_256"],
)
def test_password_auth_rejects_wrong_password(plugin_cls):
    identity_provider = SimpleIdentityProvider({"alice": "s3cret"})
    thread, port = _start(lambda username: plugin_cls(), identity_provider)
    try:
        with pytest.raises(psycopg.OperationalError):
            with psycopg.Connection.connect(
                f"host=127.0.0.1 port={port} user=alice dbname=test password=wrong"
            ):
                pass
    finally:
        thread.stop()
