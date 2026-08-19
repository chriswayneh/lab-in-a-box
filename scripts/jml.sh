#!/usr/bin/env bash
# =============================================================================
# Identity lifecycle — containerised entry point
# =============================================================================
#   bash scripts/jml.sh join  --user erin --role developer
#   bash scripts/jml.sh move  --user erin --from contractor --to developer
#   bash scripts/jml.sh leave --user erin
#   bash scripts/jml.sh show  --user erin
#
# Normally reached through `make jml-join` and friends.
#
# The engine is Python, but the lab's cross-platform promise means no host
# runtime may be required beyond Docker. scripts/lib/engine.sh runs it in a
# throwaway container attached to the lab's edge network, where keycloak, vault
# and gitea resolve by service name.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"

[[ $# -ge 1 ]] || die "usage: jml.sh {join|move|leave|show} [--user NAME] ..."

run_engine jml.py "$@"
