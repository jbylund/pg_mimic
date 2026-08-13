"""Query cancellation (a fresh connection sending a CancelRequest against a
running query's backend pid/secret, e.g. psql's Ctrl-C or a driver's
statement-timeout) is implemented (Connection.request_cancel(),
PgServer.cancel()) but was never exercised by any test.

Driven with a raw asyncio client (see wire.py) rather than psycopg's own
conn.cancel(): psycopg3's cancel() goes through libpq's PGcancel, which in
this environment deadlocks against the connection's own threading.Lock held
for the whole duration of the blocked cursor.execute() call on another
thread -- a psycopg/libpq threading quirk here, not something pg_mimic's
protocol handling can cause or fix. Talking the wire protocol directly tests
the actual server-side behavior without depending on any particular client
library's cancel() implementation.
"""

from __future__ import annotations

import asyncio

from conftest import ServerThread
from wire import connect_and_get_backend_key, make_cancel_request, make_query, parse_error_fields, read_message

from pg_mimic import PgServer, ResultColumn


async def test_cancel_request_interrupts_running_query(mock_session):
    server = PgServer(session_factory=mock_session.spawn)
    thread = ServerThread(server)
    port = thread.start()
    try:
        mock_session.columns = [ResultColumn.for_type("x", str)]
        # The session runs on the server's own loop in another thread, so the
        # event has to be set back on this one. asyncio.Event is not thread-safe:
        # setting it directly resolves the waiter's future without ever waking
        # this loop, so the wait below would sit until its timeout expired and
        # only then notice -- making the timeout a sleep, and the synchronisation
        # it looks like it provides an illusion.
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def slow_query(sql, params):
            loop.call_soon_threadsafe(started.set)
            await asyncio.sleep(10)
            yield ("never gets here",)  # pragma: no cover

        mock_session.query = slow_query

        reader, writer, pid, secret = await connect_and_get_backend_key(port)

        writer.write(make_query("SELECT x FROM slow_table"))
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=2)
        await asyncio.sleep(0.1)  # let the server actually be inside the sleep

        _, cancel_writer = await asyncio.open_connection("127.0.0.1", port)
        cancel_writer.write(make_cancel_request(pid, secret))
        await cancel_writer.drain()
        cancel_writer.close()
        await cancel_writer.wait_closed()

        tag, payload = await asyncio.wait_for(read_message(reader), timeout=5)
        assert tag == b"E"
        assert parse_error_fields(payload)["C"] == "57014"

        tag, _ = await asyncio.wait_for(read_message(reader), timeout=5)
        assert tag == b"Z"

        writer.close()

        await writer.wait_closed()
    finally:
        thread.stop()


async def test_cancel_request_with_wrong_secret_is_ignored(mock_session):
    server = PgServer(session_factory=mock_session.spawn)
    thread = ServerThread(server)
    port = thread.start()
    try:
        mock_session.columns = [ResultColumn.for_type("x", str)]
        # Set back on this loop rather than the server thread's -- see the note in
        # test_cancel_request_interrupts_running_query.
        started = asyncio.Event()
        loop = asyncio.get_running_loop()

        async def slow_query(sql, params):
            loop.call_soon_threadsafe(started.set)
            await asyncio.sleep(0.3)
            yield ("finished normally",)

        mock_session.query = slow_query

        reader, writer, pid, secret = await connect_and_get_backend_key(port)

        writer.write(make_query("SELECT x FROM slow_table"))
        await writer.drain()
        await asyncio.wait_for(started.wait(), timeout=2)

        _, cancel_writer = await asyncio.open_connection("127.0.0.1", port)
        cancel_writer.write(make_cancel_request(pid, secret ^ 1))
        await cancel_writer.drain()
        cancel_writer.close()
        await cancel_writer.wait_closed()

        # a mismatched secret must not cancel someone else's/this query --
        # it should complete normally instead of getting an ErrorResponse
        tag, payload = await asyncio.wait_for(read_message(reader), timeout=5)
        assert tag == b"T"  # RowDescription: the query actually ran to completion

        # Drain to ReadyForQuery before hanging up. Closing mid-result leaves the
        # server's handler suspended part-way through streaming, which is a state
        # no real client leaves behind and which shutdown then has to unpick.
        while tag != b"Z":
            tag, _ = await asyncio.wait_for(read_message(reader), timeout=5)

        writer.close()
        await writer.wait_closed()
    finally:
        thread.stop()
