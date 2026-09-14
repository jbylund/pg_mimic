#!/usr/bin/env bash
# Install the sqlglot that pg_mimic develops against, which is a commit on their
# main rather than a release.
#
# Why a commit: the executor fixes pg_mimic needs are merged upstream and
# unreleased. v30.18.0 is the newest release and main is well ahead of it. The
# published package still declares `sqlglot>=30.18.0` (pyproject.toml) because
# PyPI rejects a direct-URL dependency, so this pin is a development and CI
# posture only -- never what a user installing pg-mimic gets. The floor keeps
# being exercised daily by the `latest-release` leg of upstream-sqlglot.yml.
#
#   tools/install_sqlglot.sh          # the pinned commit below
#   tools/install_sqlglot.sh main     # whatever the branch tip is now
#
# Move PINNED_REV when a fix we want lands, and delete this script entirely once
# a release carries everything (then the floor in pyproject.toml is the pin).
set -euo pipefail

# tobymao/sqlglot main @ 2026-09-14, 43 commits past v30.18.0. Carries
# e27b7654, which plans DISTINCT before ORDER BY -- the fix that makes
# _take_result_order's sorting redundant (jbylund/sqlglot#54).
PINNED_REV="3ca824895ef423f7895fb13d72357548c0f1f367"

rev="${1:-$PINNED_REV}"

# uv builds a wheel from this directory, so nothing needs it afterwards.
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
dest="$work/sqlglot"

# Cloned by hand rather than `uv pip install git+...`, because that runs
# `git submodule update --init` and sqlglot pins a submodule by SSH URL to a
# private repo (fivetran/sqlglot-integration-tests), which no runner can
# authenticate to. A plain clone leaves submodules alone, and that one is their
# integration suite -- not needed to build or import.
#
# --filter=blob:none, not --depth 1: a shallow clone carries no tags, so
# setuptools_scm cannot derive a version and produces 0.0.1.dev1, which then
# violates our own sqlglot floor. A blobless clone keeps every tag and is nearly
# as quick.
git clone --quiet --filter=blob:none https://github.com/tobymao/sqlglot "$dest"
git -C "$dest" checkout --quiet --detach "$rev"

# CI sets UV_SYSTEM_PYTHON=1, which is how `--system` gets in without this
# script having to know whether it is in a virtualenv.
uv pip install --quiet --upgrade "$dest"
python -c "import sqlglot; print('sqlglot', sqlglot.__version__)"
