#!/usr/bin/env bash
# =============================================================================
# Restore
# =============================================================================
#   make restore                    restore the most recent backup
#   make restore BACKUP=20260803-141500
#
# This is a destructive operation: it replaces the contents of live volumes and
# drops and recreates databases. It stops the stack first, asks for
# confirmation, and refuses to run non-interactively unless ASSUME_YES=1.
#
# Restoring credentials is deliberately out of scope. A restored Keycloak
# expects the password that was in .env when the backup was taken — if you have
# regenerated secrets since, restore .env and secrets/local/ from wherever you keep
# them alongside this.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_docker

PROJECT="$(project_name)"
BACKUP_ROOT="${LAB_ROOT}/backups"
HELPER_IMAGE="alpine:3.21"

# -----------------------------------------------------------------------------
resolve_backup() {
  local requested="${BACKUP:-}"

  if [[ -z "$requested" ]]; then
    requested="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d 2>/dev/null \
                 | sort | tail -n1 | xargs -r basename)"
    [[ -n "$requested" ]] || die "no backups found in backups/. Run 'make backup' first."
    info "no BACKUP specified — using the most recent: ${requested}"
  fi

  BACKUP_DIR="${BACKUP_ROOT}/${requested}"
  BACKUP_NAME="$requested"

  [[ -d "$BACKUP_DIR" ]] || die "backup '${requested}' not found in backups/"
}

list_contents() {
  heading "Backup ${BACKUP_NAME}"

  if [[ -f "${BACKUP_DIR}/manifest.json" ]]; then
    local created
    created="$(grep -o '"created": "[^"]*"' "${BACKUP_DIR}/manifest.json" | cut -d'"' -f4)"
    log "${C_DIM}Created: ${created:-unknown}${C_RESET}"
  else
    warn "no manifest.json — this backup was not produced by 'make backup'"
  fi

  log ""
  local file size
  while IFS= read -r file; do
    [[ "$(basename "$file")" == "manifest.json" ]] && continue
    size="$(du -h "$file" 2>/dev/null | cut -f1)"
    printf '  %-24s %s\n' "$(basename "$file")" "${C_DIM}${size:-?}${C_RESET}"
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f | sort)
}

# -----------------------------------------------------------------------------
restore_volume() {
  local short_name="$1"
  local archive="${BACKUP_DIR}/${short_name}.tar.gz"
  local volume="${PROJECT}_${short_name}"

  [[ -f "$archive" ]] || return 0

  # Create the volume if the lab has never been started on this machine.
  docker volume create "$volume" >/dev/null 2>&1 || true

  # The contents are cleared before extracting. Extracting over a populated
  # volume would merge two states and leave files from neither backup.
  if docker run --rm \
       -v "${volume}:/target" \
       -v "${BACKUP_DIR}:/backup:ro" \
       "$HELPER_IMAGE" \
       sh -c "rm -rf /target/* /target/..?* /target/.[!.]* 2>/dev/null; tar xzf /backup/${short_name}.tar.gz -C /target" 2>/dev/null; then
    success "$(printf '%-18s' "$short_name") restored"
  else
    error "failed to restore volume ${volume}"
    return 1
  fi
}

# -----------------------------------------------------------------------------
# Drop only the two statements pg_dumpall writes for the restoring superuser.
strip_bootstrap_role() {
  local role="$1"
  # Exact whole-line string comparison: no regex, so a role name cannot
  # widen the match. validate_role_name has already restricted the name.
  awk -v drop1="DROP ROLE IF EXISTS ${role};" -v drop2="CREATE ROLE ${role};" \
    '$0 != drop1 && $0 != drop2'
}

# PostgreSQL identifiers pg_dumpall writes unquoted. Anything else is refused
# rather than guessed at.
validate_role_name() {
  [[ "$1" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
    || die "POSTGRES_USER '$1' is not a plain identifier ([A-Za-z_][A-Za-z0-9_]*); refusing to restore"
}

# -----------------------------------------------------------------------------
restore_postgres() {
  local dump="${BACKUP_DIR}/postgres.sql.gz"
  [[ -f "$dump" ]] || { error "no postgres.sql.gz in this backup"; return 1; }

  info "starting PostgreSQL to receive the dump"
  if ! compose up -d postgres >/dev/null 2>&1; then
    error "could not start PostgreSQL for the restore"
    return 1
  fi

  # Wait for readiness rather than sleeping a fixed interval, which is either
  # too short on a cold start or wasteful on a warm one.
  local user attempt=1
  user="$(env_value POSTGRES_USER lab)"
  validate_role_name "$user"
  until compose exec -T postgres pg_isready -U "$user" >/dev/null 2>&1; do
    (( attempt > 30 )) && { error "PostgreSQL did not become ready"; return 1; }
    sleep 2
    (( attempt++ ))
  done

  info "loading the dump"
  # The dump was written with --clean --if-exists, so it drops and recreates
  # each object itself. psql runs with ON_ERROR_STOP, so the first SQL error
  # stops the restore and fails it, and pipefail (set above) also fails it on
  # a corrupt archive.
  #
  # pg_dumpall always emits `DROP ROLE IF EXISTS <us>;` and `CREATE ROLE <us>;`
  # for the superuser running the restore. Postgres refuses both (it cannot
  # drop or recreate the role it is connected as), so exactly those two lines
  # are removed before loading. The superuser's ALTER ROLE, and every other
  # statement, still runs under ON_ERROR_STOP. SQL output stays suppressed
  # because a dump contains credential-bearing statements.
  if gunzip -c "$dump" \
       | strip_bootstrap_role "$user" \
       | compose exec -T postgres psql -X -q -v ON_ERROR_STOP=1 -U "$user" -d postgres \
           >/dev/null 2>&1; then
    success "PostgreSQL restored"
  else
    error "PostgreSQL restore failed — the databases may be only partly restored"
    return 1
  fi
}

# -----------------------------------------------------------------------------
main() {
  resolve_backup
  list_contents

  # Refuse before anything is stopped or overwritten. A backup without a SQL
  # dump (for example an old one that only has a postgres_data file copy)
  # would restore every other service and leave PostgreSQL behind.
  if [[ ! -s "${BACKUP_DIR}/postgres.sql.gz" ]]; then
    error "backup ${BACKUP_NAME} has no PostgreSQL dump (postgres.sql.gz) — refusing to restore"
    log "  ${C_DIM}Nothing was changed. Pick a backup made by 'make backup' that includes postgres.sql.gz.${C_RESET}"
    exit 1
  fi

  # The lab is restarted at the end, so refuse now, while it is still
  # running, if .env is missing a required credential.
  bash "${LAB_SCRIPT_DIR}/check-admin-credentials.sh" \
    || die "fix the credentials above first — nothing was changed"
  validate_role_name "$(env_value POSTGRES_USER lab)"

  log ""
  warn "This will STOP the lab and OVERWRITE current data."
  warn "Anything created since ${BACKUP_NAME} will be lost."
  log ""

  confirm "Restore backup ${BACKUP_NAME}?" || { log "Cancelled — nothing was changed."; exit 0; }

  heading "Restoring"

  info "stopping the lab"
  compose down >/dev/null 2>&1 || true

  local failures=0 volume
  for archive in "${BACKUP_DIR}"/*.tar.gz; do
    [[ -e "$archive" ]] || continue
    volume="$(basename "$archive" .tar.gz)"
    restore_volume "$volume" || (( failures++ )) || true
  done

  restore_postgres || (( failures++ )) || true

  info "bringing the lab back up"
  if ! compose up -d >/dev/null 2>&1; then
    error "the lab did not start again after the restore — check with: make ps"
    (( failures++ )) || true
  fi

  log ""
  if (( failures == 0 )); then
    success "restore complete from ${BACKUP_NAME}"
  else
    error "restore FAILED with ${failures} problem(s) — do not trust the restored data until you have checked it"
  fi

  log ""
  log "  ${C_DIM}Services need a minute to reconnect. Check with:${C_RESET} make health"
  log "  ${C_YELLOW}If credentials have been regenerated since this backup,${C_RESET}"
  log "  ${C_YELLOW}restore the matching .env and secrets/local/ too.${C_RESET}"

  (( failures == 0 ))
}

main "$@"
