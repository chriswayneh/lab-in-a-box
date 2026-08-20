#!/usr/bin/env bash
# =============================================================================
# Identity engine runner
# =============================================================================
# Sourced by scripts/jml.sh, scripts/rbac.sh and scripts/test-identity.sh.
#
# All three need the same thing: run a Python module from scripts/identity/
# inside a throwaway container, on the lab's network, with the credentials it
# needs and none it does not. Defining that once means a fix to path handling or
# credential passing lands everywhere instead of in whichever copy was noticed.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"
#   run_engine jml.py join --user erin --role developer
# =============================================================================

[[ -n "${LAB_ENGINE_SOURCED:-}" ]] && return 0
LAB_ENGINE_SOURCED=1

# Only the standard library is used, so there is no install step and the image
# is a plain upstream Python.
LAB_ENGINE_IMAGE="${LAB_JML_IMAGE:-python:3.12-alpine}"

# -----------------------------------------------------------------------------
# Path handling on Git Bash
#
# Two different needs, which is why this is not one flag:
#   - CONTAINER paths (/engine, /identity) must pass through untouched, or MSYS
#     rewrites them into C:/Program Files/Git/engine.
#   - HOST paths must be real Windows paths; $LAB_ROOT is /c/dev/... in MSYS
#     form, which the Docker daemon cannot resolve.
#
# On macOS and Linux cygpath does not exist and paths pass through unchanged.
# -----------------------------------------------------------------------------
engine_host_path() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

# -----------------------------------------------------------------------------
# Refuse to run against a lab that is not up, so a DNS failure inside the
# container becomes a clear message out here instead.
# -----------------------------------------------------------------------------
engine_preflight() {
  require_docker

  local project network
  project="$(project_name)"
  network="${project}_edge"

  docker network inspect "$network" >/dev/null 2>&1 \
    || die "the lab network '${network}' does not exist — start the lab with 'make up'"

  local service
  for service in keycloak vault gitea; do
    compose ps --status running --services 2>/dev/null | grep -qx "$service" \
      || die "service '${service}' is not running — check 'make health', then 'make up'"
  done

  printf '%s' "$network"
}

# -----------------------------------------------------------------------------
# run_engine <module.py> [args...]
#
# Extra `docker run` flags may be supplied through LAB_ENGINE_DOCKER_ARGS.
# -----------------------------------------------------------------------------
run_engine() {
  local module="$1"; shift

  local network host_root
  network="$(engine_preflight)"
  host_root="$(engine_host_path "$LAB_ROOT")"

  mkdir -p "${LAB_ROOT}/artifacts/identity" "${LAB_ROOT}/artifacts/access-review"

  # The Vault root token is exported rather than written as --env NAME=VALUE.
  # A value on the command line lands in argv, which any process on the host can
  # read out of the process table; `--env NAME` copies it from this shell's
  # environment instead.
  VAULT_TOKEN="$(env_value VAULT_DEV_ROOT_TOKEN 'vault-insecure-dev-only')"
  export VAULT_TOKEN

  # The MSYS path-conversion switches are applied to THIS command only, never
  # exported. Exporting them leaks into every later call in the same shell —
  # including the `compose ps` inside engine_preflight, whose --project-directory
  # then stays in MSYS form and cannot be resolved by the daemon. The visible
  # symptom is a second run_engine in one script reporting that keycloak is not
  # running, which is both wrong and very hard to attribute.
  # shellcheck disable=SC2086  # LAB_ENGINE_DOCKER_ARGS is intentionally split
  MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
  docker run --rm --interactive \
    --network "$network" \
    --env-file "$(engine_host_path "${LAB_ROOT}/.env")" \
    --env VAULT_TOKEN \
    --env "KEYCLOAK_URL=http://keycloak:8080" \
    --env "VAULT_ADDR=http://vault:8200" \
    --env "GITEA_URL=http://gitea:3000" \
    --env "NO_COLOR=${NO_COLOR:-}" \
    --env "LAB_ALLOW_PROTECTED=${LAB_ALLOW_PROTECTED:-0}" \
    --env PYTHONDONTWRITEBYTECODE=1 \
    ${LAB_ENGINE_DOCKER_ARGS:-} \
    --volume "${host_root}/scripts/identity:/engine:ro" \
    --volume "${host_root}/identity:/identity:ro" \
    --volume "${host_root}/configs/vault/policies:/vault-policies:ro" \
    --volume "${host_root}/artifacts:/artifacts" \
    --workdir /engine \
    "$LAB_ENGINE_IMAGE" \
    python3 "/engine/${module}" "$@"
}
