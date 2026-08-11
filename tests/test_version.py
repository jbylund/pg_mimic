"""pyproject.toml's version is the single source of truth; __version__ reads it
back from the installed distribution metadata. This pins them together so a
hardcoded literal can't creep back in -- only pyproject's copy is checked
against the git tag at release time, so a second copy could ship wrong.
"""

from __future__ import annotations

import pathlib
import re

import pg_mimic

_PYPROJECT = pathlib.Path(__file__).resolve().parent.parent / "pyproject.toml"

# Read with a regex rather than tomllib, which is 3.11+ while this package
# supports 3.10.
_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE)


def test_version_matches_pyproject():
    matches = _VERSION_RE.findall(_PYPROJECT.read_text())
    assert len(matches) == 1, f"expected exactly one top-level version in pyproject.toml, found {matches}"
    assert pg_mimic.__version__ == matches[0], (
        f"pg_mimic.__version__ is {pg_mimic.__version__!r} but pyproject.toml declares {matches[0]!r}. "
        "__version__ comes from installed distribution metadata, so after bumping the version "
        "re-run `pip install -e .` to refresh it."
    )
