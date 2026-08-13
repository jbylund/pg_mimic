"""Server-to-client push: NoticeResponse, and LISTEN/NOTIFY.

Two kinds of test here, and the split matters.

The client-library tests (psycopg's `add_notice_handler` and `notifies()`,
asyncpg's `add_listener` and `add_log_listener`) prove the feature is usable --
that the drivers people actually reach for see what they expect.

The raw-wire tests prove the part a client cannot: *where in the byte stream* an
async message lands. A NotificationResponse written between two DataRows is not
an error any driver reports; it is a row silently mis-parsed. A client that fails
to complain is no evidence at all, so those assertions are made on the tag
sequence off the socket, against the sequence a real PostgreSQL 18 produces for
the same exchange (recorded in Connection's own comments).
"""

from __future__ import annotations

import asyncio
import threading

import asyncpg
import psycopg
import pytest
from conftest import MockSession, ServerThread
from wire import (
    FLUSH,
    SYNC,
    TARGET_PORTAL,
    connect_and_get_backend_key,
    make_bind,
    make_describe,
    make_execute,
    make_parse,
    make_query,
    parse_error_fields,
    parse_notification,
    read_message,
    read_until,
)

from pg_mimic import PgServer, ResultColumn, Session
from pg_mimic.errors import NO_ACTIVE_SQL_TRANSACTION
from pg_mimic.testing import serve, serve_in_thread


def rows_session(rows, columns=None):
    """A *factory* rather than a session, and every test here uses one.

    Sharing a single session object across connections quietly breaks anything
    multi-connection: `Session.prepare()` resolves the middleware chain through
    `self._connection`, so one object serving three connections routes every
    statement through whichever attached last -- and a `LISTEN` then subscribes
    the wrong connection.
    """

    def make() -> MockSession:
        session = MockSession()
        session.rows = list(rows)
        session.columns = columns or [ResultColumn.for_type("x", int)]
        return session

    return make


def apg_dsn(server: PgServer) -> str:
    """asyncpg wants a URL, not the keyword form `PgServer.dsn()` returns."""
    return f"postgresql://test@127.0.0.1:{server.port}/test"


# --- the sequencing rule, proved on the wire ----------------------------------------


async def test_notify_during_a_query_lands_after_command_complete():
    """A notification raised while a query is producing rows must not interleave
    into the DataRow run. PostgreSQL 18 answers `T D D D D D C A Z`; so does this.

    The session parks in an executor rather than on the loop, so the server stays
    free to accept the notifier's connection and run its NOTIFY while this
    connection is provably mid-command.
    """
    producing = threading.Event()
    release = threading.Event()

    def make_session() -> MockSession:
        session = MockSession()
        session.columns = [ResultColumn.for_type("x", int)]

        async def query(sql, params):
            producing.set()
            await asyncio.get_running_loop().run_in_executor(None, release.wait)
            for i in range(5):
                yield (i,)

        session.query = query
        return session

    with serve_in_thread(make_session) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query("LISTEN chan"))
        await writer.drain()
        assert await read_until(reader, b"Z") == [b"C", b"Z"]

        writer.write(make_query("SELECT x FROM t"))
        await writer.drain()
        await asyncio.get_running_loop().run_in_executor(None, producing.wait)

        # Fired from a second connection while the first is demonstrably inside its
        # own command, and awaited to completion so the fanout has certainly run.
        notifier, notifier_writer, _p, _s = await connect_and_get_backend_key(server.port)
        notifier_writer.write(make_query("NOTIFY chan, 'mid-query'"))
        await notifier_writer.drain()
        await read_until(notifier, b"Z")
        release.set()

        tags = await read_until(reader, b"Z")
        assert tags == [b"T", b"D", b"D", b"D", b"D", b"D", b"C", b"A", b"Z"]

        writer.close()
        notifier_writer.close()


async def test_notify_mid_portal_drain_waits_for_sync():
    """A suspended portal is not a safe point, even though the connection is
    sitting idle on the socket.

    PortalSuspended is not ReadyForQuery. The portal is driven with Flush rather
    than Sync precisely so no ReadyForQuery is sent: the connection is then parked
    on the socket with a half-drained portal, which is the state that looks safe
    and is not. Measured against PostgreSQL 18, a notification raised there is
    held past the next Execute's DataRows and delivered at Sync -- `D D s`, then
    `A Z`.
    """
    with serve_in_thread(rows_session([(i,) for i in range(6)])) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query("LISTEN chan"))
        await writer.drain()
        await read_until(reader, b"Z")

        writer.write(make_parse("SELECT x FROM t"))
        writer.write(make_bind())
        writer.write(make_execute(max_rows=2))
        writer.write(FLUSH)
        await writer.drain()
        assert await read_until(reader, b"s") == [b"1", b"2", b"D", b"D", b"s"]

        notifier, notifier_writer, _p, _s = await connect_and_get_backend_key(server.port)
        notifier_writer.write(make_query("NOTIFY chan, 'mid-portal'"))
        await notifier_writer.drain()
        await read_until(notifier, b"Z")

        # The portal is half-drained and the notification is already queued. The
        # next Execute must carry its rows and nothing else -- an 'A' anywhere in
        # here is a DataRow the client would mis-parse.
        writer.write(make_execute(max_rows=2))
        writer.write(FLUSH)
        await writer.drain()
        assert await read_until(reader, b"s") == [b"D", b"D", b"s"]

        # Sync ends the exchange, and only now is there a legal place for it.
        writer.write(SYNC)
        await writer.drain()
        assert await read_until(reader, b"Z") == [b"A", b"Z"]

        writer.close()
        notifier_writer.close()


async def test_notify_while_idle_at_ready_for_query_is_delivered_at_once():
    """The counterpart: a connection that really is between ReadyForQuery and its
    next command gets the notification immediately, as its own message, rather
    than waiting for something to ask. That is what makes `conn.notifies()` and
    `add_listener` work at all on an otherwise silent connection."""
    with serve_in_thread(MockSession) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query("LISTEN chan"))
        await writer.drain()
        assert await read_until(reader, b"Z") == [b"C", b"Z"]

        notifier, notifier_writer, notifier_pid, _s = await connect_and_get_backend_key(server.port)
        notifier_writer.write(make_query("NOTIFY chan, 'while idle'"))
        await notifier_writer.drain()
        await read_until(notifier, b"Z")

        tag, payload = await read_message(reader)
        assert tag == b"A"
        assert parse_notification(payload) == (notifier_pid, "chan", "while idle")

        writer.close()
        notifier_writer.close()


class NoticeSession(Session):
    async def describe(self, sql, param_oids):
        return [ResultColumn.for_type("x", int)]

    async def query(self, sql, params):
        self.connection.notice("halfway there", severity="WARNING", D="some detail")
        yield (1,)
        yield (2,)


async def test_notice_attaches_to_its_query_in_the_extended_protocol():
    """A notice raised while answering belongs to that query's response, ahead of
    CommandComplete -- where PostgreSQL 18 puts the one it emits for a
    `DROP TABLE IF EXISTS` of a missing table (`N C Z`).

    The extended protocol is the path that matters, being the one psycopg and
    asyncpg actually use, and it places the notice exactly as Postgres does: after
    the RowDescription, among the rows it belongs to, before CommandComplete.
    """
    with serve_in_thread(NoticeSession) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_parse("SELECT x FROM t"))
        writer.write(make_bind())
        writer.write(make_describe(TARGET_PORTAL))
        writer.write(make_execute())
        writer.write(SYNC)
        await writer.drain()

        tags = []
        fields = None
        while True:
            tag, payload = await read_message(reader)
            tags.append(tag)
            if tag == b"N":
                fields = parse_error_fields(payload)
            if tag == b"Z":
                break
        assert tags == [b"1", b"2", b"T", b"N", b"D", b"D", b"C", b"Z"]
        assert fields["S"] == "WARNING"
        assert fields["V"] == "WARNING"
        assert fields["M"] == "halfway there"
        assert fields["D"] == "some detail"
        writer.close()


async def test_notice_precedes_the_row_description_in_the_simple_protocol():
    """The one place pg_mimic's notice lands earlier than Postgres would put it.

    The simple-query path drains the whole portal before it writes anything, so a
    notice raised during execution is already on the stream by the time the
    RowDescription is -- `N T D D C` where Postgres has `T N D D C`. Harmless:
    NoticeResponse is defined to be legal at any point and every client treats it
    out of band, which is exactly why both drivers report it either way. Asserted
    rather than glossed over, so a future change to that path is a deliberate one.
    """
    with serve_in_thread(NoticeSession) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query("SELECT x FROM t"))
        await writer.drain()
        assert await read_until(reader, b"Z") == [b"N", b"T", b"D", b"D", b"C", b"Z"]
        writer.close()


async def test_notification_carries_the_notifying_pid():
    with serve_in_thread(MockSession) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)
        writer.write(make_query("LISTEN chan"))
        await writer.drain()
        await read_until(reader, b"Z")

        notifier, notifier_writer, notifier_pid, _s = await connect_and_get_backend_key(server.port)
        notifier_writer.write(make_query("NOTIFY chan, 'payload'"))
        await notifier_writer.drain()
        await read_until(notifier, b"Z")

        tag, payload = await read_message(reader)
        assert tag == b"A"
        assert parse_notification(payload) == (notifier_pid, "chan", "payload")
        writer.close()
        notifier_writer.close()


# --- psycopg ------------------------------------------------------------------------


def test_psycopg_add_notice_handler():
    class NoticeSession(Session):
        async def describe(self, sql, param_oids):
            return [ResultColumn.for_type("x", int)]

        async def query(self, sql, params):
            self.connection.notice(f"answering {sql}")
            yield (1,)

    with serve_in_thread(NoticeSession) as server:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            seen = []
            conn.add_notice_handler(lambda diag: seen.append((diag.severity, diag.message_primary)))
            conn.execute("SELECT x FROM t").fetchall()
    assert seen == [("NOTICE", "answering SELECT x FROM t")]


def test_psycopg_set_local_outside_a_transaction_warns():
    """The warning real Postgres gives for a SET LOCAL with no transaction to be
    local to -- wording, severity and SQLSTATE all read off a PostgreSQL 18
    socket. pg_mimic did nothing silently until it had a way to say so."""
    with serve_in_thread(MockSession) as server:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            seen = []
            conn.add_notice_handler(lambda diag: seen.append((diag.severity, diag.sqlstate, diag.message_primary)))
            conn.execute("SET LOCAL statement_timeout = '1s'")
            assert conn.execute("SHOW statement_timeout").fetchone() == ("0",)
    assert seen == [("WARNING", NO_ACTIVE_SQL_TRANSACTION, "SET LOCAL can only be used in transaction blocks")]


def test_psycopg_notifies():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN orders")
            notifier.execute("NOTIFY orders, 'created'")
            got = [(n.channel, n.payload, n.pid) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("orders", "created", notifier.info.backend_pid)]


def test_psycopg_two_connection_fanout():
    """Every listener on the server gets it, the notifier included if it listens."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with (
            psycopg.connect(dsn, autocommit=True) as first,
            psycopg.connect(dsn, autocommit=True) as second,
            psycopg.connect(dsn, autocommit=True) as notifier,
        ):
            first.execute("LISTEN chan")
            second.execute("LISTEN chan")
            notifier.execute("LISTEN chan")
            assert len(server.listeners("chan")) == 3

            notifier.execute("NOTIFY chan, 'everyone'")
            for conn in (first, second, notifier):
                got = [(n.channel, n.payload) for n in conn.notifies(timeout=5, stop_after=1)]
                assert got == [("chan", "everyone")]


def test_pg_notify_function():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN chan")
            assert notifier.execute("SELECT pg_notify('chan', 'from a function')").fetchone() == ("",)
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "from a function")]


def test_pg_notify_does_not_fold_its_channel():
    """`pg_notify` takes the channel as a string, so it is not case-folded the way
    the identifier in `NOTIFY` is -- measured on PostgreSQL 18, where
    `pg_notify('CHAN', ...)` never reaches `LISTEN chan`."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("SELECT pg_notify('CHAN', 'unfolded')")
            notifier.execute("NOTIFY CHAN, 'folded'")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "folded")]


# --- asyncpg ------------------------------------------------------------------------


async def test_asyncpg_add_listener():
    async with serve(MockSession) as server:
        listener = await asyncpg.connect(apg_dsn(server))
        notifier = await asyncpg.connect(apg_dsn(server))
        try:
            received: asyncio.Queue = asyncio.Queue()

            def on_notify(conn, pid, channel, payload):
                received.put_nowait((pid, channel, payload))

            await listener.add_listener("events", on_notify)
            await notifier.execute("NOTIFY events, 'hello asyncpg'")
            pid, channel, payload = await asyncio.wait_for(received.get(), timeout=5)
            assert (channel, payload) == ("events", "hello asyncpg")
            assert pid == notifier.get_server_pid()

            await listener.remove_listener("events", on_notify)
            assert server.listeners("events") == set()
        finally:
            await listener.close()
            await notifier.close()


async def test_asyncpg_add_log_listener():
    class NoticeSession(Session):
        async def describe(self, sql, param_oids):
            return [ResultColumn.for_type("x", int)]

        async def query(self, sql, params):
            self.connection.notice("a deprecation, say", severity="WARNING")
            yield (1,)

    async with serve(NoticeSession) as server:
        conn = await asyncpg.connect(apg_dsn(server))
        try:
            logged: asyncio.Queue = asyncio.Queue()
            conn.add_log_listener(lambda connection, message: logged.put_nowait(message))
            await conn.fetch("SELECT x FROM t")
            message = await asyncio.wait_for(logged.get(), timeout=5)
            assert message.severity == "WARNING"
            assert message.message == "a deprecation, say"
        finally:
            await conn.close()


# --- transaction semantics ----------------------------------------------------------


def test_notify_is_delivered_at_commit():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("NOTIFY chan, 'committed'")
            assert list(listener.notifies(timeout=0.3)) == [], "delivered before COMMIT"
            notifier.commit()
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "committed")]


def test_notify_is_dropped_by_rollback():
    """The case the deferral exists for: a rolled-back unit of work must not have
    announced itself. Delivering eagerly would make that a false positive in
    exactly the event-driven code this feature is for."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("NOTIFY chan, 'rolled back'")
            notifier.rollback()
            assert list(listener.notifies(timeout=0.3)) == []

            # And the connection still works afterwards: the dropped notification
            # is gone rather than left pending for the next COMMIT to deliver.
            notifier.execute("NOTIFY chan, 'kept'")
            notifier.commit()
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "kept")]


def test_transaction_deferral_in_a_multi_statement_batch():
    """`BEGIN; NOTIFY ...; COMMIT;` as one simple-query message -- the shape a
    `psql -f script.sql` sends, where the whole transaction lives inside a single
    'Q' and the deferral has to survive being split back out of it."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as writer:
            listener.execute("LISTEN c")
            writer.execute("BEGIN; NOTIFY c, 'batched'; COMMIT;")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("c", "batched")]

            writer.execute("BEGIN; NOTIFY c, 'dropped'; ROLLBACK;")
            assert list(listener.notifies(timeout=0.3)) == []

            listener.execute("BEGIN; LISTEN other; COMMIT;")
            writer.execute("NOTIFY other, 'batched listen'")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("other", "batched listen")]


def test_notify_duplicates_collapse_within_a_transaction():
    """`NOTIFY c,'x'; NOTIFY c,'x'; NOTIFY c,'y'` commits as two notifications --
    collapsed on the (channel, payload) pair, as PostgreSQL 18 collapses them."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("NOTIFY chan, 'dup'")
            notifier.execute("NOTIFY chan, 'dup'")
            notifier.execute("NOTIFY chan, 'other'")
            notifier.commit()
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=2)]
            assert got == [("chan", "dup"), ("chan", "other")]


def test_notify_rolled_back_to_a_savepoint_is_discarded():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("NOTIFY chan, 'keep'")
            notifier.execute("SAVEPOINT sp")
            notifier.execute("NOTIFY chan, 'inner'")
            notifier.execute("ROLLBACK TO SAVEPOINT sp")
            notifier.commit()
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "keep")]


def test_listen_takes_effect_only_at_commit():
    """Measured on PostgreSQL 18: a LISTEN inside an open transaction does not
    receive a notification sent before its COMMIT."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN late")
            assert server.listeners("late") == set(), "subscribed before COMMIT"
            notifier.execute("NOTIFY late, 'too early'")
            listener.commit()
            assert len(server.listeners("late")) == 1

            listener.autocommit = True
            assert list(listener.notifies(timeout=0.3)) == []
            notifier.execute("NOTIFY late, 'in time'")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("late", "in time")]


def test_listen_rolled_back_never_subscribes():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN gone")
            listener.rollback()
            assert server.listeners("gone") == set()
            notifier.execute("NOTIFY gone, 'x'")
            listener.autocommit = True
            assert list(listener.notifies(timeout=0.3)) == []


def test_unlisten_rolled_back_keeps_the_subscription():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN kept")
            listener.autocommit = False
            listener.execute("UNLISTEN kept")
            listener.rollback()
            listener.autocommit = True
            notifier.execute("NOTIFY kept, 'still here'")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("kept", "still here")]


# --- subscription bookkeeping -------------------------------------------------------


unsubscribe_cases = {
    "unlisten_one_channel": {"statement": "UNLISTEN a", "still_listening": ["b"]},
    "unlisten_star_clears_all": {"statement": "UNLISTEN *", "still_listening": []},
    "discard_all_implies_unlisten_star": {"statement": "DISCARD ALL", "still_listening": []},
    "unlisten_an_unknown_channel_is_not_an_error": {"statement": "UNLISTEN never", "still_listening": ["a", "b"]},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(unsubscribe_cases.values()))),
    argvalues=[[v for k, v in sorted(unsubscribe_cases[name].items())] for name in sorted(unsubscribe_cases)],
    ids=sorted(unsubscribe_cases),
)
def test_unsubscribing(statement, still_listening):
    with serve_in_thread(MockSession) as server:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            conn.execute("LISTEN a")
            conn.execute("LISTEN b")
            conn.execute(statement)
            listening = sorted(channel for channel in ("a", "b") if server.listeners(channel))
            assert listening == still_listening


def test_channel_names_fold_like_identifiers():
    """`NOTIFY CHAN` and `NOTIFY chan` are one channel; `NOTIFY "CHAN"` is another."""
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("NOTIFY \"CHAN\", 'quoted, different channel'")
            notifier.execute("NOTIFY CHAN, 'unquoted, folds to chan'")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "unquoted, folds to chan")]


def test_repeated_listen_delivers_once():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN twice")
            listener.execute("LISTEN twice")
            notifier.execute("NOTIFY twice, 'once'")
            notifier.execute("NOTIFY twice, 'and again'")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=2)]
            assert got == [("twice", "once"), ("twice", "and again")]


def test_payload_quoting():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as listener, psycopg.connect(dsn, autocommit=True) as notifier:
            listener.execute("LISTEN chan")
            notifier.execute("NOTIFY chan, 'it''s a payload — ünicode'")
            notifier.execute("NOTIFY chan")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=2)]
            assert got == [("chan", "it's a payload — ünicode"), ("chan", "")]


command_tag_cases = {
    "listen": {"sql": "LISTEN c", "tag": "LISTEN"},
    "unlisten": {"sql": "UNLISTEN c", "tag": "UNLISTEN"},
    "unlisten_star": {"sql": "UNLISTEN *", "tag": "UNLISTEN"},
    "notify": {"sql": "NOTIFY c", "tag": "NOTIFY"},
    "notify_with_payload": {"sql": "NOTIFY c, 'x'", "tag": "NOTIFY"},
    "pg_notify": {"sql": "SELECT pg_notify('c', 'x')", "tag": "SELECT 1"},
}


@pytest.mark.parametrize(
    argnames=sorted(next(iter(command_tag_cases.values()))),
    argvalues=[[v for k, v in sorted(command_tag_cases[name].items())] for name in sorted(command_tag_cases)],
    ids=sorted(command_tag_cases),
)
def test_command_tags(sql, tag):
    """The tags PostgreSQL 18 answers these with, checked because a client reads
    them -- psycopg exposes the string directly as `cursor.statusmessage`."""
    with serve_in_thread(MockSession) as server:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as conn:
            assert conn.execute(sql).statusmessage == tag


# --- connection lifetime ------------------------------------------------------------


def test_a_closed_connection_leaves_the_registry():
    with serve_in_thread(MockSession) as server:
        dsn = server.dsn(user="test", dbname="test")
        with psycopg.connect(dsn, autocommit=True) as survivor:
            leaving = psycopg.connect(dsn, autocommit=True)
            leaving.execute("LISTEN chan")
            survivor.execute("LISTEN chan")
            assert len(server.listeners("chan")) == 2

            leaving.close()
            for _ in range(100):
                if len(server.listeners("chan")) == 1:
                    break
                survivor.execute("SELECT 1 FROM t")  # let the server's loop notice
            assert len(server.listeners("chan")) == 1

            # And the fanout still reaches the one that is left.
            with psycopg.connect(dsn, autocommit=True) as notifier:
                notifier.execute("NOTIFY chan, 'survivor only'")
            got = [(n.channel, n.payload) for n in survivor.notifies(timeout=5, stop_after=1)]
            assert got == [("chan", "survivor only")]


async def test_fanout_survives_a_listener_that_raises():
    """One dead listener must not swallow the notification for the rest. A
    connection can be closing while a fanout runs, and giving up half way would
    make the outcome depend on set iteration order."""

    class BrokenConnection:
        pid = 999

        def notify(self, channel, payload, pid):
            raise ConnectionResetError("gone")

    async with serve(MockSession) as server:
        listener = await asyncpg.connect(apg_dsn(server))
        try:
            received: asyncio.Queue = asyncio.Queue()
            await listener.add_listener("chan", lambda c, pid, ch, pl: received.put_nowait((ch, pl)))
            server.add_listener("chan", BrokenConnection())

            assert server.notify("chan", "delivered anyway", pid=1) == 1
            assert await asyncio.wait_for(received.get(), timeout=5) == ("chan", "delivered anyway")
        finally:
            await listener.close()


def test_server_notify_from_another_thread_via_the_server_loop():
    """Raising an event from outside the server entirely -- the embedding
    process's own code, not a session answering a query.

    Routed through `ServerThread.loop` because `notify()` writes to client
    transports and asyncio transports are not thread-safe. This is the documented
    incantation, so it gets a test rather than only a docstring.
    """
    # The one test here that cannot use `serve_in_thread`: it needs the thread's
    # loop, which the context manager deliberately does not hand out.
    server = PgServer(session_factory=MockSession)
    thread = ServerThread(server)
    thread.start()
    try:
        with psycopg.connect(server.dsn(user="test", dbname="test"), autocommit=True) as listener:
            listener.execute("LISTEN external")
            thread.loop.call_soon_threadsafe(server.notify, "external", "from the embedding process")
            got = [(n.channel, n.payload) for n in listener.notifies(timeout=5, stop_after=1)]
            assert got == [("external", "from the embedding process")]
    finally:
        thread.stop()


async def test_server_notify_reports_zero_with_no_listeners():
    async with serve(MockSession) as server:
        assert server.notify("nobody-home", "x") == 0


# --- the session-facing API ---------------------------------------------------------


async def test_session_connection_accessor_is_documented_public_api():
    """`Session.connection` rather than the private `_connection` the framework
    stashes -- and a clear failure rather than a None if read too early."""
    captured = {}

    class ReportingSession(Session):
        async def init(self, connection):
            await super().init(connection)
            captured["pid"] = self.connection.pid
            self.connection.notice(f"connected as {self.connection.username}")

        async def describe(self, sql, param_oids):
            return [ResultColumn.for_type("x", int)]

        async def query(self, sql, params):
            yield (1,)

    with pytest.raises(RuntimeError, match="not attached to a connection"):
        ReportingSession().connection

    async with serve(ReportingSession) as server:
        conn = await asyncpg.connect(apg_dsn(server))
        try:
            logged: asyncio.Queue = asyncio.Queue()
            conn.add_log_listener(lambda connection, message: logged.put_nowait(message))
            # The notice from init() was written before this connection's first
            # command, so it arrives ahead of anything this query produces.
            await conn.fetch("SELECT x FROM t")
            assert captured["pid"] == conn.get_server_pid()
        finally:
            await conn.close()


async def test_session_can_notify_listeners():
    """A session raising an event of its own, through the same fanout the NOTIFY
    statement uses."""

    class EventSession(Session):
        async def describe(self, sql, param_oids):
            return [ResultColumn.for_type("x", int)]

        async def query(self, sql, params):
            self.connection.notify_listeners("rows", "produced one")
            yield (1,)

    async with serve(EventSession) as server:
        listener = await asyncpg.connect(apg_dsn(server))
        worker = await asyncpg.connect(apg_dsn(server))
        try:
            received: asyncio.Queue = asyncio.Queue()
            await listener.add_listener("rows", lambda c, pid, ch, pl: received.put_nowait((ch, pl)))
            await worker.fetch("SELECT x FROM t")
            assert await asyncio.wait_for(received.get(), timeout=5) == ("rows", "produced one")
        finally:
            await listener.close()
            await worker.close()
