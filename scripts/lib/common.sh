#!/usr/bin/env bash
# =============================================================================
# Shared shell helpers
# =============================================================================
# Sourced by every script in scripts/. Not executable on its own.
#
#   source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# =============================================================================

# Guard against being sourced twice.
[[ -n "${LAB_COMMON_SOURCED:-}" ]] && return 0
LAB_COMMON_SOURCED=1

# -----------------------------------------------------------------------------
# Repository root, resolved from this file's location rather than $PWD, so the
# scripts work when invoked from anywhere.
# -----------------------------------------------------------------------------
LAB_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LAB_ROOT="$(cd "${LAB_SCRIPT_DIR}/.." && pwd)"
export LAB_ROOT

# -----------------------------------------------------------------------------
# Colour, but only when a human is watching. Piping output to a file or a CI
# log should not fill it with escape sequences.
# -----------------------------------------------------------------------------
if [[ -t 1 ]] && [[ -z "${NO_COLOR:-}" ]] && [[ "${TERM:-dumb}" != "dumb" ]]; then
  C_RESET=$'\033[0m'
  C_BOLD=$'\033[1m'
  C_DIM=$'\033[2m'
  C_RED=$'\033[31m'
  C_GREEN=$'\033[32m'
  C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'
  C_CYAN=$'\033[36m'
else
  C_RESET='' C_BOLD='' C_DIM='' C_RED='' C_GREEN='' C_YELLOW='' C_BLUE='' C_CYAN=''
fi
export C_RESET C_BOLD C_DIM C_RED C_GREEN C_YELLOW C_BLUE C_CYAN

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
log()     { printf '%s\n' "$*"; }
info()    { printf '%s>%s %s\n' "$C_BLUE" "$C_RESET" "$*"; }
success() { printf '%s✓%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()    { printf '%s!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
error()   { printf '%s✗%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; }
die()     { error "$*"; exit 1; }

heading() {
  printf '\n%s%s%s\n' "$C_BOLD" "$*" "$C_RESET"
  printf '%s%s%s\n' "$C_DIM" "$(printf '─%.0s' $(seq 1 ${#1}))" "$C_RESET"
}

# -----------------------------------------------------------------------------
# Preconditions
# -----------------------------------------------------------------------------
require_cmd() {
  local cmd="$1" hint="${2:-}"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    error "required command not found: ${cmd}"
    [[ -n "$hint" ]] && log "  ${C_DIM}${hint}${C_RESET}"
    exit 1
  fi
}

require_docker() {
  require_cmd docker "Install Docker Desktop or Docker Engine: https://docs.docker.com/get-docker/"
  if ! docker compose version >/dev/null 2>&1; then
    die "Docker Compose v2 is required. 'docker compose version' failed — you may have the older, separate 'docker-compose' binary."
  fi
  if ! docker info >/dev/null 2>&1; then
    die "the Docker daemon is not responding. Is Docker Desktop running?"
  fi
}

# -----------------------------------------------------------------------------
# Compose wrapper
#
# Always addresses the project from the repository root and honours the same
# override files the Makefile uses, so a script and a `make` target never
# operate on subtly different projects.
# -----------------------------------------------------------------------------
compose() {
  local -a args=(--project-directory "$LAB_ROOT" -f "${LAB_ROOT}/docker-compose.yml")
  [[ "${GPU:-0}" == "1" ]] && args+=(-f "${LAB_ROOT}/compose/overrides/gpu.yml")
  docker compose "${args[@]}" "$@"
}

# -----------------------------------------------------------------------------
# .env access
#
# Reads a single value without sourcing the file. Sourcing would execute
# whatever happens to be in there, and a stray backtick in a generated password
# would become a command substitution.
# -----------------------------------------------------------------------------
env_value() {
  local key="$1" default="${2:-}" file="${3:-${LAB_ROOT}/.env}"
  [[ -f "$file" ]] || { printf '%s' "$default"; return 0; }

  local value
  value="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//')"
  printf '%s' "${value:-$default}"
}

lab_domain() { env_value LAB_DOMAIN "lab.localhost"; }
project_name() { env_value COMPOSE_PROJECT_NAME "lab"; }

# -----------------------------------------------------------------------------
# Random secret generation
#
# Tries OpenSSL, then /dev/urandom, then Python. At least one of the three is
# present on any machine that can run this lab.
# -----------------------------------------------------------------------------
random_string() {
  local length="${1:-32}"

  if command -v openssl >/dev/null 2>&1; then
    # base64 then strip anything that needs quoting in a shell, a URL, a YAML
    # document or a JDBC connection string.
    openssl rand -base64 $((length * 2)) | tr -dc 'A-Za-z0-9' | head -c "$length"
  elif [[ -r /dev/urandom ]]; then
    LC_ALL=C tr -dc 'A-Za-z0-9' < /dev/urandom | head -c "$length"
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c "import secrets,string,sys; a=string.ascii_letters+string.digits; sys.stdout.write(''.join(secrets.choice(a) for _ in range(${length})))"
  else
    die "no source of randomness available (tried openssl, /dev/urandom, python3)"
  fi
}

# A password guaranteed to contain upper case, lower case and a digit, because
# Keycloak's realm policy rejects anything that does not — and a rejected
# password surfaces as a confusing provisioning failure much later.
random_password() {
  local length="${1:-32}"
  local body
  body="$(random_string $((length - 3)))"
  # Fixed positions would be a weakness if this were a human-chosen password;
  # for 29 random characters plus 3, it costs nothing meaningful.
  printf '%s%s%s%s' "$body" \
    "$(random_string 1 | tr '[:lower:]' '[:upper:]')" \
    "$(random_string 1 | tr '[:upper:]' '[:lower:]')" \
    "$(( RANDOM % 10 ))"
}

# -----------------------------------------------------------------------------
# Interaction
# -----------------------------------------------------------------------------
confirm() {
  local prompt="$1" reply
  # Non-interactive callers (CI, `make -s`) must opt in explicitly rather than
  # hang forever on a read that will never be answered.
  if [[ ! -t 0 ]]; then
    [[ "${ASSUME_YES:-0}" == "1" ]] && return 0
    error "refusing to continue: '${prompt}' needs confirmation but there is no terminal."
    log "  ${C_DIM}Set ASSUME_YES=1 to proceed non-interactively.${C_RESET}"
    return 1
  fi

  printf '%s%s%s [y/N] ' "$C_YELLOW" "$prompt" "$C_RESET"
  read -r reply
  [[ "$reply" =~ ^[Yy]([Ee][Ss])?$ ]]
}
