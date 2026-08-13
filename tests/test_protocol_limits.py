"""What the server does with a frame it should not trust.

Every length on the way in is a number the peer chose, and until these tests
existed the server acted on all of them: a bogus Int32 bought a read of up to
2GB that a client only had to start filling, and a length of 0 or -1 was quietly
rounded up to "empty payload" instead of being called out. The protocol version
went unread entirely.

Driven by hand (see wire.py) for the usual reason -- no client library will send
a length that contradicts the bytes behind it, which is exactly the frame that
needs testing. The server-survives half matters as much as the rejection: each
test's `serve_in_thread` exit asserts the thread shut down, which a connection
still parked on a read it will never finish would prevent.
"""

from __future__ import annotations

import asyncio

import psycopg
import pytest
from wire import (
    PROTOCOL_3_0,
    SYNC,
    connect_and_get_backend_key,
    make_bind,
    make_execute,
    make_lying_message,
    make_lying_startup,
    make_parse,
    make_query,
    make_startup,
    parse_error_fields,
    read_message,
)

from pg_mimic import ResultColumn
from pg_mimic.stream import MAX_STARTUP_PACKET_LENGTH
from pg_mimic.testing import serve_in_thread
from pg_mimic.types import unpack_int32

PROTOCOL_3_2 = (3 << 16) | 2
PROTOCOL_VIOLATION = "08P01"
FEATURE_NOT_SUPPORTED = "0A000"

# Comfortably larger than anything a client library sends as one message (psycopg
# splits COPY into 128KiB chunks, asyncpg into 512KiB ones) and comfortably under
# the default cap, so it exercises "large" rather than "over the line".
LARGE_PARAMETER = b"x" * (1024 * 1024)


async def read_error(reader: asyncio.StreamReader) -> dict[str, str]:
    tag, payload = await asyncio.wait_for(read_message(reader), timeout=2)
    assert tag == b"E", f"expected an ErrorResponse, got {tag!r}"
    return parse_error_fields(payload)


async def assert_hung_up(reader: asyncio.StreamReader) -> None:
    """The connection is gone, and gone promptly -- a FATAL that left the socket
    open would be a handler task and a socket held by a client we just refused."""
    assert await asyncio.wait_for(reader.read(), timeout=2) == b""


async def drain_to_ready(reader: asyncio.StreamReader) -> None:
    tag = b""
    while tag != b"Z":
        tag, _ = await asyncio.wait_for(read_message(reader), timeout=2)


bad_message_lengths = {
    "two_gigabytes": {"length": 2_000_000_000, "expected": "exceeds the"},
    "just_over_the_cap": {"length": 64 * 1024 * 1024 + 1, "expected": "exceeds the"},
    "zero": {"length": 0, "expected": "must be at least 4"},
    "one_short_of_the_minimum": {"length": 3, "expected": "must be at least 4"},
    "negative": {"length": -1, "expected": "must be at least 4"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(bad_message_lengths.values()))),
    argvalues=[[v for k, v in sorted(bad_message_lengths[name].items())] for name in sorted(bad_message_lengths)],
    ids=sorted(bad_message_lengths),
)
async def test_a_length_that_cannot_be_true_is_refused(mock_session, expected, length):
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)

        # The header and nothing else: a client sending 2GB would have to send it,
        # and the point is that the server never waits to find out whether it will.
        writer.write(make_lying_message(b"Q", length))
        await writer.drain()

        fields = await read_error(reader)
        assert fields["C"] == PROTOCOL_VIOLATION
        assert fields["S"] == "FATAL"
        assert expected in fields["M"]
        await assert_hung_up(reader)

        writer.close()
        await writer.wait_closed()


bad_startup_lengths = {
    "over_postgres_own_startup_limit": {"length": MAX_STARTUP_PACKET_LENGTH + 1, "expected": "exceeds the"},
    "two_gigabytes": {"length": 2_000_000_000, "expected": "exceeds the"},
    "zero": {"length": 0, "expected": "must be at least 8"},
    # 4 counts the length itself but not the protocol version that follows it --
    # eight bytes have already been read by the time it is checked.
    "no_room_for_the_version": {"length": 4, "expected": "must be at least 8"},
    "negative": {"length": -1, "expected": "must be at least 8"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(bad_startup_lengths.values()))),
    argvalues=[[v for k, v in sorted(bad_startup_lengths[name].items())] for name in sorted(bad_startup_lengths)],
    ids=sorted(bad_startup_lengths),
)
async def test_a_startup_packet_length_that_cannot_be_true_is_refused(mock_session, expected, length):
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(make_lying_startup(length))
        await writer.drain()

        fields = await read_error(reader)
        assert fields["C"] == PROTOCOL_VIOLATION
        assert fields["S"] == "FATAL"
        assert expected in fields["M"]
        await assert_hung_up(reader)

        writer.close()
        await writer.wait_closed()


async def test_the_startup_limit_is_postgres_own_however_large_messages_may_get(mock_session):
    """A generous `max_message_size` does not buy the startup packet room. Nothing
    has authenticated at that point, so the ceiling stays the 10000 bytes real
    Postgres allows -- which every real client fits inside with room to spare."""
    with serve_in_thread(mock_session.spawn, max_message_size=256 * 1024 * 1024) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(make_lying_startup(MAX_STARTUP_PACKET_LENGTH + 1))
        await writer.drain()

        fields = await read_error(reader)
        assert fields["C"] == PROTOCOL_VIOLATION
        assert f"{MAX_STARTUP_PACKET_LENGTH}-byte maximum" in fields["M"]

        writer.close()
        await writer.wait_closed()


async def test_a_large_but_legal_message_still_arrives_whole(mock_session):
    """The cap has to clear what real traffic looks like. A megabyte bind
    parameter -- a file on its way into a bytea column, the thing that actually
    approaches this limit -- goes through untouched."""
    mock_session.columns = [ResultColumn.for_type("x", str)]
    mock_session.rows = [("ok",)]
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)

        writer.write(make_parse("SELECT $1", statement_name="s"))
        writer.write(make_bind(statement_name="s", portal_name="p", params=[LARGE_PARAMETER]))
        writer.write(make_execute("p"))
        writer.write(SYNC)
        await writer.drain()
        await drain_to_ready(reader)

        assert mock_session.queries[-1][1] == [LARGE_PARAMETER.decode()]

        writer.close()
        await writer.wait_closed()


async def test_max_message_size_is_the_servers_to_set(mock_session):
    """The knob, from both sides: a message the default would have accepted is
    refused under a smaller cap, and one that fits is answered as usual."""
    mock_session.columns = [ResultColumn.for_type("x", str)]
    mock_session.rows = [("ok",)]
    with serve_in_thread(mock_session.spawn, max_message_size=4096) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query(f"SELECT '{'x' * 5000}'"))
        await writer.drain()

        fields = await read_error(reader)
        assert fields["C"] == PROTOCOL_VIOLATION
        assert "exceeds the 4096-byte maximum" in fields["M"]
        await assert_hung_up(reader)
        writer.close()
        await writer.wait_closed()

        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query("SELECT x FROM t"))
        await writer.drain()
        await drain_to_ready(reader)
        writer.close()
        await writer.wait_closed()


async def test_one_clients_bad_frame_is_not_another_clients_problem(mock_session):
    """The reason any of this matters: a malformed frame costs its own connection
    and nothing else. A legitimate session opened before it keeps working, and the
    listener keeps accepting."""
    mock_session.columns = [ResultColumn.for_type("x", str)]
    mock_session.rows = [("ok",)]
    with serve_in_thread(mock_session.spawn) as server:
        good_reader, good_writer, _pid, _secret = await connect_and_get_backend_key(server.port)

        bad_reader, bad_writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        bad_writer.write(make_lying_message(b"Q", 2_000_000_000))
        await bad_writer.drain()
        assert (await read_error(bad_reader))["C"] == PROTOCOL_VIOLATION
        await assert_hung_up(bad_reader)
        bad_writer.close()
        await bad_writer.wait_closed()

        good_writer.write(make_query("SELECT x FROM t"))
        await good_writer.drain()
        tag, _ = await asyncio.wait_for(read_message(good_reader), timeout=2)
        assert tag == b"T"  # the query ran, on a connection the other one never touched
        await drain_to_ready(good_reader)
        good_writer.close()
        await good_writer.wait_closed()

        # And a client that arrives afterwards is served, i.e. the listener itself
        # survived. Sync psycopg is safe here: the server has its own thread.
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            assert conn.execute("SELECT x FROM t").fetchall() == [("ok",)]


async def test_a_newer_minor_version_is_negotiated_down(mock_session):
    """3.2 is a version we don't speak, and a minor version is negotiable: the
    client is told what it is getting rather than left to assume."""
    mock_session.columns = [ResultColumn.for_type("x", str)]
    mock_session.rows = [("ok",)]
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(make_startup(version=PROTOCOL_3_2))
        await writer.drain()

        tag, payload = await asyncio.wait_for(read_message(reader), timeout=2)
        assert tag == b"v"  # NegotiateProtocolVersion, before authentication
        assert unpack_int32(payload, 0) == PROTOCOL_3_0
        assert unpack_int32(payload, 4) == 0  # no unrecognised options, just the version

        # ...and the connection carries on as a 3.0 one.
        await drain_to_ready(reader)
        writer.write(make_query("SELECT x FROM t"))
        await writer.drain()
        tag, _ = await asyncio.wait_for(read_message(reader), timeout=2)
        assert tag == b"T"
        await drain_to_ready(reader)

        writer.close()
        await writer.wait_closed()


async def test_the_version_we_do_speak_is_negotiated_silently(mock_session):
    """No NegotiateProtocolVersion when there is nothing to negotiate -- a 3.0
    client's first reply is AuthenticationOk, as it has always been."""
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(make_startup())
        await writer.drain()

        tag, _ = await asyncio.wait_for(read_message(reader), timeout=2)
        assert tag == b"R"

        await drain_to_ready(reader)
        writer.close()
        await writer.wait_closed()


async def test_an_unrecognised_protocol_extension_is_reported_not_applied(mock_session):
    """NegotiateProtocolVersion's other job. `_pq_.` parameters are protocol
    extension requests, so a session must not see one as a setting it was asked
    to apply -- it is named back to the client instead."""
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(make_startup(extra={"_pq_.made_up": "1", "application_name": "app"}))
        await writer.drain()

        tag, payload = await asyncio.wait_for(read_message(reader), timeout=2)
        assert tag == b"v"
        assert unpack_int32(payload, 0) == PROTOCOL_3_0
        assert unpack_int32(payload, 4) == 1
        assert payload[8:] == b"_pq_.made_up\x00"

        await drain_to_ready(reader)
        connection = next(iter(server._connections.values()))
        assert "_pq_.made_up" not in connection.startup_params
        assert connection.startup_params["application_name"] == "app"
        assert connection.protocol_version == PROTOCOL_3_0

        writer.close()
        await writer.wait_closed()


unsupported_majors = {
    "version_2_0": {"version": 2 << 16},
    "version_4_0": {"version": 4 << 16},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(unsupported_majors.values()))),
    argvalues=[[v for k, v in sorted(unsupported_majors[name].items())] for name in sorted(unsupported_majors)],
    ids=sorted(unsupported_majors),
)
async def test_a_major_version_we_do_not_speak_is_refused(mock_session, version):
    """A major version is not negotiable -- 2.0 framed its messages differently.
    Said plainly, rather than left to surface as a parse failure against bytes
    read the wrong way."""
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
        writer.write(make_startup(version=version))
        await writer.drain()

        fields = await read_error(reader)
        assert fields["C"] == FEATURE_NOT_SUPPORTED
        assert fields["S"] == "FATAL"
        assert f"unsupported frontend protocol {version >> 16}.0" in fields["M"]
        assert "server supports 3.0 to 3.0" in fields["M"]
        await assert_hung_up(reader)

        writer.close()
        await writer.wait_closed()


@pytest.mark.skipif(psycopg.pq.version() < 180000, reason="libpq 18 is where max_protocol_version and 3.2 arrived")
def test_libpq_asking_for_3_2_connects_anyway(mock_session):
    """Pinned against a real libpq rather than assumed. libpq 18 defaults to 3.0
    but asks for 3.2 on request, and accepts the downgrade only if it can parse
    the NegotiateProtocolVersion it gets back -- so this is what proves the
    message's version field carries the whole major/minor word."""
    mock_session.columns = [ResultColumn.for_type("x", str)]
    mock_session.rows = [("ok",)]
    with serve_in_thread(mock_session.spawn) as server:
        dsn = server.dsn(user="test", dbname="test") + " max_protocol_version=latest"
        with psycopg.connect(dsn, autocommit=True) as conn:
            assert conn.execute("SELECT x FROM t").fetchall() == [("ok",)]

        # And a client that refuses to be downgraded gets to refuse: the reply is
        # read, understood, and found wanting, which no unparsed message could be.
        floor = server.dsn(user="test", dbname="test") + " min_protocol_version=3.2 max_protocol_version=3.2"
        with pytest.raises(psycopg.OperationalError, match="3.2|protocol"):
            psycopg.connect(floor)
