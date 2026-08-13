"""Extended-protocol lifecycle edges that no client library in the test
suite happens to exercise on its own: explicitly Close-ing a named prepared
statement/portal (psycopg manages its own prepared-statement cache and
rarely calls this on demand), and Describe(Statement) before any Bind
(psycopg's default flow only Describes the *portal*, after Bind)."""

from __future__ import annotations

from wire import (
    TARGET_PORTAL,
    TARGET_STATEMENT,
    connect_and_get_backend_key,
    make_bind,
    make_close,
    make_describe,
    make_execute,
    make_parse,
    parse_error_fields,
    read_message,
)

from pg_mimic import ResultColumn
from pg_mimic.testing import serve_in_thread
from pg_mimic.types import unpack_int16, unpack_int32


async def test_close_statement_and_portal(mock_session):
    mock_session.columns = [ResultColumn.for_type("x", int)]
    mock_session.rows = [(1,)]
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)

        writer.write(make_parse("SELECT x FROM t", statement_name="s1"))
        writer.write(make_bind(statement_name="s1", portal_name="p1"))
        await writer.drain()
        assert (await read_message(reader))[0] == b"1"  # ParseComplete
        assert (await read_message(reader))[0] == b"2"  # BindComplete

        writer.write(make_close(TARGET_STATEMENT, "s1"))
        await writer.drain()
        assert (await read_message(reader))[0] == b"3"  # CloseComplete

        writer.write(make_close(TARGET_PORTAL, "p1"))
        await writer.drain()
        assert (await read_message(reader))[0] == b"3"  # CloseComplete

        # the statement no longer exists -- Bind against it must now fail
        writer.write(make_bind(statement_name="s1", portal_name="p2"))
        await writer.drain()
        tag, payload = await read_message(reader)
        assert tag == b"E"
        assert parse_error_fields(payload)["C"] == "26000"

        writer.close()

        await writer.wait_closed()


async def test_describe_statement_before_bind(mock_session):
    mock_session.columns = [ResultColumn.for_type("x", int)]
    mock_session.rows = [(1,)]
    with serve_in_thread(mock_session.spawn) as server:
        reader, writer, _pid, _secret = await connect_and_get_backend_key(server.port)

        writer.write(make_parse("SELECT x FROM t", statement_name="s1"))
        await writer.drain()
        assert (await read_message(reader))[0] == b"1"  # ParseComplete

        # Describe(Statement) -- no Bind has happened yet
        writer.write(make_describe(TARGET_STATEMENT, "s1"))
        await writer.drain()

        tag, payload = await read_message(reader)
        assert tag == b"t"  # ParameterDescription
        assert unpack_int16(payload, 0) == 0  # no bind params in this statement

        tag, payload = await read_message(reader)
        assert tag == b"T"  # RowDescription -- known without ever Binding
        num_cols = unpack_int16(payload, 0)
        assert num_cols == 1

        # now actually run it via Bind/Execute using the same Statement
        writer.write(make_bind(statement_name="s1", portal_name="p1"))
        writer.write(make_execute("p1"))
        await writer.drain()
        assert (await read_message(reader))[0] == b"2"  # BindComplete
        tag, payload = await read_message(reader)
        assert tag == b"D"  # DataRow
        assert unpack_int16(payload, 0) == 1  # one column
        value_len = unpack_int32(payload, 2)
        assert payload[6 : 6 + value_len] == b"1"  # text-format "1"

        writer.close()

        await writer.wait_closed()
