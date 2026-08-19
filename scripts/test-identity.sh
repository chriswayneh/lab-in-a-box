#!/usr/bin/env bash
# =============================================================================
# Identity lifecycle test suite — containerised entry point
# =============================================================================
#   make jml-test
#
# Runs the suite against the RUNNING lab. These are integration tests by design:
# the feature being verified is whether revocation actually revokes, and that is
# not a question a mock can answer.
#
# Uses disposable identities (jmltest, jmltoken). The seeded demo users are
# protected by model.PROTECTED_USERNAMES and are never modified.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

RUNNER_IMAGE="${LAB_JML_IMAGE:-python:3.12-alpine}"

require_docker

PROJECT="$(project_name)"
NETWORK="${PROJECT}_edge"

docker network inspect "$NETWORK" >/dev/null 2>&1 \
  || die "the lab network '${NETWORK}' does not exist — start the lab with 'make up'"

for service in keycloak vault gitea; do
  compose ps --status running --services 2>/dev/null | grep -qx "$service" \
    || die "service '${service}' is not running — check 'make health'"
done

heading "Identity lifecycle tests"
log "${C_DIM}Running against the live lab. This creates and offboards disposable users.${C_RESET}"

mkdir -p "${LAB_ROOT}/artifacts/identity"

# See the equivalent block in scripts/jml.sh: container paths must survive
# MSYS rewriting, host paths must be converted into real Windows paths.
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

HOST_ROOT="$(host_path "$LAB_ROOT")"

# See scripts/jml.sh: exported, not passed as --env NAME=VALUE, so the token
# never appears in argv and therefore never in the host process table.
VAULT_TOKEN="$(env_value VAULT_DEV_ROOT_TOKEN 'vault-insecure-dev-only')"
export VAULT_TOKEN

exec docker run --rm \
  --network "$NETWORK" \
  --env-file "$(host_path "${LAB_ROOT}/.env")" \
  --env VAULT_TOKEN \
  --env "KEYCLOAK_URL=http://keycloak:8080" \
  --env "VAULT_ADDR=http://vault:8200" \
  --env "GITEA_URL=http://gitea:3000" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --volume "${HOST_ROOT}/scripts/identity:/engine:ro" \
  --volume "${HOST_ROOT}/identity:/identity:ro" \
  --volume "${HOST_ROOT}/configs/vault/policies:/vault-policies:ro" \
  --volume "${HOST_ROOT}/artifacts:/artifacts" \
  --workdir /engine \
  "$RUNNER_IMAGE" \
  python3 /engine/test_lifecycle.py
