"""Pluggable authentication.

Postgres's auth handshake is more heterogeneous than MySQL's plugin-negotiation
dance -- each mechanism (trust/cleartext/MD5/SCRAM) uses a genuinely different
`Authentication*` challenge message shape, so rather than force everything
through one generic challenge/response generator (as mysql-mimic does), each
`AuthPlugin` just drives its own exchange directly against the stream.
"""

from __future__ import annotations

import hashlib
import os
from abc import ABC, abstractmethod

from scramp import ScramException, ScramMechanism

from . import messages
from .errors import INVALID_PASSWORD, PgError
from .stream import PgStream


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
            server.set_client_first(client_first.decode("utf-8"))
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
            return False

        stream.write(messages.make_authentication_sasl_final(server_final.encode("utf-8")))
        await stream.drain()
        return True
