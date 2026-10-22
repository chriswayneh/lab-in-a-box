#!/usr/bin/env bash
# =============================================================================
# Tests for check-admin-credentials.sh and the generated-credential flow
# =============================================================================
#   bash scripts/test-admin-credentials.sh
#
# Works on throwaway copies of the repository; never touches your .env and
# never prints a credential value.
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
FAILS=0
PASSES=0
ok()  { printf 'ok   %s\n' "$1"; (( PASSES++ )) || true; }
bad() { printf 'FAIL %s: %s\n' "$1" "$2"; (( FAILS++ )) || true; }

new_fixture() {
  FIX="$(mktemp -d "${TMPDIR:-/tmp}/lab-cred-test.XXXXXXXX")"
  mkdir -p "$FIX/scripts/lib" "$FIX/secrets"
  cp "$HERE/check-admin-credentials.sh" "$HERE/generate-secrets.sh" "$FIX/scripts/"
  cp "$HERE/lib/common.sh" "$FIX/scripts/lib/"
  cp "$ROOT/.env.example" "$FIX/.env.example"
}
cleanup() { case "${FIX:-}" in */lab-cred-test.*) rm -rf -- "$FIX" ;; esac; }

# Run the checker with neither variable inherited from the caller's shell.
check() { env -u KEYCLOAK_ADMIN_PASSWORD -u VAULT_DEV_ROOT_TOKEN "$@" bash "$FIX/scripts/check-admin-credentials.sh"; }

test_compose_has_no_fallback_credentials() {
  local t=${FUNCNAME[0]}
  if grep -nE '\$\{(KEYCLOAK_ADMIN_PASSWORD|VAULT_DEV_ROOT_TOKEN):-[^}]+\}' "$ROOT"/compose/*.yml >/dev/null; then
    bad "$t" "a Compose file still has a built-in fallback for an admin credential"
  else ok "$t"; fi
}

test_missing_or_empty_credentials_are_rejected() {
  local t=${FUNCNAME[0]} key other out
  for key in KEYCLOAK_ADMIN_PASSWORD VAULT_DEV_ROOT_TOKEN; do
    [[ $key == KEYCLOAK_ADMIN_PASSWORD ]] && other=VAULT_DEV_ROOT_TOKEN || other=KEYCLOAK_ADMIN_PASSWORD
    for mode in missing empty; do
      new_fixture
      printf '%s=fixture-good-value-1\n' "$other" > "$FIX/.env"
      [[ $mode == empty ]] && printf '%s=\n' "$key" >> "$FIX/.env"
      if out="$(check 2>&1)"; then bad "$t" "$key $mode was accepted"
      elif [[ "$out" != *"$key"* ]]; then bad "$t" "error for $key $mode does not name the variable"
      else ok "$t ($key $mode)"; fi
      cleanup
    done
  done
}

test_placeholders_and_old_defaults_are_rejected() {
  local t=${FUNCNAME[0]} v
  for v in CHANGEME_KEYCLOAK_ADMIN_PASSWORD "admin-insecure-dev-only"; do
    new_fixture
    printf 'KEYCLOAK_ADMIN_PASSWORD=%s\nVAULT_DEV_ROOT_TOKEN=fixture-good-value-2\n' "$v" > "$FIX/.env"
    if check >/dev/null 2>&1; then bad "$t" "a placeholder/default was accepted"
    else ok "$t (${v%%_*})"; fi
    cleanup
  done
}

test_shell_override_wins_over_env_file() {
  local t=${FUNCNAME[0]}
  new_fixture
  printf 'KEYCLOAK_ADMIN_PASSWORD=fixture-good-value-3\nVAULT_DEV_ROOT_TOKEN=fixture-good-value-4\n' > "$FIX/.env"
  if check KEYCLOAK_ADMIN_PASSWORD= >/dev/null 2>&1; then bad "$t" "an empty shell override was accepted"
  elif ! check >/dev/null 2>&1; then bad "$t" "valid .env values were rejected"
  else ok "$t"; fi
  cleanup
}

test_fresh_setup_generates_distinct_credentials() {
  local t=${FUNCNAME[0]}
  new_fixture
  if ! (cd "$FIX" && env -u KEYCLOAK_ADMIN_PASSWORD -u VAULT_DEV_ROOT_TOKEN bash scripts/generate-secrets.sh) >/dev/null 2>&1; then
    bad "$t" "generate-secrets.sh failed on a fresh fixture"
  else
    local kc vt
    kc="$(grep -E '^KEYCLOAK_ADMIN_PASSWORD=' "$FIX/.env" | cut -d= -f2-)"
    vt="$(grep -E '^VAULT_DEV_ROOT_TOKEN=' "$FIX/.env" | cut -d= -f2-)"
    if [[ -z "$kc" || -z "$vt" ]]; then bad "$t" "a credential was not generated"
    elif [[ "$kc" == "$vt" ]]; then bad "$t" "both credentials got the same value"
    elif ! check >/dev/null 2>&1; then bad "$t" "checker rejected generated values"
    else ok "$t"; fi
  fi
  cleanup
}

test_existing_credentials_are_preserved() {
  local t=${FUNCNAME[0]}
  new_fixture
  (cd "$FIX" && bash scripts/generate-secrets.sh) >/dev/null 2>&1
  local before after
  before="$(sha256sum "$FIX/.env" | cut -d' ' -f1)"
  (cd "$FIX" && bash scripts/generate-secrets.sh) >/dev/null 2>&1
  after="$(sha256sum "$FIX/.env" | cut -d' ' -f1)"
  if [[ "$before" == "$after" ]]; then ok "$t"; else bad "$t" ".env changed on a second run"; fi
  cleanup
}

test_invalid_existing_env_fails_without_rotation() {
  local t=${FUNCNAME[0]}
  new_fixture
  printf 'KEYCLOAK_ADMIN_PASSWORD=fixture-good-value-5\n' > "$FIX/.env"
  # Setup may append LAB_SECRETS_DIR; it must not touch credential lines.
  creds() { grep -E '^(KEYCLOAK_ADMIN_PASSWORD|VAULT_DEV_ROOT_TOKEN)=' "$FIX/.env" | sha256sum | cut -d' ' -f1; }
  local before; before="$(creds)"
  if (cd "$FIX" && env -u KEYCLOAK_ADMIN_PASSWORD -u VAULT_DEV_ROOT_TOKEN bash scripts/generate-secrets.sh) >/dev/null 2>&1; then
    bad "$t" "setup passed with VAULT_DEV_ROOT_TOKEN missing"
  elif [[ "$(creds)" != "$before" ]]; then
    bad "$t" "setup changed or generated admin credentials in an existing .env"
  else ok "$t"; fi
  cleanup
}

test_failures_do_not_disclose_credentials() {
  local t=${FUNCNAME[0]} out
  new_fixture
  printf 'KEYCLOAK_ADMIN_PASSWORD=Fixture-Leak-Canary-1234\nVAULT_DEV_ROOT_TOKEN=\n' > "$FIX/.env"
  out="$(check 2>&1)"
  if [[ "$out" == *Fixture-Leak-Canary-1234* ]]; then bad "$t" "a credential value was printed"
  else ok "$t"; fi
  cleanup
}

test_compose_has_no_fallback_credentials
test_missing_or_empty_credentials_are_rejected
test_placeholders_and_old_defaults_are_rejected
test_shell_override_wins_over_env_file
test_fresh_setup_generates_distinct_credentials
test_existing_credentials_are_preserved
test_invalid_existing_env_fails_without_rotation
test_failures_do_not_disclose_credentials

printf '\n%d passed, %d failed\n' "$PASSES" "$FAILS"
(( FAILS == 0 ))
