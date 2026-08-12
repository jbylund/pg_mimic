"""The configuration parameters a real PostgreSQL server is born knowing.

Generated from a live server's `pg_settings` by tools/generate_pg_settings.py and
shipped as pg_settings.json. Regenerate it against a newer major release rather
than editing either file by hand.

This exists for the *names* as much as the values. Answering `SHOW work_mem` with
something plausible is the visible half, but the half that matters is being able
to tell a parameter pg_mimic does not model from a parameter that does not exist:
without the list, `SHOW work_mem` and `SHOW not_a_setting` are the same question,
and every answer to one is wrong for the other. See #32.

The values are honest defaults rather than descriptions of pg_mimic. `shared_buffers`
reads 128MB here and there is no buffer pool behind it -- a mimic that answered "0"
would be no truer and would break arithmetic on the client. What a session actually
depends on -- encodings, the search path, the server version -- is answered from
the connection instead, in middleware._DEFAULT_SETTINGS, which is consulted first.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

_DOCUMENT: dict[str, Any] = json.loads(files(__package__).joinpath("pg_settings.json").read_text(encoding="utf-8"))

#: name -> {"default": str, "vartype": str, "context": str}. Keys are lower-cased,
#: which is how Postgres compares parameter names.
SETTINGS: dict[str, dict[str, str]] = _DOCUMENT["settings"]

#: The server the file was generated from, for the "which release is this" question.
GENERATED_FROM: str = _DOCUMENT["_generated_from"]


def default(name: str) -> str | None:
    """The boot default for `name`, or None if no such parameter exists.

    None is the "no such parameter" answer, distinct from a parameter whose default
    is genuinely the empty string -- there are several, and they are not errors.
    """
    entry = SETTINGS.get(name.lower())
    return None if entry is None else entry["default"]


def context(name: str) -> str | None:
    """Who may set `name` -- 'user' for the 151 a client can change, and postmaster,
    sighup, superuser, internal or backend for the ones it cannot. None if unknown."""
    entry = SETTINGS.get(name.lower())
    return None if entry is None else entry["context"]
