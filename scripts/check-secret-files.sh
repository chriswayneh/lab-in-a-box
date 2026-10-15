#!/usr/bin/env bash
# =============================================================================
# Fail early when the lab uses generated secrets (LAB_SECRETS_DIR in .env) but
# a Docker secret file that docker-compose.yml needs is missing. This happens
# to labs created before a new secret was added (for example
# portainer_admin_password.txt). `make secrets` fills in missing files and
# keeps existing ones.
# =============================================================================
set -uo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

ENV_FILE="${LAB_ROOT}/.env"
[[ -f "$ENV_FILE" ]] || exit 0
dir="$(sed -n 's/^LAB_SECRETS_DIR=//p' "$ENV_FILE" | tail -1)"
[[ -n "$dir" ]] || exit 0
[[ "$dir" = /* ]] || dir="${LAB_ROOT}/${dir#./}"

missing=()
while IFS= read -r name; do
  [[ -f "${dir}/${name}" ]] || missing+=("$name")
done < <(sed -n 's#^ *file: \${LAB_SECRETS_DIR:-[^}]*}/##p' "${LAB_ROOT}/docker-compose.yml")

if (( ${#missing[@]} )); then
  error "missing secret file(s) in ${dir}: ${missing[*]}"
  log "  Run ${C_CYAN:-}make secrets${C_RESET:-} to generate them (existing credentials are kept), then retry." >&2
  exit 1
fi
