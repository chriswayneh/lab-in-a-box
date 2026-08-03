#!/usr/bin/env bash
# =============================================================================
# Documentation generation
# =============================================================================
#   make docs
#
# Derives two documents from the compose project itself:
#
#   docs/SERVICES.md                   the service catalogue
#   architecture/dependency-graph.mmd  a Mermaid startup-order graph
#
# Both are generated rather than written by hand, because a hand-written
# service table is wrong the first time someone adds a container and nobody
# notices for six months. If these files disagree with the stack, the stack
# wins — regenerate and commit.
#
# CI runs this and fails if the result differs from what is committed.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

require_docker
require_cmd python3 "Install Python 3: https://www.python.org/downloads/"

SERVICES_DOC="${LAB_ROOT}/docs/SERVICES.md"
GRAPH_FILE="${LAB_ROOT}/architecture/dependency-graph.mmd"

main() {
  heading "Generating documentation"

  mkdir -p "$(dirname "$SERVICES_DOC")" "$(dirname "$GRAPH_FILE")"

  # Resolve with every profile enabled, so optional services are documented
  # too — they are part of the lab even when they are not running.
  local config_json
  config_json="$(docker compose -f "${LAB_ROOT}/docker-compose.yml" \
                   --profile qdrant --profile watchtower \
                   config --format json 2>/dev/null)" \
    || die "could not resolve the compose project"

  printf '%s' "$config_json" | python3 "${LAB_ROOT}/scripts/lib/render_docs.py" \
    --services-out "$SERVICES_DOC" \
    --graph-out "$GRAPH_FILE" \
    --domain "$(lab_domain)"

  success "docs/SERVICES.md"
  success "architecture/dependency-graph.mmd"

  log ""
  log "  ${C_DIM}The Mermaid graph renders directly on GitHub — see docs/architecture.md.${C_RESET}"
}

main "$@"
