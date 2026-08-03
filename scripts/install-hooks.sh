#!/usr/bin/env bash
# =============================================================================
# Install git hooks
# =============================================================================
#   make hooks
#
# Installs a pre-commit hook that refuses to commit a real credential.
#
# This is opt-in rather than automatic. Hooks execute code on someone's machine
# as a side effect of a normal git command, and a repository that installs them
# behind your back is a repository you should not have cloned.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

HOOK_DIR="${LAB_ROOT}/.git/hooks"
HOOK_PATH="${HOOK_DIR}/pre-commit"

git -C "$LAB_ROOT" rev-parse --git-dir >/dev/null 2>&1 \
  || die "not a git repository — nothing to install into"

mkdir -p "$HOOK_DIR"

if [[ -f "$HOOK_PATH" ]] && ! grep -q "lab-in-a-box" "$HOOK_PATH" 2>/dev/null; then
  warn "a pre-commit hook already exists and was not written by this project."
  log "  ${C_DIM}Inspect ${HOOK_PATH} and merge by hand if you want both.${C_RESET}"
  exit 1
fi

cat > "$HOOK_PATH" <<'HOOK'
#!/usr/bin/env bash
# lab-in-a-box pre-commit hook — installed by `make hooks`.
#
# Blocks a commit that replaces the tracked development defaults in secrets/*.txt
# with a generated credential. Real credentials belong in secrets/local/.
#
# Bypass deliberately with:  git commit --no-verify
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
exec bash "${repo_root}/scripts/check-secrets.sh" --staged
HOOK

chmod +x "$HOOK_PATH" 2>/dev/null || true

success "pre-commit hook installed at .git/hooks/pre-commit"
log ""
log "  It blocks any commit that would publish a generated credential."
log "  ${C_DIM}Bypass for one commit with: git commit --no-verify${C_RESET}"
log "  ${C_DIM}Remove with: rm .git/hooks/pre-commit${C_RESET}"
