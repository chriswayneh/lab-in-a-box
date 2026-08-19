#!/usr/bin/env bash
# =============================================================================
# Markdown lint
# =============================================================================
#   bash scripts/check-markdown.sh
#
# Called by `make validate` AND by the Markdown job in CI, so both run byte-for
# byte the same linter over the same files.
#
# Why this file exists
# --------------------
# CI previously used the markdownlint-cli2 GitHub Action while contributors ran
# whatever `npm install -g markdownlint-cli2` happened to give them. The two
# drifted: a local run on markdownlint v0.37 reported a clean tree, and CI on
# v0.41 rejected it for MD060, a rule the older engine did not have. The failure
# surfaced only after pushing, which is exactly what local validation exists to
# prevent.
#
# One pinned image, referenced from one place, removes that class of bug. It
# also means no host install: markdownlint needs Node, and this project's rule
# is that Docker is the only prerequisite.
#
# Rules, globs and ignores all live in .markdownlint-cli2.jsonc, so this script
# passes no arguments — the config is the single source of truth for what gets
# linted, and adding a glob here would silently override it.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# Pinned deliberately. The tag is the cli2 wrapper version; the number that
# actually governs which rules exist is the markdownlint engine it bundles:
#
#   davidanson/markdownlint-cli2:v0.23.2  ->  markdownlint v0.41.1
#
# When bumping, run the image once and confirm the engine version it prints,
# then update this comment. A wrapper bump that changes the engine changes
# which rules apply.
MARKDOWNLINT_IMAGE="${MARKDOWNLINT_IMAGE:-davidanson/markdownlint-cli2:v0.23.2}"

require_docker

# Git Bash rewrites POSIX-looking paths in container arguments. /workdir is a
# path inside the container and must survive untouched.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

docker run --rm \
  --volume "$(host_path "$LAB_ROOT"):/workdir" \
  --workdir /workdir \
  "$MARKDOWNLINT_IMAGE"
