#!/usr/bin/env bash
# =============================================================================
# Health check
# =============================================================================
#   make health
#
# Reports on three things, in order of usefulness when something is wrong:
#
#   1. Container state      — running? healthy? restarting?
#   2. Provisioning jobs    — did the one-shot init containers succeed?
#   3. HTTP reachability    — does the edge actually route to each service?
#
# Exit codes:
#   0  everything healthy
#   1  at least one service is unhealthy or unreachable
#   2  the lab is not running at all
# =============================================================================
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_docker

DOMAIN="$(lab_domain)"
PROJECT="$(project_name)"
FAILURES=0
WARNINGS=0

# Services that are expected to run and exit. A stopped init job is success,
# not a fault — but a *failed* one means provisioning did not happen.
INIT_JOBS=(keycloak-init vault-init minio-init gitea-init ollama-init)

# subdomain:label — the routes the landing page advertises.
ROUTES=(
  ":Landing page"
  "traefik:Traefik dashboard"
  "keycloak:Keycloak"
  "vault:Vault"
  "grafana:Grafana"
  "prometheus:Prometheus"
  "chat:Open WebUI"
  "ollama:Ollama API"
  "git:Gitea"
  "minio:MinIO console"
  "portainer:Portainer"
  "pgadmin:pgAdmin"
  "adminer:Adminer"
)

# -----------------------------------------------------------------------------
# 1. Container state
# -----------------------------------------------------------------------------
check_containers() {
  heading "Containers"

  local ps_output
  ps_output="$(compose ps --format '{{.Service}}\t{{.State}}\t{{.Health}}' 2>/dev/null)" || true

  if [[ -z "$ps_output" ]]; then
    error "no containers found for project '${PROJECT}'"
    log "  ${C_DIM}Start the lab with: make up${C_RESET}"
    exit 2
  fi

  local service state health
  while IFS=$'\t' read -r service state health; do
    [[ -z "$service" ]] && continue

    # Init jobs are handled separately — skip them here.
    local is_init=0 job
    for job in "${INIT_JOBS[@]}"; do
      [[ "$service" == "$job" ]] && is_init=1
    done
    (( is_init )) && continue

    case "${health:-}" in
      healthy)
        success "$(printf '%-16s' "$service") ${C_DIM}running, healthy${C_RESET}"
        ;;
      starting)
        warn "$(printf '%-16s' "$service") still starting its healthcheck"
        (( WARNINGS++ )) || true
        ;;
      unhealthy)
        error "$(printf '%-16s' "$service") UNHEALTHY"
        log "    ${C_DIM}docker compose logs --tail 50 ${service}${C_RESET}"
        (( FAILURES++ )) || true
        ;;
      *)
        # No healthcheck defined, or Compose reported none.
        if [[ "$state" == "running" ]]; then
          success "$(printf '%-16s' "$service") ${C_DIM}running${C_RESET}"
        else
          error "$(printf '%-16s' "$service") ${state:-unknown}"
          (( FAILURES++ )) || true
        fi
        ;;
    esac
  done <<< "$ps_output"
}

# -----------------------------------------------------------------------------
# 2. Provisioning jobs
#
# These containers exit as soon as their work is done, so `docker compose ps`
# shows them as stopped. What matters is the exit code they stopped with.
# -----------------------------------------------------------------------------
check_init_jobs() {
  heading "Provisioning"

  local job container exit_code status
  for job in "${INIT_JOBS[@]}"; do
    container="${PROJECT}-${job}"

    if ! docker container inspect "$container" >/dev/null 2>&1; then
      log "  ${C_DIM}${job}: not present (optional, or not started yet)${C_RESET}"
      continue
    fi

    status="$(docker container inspect -f '{{.State.Status}}' "$container" 2>/dev/null)"
    exit_code="$(docker container inspect -f '{{.State.ExitCode}}' "$container" 2>/dev/null)"

    if [[ "$status" == "running" ]]; then
      info "$(printf '%-16s' "$job") still working"
      log "    ${C_DIM}docker compose logs -f ${job}${C_RESET}"
      (( WARNINGS++ )) || true
    elif [[ "$exit_code" == "0" ]]; then
      success "$(printf '%-16s' "$job") ${C_DIM}completed${C_RESET}"
    else
      error "$(printf '%-16s' "$job") FAILED (exit ${exit_code})"
      log "    ${C_DIM}docker compose logs ${job}${C_RESET}"
      (( FAILURES++ )) || true
    fi
  done
}

# -----------------------------------------------------------------------------
# 3. HTTP reachability through Traefik
#
# `--insecure` is correct here: the lab's default certificate is self-signed,
# and this check is asking "does the edge route this?", not "is this
# certificate trustworthy?".
# -----------------------------------------------------------------------------
check_routes() {
  heading "HTTP routes"

  if ! command -v curl >/dev/null 2>&1; then
    warn "curl not found — skipping route checks"
    return 0
  fi

  local entry subdomain label host code
  for entry in "${ROUTES[@]}"; do
    subdomain="${entry%%:*}"
    label="${entry#*:}"
    host="${subdomain:+${subdomain}.}${DOMAIN}"

    code="$(curl -s -o /dev/null -w '%{http_code}' \
              --insecure --max-time 8 --location \
              "https://${host}/" 2>/dev/null || echo 000)"

    case "$code" in
      # 2xx and 3xx are healthy. So is 401/403 — a service that demands
      # credentials is a service that is up and enforcing them.
      2??|3??|401|403)
        success "$(printf '%-16s' "$label") ${C_DIM}${host} → ${code}${C_RESET}"
        ;;
      000)
        error "$(printf '%-16s' "$label") ${host} unreachable"
        (( FAILURES++ )) || true
        ;;
      404|502|503|504)
        warn "$(printf '%-16s' "$label") ${host} → ${code} (starting, or no route)"
        (( WARNINGS++ )) || true
        ;;
      *)
        warn "$(printf '%-16s' "$label") ${host} → ${code}"
        (( WARNINGS++ )) || true
        ;;
    esac
  done
}

# -----------------------------------------------------------------------------
summary() {
  heading "Summary"

  if (( FAILURES == 0 && WARNINGS == 0 )); then
    success "the lab is fully healthy"
    log ""
    log "  Open ${C_CYAN}https://${DOMAIN}${C_RESET} to get started."
    return 0
  fi

  if (( FAILURES == 0 )); then
    warn "${WARNINGS} warning(s), no failures"
    log ""
    log "  ${C_DIM}Warnings are normal for a few minutes after 'make up' — model"
    log "  downloads and database migrations both take a while.${C_RESET}"
    return 0
  fi

  error "${FAILURES} failure(s), ${WARNINGS} warning(s)"
  log ""
  log "  ${C_DIM}Start with:${C_RESET} make logs"
  return 1
}

main() {
  log "${C_BOLD}Lab-in-a-Box health check${C_RESET} ${C_DIM}(project: ${PROJECT}, domain: ${DOMAIN})${C_RESET}"

  check_containers
  check_init_jobs
  check_routes
  summary
}

main "$@"
