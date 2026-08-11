"""Pluggable authentication.

Postgres's auth handshake is more heterogeneous than MySQL's plugin-negotiation
dance -- each mechanism (trust/cleartext/MD5/SCRAM) uses a genuinely different
`Authentication*` challenge message shape, so rather than force everything
through one generic challenge/response generator (as mysql-mimic does), each
`AuthPlugin` just drives its own exchange directly against the stream.
"""

from __future__ import annotations

import hashlib
import logging
import os
from abc import ABC, abstractmethod

from scramp import ScramException, ScramMechanism

from . import messages
from .errors import INVALID_PASSWORD, PgError
from .stream import PgStream

logger = logging.getLogger(__name__)


class IdentityProvider(ABC):
    @abstractmethod
    async def get_password(self, username: str) -> str | None:
        """Return the user's plaintext password, or None to accept the user
        with any (or no) password -- the default posture for local/testing use."""


class SimpleIdentityProvider(IdentityProvider):
    """In-memory username -> plaintext-password map. Unknown usernames are
    accepted unconditionally (mirrors mysql-mimic's default SimpleIdentityProvider)."""

    def __init__(self, users: dict[str, str] | None = None):
        self.users = users or {}

    async def get_password(self, username: str) -> str | None:
        return self.users.get(username)


class AuthPlugin(ABC):
    @abstractmethod
    async def authenticate(self, stream: PgStream, username: str, identity_provider: IdentityProvider) -> bool:
        """Drive this mechanism's full challenge/response exchange. Return True
        on success, False on a rejected password."""


class TrustAuthPlugin(AuthPlugin):
    """No challenge at all -- any client is accepted. Postgres's `trust` pg_hba mode."""

    async def authenticate(self, stream, username, identity_provider) -> bool:
        return True


class ClearTextPasswordAuthPlugin(AuthPlugin):
    async def authenticate(self, stream: PgStream, username: str, identity_provider: IdentityProvider) -> bool:
        stream.write(messages.make_authentication_cleartext_password())
        await stream.drain()
        tag, payload = await stream.read_message()
        if tag != messages.PASSWORD:
            raise PgError(INVALID_PASSWORD, "expected a password response")
        password = payload.rstrip(b"\x00").decode("utf-8")
        expected = await identity_provider.get_password(username)
        return expected is None or password == expected


class Md5PasswordAuthPlugin(AuthPlugin):
    async def authenticate(self, stream: PgStream, username: str, identity_provider: IdentityProvider) -> bool:
        salt = os.urandom(4)
        stream.write(messages.make_authentication_md5_password(salt))
        await stream.drain()
        tag, payload = await stream.read_message()
        if tag != messages.PASSWORD:
            raise PgError(INVALID_PASSWORD, "expected a password response")
        response = payload.rstrip(b"\x00").decode("utf-8")

        expected_password = await identity_provider.get_password(username)
        if expected_password is None:
            return True

        inner = hashlib.md5((expected_password + username).encode("utf-8")).hexdigest()
        expected = "md5" + hashlib.md5(inner.encode("ascii") + salt).hexdigest()
        return response == expected


_SCRAM_ITERATIONS = 4096


def _hmac_sha256(key: bytes, msg: bytes) -> bytes:
    import hmac

    return hmac.new(key, msg=msg, digestmod=hashlib.sha256).digest()


def _derive_scram_credentials(password: str, salt: bytes, iterations: int) -> tuple[bytes, bytes]:
    """RFC 5802: SaltedPassword -> ClientKey/StoredKey, ServerKey."""
    salted_password = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    client_key = _hmac_sha256(salted_password, b"Client Key")
    stored_key = hashlib.sha256(client_key).digest()
    server_key = _hmac_sha256(salted_password, b"Server Key")
    return stored_key, server_key


# Any non-empty value works; scramp hands it to our auth_fn, which ignores it.
_SCRAM_USERNAME_PLACEHOLDER = "pg-mimic"


def _set_client_first(server, client_first: str) -> None:
    """Feed client-first to a scramp server, tolerating Postgres's empty username.

    libpq (and so psycopg) sends a bare `n=` here: the username already travelled
    in the startup packet, so real Postgres ignores this field entirely. RFC 5802's
    grammar nonetheless wants at least one character, and scramp >= 1.4.15 enforces
    that, rejecting every stock Postgres client.

    So substitute a placeholder to get past the parser, then restore the original
    client-first-bare. Those exact bytes go into the auth message that both sides
    sign, so they have to survive verbatim or the proofs won't match.
    """
    parts = client_first.split(",")
    # gs2 header is the first two fields (channel-binding flag, authzid); the
    # client-first-bare that gets signed is everything after it.
    if len(parts) < 3 or parts[2] != "n=":
        server.set_client_first(client_first)
        return

    patched = ",".join(parts[:2] + ["n=" + _SCRAM_USERNAME_PLACEHOLDER] + parts[3:])
    server.set_client_first(patched)
    server.client_first_bare = ",".join(parts[2:])


class ScramSha256AuthPlugin(AuthPlugin):
    """SCRAM-SHA-256 (RFC 5802 / RFC 7677), via the `scramp` library's server-side
    ScramServer -- the same library pg8000 uses client-side."""

    MECHANISM = "SCRAM-SHA-256"

    async def authenticate(self, stream: PgStream, username: str, identity_provider: IdentityProvider) -> bool:
        expected_password = await identity_provider.get_password(username)
        if expected_password is None:
            expected_password = ""  # unknown user: proceed through the exchange, then fail closed

        def auth_fn(_user: str) -> tuple[bytes, bytes, bytes, int]:
            salt = os.urandom(16)
            stored_key, server_key = _derive_scram_credentials(expected_password, salt, _SCRAM_ITERATIONS)
            return salt, stored_key, server_key, _SCRAM_ITERATIONS

        server = ScramMechanism(self.MECHANISM).make_server(auth_fn)

        stream.write(messages.make_authentication_sasl([self.MECHANISM]))
        await stream.drain()

        tag, payload = await stream.read_message()
        if tag != messages.PASSWORD:
            raise PgError(INVALID_PASSWORD, "expected a SASLInitialResponse")
        _mechanism, client_first = messages.parse_sasl_initial_response(payload)

        try:
            _set_client_first(server, client_first.decode("utf-8"))
            server_first = server.get_server_first()
            stream.write(messages.make_authentication_sasl_continue(server_first.encode("utf-8")))
            await stream.drain()

            tag, payload = await stream.read_message()
            if tag != messages.PASSWORD:
                raise PgError(INVALID_PASSWORD, "expected a SASLResponse")
            client_final = payload.decode("utf-8")

            server.set_client_final(client_final)
            server_final = server.get_server_final()
        except ScramException:
            # Covers both a wrong password and a genuinely malformed exchange, and the
            # client only ever sees "authentication failed" -- so log it, or protocol
            # bugs are indistinguishable from bad credentials.
            logger.warning("SCRAM-SHA-256 exchange failed for user %r", username, exc_info=True)
            return False

        stream.write(messages.make_authentication_sasl_final(server_final.encode("utf-8")))
        await stream.drain()
        return True
