#!/usr/bin/env bash
# =============================================================================
# Python lint, type check and unit tests
# =============================================================================
#   bash scripts/check-python.sh            lint + types + unit tests
#   bash scripts/check-python.sh lint       just ruff
#   bash scripts/check-python.sh types      just mypy
#   bash scripts/check-python.sh unit       just pytest
#
# Called by `make validate` AND by the Python job in CI, the same arrangement
# scripts/check-markdown.sh uses, so both run byte-for-byte the same tools at
# the same versions.
#
# Why this file exists
# --------------------
# The identity engine grew past 7,000 lines of Python across two roadmap
# milestones and two different authoring tools with no automated gate of any
# kind. CI validated compose, YAML, JSON, shell and Markdown; it never opened a
# .py file. A refactor that broke rbac.resource_matches would have shipped with
# twelve green checks, and the only thing standing between that and production
# was somebody happening to run the live suites by hand.
#
# What this is NOT
# ----------------
# This does not replace scripts/test-identity.sh. Those suites need a running
# lab and take minutes; they prove revocation actually revokes, which is not a
# question this file can answer. This covers the fast half: does the code
# parse, does it type-check, and do the pure functions behave. See
# docs/identity-governance.md for how the two relate.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# Pinned deliberately, same reasoning as the markdownlint pin: an unpinned lint
# tool turns "CI is green" into "CI was green on whatever version it resolved
# today", and a new default rule then fails a tree nobody changed.
#
# ruff and mypy are installed into the image at run time rather than baked into
# a custom image, because a Dockerfile here would be one more thing to build,
# publish and keep current for two dependencies that install in seconds.
PYTHON_IMAGE="${PYTHON_IMAGE:-python:3.12-alpine}"
RUFF_VERSION="${RUFF_VERSION:-0.14.14}"
MYPY_VERSION="${MYPY_VERSION:-1.19.0}"
PYTEST_VERSION="${PYTEST_VERSION:-9.0.2}"

MODE="${1:-all}"

require_docker

# Git Bash rewrites POSIX-looking paths in container arguments. /workdir is a
# path inside the container and must survive untouched.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

case "$MODE" in
  all|lint|types|unit) ;;
  *) die "unknown mode '${MODE}' — expected: all, lint, types, unit" ;;
esac

# -----------------------------------------------------------------------------
# One container, three tools.
#
# Installing once and running all three in the same container is what keeps
# this under a minute; three `docker run` invocations would pay the pip cost
# three times.
#
# pytest is pointed at scripts/identity/unit/ explicitly. The suites beside it
# (test_lifecycle.py, test_rbac.py, test_campaign.py, test_scim.py,
# test_audit.py) are live integration harnesses with their own main() and
# their own runner; they define module-level test_* functions taking service
# handles as arguments, which pytest would happily collect and then fail on
# missing fixtures. Scoping the path is what keeps the two kinds of test from
# colliding.
# -----------------------------------------------------------------------------
run_in_container() {
  docker run --rm \
    --env PYTHONDONTWRITEBYTECODE=1 \
    --env "MODE=${MODE}" \
    --volume "$(host_path "$LAB_ROOT"):/workdir" \
    --workdir /workdir \
    "$PYTHON_IMAGE" \
    sh -euc '
      # --root-user-action=ignore: the container runs as root by design and is
      # discarded immediately, so pip warning about it is pure noise here.
      pip install --quiet --disable-pip-version-check --root-user-action=ignore \
        "ruff=='"$RUFF_VERSION"'" "mypy=='"$MYPY_VERSION"'" "pytest=='"$PYTEST_VERSION"'"

      status=0

      if [ "$MODE" = all ] || [ "$MODE" = lint ]; then
        echo "--- ruff ---"
        ruff check scripts/identity || status=1
      fi

      if [ "$MODE" = all ] || [ "$MODE" = types ]; then
        echo "--- mypy ---"
        # Run from inside scripts/identity, not from the repo root. Two
        # reasons, and both matter: mypy.ini lives there and is only found
        # when it is the working directory, and the engine imports flat
        # (`import rbac`), which only resolves when that directory is the
        # root of the check. Checking from above would both miss the config
        # and model the imports differently from how they actually run.
        ( cd scripts/identity && mypy . ) || status=1
      fi

      if [ "$MODE" = all ] || [ "$MODE" = unit ]; then
        echo "--- pytest ---"
        pytest -q scripts/identity/unit || status=1
      fi

      exit $status
    '
}

run_in_container
