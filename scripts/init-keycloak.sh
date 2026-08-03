#!/usr/bin/env bash
# =============================================================================
# Keycloak provisioning
# =============================================================================
# Runs once per `docker compose up`, inside the Keycloak image itself so that
# kcadm.sh and a compatible JRE are guaranteed to be present.
#
# The realm's *structure* arrives via --import-realm from
# configs/keycloak/realm-export.json. This script supplies everything that must
# NOT live in a tracked file:
#
#   1. Demo user passwords          (from DEMO_USER_PASSWORD)
#   2. The Grafana OIDC client secret (from KEYCLOAK_GRAFANA_CLIENT_SECRET)
#   3. Redirect URIs, when LAB_DOMAIN is not the default lab.localhost
#
# Every step is idempotent — running it against an already-provisioned realm
# converges to the same state rather than failing.
# =============================================================================
set -euo pipefail

KCADM="/opt/keycloak/bin/kcadm.sh"
# kcadm caches its session in $HOME/.keycloak by default. Home may be read-only
# depending on how the image is run, so pin the config somewhere always writable.
KCADM_CONFIG="/tmp/kcadm.config"

KC_URL="${KC_URL:-http://keycloak:8080}"
KC_REALM="${KC_REALM:-lab}"
KC_ADMIN="${KC_ADMIN:-admin}"
KC_ADMIN_PASSWORD="${KC_ADMIN_PASSWORD:?KC_ADMIN_PASSWORD is required}"
DEMO_USER_PASSWORD="${DEMO_USER_PASSWORD:?DEMO_USER_PASSWORD is required}"
GRAFANA_CLIENT_SECRET="${GRAFANA_CLIENT_SECRET:?GRAFANA_CLIENT_SECRET is required}"
LAB_DOMAIN="${LAB_DOMAIN:-lab.localhost}"

DEMO_USERS=(alice bob carol dave)

log()  { printf '[keycloak-init] %s\n' "$*"; }
warn() { printf '[keycloak-init] WARNING: %s\n' "$*" >&2; }
die()  { printf '[keycloak-init] ERROR: %s\n' "$*" >&2; exit 1; }

kc() { "$KCADM" "$@" --config "$KCADM_CONFIG"; }

# -----------------------------------------------------------------------------
# 1. Authenticate
#
# Compose already gates this container on Keycloak reporting healthy, but
# "healthy" and "admin API is answering" are a second or two apart on a cold
# start, so the login is retried rather than assumed.
# -----------------------------------------------------------------------------
authenticate() {
  local attempt=1 max=30
  until kc config credentials \
          --server "$KC_URL" \
          --realm master \
          --user "$KC_ADMIN" \
          --password "$KC_ADMIN_PASSWORD" >/dev/null 2>&1; do
    if (( attempt >= max )); then
      die "could not authenticate against $KC_URL after $max attempts"
    fi
    log "waiting for the Keycloak admin API (attempt $attempt/$max)"
    sleep 3
    (( attempt++ ))
  done
  log "authenticated as '$KC_ADMIN'"
}

# -----------------------------------------------------------------------------
# 2. Confirm the realm import landed
# -----------------------------------------------------------------------------
require_realm() {
  if kc get "realms/${KC_REALM}" --fields realm >/dev/null 2>&1; then
    log "realm '${KC_REALM}' present"
    return
  fi
  die "realm '${KC_REALM}' was not imported. Check the Keycloak container logs
        for import errors, then recreate the stack with 'make clean && make up'."
}

# -----------------------------------------------------------------------------
# 3. Set demo user passwords
#
# Passwords never appear in the realm JSON, so they are set here on every boot.
# The realm enforces a 12-character policy with mixed case and digits; a weak
# DEMO_USER_PASSWORD is rejected by Keycloak rather than silently accepted.
# -----------------------------------------------------------------------------
set_demo_passwords() {
  local user failures=0
  for user in "${DEMO_USERS[@]}"; do
    if kc set-password -r "$KC_REALM" \
         --username "$user" \
         --new-password "$DEMO_USER_PASSWORD" >/dev/null 2>&1; then
      log "password set for '$user'"
    else
      warn "could not set the password for '$user' — it may violate the realm password policy"
      (( failures++ )) || true
    fi
  done

  if (( failures == ${#DEMO_USERS[@]} )); then
    die "no demo passwords could be set. DEMO_USER_PASSWORD must satisfy the
        realm policy: at least 12 characters, with upper case, lower case and a
        digit. Run 'make secrets' to generate a compliant value."
  fi
}

# -----------------------------------------------------------------------------
# 4. Inject client secrets
#
# The internal client UUID differs from the human-readable clientId, so it has
# to be looked up. `--format csv --noquotes` avoids needing jq, which the
# Keycloak image does not ship.
# -----------------------------------------------------------------------------
client_uuid() {
  kc get clients -r "$KC_REALM" \
     -q "clientId=$1" --fields id --format csv --noquotes 2>/dev/null | head -n1 | tr -d '\r'
}

set_client_secret() {
  local client_id="$1" secret="$2" uuid
  uuid="$(client_uuid "$client_id")"

  if [[ -z "$uuid" ]]; then
    warn "client '$client_id' not found; skipping secret injection"
    return
  fi

  kc update "clients/${uuid}" -r "$KC_REALM" -s "secret=${secret}" >/dev/null
  log "client secret set for '$client_id'"
}

# -----------------------------------------------------------------------------
# 5. Re-point redirect URIs at the configured domain
#
# The realm JSON is written against the default lab.localhost. Anyone running
# the lab on a different domain would otherwise hit an "Invalid redirect_uri"
# error on their first login, with no obvious cause.
# -----------------------------------------------------------------------------
retarget_client_domain() {
  local client_id="$1" subdomain="$2" uuid base
  uuid="$(client_uuid "$client_id")"
  [[ -z "$uuid" ]] && { warn "client '$client_id' not found; skipping retarget"; return; }

  base="https://${subdomain}.${LAB_DOMAIN}"

  kc update "clients/${uuid}" -r "$KC_REALM" \
     -s "rootUrl=${base}" \
     -s "redirectUris=[\"${base}/*\",\"http://${subdomain}.${LAB_DOMAIN}/*\"]" \
     -s "webOrigins=[\"${base}\",\"http://${subdomain}.${LAB_DOMAIN}\"]" >/dev/null

  log "redirect URIs for '$client_id' now point at ${subdomain}.${LAB_DOMAIN}"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  log "provisioning realm '${KC_REALM}' at ${KC_URL}"

  authenticate
  require_realm
  set_demo_passwords
  set_client_secret grafana "$GRAFANA_CLIENT_SECRET"

  if [[ "$LAB_DOMAIN" != "lab.localhost" ]]; then
    log "LAB_DOMAIN is '${LAB_DOMAIN}' — rewriting client redirect URIs"
    retarget_client_domain grafana   grafana
    retarget_client_domain openwebui chat
  fi

  log "done. Demo users: ${DEMO_USERS[*]} (all share DEMO_USER_PASSWORD)"
  log "admin console: https://keycloak.${LAB_DOMAIN}/admin"
}

main "$@"
