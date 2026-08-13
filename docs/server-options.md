# Server options

Two things `PgServer` takes that shape a connection before any session sees it: who is allowed to
open one, and how much a client is allowed to send down it.

## Authentication

Four pluggable mechanisms, matching real Postgres's `pg_hba.conf` methods:

```python
from pg_mimic import PgServer, TrustAuthPlugin, Md5PasswordAuthPlugin, ScramSha256AuthPlugin, SimpleIdentityProvider

# trust (default): any username/password accepted
server = PgServer(session_factory=MySession)

# password-protected
server = PgServer(
    session_factory=MySession,
    auth_plugin_factory=lambda username: ScramSha256AuthPlugin(),  # or Md5PasswordAuthPlugin / ClearTextPasswordAuthPlugin
    identity_provider=SimpleIdentityProvider({"alice": "s3cret"}),
)
```

## Message size and protocol version

Every message a client sends is length-prefixed, and that length is the client's word for how much
the server should buffer. pg_mimic checks it before acting on it. A length larger than
`max_message_size`, or one that contradicts the header it belongs to — 0, negative, or under the four
bytes it counts for itself — is refused with `08P01` (`protocol_violation`) at `FATAL` severity, and
that connection alone is dropped:

```python
server = PgServer(session_factory=MySession, max_message_size=8 * 1024 * 1024)
```

The default is 64 MiB. Real Postgres's own limit is 1 GB, which is the size of the values it has to
be able to store; a mimic holds the whole message in memory and answers from Python objects, so it
has no such obligation, and a limit that large would leave a single bogus `Int32` able to park the
server on a two-gigabyte read. 64 MiB is what MySQL's `max_allowed_packet` picked for the same job,
and it clears real traffic by orders of magnitude — psycopg splits a `COPY` stream into 128 KiB
messages and asyncpg into 512 KiB ones, so what actually approaches this number is a single enormous
bind parameter, such as a file on its way into a `bytea` column. The startup packet keeps its own,
much smaller ceiling of 10 000 bytes, matching real Postgres: nothing has authenticated at that
point.

The protocol version in the startup packet is read rather than assumed. pg_mimic speaks 3.0, so a
client asking for a newer *minor* version — libpq 18 does, given `max_protocol_version=latest` — gets
a `NegotiateProtocolVersion` telling it what it is actually getting, and carries on. That message
also names any `_pq_.` protocol extension the client asked for, which pg_mimic reports back rather
than passing to your session as a setting. A *major* version pg_mimic doesn't speak is refused with
`0A000` and a clear message, instead of a parse failure somewhere further in against bytes read the
wrong way.
