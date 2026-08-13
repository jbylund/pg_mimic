"""`PgServer.run()`: the blocking entry point for when the server is the program.

It owns the loop and returns on interrupt, so it is driven here in a subprocess
with a real SIGINT rather than in-process. Anything less proves less: the point
of the method is what happens to the *process*, and a KeyboardInterrupt raised by
hand does not test that `asyncio.run` was left in a state that can exit.

Note a subprocess started by a shell without job control inherits SIGINT as
SIG_IGN, so the child restores the default handler before it starts. That is a
property of how it is launched here, not of pg_mimic -- but it cost an hour once,
so it is written down.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

import psycopg
import pytest

_PROGRAM = """
import logging, signal, sys
signal.signal(signal.SIGINT, signal.default_int_handler)
sys.path.insert(0, {root!r})
from pg_mimic import PgServer, TableSession

logging.basicConfig(level=logging.INFO, format="%(message)s")
PgServer(session_factory=lambda: TableSession({{"t": [{{"id": 1}}]}})).run(port={port})
print("returned", flush=True)
"""


def _serving(tmp_path, port: int, root: str) -> subprocess.Popen:
    script = tmp_path / "run_server.py"
    script.write_text(_PROGRAM.format(root=root, port=port))
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            psycopg.connect(f"host=127.0.0.1 port={port} user=u dbname=d", connect_timeout=1).close()
            return process
        except psycopg.OperationalError:
            time.sleep(0.05)
    process.kill()
    raise AssertionError("the server never came up")


# The suite's 4s per-test timeout is there to turn a protocol hang into a failure.
# These two spawn a Python subprocess and wait for it to import pg_mimic and bind a
# socket, which is legitimately slower than that under load -- and was flaky at the
# default while passing in isolation. A longer limit still catches the hang it is
# guarding against, which is the server never exiting.
_SUBPROCESS_TIMEOUT = pytest.mark.timeout(30)


@pytest.fixture
def repo_root() -> str:
    import pg_mimic

    return str(__import__("pathlib").Path(pg_mimic.__file__).resolve().parent.parent)


@_SUBPROCESS_TIMEOUT
def test_run_serves_and_stops_on_interrupt(tmp_path, repo_root):
    process = _serving(tmp_path, 15551, repo_root)
    try:
        with psycopg.connect("host=127.0.0.1 port=15551 user=u dbname=d", autocommit=True) as conn:
            assert conn.execute("SELECT id FROM t").fetchone() == (1,)

        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, f"interrupt should be a clean exit, not a traceback:\n{output}"
    assert "returned" in output, "run() should return rather than raise KeyboardInterrupt"
    assert "listening on 127.0.0.1:15551" in output
    assert "Traceback" not in output


@_SUBPROCESS_TIMEOUT
def test_run_stops_with_a_client_still_attached(tmp_path, repo_root):
    """The case `close()` warns about: from 3.12 a cancelled `serve_forever()`
    waits on every live connection, so without `close()` dropping them the server
    never returns at all."""
    process = _serving(tmp_path, 15552, repo_root)
    attached = psycopg.connect("host=127.0.0.1 port=15552 user=u dbname=d", autocommit=True)
    try:
        assert attached.execute("SELECT id FROM t").fetchone() == (1,)
        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
        attached.close()

    assert process.returncode == 0, f"a live client must not stop it exiting:\n{output}"
    assert "returned" in output


@_SUBPROCESS_TIMEOUT
@pytest.mark.parametrize(
    argnames=["signal_name"],
    argvalues=[["SIGINT"], ["SIGTERM"]],
    ids=["sigint", "sigterm"],
)
def test_either_stop_signal_is_a_clean_exit(tmp_path, repo_root, signal_name):
    """SIGTERM as well as SIGINT, because that is what a container or systemd
    sends and it would otherwise kill the process outright.

    Both are handled by the loop rather than by Python's default handler. A
    default-handled SIGINT raises KeyboardInterrupt into whichever coroutine is
    running, so a connection parked in read_message() died with an exception
    nobody retrieved and asyncio printed it -- intermittently, depending on where
    the signal landed. This asserts the absence of that traceback.
    """
    port = 15561 if signal_name == "SIGINT" else 15562
    process = _serving(tmp_path, port, repo_root)
    try:
        process.send_signal(getattr(signal, signal_name))
        output, _ = process.communicate(timeout=20)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, f"{signal_name} should be a clean exit:\n{output}"
    assert "returned" in output
    assert "Traceback" not in output, f"{signal_name} leaked an unretrieved task exception:\n{output}"


@_SUBPROCESS_TIMEOUT
def test_run_on_port_zero_reports_the_port_it_actually_bound(tmp_path, repo_root):
    """`port=0` means "any free one", which is the only way to start a server on a
    machine that already has PostgreSQL on 5432. The port the kernel picked is then
    the one thing a client cannot guess, so `run()` has to log the bound port rather
    than the requested one -- it announced ":0" before, exactly when it mattered."""
    script = tmp_path / "run_server.py"
    script.write_text(_PROGRAM.format(root=repo_root, port=0))
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = process.stdout.readline()
        assert "listening on 127.0.0.1:" in line, line
        port = int(line.rsplit(":", 1)[1])
        assert port != 0, "the requested port, not the bound one -- a client cannot connect to 0"

        # the reported port is the real one, not merely non-zero
        with psycopg.connect(f"host=127.0.0.1 port={port} user=u dbname=d", autocommit=True) as conn:
            assert conn.execute("SELECT id FROM t").fetchone() == (1,)

        process.send_signal(signal.SIGINT)
        output, _ = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()

    assert process.returncode == 0, output
    assert "Traceback" not in output
