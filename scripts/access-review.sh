#!/usr/bin/env bash
# =============================================================================
# Access review campaigns, containerised entry point
# =============================================================================
#   bash scripts/access-review.sh create   --name quarterly-q3
#   bash scripts/access-review.sh show     --campaign quarterly-q3-20260820T010000Z
#   bash scripts/access-review.sh list
#   bash scripts/access-review.sh decide   --campaign <id> --user erin --entitlement "Gitea:team developers" --decision approve
#   bash scripts/access-review.sh complete --campaign <id>
#   bash scripts/access-review.sh remediate --campaign <id>
#
# Normally reached through `make access-review-*`.
#
# Discovery reuses the RBAC simulator (read-only); remediation reuses the same
# Keycloak/Vault/Gitea adapters the JML commands use. This entry point adds
# nothing of its own beyond running the campaign workflow in the container.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"

[[ $# -ge 1 ]] || die "usage: access-review.sh {create|show|list|decide|complete|cancel|remediate} ..."

# These commands operate only on persisted campaign data. Running their engine
# container with no network proves they remain available during a service outage
# and prevents an accidental live-state dependency from creeping back in.
case "$1" in
  show|list|decide|cancel) export LAB_ENGINE_OFFLINE=1 ;;
esac

run_engine campaign_cli.py "$@"
