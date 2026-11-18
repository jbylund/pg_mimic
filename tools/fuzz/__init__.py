"""Differential fuzzing of sqlglot's executor against a real PostgreSQL.

Not a test suite: a bug-finding tool whose *output* becomes tests. A finding here
is turned into a strict-xfail tripwire in tests/test_sqlglot_workarounds.py, which
is the thing that runs in CI and tells us when upstream fixes it.

See tools/fuzz/README.md for how to run it and what the report means.
"""
