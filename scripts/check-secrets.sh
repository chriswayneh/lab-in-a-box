#!/usr/bin/env bash
# =============================================================================
# Guard: never commit a real credential
# =============================================================================
#   bash scripts/check-secrets.sh
#
# Run by `make validate`, by CI, and by the pre-commit hook that `make hooks`
# installs.
#
# Why this exists
# ---------------
# secrets/*.txt is tracked on purpose: Compose resolves secret file paths when
# it parses the project, before any container exists, so a fresh clone needs
# known development defaults. `make secrets` writes real credentials to the
# ignored secrets/local/ directory instead. This guard makes sure the tracked
# defaults are never replaced by a real credential.
#
# Exit codes:
#   0  safe to commit
#   1  a real credential is staged, or about to be
# =============================================================================
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

# Every shipped placeholder contains this marker. Anything else in these files
# is, by definition, a generated credential.
PLACEHOLDER_MARKER="insecure-dev-only"
SECRETS_GLOB="${LAB_ROOT}/secrets/*.txt"

PROBLEMS=0

# -----------------------------------------------------------------------------
# 1. The working-tree state of the tracked fallback secrets.
#
# `--staged` restricts the check to what is actually about to be committed,
# which is what the pre-commit hook wants. Without it, the check looks at the
# files on disk, which is what `make validate` wants.
# -----------------------------------------------------------------------------
STAGED_ONLY=0
[[ "${1:-}" == "--staged" ]] && STAGED_ONLY=1

check_secret_files() {
  local path name content

  for path in $SECRETS_GLOB; do
    [[ -e "$path" ]] || continue
    name="secrets/$(basename "$path")"

    if (( STAGED_ONLY )); then
      # Only inspect files that are actually staged for this commit.
      git -C "$LAB_ROOT" diff --cached --name-only 2>/dev/null \
        | grep -qx "$name" || continue
      content="$(git -C "$LAB_ROOT" show ":${name}" 2>/dev/null)"
    else
      content="$(cat "$path")"
    fi

    if [[ "$content" == *"$PLACEHOLDER_MARKER"* ]]; then
      (( STAGED_ONLY )) || log "  ${C_DIM}ok   ${name} (placeholder)${C_RESET}"
    else
      error "${name} contains a generated credential"
      (( PROBLEMS++ )) || true
    fi
  done
}

# -----------------------------------------------------------------------------
# 2. .env must never be tracked at all
# -----------------------------------------------------------------------------
check_env_not_tracked() {
  local tracked
  tracked="$(git -C "$LAB_ROOT" ls-files 2>/dev/null \
             | grep -E '^\.env($|\.)' \
             | grep -v '^\.env\.example$' || true)"

  if [[ -n "$tracked" ]]; then
    error "these credential files are tracked by git:"
    printf '    %s\n' $tracked >&2
    (( PROBLEMS++ )) || true
  fi
}

# -----------------------------------------------------------------------------
main() {
  # Not a git repository — nothing to protect.
  git -C "$LAB_ROOT" rev-parse --git-dir >/dev/null 2>&1 || exit 0

  (( STAGED_ONLY )) || heading "Secret guard"

  check_secret_files
  check_env_not_tracked

  if (( PROBLEMS == 0 )); then
    (( STAGED_ONLY )) || success "no real credentials are committed"
    exit 0
  fi

  log "" >&2
  error "Refusing to let a real credential reach the repository."
  log "" >&2
  log "  Tracked secrets/*.txt must contain only the shipped development defaults." >&2
  log "  Keep real credentials in the git-ignored secrets/local/ directory;" >&2
  log "  `make secrets` creates that directory automatically." >&2
  log "" >&2
  exit 1
}

main "$@"
