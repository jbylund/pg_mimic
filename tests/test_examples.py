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

The two git_sql set-operation tests at the bottom are the exception to "shallow".
They run over the wire because that is the only place the bug they cover shows:
a `UNION` with an `ORDER BY` comes back from the executor with its columns
stripped, which is not a wrong value but a `D` message that contradicts the
`RowDescription` already sent. In-process the rows merely look odd; on a socket
psycopg drops the connection.
"""

from __future__ import annotations

import contextlib
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


@contextlib.contextmanager
def _serving(example: str):
    """The example running on a free port, yielding a connection to it."""
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
        with psycopg.connect(f"host=127.0.0.1 port={port} user={user} dbname={dbname}", connect_timeout=10) as conn:
            yield conn
    finally:
        process.kill()
        process.wait(timeout=10)


@_SUBPROCESS_TIMEOUT
@pytest.mark.parametrize(argnames=["example"], argvalues=[[name] for name in _example_files()], ids=_example_files())
def test_an_example_starts_on_an_open_port_and_reports_it(example):
    # Connecting at all is the assertion: it completes startup, which means the
    # example built its Session and is speaking the protocol on the port it named.
    with _serving(example):
        pass


@_SUBPROCESS_TIMEOUT
def test_git_sql_serves_a_set_operation():
    """A UNION, EXCEPT or INTERSECT is as read-only as a SELECT.

    All three were refused as writes, because the guard tested for exp.Select and
    a set operation is an exp.SetOperation -- so psql's own `\\d` footer query,
    three SELECTs joined by UNION, came back as "a git repo is read-only".
    """
    with _serving("git_sql.py") as conn, conn.cursor() as cur:
        cur.execute("SELECT name FROM branches UNION SELECT name FROM branches")
        union = {row[0] for row in cur.fetchall()}
        cur.execute("SELECT name FROM branches")
        assert union == {row[0] for row in cur.fetchall()}

        cur.execute("SELECT path FROM files INTERSECT SELECT path FROM commit_files LIMIT 1")
        assert len(cur.fetchall()) == 1


@_SUBPROCESS_TIMEOUT
def test_git_sql_refuses_an_order_by_over_a_set_operation():
    """0A000 rather than a protocol violation -- see the module docstring.

    Delete this and _reject_stripped_columns together when sqlglot keeps the
    columns: https://github.com/jbylund/sqlglot/pull/34.
    """
    with _serving("git_sql.py") as conn, conn.cursor() as cur:
        with pytest.raises(psycopg.errors.FeatureNotSupported, match="columns stripped"):
            cur.execute("SELECT name FROM branches UNION SELECT name FROM branches ORDER BY 1")
        conn.rollback()
        # The connection survives it, which is the difference between an error and
        # the `D` message that used to reach psycopg here.
        cur.execute("SELECT name FROM branches LIMIT 1")
        assert len(cur.fetchall()) == 1


@pytest.mark.parametrize(
    argnames=["example"],
    argvalues=[["simple.py"], ["git_sql.py"]],
    ids=["a parser with nothing of its own", "a parser that adds a positional"],
)
def test_port_and_open_port_are_mutually_exclusive(example):
    """They answer the same question two ways, so asking both ways is a mistake
    worth being told about rather than one of them silently winning.

    Both parser shapes, because a mutually exclusive group has to survive
    argparse's `parents=` to reach the example that extends it -- which is the half
    that would fail quietly if it didn't.
    """
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / example), "--port", "6000", "--open-port"],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=EXAMPLES.parent,
    )
    assert result.returncode == 2, f"argparse should refuse this, not serve:\n{result.stdout}{result.stderr}"
    assert "not allowed with argument --port" in result.stderr
