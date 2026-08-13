from __future__ import annotations

import psycopg
import pytest
from conftest import MockSession
from scramp import ScramMechanism

from pg_mimic import (
    ClearTextPasswordAuthPlugin,
    Md5PasswordAuthPlugin,
    ScramSha256AuthPlugin,
    SimpleIdentityProvider,
    TrustAuthPlugin,
)
from pg_mimic.auth import _derive_scram_credentials, _set_client_first
from pg_mimic.testing import serve_in_thread


def _serving(auth_plugin_factory, identity_provider=None):
    """A server whose only interesting property is how it authenticates -- these
    tests never get as far as running a query, so the session is a placeholder."""
    session = MockSession()
    return serve_in_thread(
        lambda: session,
        auth_plugin_factory=auth_plugin_factory,
        identity_provider=identity_provider,
    )


def test_trust_accepts_any_password():
    with _serving(lambda username: TrustAuthPlugin()) as server:
        with psycopg.Connection.connect(server.dsn(user="test", dbname="test") + " password=whatever-i-want"):
            pass


@pytest.mark.parametrize(
    argnames=["plugin_cls"],
    argvalues=[[ClearTextPasswordAuthPlugin], [Md5PasswordAuthPlugin], [ScramSha256AuthPlugin]],
    ids=["cleartext", "md5", "scram_sha_256"],
)
def test_password_auth_accepts_correct_password(plugin_cls):
    identity_provider = SimpleIdentityProvider({"alice": "s3cret"})
    with _serving(lambda username: plugin_cls(), identity_provider) as server:
        with psycopg.Connection.connect(server.dsn(user="alice", dbname="test") + " password=s3cret"):
            pass


@pytest.mark.parametrize(
    argnames=["plugin_cls"],
    argvalues=[[ClearTextPasswordAuthPlugin], [Md5PasswordAuthPlugin], [ScramSha256AuthPlugin]],
    ids=["cleartext", "md5", "scram_sha_256"],
)
def test_password_auth_rejects_wrong_password(plugin_cls):
    identity_provider = SimpleIdentityProvider({"alice": "s3cret"})
    with _serving(lambda username: plugin_cls(), identity_provider) as server:
        with pytest.raises(psycopg.OperationalError):
            with psycopg.Connection.connect(server.dsn(user="alice", dbname="test") + " password=wrong"):
                pass


_client_first_testcases = {
    # What libpq/psycopg actually sends -- rejected by scramp >= 1.4.15.
    "empty_username": {
        "client_first": "n,,n=,r=rOprNGfwEbeRWgbNEkqO",
        "expected_bare": "n=,r=rOprNGfwEbeRWgbNEkqO",
    },
    # A username-bearing client is passed through untouched.
    "explicit_username": {
        "client_first": "n,,n=alice,r=rOprNGfwEbeRWgbNEkqO",
        "expected_bare": "n=alice,r=rOprNGfwEbeRWgbNEkqO",
    },
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(_client_first_testcases.values()))),
    argvalues=[[v for k, v in sorted(_client_first_testcases[testname].items())] for testname in sorted(_client_first_testcases)],
    ids=sorted(_client_first_testcases),
)
def test_set_client_first_preserves_signed_bare(client_first, expected_bare):
    """libpq sends a bare `n=` (the username rides in the startup packet instead),
    which scramp >= 1.4.15 rejects as an empty attribute value. We work around it,
    but the client-first-bare feeds the auth message both sides sign, so it has to
    come back out byte-for-byte or every proof would mismatch."""
    salt = b"\x01" * 16
    stored_key, server_key = _derive_scram_credentials("s3cret", salt, 4096)
    server = ScramMechanism("SCRAM-SHA-256").make_server(lambda user: (salt, stored_key, server_key, 4096))

    _set_client_first(server, client_first)

    assert server.client_first_bare == expected_bare
