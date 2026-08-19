#!/usr/bin/env bash
# =============================================================================
# RBAC simulator — containerised entry point
# =============================================================================
#   bash scripts/rbac.sh show    --user erin
#   bash scripts/rbac.sh show    --user erin --format json
#   bash scripts/rbac.sh diff    --user alice --other bob
#   bash scripts/rbac.sh who-can --permission vault:secret/data/security/*
#
# Normally reached through `make rbac-show`, `make rbac-diff`, `make rbac-who-can`.
#
# READ-ONLY. This entry point runs the analysis engine, which issues only GET
# requests. Nothing here can change identity state.
#
# GRAFANA_OIDC_ENABLED is forwarded because the simulator needs to know whether
# Grafana is actually wired to Keycloak in this deployment before it claims a
# Grafana role for anyone.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"

[[ $# -ge 1 ]] || die "usage: rbac.sh {show|diff|who-can} [--user NAME] ..."

run_engine rbac_cli.py "$@"
