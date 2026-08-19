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
# runtime may be required beyond Docker. So it runs in a throwaway
# python:3.12-alpine container attached to the lab's edge network, where
# keycloak, vault and gitea resolve by service name. The engine imports nothing
# outside the standard library, so there is no install step.
#
# Credentials arrive as environment variables from .env and are never passed as
# command-line arguments — argv is visible to every process on the host, an
# environment block is not.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

RUNNER_IMAGE="${LAB_JML_IMAGE:-python:3.12-alpine}"

require_docker

[[ $# -ge 1 ]] || die "usage: jml.sh {join|move|leave|show} [--user NAME] ..."

# -----------------------------------------------------------------------------
# The engine talks to services over the lab's internal network, so the lab has
# to be running. Checking here turns a confusing DNS error into a clear message.
# -----------------------------------------------------------------------------
PROJECT="$(project_name)"
NETWORK="${PROJECT}_edge"

if ! docker network inspect "$NETWORK" >/dev/null 2>&1; then
  die "the lab network '${NETWORK}' does not exist — start the lab first with 'make up'"
fi

for service in keycloak vault gitea; do
  if ! compose ps --status running --services 2>/dev/null | grep -qx "$service"; then
    die "service '${service}' is not running — check 'make health', then 'make up'"
  fi
done

# -----------------------------------------------------------------------------
# Artifacts are written by the container as root; keeping them under the repo
# means the operator can read them afterwards without a docker cp.
# -----------------------------------------------------------------------------
mkdir -p "${LAB_ROOT}/artifacts/identity"

# -----------------------------------------------------------------------------
# Path handling on Git Bash
#
# Two different things need two different treatments, which is why this is not
# one flag:
#
#   - CONTAINER paths (/engine, /identity) must be passed through untouched.
#     MSYS would otherwise rewrite them into C:/Program Files/Git/engine.
#   - HOST paths must be real Windows paths. $LAB_ROOT is /c/dev/... in MSYS
#     form, which the Docker daemon cannot resolve.
#
# So: disable the automatic rewriting, and convert the host paths deliberately.
# On macOS and Linux cygpath does not exist and the paths pass through as-is.
# -----------------------------------------------------------------------------
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

HOST_ROOT="$(host_path "$LAB_ROOT")"

# The Vault root token is exported rather than written as --env NAME=VALUE.
# A value on the command line lands in argv, which every process on the host can
# read out of the process table; `--env NAME` with no value tells Docker to copy
# it from this shell's environment instead, where it is not world-readable.
VAULT_TOKEN="$(env_value VAULT_DEV_ROOT_TOKEN 'vault-insecure-dev-only')"
export VAULT_TOKEN

exec docker run --rm --interactive \
  --network "$NETWORK" \
  --env-file "$(host_path "${LAB_ROOT}/.env")" \
  --env VAULT_TOKEN \
  --env "KEYCLOAK_URL=http://keycloak:8080" \
  --env "VAULT_ADDR=http://vault:8200" \
  --env "GITEA_URL=http://gitea:3000" \
  --env "NO_COLOR=${NO_COLOR:-}" \
  --env "LAB_ALLOW_PROTECTED=${LAB_ALLOW_PROTECTED:-0}" \
  --env PYTHONDONTWRITEBYTECODE=1 \
  --volume "${HOST_ROOT}/scripts/identity:/engine:ro" \
  --volume "${HOST_ROOT}/identity:/identity:ro" \
  --volume "${HOST_ROOT}/configs/vault/policies:/vault-policies:ro" \
  --volume "${HOST_ROOT}/artifacts:/artifacts" \
  --workdir /engine \
  "$RUNNER_IMAGE" \
  python3 /engine/jml.py "$@"
