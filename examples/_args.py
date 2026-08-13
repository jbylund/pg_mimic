"""The command line every example shares, as a parent parser.

Each example is a program whose interesting part is its `Session`, so the
host/port plumbing lives here once instead of six times:

    from _args import example_parser, parse_args, serve

    parser = example_parser(__doc__)              # --host, --port, --open-port
    parser.add_argument("repo", nargs="?", default=".")   # anything extra
    args = parse_args(parser)
    serve(MySession, args)

An example with nothing of its own is the one-liner
`serve(MySession, parse_args(example_parser(__doc__)))`.

`example_parser` returns a parser that already *has* the common arguments (via
argparse's `parents=`), so an example only declares what is its own.

Why `--open-port` exists: the default 5432 is the port a real PostgreSQL is
already on, and anyone evaluating a Postgres mimic is likely to have PostgreSQL
installed. `--open-port` asks the kernel for any free port, and `PgServer.run()`
logs the one it got -- which is the only way to find the server afterwards, so
that log line is the feature rather than a detail.

Not part of the pg_mimic package: `pyproject.toml` ships `packages = ["pg_mimic"]`,
and this is example plumbing rather than public API. An example run as
`python examples/simple.py` finds it because the script's own directory is on
sys.path.
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, Callable

from pg_mimic import PgServer

# What PgServer wants: something it can call per connection to get a Session.
# Spelled structurally rather than imported, because pg_mimic exports the server
# and the Session but not the alias for the callable between them.
SessionFactory = Callable[[], Any]

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 5432

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "configure_logging", "example_parser", "parse_args", "resolve_port", "serve"]


def configure_logging() -> None:
    """Send INFO to stderr, plainly. Called before an example logs anything.

    Order matters more than it looks. The module-level `logging.info()` installs a
    default handler at WARNING if none exists, and `basicConfig()` is a no-op once
    a handler is there -- so one stray `logging.info()` before this runs both loses
    its own message *and* silently swallows `run()`'s "listening on" line, which
    under `--open-port` is the only way to find the server. Hence `force=True`, and
    hence `parse_args()` calling this before an example gets a chance to log.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s", force=True)


def parse_args(parser: argparse.ArgumentParser) -> argparse.Namespace:
    """Parse, and configure logging while doing it -- see `configure_logging`."""
    args = parser.parse_args()
    configure_logging()
    return args


def _common() -> argparse.ArgumentParser:
    """The shared arguments, as a parser that only ever gets used as a parent.

    `add_help=False` is what makes it usable as one: the child adds `-h` itself,
    and two parsers both adding it is an argparse error rather than a warning.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"interface to listen on (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--open-port",
        action="store_true",
        help="listen on any free port instead of --port, and log which one -- for when 5432 is taken",
    )
    return parser


def example_parser(description: str | None = None) -> argparse.ArgumentParser:
    """A parser carrying `--host`, `--port` and `--open-port`, ready to extend.

    `description` is meant to be the example's `__doc__`, kept unwrapped so the
    usage text reads like the module docstring it came from.
    """
    return argparse.ArgumentParser(
        description=description,
        parents=[_common()],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )


def resolve_port(args: argparse.Namespace) -> int:
    """The port to bind: 0 (meaning "any free one") when `--open-port` was given.

    Kept separate from `serve()` so an example that starts the server its own way
    -- inside an existing event loop, say -- reads the flag the same way.
    """
    return 0 if args.open_port else args.port


def serve(session_factory: SessionFactory, args: argparse.Namespace, **kwargs: Any) -> None:
    """Serve until interrupted, on the host and port `args` asked for.

    Configures logging again in case an example called `parser.parse_args()`
    directly: `run()` reports the listening address through the package logger at
    INFO, and with nothing configured that goes nowhere -- which under
    `--open-port` leaves no way to find the server at all.
    """
    configure_logging()
    PgServer(session_factory=session_factory, **kwargs).run(host=args.host, port=resolve_port(args))
