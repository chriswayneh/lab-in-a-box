#!/usr/bin/env bash
# =============================================================================
# Backup
# =============================================================================
#   make backup
#
# Produces one timestamped directory under backups/ containing:
#
#   postgres.sql.gz     logical dump of every database (pg_dumpall)
#   <volume>.tar.gz     a tar of each named Docker volume
#   manifest.json       what was captured, from which image, and when
#
# PostgreSQL gets a logical dump rather than a volume copy because copying a
# database's files while the server is running produces a backup that restores
# into a corrupt cluster. Everything else is file-level state where a tar is
# both correct and much faster.
#
# The lab keeps running throughout. That is the right trade for a development
# environment; a backup that requires downtime is a backup nobody takes.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_docker

PROJECT="$(project_name)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP_ROOT="${LAB_ROOT}/backups"
BACKUP_DIR="${BACKUP_ROOT}/${TIMESTAMP}"

# Volumes worth capturing. Deliberately excluded:
#   ollama_models       many gigabytes, re-downloadable in one command
#   prometheus_data     time-series data that is stale the moment it is restored
#   loki_data           same
VOLUMES=(
  postgres_data
  redis_data
  keycloak_data
  grafana_data
  gitea_data
  minio_data
  openwebui_data
  portainer_data
  pgadmin_data
  vault_data
)

# Alpine is used as a neutral container to read volumes that belong to other
# containers — it needs no tooling beyond tar, which busybox provides.
HELPER_IMAGE="alpine:3.21"

# -----------------------------------------------------------------------------
backup_postgres() {
  info "dumping PostgreSQL"

  if ! compose ps --status running --services 2>/dev/null | grep -qx postgres; then
    warn "postgres is not running — skipping the database dump"
    return 1
  fi

  local user
  user="$(env_value POSTGRES_USER lab)"

  # pg_dumpall captures roles and grants as well as data, which pg_dump alone
  # would silently omit — restoring without them leaves every service unable
  # to log in.
  if compose exec -T postgres pg_dumpall -U "$user" --clean --if-exists 2>/dev/null \
       | gzip -9 > "${BACKUP_DIR}/postgres.sql.gz"; then
    local size
    size="$(du -h "${BACKUP_DIR}/postgres.sql.gz" 2>/dev/null | cut -f1)"
    success "postgres.sql.gz ${C_DIM}(${size:-unknown})${C_RESET}"
    return 0
  fi

  error "the PostgreSQL dump failed"
  rm -f "${BACKUP_DIR}/postgres.sql.gz"
  return 1
}

# -----------------------------------------------------------------------------
backup_volume() {
  local short_name="$1"
  local volume="${PROJECT}_${short_name}"

  if ! docker volume inspect "$volume" >/dev/null 2>&1; then
    log "  ${C_DIM}skip ${short_name} (no such volume)${C_RESET}"
    return 0
  fi

  # Mounted read-only: a backup job must not be able to modify what it reads.
  if docker run --rm \
       -v "${volume}:/source:ro" \
       -v "${BACKUP_DIR}:/backup" \
       "$HELPER_IMAGE" \
       tar czf "/backup/${short_name}.tar.gz" -C /source . 2>/dev/null; then
    local size
    size="$(du -h "${BACKUP_DIR}/${short_name}.tar.gz" 2>/dev/null | cut -f1)"
    success "$(printf '%-18s' "${short_name}.tar.gz") ${C_DIM}${size:-unknown}${C_RESET}"
  else
    error "failed to archive volume ${volume}"
    rm -f "${BACKUP_DIR}/${short_name}.tar.gz"
    return 1
  fi
}

# -----------------------------------------------------------------------------
# A manifest turns an opaque directory of tarballs into something restorable
# months later, by someone who was not there when it was written.
# -----------------------------------------------------------------------------
write_manifest() {
  # Built with a glob rather than `ls | grep` so that a filename containing a
  # space or a newline cannot corrupt the JSON. The separator is prepended
  # rather than appended so the last entry never gets a trailing comma, which
  # would make the manifest invalid JSON.
  local files="" separator="" path name
  for path in "${BACKUP_DIR}"/*; do
    [[ -f "$path" ]] || continue
    name="$(basename "$path")"
    [[ "$name" == "manifest.json" ]] && continue
    files+="${separator}    \"${name}\""
    separator=$',\n'
  done

  cat > "${BACKUP_DIR}/manifest.json" <<JSON
{
  "created": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "project": "${PROJECT}",
  "domain": "$(lab_domain)",
  "helper_image": "${HELPER_IMAGE}",
  "host": "$(hostname 2>/dev/null || echo unknown)",
  "contents": [
${files}
  ],
  "excluded": [
    "ollama_models  — large and re-downloadable via ollama-init",
    "prometheus_data — time-series data, stale on restore",
    "loki_data       — log data, stale on restore"
  ],
  "restore": "make restore BACKUP=${TIMESTAMP}",
  "note": "Credentials are NOT included. .env and secrets/local/ must be backed up separately, and stored somewhere appropriate for secret material."
}
JSON
  success "manifest.json"
}

# -----------------------------------------------------------------------------
prune_old_backups() {
  local keep="${BACKUP_KEEP:-5}"
  local count
  count="$(find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l | tr -d ' ')"

  (( count <= keep )) && return 0

  info "pruning old backups (keeping the newest ${keep})"
  find "$BACKUP_ROOT" -maxdepth 1 -mindepth 1 -type d 2>/dev/null \
    | sort \
    | head -n "$(( count - keep ))" \
    | while IFS= read -r old; do
        rm -rf "$old"
        log "  ${C_DIM}removed $(basename "$old")${C_RESET}"
      done
}

# -----------------------------------------------------------------------------
main() {
  heading "Backing up ${PROJECT} → backups/${TIMESTAMP}"

  mkdir -p "$BACKUP_DIR"

  local failures=0
  backup_postgres || (( failures++ )) || true

  info "archiving volumes"
  local volume
  for volume in "${VOLUMES[@]}"; do
    backup_volume "$volume" || (( failures++ )) || true
  done

  write_manifest
  prune_old_backups

  local total
  total="$(du -sh "$BACKUP_DIR" 2>/dev/null | cut -f1)"

  log ""
  if (( failures == 0 )); then
    success "backup complete — ${total:-unknown} in backups/${TIMESTAMP}"
  else
    warn "backup finished with ${failures} problem(s) — ${total:-unknown} in backups/${TIMESTAMP}"
  fi

  log ""
  log "  ${C_DIM}Restore with:${C_RESET} make restore BACKUP=${TIMESTAMP}"
  log "  ${C_YELLOW}Credentials are not included.${C_RESET} ${C_DIM}Back up .env and secrets/local/ separately.${C_RESET}"

  (( failures == 0 ))
}

main "$@"
