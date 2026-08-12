"""Regenerate pg_mimic/pg_settings.json from a real PostgreSQL server.

    python tools/generate_pg_settings.py "host=127.0.0.1 dbname=postgres"

pg_mimic answers SHOW and current_setting() for names it does not model, so it
needs the set of names a real server is born knowing and a plausible value for
each. Both come from pg_settings, and neither is worth maintaining by hand: the
list is ~400 long and changes every major release.

Two things this is careful about.

*It takes boot_val, not the running value.* `setting` is whatever the server it
was pointed at happens to be configured with, and dumping that bakes one
machine's postgresql.conf into the package -- data_directory, lc_messages, the
extensions it happens to load. boot_val is the compiled-in default, which is the
only value that is a property of PostgreSQL rather than of a host.

*It renders units the way SHOW does.* pg_settings.setting is raw (work_mem is
4096); SHOW and current_setting apply units (4MB). Clients read the rendered
form, so that is what gets stored. The renderer is checked against the server's
own output for every setting still at its boot value -- see _verify -- and only
then trusted for the handful that have drifted, which is the only place it is
used unsupervised.
"""

from __future__ import annotations

import json
import pathlib
import sys

# Ordered largest first, as PostgreSQL's own conversion is: the rendered unit is
# the largest one the value divides by evenly. Multipliers are relative to the
# base of each family -- bytes for memory, milliseconds for time.
_MEMORY_UNITS = (("TB", 1024**4), ("GB", 1024**3), ("MB", 1024**2), ("kB", 1024), ("B", 1))
_TIME_UNITS = (("d", 86_400_000), ("h", 3_600_000), ("min", 60_000), ("s", 1_000), ("ms", 1), ("us", 0))

# What one of pg_settings.unit is worth in its family's base.
_BASE = {
    "B": (_MEMORY_UNITS, 1),
    "kB": (_MEMORY_UNITS, 1024),
    "MB": (_MEMORY_UNITS, 1024**2),
    "GB": (_MEMORY_UNITS, 1024**3),
    "8kB": (_MEMORY_UNITS, 8 * 1024),
    "us": (_TIME_UNITS, 0),
    "ms": (_TIME_UNITS, 1),
    "s": (_TIME_UNITS, 1_000),
    "min": (_TIME_UNITS, 60_000),
}


def render(value: str, unit: str | None) -> str:
    """`value` in `unit`, spelled as SHOW spells it."""
    if not unit or unit not in _BASE:
        return value
    try:
        amount = int(value)
    except ValueError:
        return value
    # -1 and 0 are the "disabled" and "unset" spellings, and Postgres prints them
    # bare: `archive_timeout` is `0`, not `0d`. Any unit divides them evenly, so
    # without this they would render as whatever the largest unit happens to be.
    if amount <= 0:
        return value
    units, multiplier = _BASE[unit]
    total = amount * multiplier
    for name, size in units:
        if size and total % size == 0:
            return f"{total // size}{name}"
    return value


# boot_val is a property of PostgreSQL for almost every setting, but not quite all:
# these carry the build prefix or the host's filesystem, so they would smuggle in the
# machine this was generated on exactly as `setting` would. A mimic has no data
# directory or keytab to name, and every one of them is postmaster- or sighup-context,
# so no client can set one either. Empty is both honest and inert.
#
# The NULL boot_vals (config_file, data_directory, hba_file, ident_file,
# external_pid_file, timezone_abbreviations) are the same story told by the server
# itself: it has no compiled-in answer, it fills them in at startup.
_BUILD_SPECIFIC = frozenset({"krb_server_keyfile"})


def _default_for(row: dict) -> str:
    """The stored default: boot_val rendered, or empty where boot_val is not a
    property of PostgreSQL itself."""
    if row["boot_val"] is None or row["name"].lower() in _BUILD_SPECIFIC:
        return ""
    return render(row["boot_val"], row["unit"])


def _verify(rows: list[dict]) -> None:
    """Prove `render` reproduces the server before trusting it where it can't be checked.

    Only settings still at boot_val can be checked this way -- for those the server
    has already rendered the same number we are rendering, and current_setting is
    the answer to compare against.
    """
    checked = mismatched = 0
    for row in rows:
        if row["setting"] != row["boot_val"]:
            continue
        checked += 1
        ours = render(row["boot_val"], row["unit"])
        if ours != row["shown"]:
            mismatched += 1
            print(f"  MISMATCH {row['name']}: rendered {ours!r}, server says {row['shown']!r}", file=sys.stderr)
    if mismatched:
        raise SystemExit(f"renderer disagrees with the server on {mismatched} of {checked} settings")
    print(f"  renderer matches the server on all {checked} settings still at boot_val")


def main(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('server_version')")
        # "18.4 (Homebrew)" -- the build tag is the packager's, not PostgreSQL's.
        server_version = cursor.fetchone()[0].split()[0]
        cursor.execute("""
            SELECT name, setting, boot_val, unit, vartype, context, current_setting(name) AS shown
            FROM pg_settings ORDER BY name
        """)
        columns = [description.name for description in cursor.description]
        rows = [dict(zip(columns, values)) for values in cursor.fetchall()]

    _verify(rows)

    settings = {
        row["name"].lower(): {
            "default": _default_for(row),
            "vartype": row["vartype"],
            "context": row["context"],
        }
        for row in rows
    }
    document = {
        "_comment": "Generated by tools/generate_pg_settings.py -- do not edit by hand.",
        "_generated_from": f"PostgreSQL {server_version}",
        "settings": settings,
    }
    target = pathlib.Path(__file__).resolve().parent.parent / "pg_mimic" / "pg_settings.json"
    target.write_text(json.dumps(document, indent=1, sort_keys=False) + "\n")
    print(f"  wrote {len(settings)} settings to {target}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "")
