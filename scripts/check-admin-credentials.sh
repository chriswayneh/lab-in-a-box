#!/usr/bin/env bash
# =============================================================================
# Required credential check
# =============================================================================
#   bash scripts/check-admin-credentials.sh
#
# None of the lab's passwords, tokens and client secrets listed in REQUIRED
# has a fallback value in compose/. They must come from .env, which `make secrets`
# generates. It runs in `make up`, `make update`, `make restore` (before the
# lab is stopped) and after `make secrets`, and
# fails if any one is missing, empty, still a CHANGEME_ placeholder, or an
# old shipped development default.
#
# It reads values the way Compose does: a variable set in your shell wins,
# otherwise the value in .env is used. It never prints a credential, only
# the variable name.
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

ENV_FILE="${LAB_ROOT}/.env"
# Every credential that has no built-in default in compose/. Keep in step
# with REQUIRED_SECRETS in compose/01-core.yml (secrets-init).
REQUIRED=(
  KEYCLOAK_ADMIN_PASSWORD
  VAULT_DEV_ROOT_TOKEN
  DEMO_USER_PASSWORD
  KEYCLOAK_DB_PASSWORD
  GITEA_DB_PASSWORD
  REDIS_PASSWORD
  KEYCLOAK_GRAFANA_CLIENT_SECRET
  KEYCLOAK_OAUTH2_PROXY_CLIENT_SECRET
  OAUTH2_PROXY_COOKIE_SECRET
  KEYCLOAK_SCIM_CLIENT_SECRET
  GITEA_ADMIN_PASSWORD
  OPENWEBUI_SECRET_KEY
  PGADMIN_PASSWORD
)

# Print the value Compose would use for KEY from .env, following its rules:
# quoted values end at the closing quote, an unquoted value ends at " #".
# A value that still contains `${` or `$NAME` would be interpolated by
# Compose, so it is reported as INTERPOLATED rather than trusted.
env_file_value() {
  local key="$1" line raw value
  [[ -f "$ENV_FILE" ]] || return 0
  line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$ENV_FILE" | tail -n1)" || true
  [[ -n "$line" ]] || return 0
  raw="${line#*=}"
  raw="${raw#"${raw%%[![:space:]]*}"}"
  case "$raw" in
    \'*) value="${raw#\'}"; value="${value%%\'*}"; printf '%s' "$value"; return 0 ;;
    \"*) value="${raw#\"}"; value="${value%%\"*}" ;;
    *)   value="$(printf '%s' "$raw" | sed -E 's/[[:space:]]+#.*$//; s/[[:space:]]+$//')" ;;
  esac
  if [[ "$value" =~ \$(\{|[A-Za-z_]) ]]; then
    printf 'INTERPOLATED'
    return 0
  fi
  printf '%s' "$value"
}

effective_value() {
  local key="$1"
  if [[ -n "${!key+set}" ]]; then
    printf '%s' "${!key}"
  else
    env_file_value "$key"
  fi
}

problems=0
for key in "${REQUIRED[@]}"; do
  value="$(effective_value "$key")"
  reason=""
  if [[ "$value" == INTERPOLATED ]]; then
    reason="refers to another variable (\${...}); set a literal value"
  elif [[ -z "${value//[[:space:]]/}" ]]; then
    reason="is missing or empty"
  elif [[ "$value" == CHANGEME_* ]]; then
    reason="is still the CHANGEME_ placeholder from .env.example"
  elif [[ "$value" == *insecure-dev-only || "$value" == 'LabDemoPass1!' ]]; then
    reason="is an old shared development default"
  fi
  if [[ -n "$reason" ]]; then
    error "${key} ${reason}"
    (( problems++ )) || true
  fi
done

# File-backed Docker secrets. The tracked secrets/*.txt placeholders only let
# Compose parse; the lab must run with the generated files in LAB_SECRETS_DIR.
SECRET_FILES=(postgres_password minio_root_password grafana_admin_password portainer_admin_password)
secrets_dir="$(effective_value LAB_SECRETS_DIR)"
[[ -n "$secrets_dir" && "$secrets_dir" != INTERPOLATED ]] || secrets_dir="./secrets"
[[ "$secrets_dir" == /* ]] || secrets_dir="${LAB_ROOT}/${secrets_dir#./}"
for name in "${SECRET_FILES[@]}"; do
  path="${secrets_dir}/${name}.txt"
  reason=""
  if [[ ! -f "$path" ]]; then
    reason="is missing"
  else
    content="$(tr -d ' \t\r\n' < "$path")"
    if [[ -z "$content" ]]; then reason="is empty"
    elif [[ "$content" == *insecure-dev-only || "$content" == CHANGEME_* ]]; then
      reason="still holds the shipped placeholder"
    fi
  fi
  if [[ -n "$reason" ]]; then
    error "secret file ${name}.txt ${reason}"
    (( problems++ )) || true
  fi
done

if (( problems > 0 )); then
  log ""
  log "  Fresh install?  Run ${C_CYAN}make secrets${C_RESET} to generate them."
  log "  Existing lab?   Put your lab's current values in .env. Changing the"
  log "                  Keycloak password in .env alone does not change it in Keycloak."
  exit 1
fi
success "all ${#REQUIRED[@]} required credentials and ${#SECRET_FILES[@]} secret files are set"
