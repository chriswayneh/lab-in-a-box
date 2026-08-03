#!/usr/bin/env bash
# =============================================================================
# Credential summary
# =============================================================================
#   make creds
#
# Prints every URL and login for the running lab, reading them from .env and
# the selected Docker-secret directory rather than documentation that could
# drift out of date.
#
# Output goes to the terminal on purpose. Piping it to a file or a chat window
# writes every password in the lab to somewhere it will outlive the lab.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

DOMAIN="$(lab_domain)"
SECRETS_DIR="$(env_value LAB_SECRETS_DIR "./secrets")"
[[ "$SECRETS_DIR" == /* ]] || SECRETS_DIR="${LAB_ROOT}/${SECRETS_DIR#./}"

read_secret() {
  local file="${SECRETS_DIR}/$1"
  [[ -r "$file" ]] || { printf '(missing: %s)' "$1"; return 0; }
  tr -d '\r\n' < "$file"
}

# Flag values that are still the shipped placeholders, so an insecure lab is
# impossible to overlook.
INSECURE_FOUND=0
mark() {
  local value="$1"
  if [[ "$value" == *"insecure-dev-only"* || "$value" == *"CHANGEME_"* ]]; then
    INSECURE_FOUND=1
    printf '%s%s%s  %s← default, run: make secrets%s' \
      "$C_RED" "$value" "$C_RESET" "$C_YELLOW" "$C_RESET"
  else
    printf '%s%s%s' "$C_BOLD" "$value" "$C_RESET"
  fi
}

row() {
  printf '  %-14s %s\n' "$1" "$(mark "$2")"
}

service() {
  printf '\n%s%s%s\n' "$C_CYAN" "$1" "$C_RESET"
  printf '  %-14s %s\n' "URL" "https://$2.${DOMAIN}"
}

main() {
  if [[ ! -f "${LAB_ROOT}/.env" ]]; then
    warn "no .env found — the lab is running on built-in development defaults."
    log "  ${C_DIM}Generate real credentials with: make secrets${C_RESET}"
    log ""
  fi

  heading "Lab-in-a-Box credentials"
  log "${C_DIM}Domain: ${DOMAIN}${C_RESET}"

  printf '\n%s%s%s\n' "$C_CYAN" "Landing page" "$C_RESET"
  printf '  %-14s %s\n' "URL" "https://${DOMAIN}"

  service "Keycloak" "keycloak"
  row "Admin user" "$(env_value KEYCLOAK_ADMIN admin)"
  row "Password" "$(env_value KEYCLOAK_ADMIN_PASSWORD admin-insecure-dev-only)"
  printf '  %-14s %s\n' "Realm" "$(env_value KEYCLOAK_REALM lab)"
  printf '  %-14s %s\n' "Demo users" "alice, bob, carol, dave"
  row "Demo password" "$(env_value DEMO_USER_PASSWORD demo-insecure-dev-only)"

  service "Vault" "vault"
  row "Root token" "$(env_value VAULT_DEV_ROOT_TOKEN vault-root-insecure-dev-only)"
  printf '  %-14s %s\n' "userpass" "alice / bob / carol (same demo password)"

  service "Grafana" "grafana"
  row "User" "$(env_value GRAFANA_ADMIN_USER admin)"
  row "Password" "$(read_secret grafana_admin_password.txt)"

  service "MinIO" "minio"
  row "Root user" "$(env_value MINIO_ROOT_USER labadmin)"
  row "Password" "$(read_secret minio_root_password.txt)"
  printf '  %-14s %s\n' "S3 endpoint" "https://s3.${DOMAIN}"

  service "Gitea" "git"
  row "Admin user" "$(env_value GITEA_ADMIN_USER labadmin)"
  row "Password" "$(env_value GITEA_ADMIN_PASSWORD gitea-insecure-dev-only)"
  printf '  %-14s %s\n' "SSH" "ssh://git@${DOMAIN}:$(env_value GITEA_SSH_PORT 2222)"

  service "pgAdmin" "pgadmin"
  row "Email" "$(env_value PGADMIN_EMAIL labadmin@lab.local)"
  row "Password" "$(env_value PGADMIN_PASSWORD pgadmin-insecure-dev-only)"

  printf '\n%s%s%s\n' "$C_CYAN" "PostgreSQL" "$C_RESET"
  printf '  %-14s %s\n' "Host" "localhost:$(env_value POSTGRES_PORT 5432)"
  row "User" "$(env_value POSTGRES_USER lab)"
  row "Password" "$(read_secret postgres_password.txt)"
  printf '  %-14s %s\n' "Databases" "lab, keycloak, gitea"

  printf '\n%s%s%s\n' "$C_CYAN" "Redis" "$C_RESET"
  printf '  %-14s %s\n' "Host" "localhost:$(env_value REDIS_PORT 6379)"
  row "Password" "$(env_value REDIS_PASSWORD redis-insecure-dev-only)"

  printf '\n%s%s%s\n' "$C_CYAN" "Portainer / Open WebUI" "$C_RESET"
  printf '  %s\n' "Both prompt you to create an administrator on first visit."
  printf '  %s\n' "https://portainer.${DOMAIN}   https://chat.${DOMAIN}"

  log ""
  if (( INSECURE_FOUND )); then
    log "$(printf '%s%s%s' "$C_RED" "$(printf '─%.0s' $(seq 1 68))" "$C_RESET")"
    error "This lab is using built-in development credentials."
    log "  They are published in this repository and are known to everyone."
    log ""
    log "  ${C_BOLD}Fix:${C_RESET}  ${C_CYAN}make secrets && make clean && make up${C_RESET}"
    log "  ${C_DIM}(clean is needed because services store the old password in their volumes)${C_RESET}"
  else
    success "all credentials are randomly generated"
  fi
}

main "$@"
