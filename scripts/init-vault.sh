#!/bin/sh
# =============================================================================
# Vault provisioning
# =============================================================================
# POSIX sh, not bash: the hashicorp/vault image is Alpine-based and ships ash.
#
# Turns a bare dev-mode Vault into something worth demonstrating:
#
#   1. An audit device, enabled first so that everything after it is logged
#   2. KV v2 secrets under a realistic path hierarchy
#   3. ACL policies loaded from configs/vault/policies/*.hcl
#   4. AppRole auth for machines, userpass for humans
#   5. The transit engine, for encryption without ever handling a key
#
# Idempotent throughout: re-running converges rather than fails.
# =============================================================================
set -eu

VAULT_ADDR="${VAULT_ADDR:-http://vault:8200}"
export VAULT_ADDR
: "${VAULT_TOKEN:?VAULT_TOKEN is required}"
export VAULT_TOKEN

POLICY_DIR="/policies"
DEMO_USER_PASSWORD="${DEMO_USER_PASSWORD:-demo-insecure-dev-only}"

log()  { printf '[vault-init] %s\n' "$*"; }
warn() { printf '[vault-init] WARNING: %s\n' "$*" >&2; }
die()  { printf '[vault-init] ERROR: %s\n' "$*" >&2; exit 1; }

# -----------------------------------------------------------------------------
# Wait for the API. Compose gates on the healthcheck, but a dev-mode Vault
# unseals a moment after the process starts listening.
# -----------------------------------------------------------------------------
wait_for_vault() {
  attempt=1
  while [ "$attempt" -le 30 ]; do
    if vault status >/dev/null 2>&1; then
      log "vault is unsealed and responding at ${VAULT_ADDR}"
      return 0
    fi
    log "waiting for Vault (attempt ${attempt}/30)"
    sleep 2
    attempt=$((attempt + 1))
  done
  die "Vault did not become ready at ${VAULT_ADDR}"
}

# `vault secrets enable` fails if the path is already mounted, which is the
# normal case on a second run — so check before enabling.
ensure_secrets_engine() {
  type="$1"; path="$2"; shift 2
  if vault secrets list -format=json 2>/dev/null | grep -q "\"${path}/\""; then
    log "secrets engine '${path}/' already enabled"
    return 0
  fi
  # "$@" carries any remaining flags (e.g. -version=2) with quoting intact.
  vault secrets enable -path="${path}" "$@" "${type}" >/dev/null
  log "enabled ${type} secrets engine at '${path}/'"
}

ensure_auth_method() {
  type="$1"
  if vault auth list -format=json 2>/dev/null | grep -q "\"${type}/\""; then
    log "auth method '${type}/' already enabled"
    return 0
  fi
  vault auth enable "${type}" >/dev/null
  log "enabled '${type}' auth method"
}

# -----------------------------------------------------------------------------
# 1. Audit device — enabled before anything else, so the provisioning itself
#    lands in the audit trail.
# -----------------------------------------------------------------------------
enable_audit() {
  if vault audit list -format=json 2>/dev/null | grep -q '"file/"'; then
    log "audit device already enabled"
    return 0
  fi
  if vault audit enable file file_path=/vault/logs/audit.log >/dev/null 2>&1; then
    log "audit logging enabled at /vault/logs/audit.log"
  else
    # Non-fatal: a read-only or missing log volume should not stop the lab from
    # coming up, but it must be loud, because silent audit loss is the failure
    # mode that matters.
    warn "could not enable the audit device — /vault/logs may not be writable"
  fi
}

# -----------------------------------------------------------------------------
# 2. Secrets
#
# The path hierarchy mirrors the policy model:
#   secret/apps/*            owned by developers
#   secret/infrastructure/*  owned by platform, readable by developers
#   secret/security/*        owned by security, invisible to developers
#   secret/ci/*              readable by the pipeline AppRole
# -----------------------------------------------------------------------------
seed_secrets() {
  ensure_secrets_engine kv secret "-version=2"

  vault kv put secret/infrastructure/database \
    host="postgres" \
    port="5432" \
    username="${POSTGRES_USER:-lab}" \
    note="Password is a Docker secret; this entry models the non-sensitive DSN parts." >/dev/null

  vault kv put secret/infrastructure/object-storage \
    endpoint="http://minio:9000" \
    region="us-east-1" \
    access_key="${MINIO_ROOT_USER:-labadmin}" >/dev/null

  vault kv put secret/infrastructure/registry \
    url="https://ghcr.io" \
    username="lab-ci" >/dev/null

  vault kv put secret/apps/demo-api \
    jwt_issuer="https://keycloak.lab.localhost/realms/lab" \
    feature_flags="beta-search,new-billing" >/dev/null

  vault kv put secret/ci/deploy \
    environment="lab" \
    artifact_bucket="lab-artifacts" >/dev/null

  vault kv put secret/security/incident-response \
    pager_rotation="security-oncall" \
    escalation_minutes="15" >/dev/null

  log "seeded example secrets under secret/{infrastructure,apps,ci,security}"
}

# -----------------------------------------------------------------------------
# 3. Policies
# -----------------------------------------------------------------------------
load_policies() {
  if [ ! -d "$POLICY_DIR" ]; then
    warn "policy directory ${POLICY_DIR} is missing; skipping policy load"
    return 0
  fi

  found=0
  for policy_file in "$POLICY_DIR"/*.hcl; do
    [ -e "$policy_file" ] || continue
    name="$(basename "$policy_file" .hcl)"
    vault policy write "$name" "$policy_file" >/dev/null
    log "policy '${name}' written"
    found=$((found + 1))
  done

  [ "$found" -eq 0 ] && warn "no .hcl policies found in ${POLICY_DIR}"
  return 0
}

# -----------------------------------------------------------------------------
# 4. Auth methods
#
# AppRole models a machine identity: the role id is public, the secret id is
# short-lived and issued per run. userpass models a human, mapped to the same
# policies the Keycloak realm hands out as roles.
# -----------------------------------------------------------------------------
configure_approle() {
  ensure_auth_method approle

  vault write auth/approle/role/ci-pipeline \
    token_policies="ci-pipeline" \
    token_ttl=20m \
    token_max_ttl=1h \
    secret_id_ttl=10m \
    secret_id_num_uses=1 \
    bind_secret_id=true >/dev/null

  role_id="$(vault read -field=role_id auth/approle/role/ci-pipeline/role-id 2>/dev/null || echo '')"
  log "AppRole 'ci-pipeline' configured (role_id: ${role_id:-unavailable})"
  log "  issue a single-use secret id with:"
  log "  vault write -f auth/approle/role/ci-pipeline/secret-id"
}

configure_userpass() {
  ensure_auth_method userpass

  # Mirrors the Keycloak realm: alice operates the platform, bob builds on it,
  # carol investigates it.
  vault write "auth/userpass/users/alice" \
    password="$DEMO_USER_PASSWORD" token_policies="platform-admin" token_ttl=1h >/dev/null
  vault write "auth/userpass/users/bob" \
    password="$DEMO_USER_PASSWORD" token_policies="developer" token_ttl=1h >/dev/null
  vault write "auth/userpass/users/carol" \
    password="$DEMO_USER_PASSWORD" token_policies="security-analyst" token_ttl=1h >/dev/null

  log "userpass logins ready: alice=platform-admin, bob=developer, carol=security-analyst"
}

# -----------------------------------------------------------------------------
# 5. Transit — encryption as a service
#
# Applications send plaintext and receive ciphertext; the key never leaves
# Vault, so a database dump is worthless on its own.
# -----------------------------------------------------------------------------
configure_transit() {
  ensure_secrets_engine transit transit

  if vault read transit/keys/lab-app >/dev/null 2>&1; then
    log "transit key 'lab-app' already exists"
  else
    vault write -f transit/keys/lab-app >/dev/null
    log "created transit key 'lab-app'"
  fi

  # Allow rotation, forbid export: the key can be replaced but never extracted.
  vault write transit/keys/lab-app/config \
    exportable=false \
    allow_plaintext_backup=false \
    deletion_allowed=false >/dev/null

  log "  try it:  vault write transit/encrypt/lab-app plaintext=\$(echo -n 'hello' | base64)"
}

# -----------------------------------------------------------------------------
main() {
  log "provisioning Vault at ${VAULT_ADDR}"

  wait_for_vault
  enable_audit
  seed_secrets
  load_policies
  configure_approle
  configure_userpass
  configure_transit

  log "done. UI: http://vault.${LAB_DOMAIN:-lab.localhost} (log in with the root token from .env)"
}

main "$@"
