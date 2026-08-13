"""The examples start, and say where.

Nothing covered `examples/` before, so a refactor of the six could break every one
of them without CI noticing -- and they are the first thing a new user runs. The
assertion is deliberately shallow: each example comes up, reports a port, and
completes a startup handshake on it. What each one then *answers* is its own
business and mostly covered elsewhere (TableSession in test_table_session.py, the
protocol in the rest of the suite).

`--open-port` is how they are started here, which is also what makes the test
possible: a fixed port would collide with whatever is on 5432 on the developer's
machine, and with a parallel run of this same test.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import psycopg
import pytest

EXAMPLES = pathlib.Path(__file__).resolve().parent.parent / "examples"

# Subprocess startup, a git collection pass in git_sql's case, and a handshake.
_SUBPROCESS_TIMEOUT = pytest.mark.timeout(60)

# user/dbname are startup parameters rather than credentials -- trust auth takes
# any -- but git_sql names a database and it costs nothing to use each one's own.
_CONNECT_AS = {"git_sql.py": ("me", "git")}


def _example_files() -> list[str]:
    return sorted(path.name for path in EXAMPLES.glob("*.py") if not path.name.startswith("_"))


def _listening_port(process: subprocess.Popen) -> int:
    """The port from run()'s own log line, which under --open-port is the only
    place it exists. Reads until it appears rather than assuming the first line:
    several examples log something of their own first."""
    assert process.stdout is not None
    for _ in range(40):
        line = process.stdout.readline()
        if not line:
            break
        if "listening on" in line:
            return int(line.rsplit(":", 1)[1])
    raise AssertionError("the example never reported a listening port")


@_SUBPROCESS_TIMEOUT
@pytest.mark.parametrize(argnames=["example"], argvalues=[[name] for name in _example_files()], ids=_example_files())
def test_an_example_starts_on_an_open_port_and_reports_it(example):
    process = subprocess.Popen(
        [sys.executable, str(EXAMPLES / example), "--open-port"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=EXAMPLES.parent,
    )
    try:
        port = _listening_port(process)
        assert port != 0, "0 is the request, not the port anything can connect to"

        user, dbname = _CONNECT_AS.get(example, ("test", "test"))
        # Connecting at all is the assertion: it completes startup, which means the
        # example built its Session and is speaking the protocol on the port it named.
        with psycopg.connect(f"host=127.0.0.1 port={port} user={user} dbname={dbname}", connect_timeout=10):
            pass
    finally:
        process.kill()
        process.wait(timeout=10)
