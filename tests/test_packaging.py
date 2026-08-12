"""Packaging declarations that fail silently when they break.

A missing py.typed doesn't stop pg_mimic importing -- it just makes every symbol
read as Any in a consumer's type checker, which nothing here would ever notice.
A licence declared in pyproject.toml but not shipped as a file is the same shape
of failure: the metadata says MIT and the wheel carries no text. Both are checked
against the *installed* distribution, which is what a consumer actually gets.
"""

from __future__ import annotations

import pathlib
from importlib.metadata import metadata

import pg_mimic


def _installed_metadata():
    """The installed distribution's metadata, with a readable failure when it is stale.

    These declarations are recorded at install time, not read from pyproject.toml at
    test time, so an editable install predating a metadata change still reports the
    old values. Left alone that surfaces as `TypeError: 'NoneType' object is not
    iterable` from a missing Project-URL, which says nothing about the cause.
    """
    m = metadata("pg-mimic")
    if m.get("License-Expression") is None and m.get_all("Project-URL") is None:
        raise AssertionError(
            "the installed pg-mimic metadata predates these declarations. Reinstall "
            "(`uv pip install -e '.[dev]'`) and run again -- metadata is recorded when the "
            "distribution is installed, so editing pyproject.toml alone does not refresh it."
        )
    return m


def test_py_typed_marker_is_installed():
    marker = pathlib.Path(pg_mimic.__file__).resolve().parent / "py.typed"
    assert marker.is_file(), (
        f"{marker} is missing. Without the PEP 561 marker beside the package, a consumer's "
        "mypy/pyright reads every pg_mimic import as Any -- silently, since the package still imports."
    )


def test_license_is_declared_and_shipped():
    m = _installed_metadata()
    assert m.get_all("License-Expression") == ["MIT"]
    assert m.get_all("License-File") == ["LICENSE"], (
        "the built distribution declares no licence file. `license-files` in pyproject.toml is what "
        "puts LICENSE into the wheel and the sdist; a licence expression alone ships no text."
    )


def test_project_urls_point_at_the_repository():
    urls = dict(entry.split(", ", 1) for entry in _installed_metadata().get_all("Project-URL"))
    assert set(urls) >= {"Homepage", "Repository", "Issues"}
    assert all(url.startswith("https://github.com/jbylund/pg_mimic") for url in urls.values()), urls
