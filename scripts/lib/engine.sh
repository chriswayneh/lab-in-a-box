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
# Refuse live-state commands when the lab is not up, so a DNS failure inside
# the container becomes a clear message out here instead. Snapshot-only access
# review commands set LAB_ENGINE_OFFLINE=1 and run with Docker networking
# disabled because they only need the artifact mount.
# -----------------------------------------------------------------------------
engine_preflight() {
  require_docker

  if [[ "${LAB_ENGINE_OFFLINE:-0}" == "1" ]]; then
    printf 'none'
    return 0
  fi

  local project network network_name
  project="$(project_name)"
  network_name="${LAB_ENGINE_NETWORK:-edge}"
  network="${project}_${network_name}"

  docker network inspect "$network" >/dev/null 2>&1 \
    || die "the lab network '${network}' does not exist — start the lab with 'make up'"

  local service
  for service in ${LAB_ENGINE_SERVICES:-keycloak vault gitea}; do
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
run_engine() (
  set -euo pipefail
  local module="$1"; shift

  local network host_root
  network="$(engine_preflight)"
  host_root="$(engine_host_path "$LAB_ROOT")"

  mkdir -p "${LAB_ROOT}/artifacts/identity" "${LAB_ROOT}/artifacts/access-review"

  # Resolve authentication through Compose, preserving defaults, dotenv
  # interpolation and shell overrides without exposing values in argv.
  local auth_file setting value
  local -a env_file_args=()
  auth_file="$(umask 077; mktemp)"
  trap 'rm -f "$auth_file"' EXIT
  if ! compose config --format json | docker run --rm --interactive \
    --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges "$LAB_ENGINE_IMAGE" python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
fields = {
    "KEYCLOAK_ADMIN": ("keycloak-init", "KC_ADMIN"),
    "KEYCLOAK_ADMIN_PASSWORD": ("keycloak-init", "KC_ADMIN_PASSWORD"),
    "KEYCLOAK_REALM": ("keycloak-init", "KC_REALM"),
    "DEMO_USER_PASSWORD": ("keycloak-init", "DEMO_USER_PASSWORD"),
    "GITEA_ADMIN_USER": ("gitea-init", "GITEA_ADMIN_USER"),
    "GITEA_ADMIN_PASSWORD": ("gitea-init", "GITEA_ADMIN_PASSWORD"),
    "VAULT_TOKEN": ("vault-init", "VAULT_TOKEN"),
    "KEYCLOAK_DB_PASSWORD": ("keycloak", "KC_DB_PASSWORD"),
}
for name, (service, key) in fields.items():
    value = services[service]["environment"][key]
    if not isinstance(value, str) or "\0" in value:
        raise SystemExit("Invalid Compose authentication setting: " + name)
    sys.stdout.buffer.write((name + "\0" + value + "\0").encode())
' >"$auth_file"; then
    die "cannot resolve identity engine authentication from Compose"
  fi
  while IFS= read -r -d '' setting && IFS= read -r -d '' value; do
    printf -v "$setting" '%s' "$value"
    export "${setting?}"
  done <"$auth_file"
  rm -f "$auth_file"
  if [[ -f "${LAB_ROOT}/.env" ]]; then
    env_file_args=(--env-file "$(engine_host_path "${LAB_ROOT}/.env")")
  fi

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
    "${env_file_args[@]}" \
    --env KEYCLOAK_ADMIN --env KEYCLOAK_ADMIN_PASSWORD --env KEYCLOAK_REALM \
    --env DEMO_USER_PASSWORD --env GITEA_ADMIN_USER --env GITEA_ADMIN_PASSWORD \
    --env KEYCLOAK_DB_PASSWORD \
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
)
