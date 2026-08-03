#!/bin/sh
# =============================================================================
# Gitea provisioning
# =============================================================================
# Creates the first administrator so that Gitea is usable immediately instead
# of presenting its web installer.
#
# Runs in the Gitea image with the data volume mounted, because `gitea admin`
# is a local command: it reads app.ini for the database connection and writes
# to that database directly.
#
# Every failure path here is non-fatal by design. If this script cannot create
# the account, Gitea still comes up and its web installer still works — a
# provisioning hiccup should cost a click, not the whole lab.
# =============================================================================
set -eu

GITEA_APP_INI="${GITEA_APP_INI:-/data/gitea/conf/app.ini}"
GITEA_ADMIN_USER="${GITEA_ADMIN_USER:-labadmin}"
GITEA_ADMIN_PASSWORD="${GITEA_ADMIN_PASSWORD:-gitea-insecure-dev-only}"
GITEA_ADMIN_EMAIL="${GITEA_ADMIN_EMAIL:-labadmin@lab.local}"

log()  { printf '[gitea-init] %s\n' "$*"; }
warn() { printf '[gitea-init] WARNING: %s\n' "$*" >&2; }

# `gitea` needs to know where its configuration and working directory are when
# invoked outside the image's own supervisor.
export GITEA_WORK_DIR="${GITEA_WORK_DIR:-/data/gitea}"

# -----------------------------------------------------------------------------
# app.ini is written by Gitea on its first start. Compose gates this container
# on Gitea being healthy, so the file should exist — but the write and the
# healthcheck are not ordered relative to each other.
# -----------------------------------------------------------------------------
wait_for_config() {
  attempt=1
  while [ "$attempt" -le 40 ]; do
    if [ -f "$GITEA_APP_INI" ]; then
      log "found configuration at ${GITEA_APP_INI}"
      return 0
    fi
    log "waiting for Gitea to write its configuration (attempt ${attempt}/40)"
    sleep 3
    attempt=$((attempt + 1))
  done
  warn "${GITEA_APP_INI} never appeared; skipping admin creation"
  warn "complete the setup in the browser at https://git.${LAB_DOMAIN:-lab.localhost}"
  exit 0
}

admin_exists() {
  gitea admin user list --config "$GITEA_APP_INI" 2>/dev/null \
    | awk 'NR > 1 { print $2 }' \
    | grep -qx "$GITEA_ADMIN_USER"
}

create_admin() {
  if admin_exists; then
    log "administrator '${GITEA_ADMIN_USER}' already exists"
    return 0
  fi

  log "creating administrator '${GITEA_ADMIN_USER}'"

  if gitea admin user create \
       --config "$GITEA_APP_INI" \
       --username "$GITEA_ADMIN_USER" \
       --password "$GITEA_ADMIN_PASSWORD" \
       --email "$GITEA_ADMIN_EMAIL" \
       --admin \
       --must-change-password=false 2>&1; then
    log "administrator ready"
  else
    warn "could not create the administrator account."
    warn "Gitea is still running — create one at https://git.${LAB_DOMAIN:-lab.localhost}"
  fi
}

main() {
  wait_for_config
  create_admin
  log "done. Gitea: https://git.${LAB_DOMAIN:-lab.localhost}"
}

main "$@"
