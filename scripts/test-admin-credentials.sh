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

REQUIRED_NAMES=()
while IFS= read -r name; do REQUIRED_NAMES+=("$name"); done < <(
  sed -n '/^REQUIRED=(/,/^)/p' "$HERE/check-admin-credentials.sh" | sed '1d;$d' | tr -d ' ')

# Run the checker with no required variable inherited from the caller's shell.
check() {
  local -a unset_args=()
  local n
  for n in "${REQUIRED_NAMES[@]}"; do unset_args+=(-u "$n"); done
  env "${unset_args[@]}" "$@" bash "$FIX/scripts/check-admin-credentials.sh"
}

# Write a .env with a valid value for every required name except those given.
good_env_except() {
  local n skip
  : > "$FIX/.env"
  for n in "${REQUIRED_NAMES[@]}"; do
    skip=0
    for s in "$@"; do [[ "$n" == "$s" ]] && skip=1; done
    (( skip )) || printf '%s=Fixture-Good-%s-1\n' "$n" "${#n}" >> "$FIX/.env"
  done
}

test_secrets_init_lists_every_required_name() {
  local t=${FUNCNAME[0]} listed n missing=""
  listed="$(sed -n '/REQUIRED_SECRETS: >-/,/^      [A-Z]/p' "$ROOT/compose/01-core.yml")"
  for n in "${REQUIRED_NAMES[@]}"; do
    grep -qE "^        ${n}\$" <<<"$listed" || missing="$missing $n"
    grep -qE "^      ${n}: \\\$\\{${n}:-\\}\$" "$ROOT/compose/01-core.yml" || missing="$missing $n(env)"
  done
  if [[ -n "$missing" ]]; then bad "$t" "secrets-init does not check:$missing"; else ok "$t"; fi
}

test_every_consumer_waits_for_secrets_init() {
  # Any service whose definition interpolates a required name must depend on
  # secrets-init, so a direct `docker compose up` fails closed.
  local t=${FUNCNAME[0]}
  local missing
  missing="$(python3 - "$ROOT" "${REQUIRED_NAMES[@]}" <<'PY'
import glob, re, sys
root, names = sys.argv[1], sys.argv[2:]
missing = []
for path in sorted(glob.glob(root + "/compose/*.yml")):
    blocks = re.split(r"^  (?=[a-z0-9-]+:\s*$)", open(path).read(), flags=re.M)
    for block in blocks[1:]:
        svc = block.split(":", 1)[0]
        if svc == "secrets-init":
            continue
        uses = any(re.search(r"\$\$?\{" + n + r"[:}]", block) for n in names)
        if uses and not re.search(r"^      secrets-init:\n        condition: service_completed_successfully", block, re.M):
            missing.append(svc)
print(" ".join(missing))
PY
)"
  if [[ -n "$missing" ]]; then bad "$t" "services not gated on secrets-init: $missing"; else ok "$t"; fi
}

test_quoted_empty_with_comment_is_rejected() {
  local t=${FUNCNAME[0]}
  new_fixture
  good_env_except REDIS_PASSWORD
  printf 'REDIS_PASSWORD="" # set me\n' >> "$FIX/.env"
  if check >/dev/null 2>&1; then bad "$t" 'KEY="" # comment was accepted'; else ok "$t"; fi
  cleanup
}

test_interpolated_value_is_rejected() {
  local t=${FUNCNAME[0]} form
  for form in '${UNSET_LAB_VAR}' '"${UNSET_LAB_VAR}"' '$UNSET_LAB_VAR'; do
    new_fixture
    good_env_except PGADMIN_PASSWORD
    printf 'PGADMIN_PASSWORD=%s\n' "$form" >> "$FIX/.env"
    if check >/dev/null 2>&1; then bad "$t" "PGADMIN_PASSWORD=$form was accepted"
    else ok "$t ($form)"; fi
    cleanup
  done
}

test_unquoted_value_with_comment_is_accepted() {
  local t=${FUNCNAME[0]}
  new_fixture
  good_env_except REDIS_PASSWORD
  printf 'REDIS_PASSWORD=Fixture-Real-Value-77  # generated\n' >> "$FIX/.env"
  if check >/dev/null 2>&1; then ok "$t"; else bad "$t" "a valid value with a trailing comment was rejected"; fi
  cleanup
}

test_make_targets_call_guard_before_compose_up() {
  local t=${FUNCNAME[0]} target out guard_line up_line
  command -v make >/dev/null 2>&1 || { bad "$t" "make is not installed"; return; }
  for target in up update; do
    out="$(cd "$ROOT" && make -n "$target" 2>/dev/null)" || true
    guard_line="$(grep -n 'check-admin-credentials.sh' <<<"$out" | head -n1 | cut -d: -f1)"
    up_line="$(grep -nE 'compose.* up -d' <<<"$out" | head -n1 | cut -d: -f1)"
    if [[ -z "$up_line" ]]; then bad "$t" "make -n $target shows no compose up"
    elif [[ -z "$guard_line" ]]; then bad "$t" "make $target does not run check-admin-credentials.sh"
    elif (( guard_line > up_line )); then bad "$t" "make $target runs the guard after compose up"
    else ok "$t ($target)"; fi
  done
}

test_restore_calls_guard_before_stopping_lab() {
  local t=${FUNCNAME[0]} guard down
  guard="$(grep -n 'check-admin-credentials.sh' "$HERE/restore.sh" | head -n1 | cut -d: -f1)"
  down="$(grep -n 'compose down' "$HERE/restore.sh" | head -n1 | cut -d: -f1)"
  if [[ -z "$guard" ]]; then bad "$t" "restore.sh does not run the guard"
  elif (( guard > down )); then bad "$t" "restore.sh stops the lab before running the guard"
  else ok "$t"; fi
}

test_compose_has_no_fallback_credentials() {
  local t=${FUNCNAME[0]}
  local n found=""
  for n in "${REQUIRED_NAMES[@]}"; do
    grep -qE "\\\$\\{${n}:-[^}]+\\}" "$ROOT"/compose/*.yml && found="$found $n"
  done
  if [[ -n "$found" ]]; then bad "$t" "Compose still has a built-in fallback for:$found"
  else ok "$t"; fi
}

test_missing_or_empty_credentials_are_rejected() {
  local t=${FUNCNAME[0]} key out
  for key in "${REQUIRED_NAMES[@]}"; do
    for mode in missing empty; do
      new_fixture
      good_env_except "$key"
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
    good_env_except KEYCLOAK_ADMIN_PASSWORD
    printf 'KEYCLOAK_ADMIN_PASSWORD=%s\n' "$v" >> "$FIX/.env"
    if check >/dev/null 2>&1; then bad "$t" "a placeholder/default was accepted"
    else ok "$t (${v%%_*})"; fi
    cleanup
  done
}

test_shell_override_wins_over_env_file() {
  local t=${FUNCNAME[0]}
  new_fixture
  good_env_except
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
    if ! check >/dev/null 2>&1; then bad "$t" "not every required credential was generated"
    elif [[ -z "$kc" || -z "$vt" ]]; then bad "$t" "a credential was not generated"
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
  good_env_except VAULT_DEV_ROOT_TOKEN
  # Setup may append LAB_SECRETS_DIR; it must not touch credential lines.
  creds() { grep -E '^(KEYCLOAK_ADMIN_PASSWORD|VAULT_DEV_ROOT_TOKEN)=' "$FIX/.env" | sha256sum | cut -d' ' -f1; }
  local before; before="$(creds)"
  if (cd "$FIX" && env -u VAULT_DEV_ROOT_TOKEN bash scripts/generate-secrets.sh) >/dev/null 2>&1; then
    bad "$t" "setup passed with VAULT_DEV_ROOT_TOKEN missing"
  elif [[ "$(creds)" != "$before" ]]; then
    bad "$t" "setup changed or generated admin credentials in an existing .env"
  else ok "$t"; fi
  cleanup
}

test_failures_do_not_disclose_credentials() {
  local t=${FUNCNAME[0]} out
  new_fixture
  good_env_except KEYCLOAK_ADMIN_PASSWORD VAULT_DEV_ROOT_TOKEN
  printf 'KEYCLOAK_ADMIN_PASSWORD=Fixture-Leak-Canary-1234\nVAULT_DEV_ROOT_TOKEN=\n' >> "$FIX/.env"
  out="$(check 2>&1)"
  if [[ "$out" == *Fixture-Leak-Canary-1234* ]]; then bad "$t" "a credential value was printed"
  else ok "$t"; fi
  cleanup
}

test_compose_has_no_fallback_credentials
test_secrets_init_lists_every_required_name
test_every_consumer_waits_for_secrets_init
test_quoted_empty_with_comment_is_rejected
test_interpolated_value_is_rejected
test_unquoted_value_with_comment_is_accepted
test_make_targets_call_guard_before_compose_up
test_restore_calls_guard_before_stopping_lab
test_missing_or_empty_credentials_are_rejected
test_placeholders_and_old_defaults_are_rejected
test_shell_override_wins_over_env_file
test_fresh_setup_generates_distinct_credentials
test_existing_credentials_are_preserved
test_invalid_existing_env_fails_without_rotation
test_failures_do_not_disclose_credentials

printf '\n%d passed, %d failed\n' "$PASSES" "$FAILS"
(( FAILS == 0 ))
