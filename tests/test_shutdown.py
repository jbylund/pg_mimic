"""Server shutdown with connections still live.

From Python 3.12 a cancelled `Server.serve_forever()` awaits `wait_closed()`
before re-raising, and that waits for every active connection. So a client that
is still connected -- or one that hung up without the server closing its side --
used to leave `PgServer.close()` unable to shut the server down at all. On 3.10
the same code shut down fine, which is why it went unnoticed.

These drive the server directly rather than through the ServerThread fixture:
the bug is in what close() does, and the fixture is one of the things that was
papering over it.
"""

from __future__ import annotations

import asyncio

import pytest
from conftest import MockSession

from pg_mimic import PgServer, ResultColumn


async def _serving():
    session = MockSession()
    session.columns = [ResultColumn.for_type("x", int)]
    session.rows = [(1,)]
    server = PgServer(session_factory=lambda: session)
    await server.start_server(host="127.0.0.1", port=0)
    port = server.sockets()[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0)  # let serve_forever actually start before anyone closes it
    return server, port, serve_task


async def _shuts_down(serve_task):
    """close() has been called; did serve_forever actually come back?"""
    try:
        # Comfortably under the suite-wide per-test timeout, so a regression here
        # reports as this assertion rather than as a bare pytest timeout.
        await asyncio.wait_for(serve_task, timeout=2)
    except asyncio.TimeoutError:
        return False
    except asyncio.CancelledError:
        pass
    return True


async def test_close_shuts_down_with_no_connections():
    server, _port, serve_task = await _serving()
    server.close()
    assert await _shuts_down(serve_task)


async def test_close_shuts_down_with_a_client_still_connected():
    """The case that hung: the client never goes away, so waiting for it can't work
    -- close() has to drop the connection rather than wait for it."""
    server, port, serve_task = await _serving()
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    await asyncio.sleep(0)

    server.close()
    assert await _shuts_down(serve_task)

    writer.close()


async def test_close_shuts_down_after_a_client_hung_up():
    """Also hung, less obviously: the handler returns on EOF without closing its
    own side, so asyncio still counted the connection as active."""
    server, port, serve_task = await _serving()
    _reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0)

    server.close()
    assert await _shuts_down(serve_task)


async def test_close_shuts_down_mid_query():
    """A handler suspended part-way through streaming a result -- what a client
    that reads one message and hangs up leaves behind."""
    session = MockSession()
    session.columns = [ResultColumn.for_type("x", int)]

    async def slow_query(sql, params):
        await asyncio.sleep(30)  # never finishes on its own within the test
        yield (1,)

    session.query = slow_query
    server = PgServer(session_factory=lambda: session)
    await server.start_server(host="127.0.0.1", port=0)
    port = server.sockets()[0].getsockname()[1]
    serve_task = asyncio.create_task(server.serve_forever())
    await asyncio.sleep(0)

    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(b"\x00\x00\x00\x08\x04\xd2\x16/")  # SSLRequest, answered with 'N'
    await writer.drain()
    assert await reader.readexactly(1) == b"N"

    server.close()
    assert await _shuts_down(serve_task)

    writer.close()


@pytest.mark.parametrize(argnames=["connections"], argvalues=[[1], [5]], ids=["one", "several"])
async def test_close_drops_every_connection(connections):
    server, port, serve_task = await _serving()
    writers = []
    for _ in range(connections):
        _reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(writer)
    await asyncio.sleep(0)

    server.close()
    assert await _shuts_down(serve_task)

    for writer in writers:
        writer.close()
