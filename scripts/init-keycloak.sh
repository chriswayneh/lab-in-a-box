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
#   3. The SCIM service-account secret and least-privilege realm roles
#   4. Redirect URIs and the SCIM audience for a custom LAB_DOMAIN
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
SCIM_CLIENT_SECRET="${SCIM_CLIENT_SECRET:?SCIM_CLIENT_SECRET is required}"
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

enable_scim_api() {
  kc update "realms/${KC_REALM}" -s scimApiEnabled=true >/dev/null
  log "SCIM API enabled for realm '${KC_REALM}'"
}

ensure_scim_group() {
  local group_id
  group_id="$(kc get groups -r "$KC_REALM" -q 'search=SCIM Managed' \
    --fields id,name --format csv --noquotes 2>/dev/null \
    | grep ',SCIM Managed$' | head -n1 | cut -d, -f1 | tr -d '\r' || true)"
  if [[ -z "$group_id" ]]; then
    kc create groups -r "$KC_REALM" -s 'name=SCIM Managed' >/dev/null
    log "group '/SCIM Managed' created"
  else
    log "group '/SCIM Managed' present"
  fi
}

# -----------------------------------------------------------------------------
# 3. Set demo user passwords
#
# Passwords never appear in the realm JSON, so they are set here on every boot.
# The realm enforces a 12-character policy with mixed case and digits; a weak
# DEMO_USER_PASSWORD is rejected by Keycloak rather than silently accepted.
# -----------------------------------------------------------------------------
set_demo_passwords() {
  local user user_id credentials failures=0
  for user in "${DEMO_USERS[@]}"; do
    user_id="$(kc get users -r "$KC_REALM" -q "username=${user}" \
      --fields id --format csv --noquotes 2>/dev/null | head -n1 | tr -d '\r')"
    if [[ -n "$user_id" ]]; then
      credentials="$(kc get "users/${user_id}/credentials" -r "$KC_REALM" \
        --fields type --format csv --noquotes 2>/dev/null || true)"
      if grep -qx 'password' <<<"$credentials"; then
        log "password already present for '$user' (not reset)"
        continue
      fi
    fi
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
# 6. SCIM service account
#
# Keycloak protects the SCIM API with the same fine-grained realm-management
# roles as its Admin REST API. Assign only manage-users: it implies the query
# and view permissions needed by Users, Groups and the discovery endpoints,
# without granting realm or client administration.
# -----------------------------------------------------------------------------
configure_scim_client() {
  local uuid service_user_id mapper_id audience
  uuid="$(client_uuid lab-scim)"
  if [[ -z "$uuid" ]]; then
    kc create clients -r "$KC_REALM" \
      -s clientId=lab-scim \
      -s 'name=Lab SCIM Provisioner' \
      -s 'description=Confidential service account authorized to manage the lab realm through SCIM 2.0.' \
      -s enabled=true \
      -s protocol=openid-connect \
      -s publicClient=false \
      -s standardFlowEnabled=false \
      -s directAccessGrantsEnabled=false \
      -s serviceAccountsEnabled=true \
      -s fullScopeAllowed=false >/dev/null
    uuid="$(client_uuid lab-scim)"
    log "client 'lab-scim' created for this existing realm"
  fi
  [[ -n "$uuid" ]] || { warn "client 'lab-scim' could not be created"; return; }

  set_client_secret lab-scim "$SCIM_CLIENT_SECRET"
  kc update "clients/${uuid}" -r "$KC_REALM" -s fullScopeAllowed=true >/dev/null

  service_user_id="$(kc get "clients/${uuid}/service-account-user" -r "$KC_REALM" \
    --fields id --format csv --noquotes 2>/dev/null | head -n1 | tr -d '\r')"
  if [[ -n "$service_user_id" ]]; then
    kc add-roles -r "$KC_REALM" --uid "$service_user_id" \
      --cclientid realm-management --rolename manage-users >/dev/null
    log "realm-management/manage-users assigned to the SCIM service account"
  else
    warn "could not resolve the lab-scim service account user"
  fi

  # The SCIM API validates the token audience against its externally visible
  # base URL. Keep the mapper correct when LAB_DOMAIN is customized.
  audience="https://keycloak.${LAB_DOMAIN}/realms/${KC_REALM}/scim/v2"
  mapper_id="$(kc get "clients/${uuid}/protocol-mappers/models" -r "$KC_REALM" \
    -q name=scim-audience --fields id --format csv --noquotes 2>/dev/null | head -n1 | tr -d '\r')"
  if [[ -z "$mapper_id" ]]; then
    kc create "clients/${uuid}/protocol-mappers/models" -r "$KC_REALM" \
      -s name=scim-audience \
      -s protocol=openid-connect \
      -s protocolMapper=oidc-audience-mapper \
      -s consentRequired=false \
      -s "config.\"included.custom.audience\"=${audience}" \
      -s 'config."access.token.claim"=true' \
      -s 'config."id.token.claim"=false' >/dev/null
    mapper_id="$(kc get "clients/${uuid}/protocol-mappers/models" -r "$KC_REALM" \
      -q name=scim-audience --fields id --format csv --noquotes 2>/dev/null | head -n1 | tr -d '\r')"
  fi
  kc update "clients/${uuid}/protocol-mappers/models/${mapper_id}" -r "$KC_REALM" \
    -s "config.\"included.custom.audience\"=${audience}" >/dev/null
  log "SCIM token audience set to ${audience}"
}

# -----------------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------------
main() {
  log "provisioning realm '${KC_REALM}' at ${KC_URL}"

  authenticate
  require_realm
  enable_scim_api
  ensure_scim_group
  set_demo_passwords
  set_client_secret grafana "$GRAFANA_CLIENT_SECRET"
  configure_scim_client

  if [[ "$LAB_DOMAIN" != "lab.localhost" ]]; then
    log "LAB_DOMAIN is '${LAB_DOMAIN}' — rewriting client redirect URIs"
    retarget_client_domain grafana   grafana
    retarget_client_domain openwebui chat
  fi

  log "done. Demo users: ${DEMO_USERS[*]} (all share DEMO_USER_PASSWORD)"
  log "admin console: https://keycloak.${LAB_DOMAIN}/admin"
}

main "$@"
