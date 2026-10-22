#!/usr/bin/env bash
# =============================================================================
# Admin credential check
# =============================================================================
#   bash scripts/check-admin-credentials.sh
#
# The Keycloak admin password and the Vault root token have no fallback value
# in compose/02-iam.yml. They must come from .env, which `make secrets`
# generates. This check runs before `make up` and after `make secrets`, and
# fails if either one is missing, empty, still a CHANGEME_ placeholder, or an
# old shipped development default.
#
# It reads values the way Compose does: a variable set in your shell wins,
# otherwise the value in .env is used. It never prints a credential, only
# the variable name.
# =============================================================================
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

ENV_FILE="${LAB_ROOT}/.env"
REQUIRED=(KEYCLOAK_ADMIN_PASSWORD VAULT_DEV_ROOT_TOKEN)

effective_value() {
  local key="$1"
  if [[ -n "${!key+set}" ]]; then
    printf '%s' "${!key}"
  else
    env_value "$key" "" "$ENV_FILE"
  fi
}

problems=0
for key in "${REQUIRED[@]}"; do
  value="$(effective_value "$key")"
  # Strip one layer of single quotes, as Compose does for .env values.
  value="${value#\'}"; value="${value%\'}"
  reason=""
  if [[ -z "${value//[[:space:]]/}" ]]; then
    reason="is missing or empty"
  elif [[ "$value" == CHANGEME_* ]]; then
    reason="is still the CHANGEME_ placeholder from .env.example"
  elif [[ "$value" == *insecure-dev-only ]]; then
    reason="is an old shared development default"
  fi
  if [[ -n "$reason" ]]; then
    error "${key} ${reason}"
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
success "Keycloak and Vault admin credentials are set"
