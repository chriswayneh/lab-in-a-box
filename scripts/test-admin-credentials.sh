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
  good_secret_files
}

# Generated-looking file secrets in secrets/local, referenced from .env.
good_secret_files() {
  local f
  mkdir -p "$FIX/secrets/local"
  for f in postgres_password minio_root_password grafana_admin_password portainer_admin_password; do
    printf 'Fixture-File-%s' "${#f}" > "$FIX/secrets/local/$f.txt"
  done
  printf 'LAB_SECRETS_DIR=./secrets/local\n' >> "$FIX/.env"
}

test_whitespace_only_value_is_rejected() {
  local t=${FUNCNAME[0]}
  new_fixture
  good_env_except OPENWEBUI_SECRET_KEY
  printf 'OPENWEBUI_SECRET_KEY="  \t "\n' >> "$FIX/.env"
  if check >/dev/null 2>&1; then bad "$t" "a whitespace-only value was accepted"; else ok "$t"; fi
  cleanup
}

test_old_demo_default_is_rejected() {
  local t=${FUNCNAME[0]}
  new_fixture
  good_env_except DEMO_USER_PASSWORD
  printf "DEMO_USER_PASSWORD='%s'\n" "LabDemo""Pass1!" >> "$FIX/.env"
  if check >/dev/null 2>&1; then bad "$t" "the old published demo password was accepted"; else ok "$t"; fi
  cleanup
}

test_placeholder_secret_files_are_rejected() {
  local t=${FUNCNAME[0]} f mode
  for f in postgres_password minio_root_password grafana_admin_password portainer_admin_password; do
    for mode in placeholder empty missing; do
      new_fixture
      good_env_except
      case $mode in
        placeholder) printf 'changeme-%s-insecure-dev-only' "$f" > "$FIX/secrets/local/$f.txt" ;;
        empty) printf ' \n' > "$FIX/secrets/local/$f.txt" ;;
        missing) rm -f "$FIX/secrets/local/$f.txt" ;;
      esac
      if check >/dev/null 2>&1; then bad "$t" "$f $mode was accepted"
      else ok "$t ($f $mode)"; fi
      cleanup
    done
  done
  # Without LAB_SECRETS_DIR the tracked placeholders in ./secrets are used: refuse.
  new_fixture
  good_env_except
  sed -i '/^LAB_SECRETS_DIR=/d' "$FIX/.env"
  cp "$ROOT"/secrets/*.txt "$FIX/secrets/"
  if check >/dev/null 2>&1; then bad "$t" "the tracked placeholder files were accepted"
  else ok "$t (tracked placeholders)"; fi
  cleanup
}

test_secret_file_consumers_wait_for_secrets_init() {
  local t=${FUNCNAME[0]} missing
  missing="$(python3 - "$ROOT" <<'PY'
import glob, re, sys
root = sys.argv[1]
names = ["postgres_password", "minio_root_password", "grafana_admin_password", "portainer_admin_password"]
missing = []
for path in sorted(glob.glob(root + "/compose/*.yml")):
    for block in re.split(r"^  (?=[a-z0-9-]+:\s*$)", open(path).read(), flags=re.M)[1:]:
        svc = block.split(":", 1)[0]
        if svc == "secrets-init":
            continue
        m = re.search(r"^    secrets:\n((?:      - .*\n)+)", block, re.M)
        if m and any(n in m.group(1) for n in names) and not re.search(
                r"^      secrets-init:\n        condition: service_completed_successfully", block, re.M):
            missing.append(svc)
print(" ".join(missing))
PY
)"
  if [[ -n "$missing" ]]; then bad "$t" "secret-file consumers not gated: $missing"; else ok "$t"; fi
}

SECRET_FILE_NAMES="grafana_admin_password minio_root_password portainer_admin_password postgres_password"

# Print one field of the rendered secrets-init service: mounts | files | command.
secrets_init_field() {
  (cd "$ROOT" && docker compose -f docker-compose.yml config --format json 2>/dev/null) | python3 -c '
import json, sys
svc = json.load(sys.stdin)["services"]["secrets-init"]
field = sys.argv[1]
if field == "mounts":
    print(" ".join(sorted(s["source"] for s in svc.get("secrets") or [])))
elif field == "files":
    print(" ".join(sorted((svc.get("environment") or {}).get("REQUIRED_SECRET_FILES", "").split())))
else:
    print(svc["command"][0].replace("$$", "$"))' "$1"
}

test_secrets_init_mounts_exactly_the_file_secrets() {
  local t=${FUNCNAME[0]} got
  if ! docker compose version >/dev/null 2>&1; then bad "$t" "docker compose is not available"; return; fi
  got="$(secrets_init_field mounts)"
  if [[ "$got" == "$SECRET_FILE_NAMES" ]]; then ok "$t"; else bad "$t" "secrets-init mounts [$got], want [$SECRET_FILE_NAMES]"; fi
}

test_secrets_init_checks_every_file_secret() {
  local t=${FUNCNAME[0]} got
  if ! docker compose version >/dev/null 2>&1; then bad "$t" "docker compose is not available"; return; fi
  got="$(secrets_init_field files)"
  if [[ "$got" == "$SECRET_FILE_NAMES" ]]; then ok "$t"; else bad "$t" "REQUIRED_SECRET_FILES is [$got], want [$SECRET_FILE_NAMES]"; fi
}

test_secrets_init_script_behaviour() {
  # Run the real secrets-init script and its REQUIRED_SECRET_FILES, as Compose renders them.
  local t=${FUNCNAME[0]} script dir files f g
  if ! docker compose version >/dev/null 2>&1; then bad "$t" "docker compose is not available"; return; fi
  dir="$(mktemp -d "${TMPDIR:-/tmp}/lab-cred-test.XXXXXXXX")"
  script="$dir/secrets-init.sh"
  secrets_init_field command | sed "s#/run/secrets/#$dir/run/#g" > "$script"
  files="$(secrets_init_field files)"
  mkdir -p "$dir/run"
  good_files() { for g in $SECRET_FILE_NAMES; do printf 'Fixture-File-1' > "$dir/run/$g"; done; }
  run_si() { env -i PATH="$PATH" REQUIRED_SECRET_FILES="$files" REQUIRED_SECRETS="A B" "$@" sh "$script" >/dev/null 2>&1; }
  local fails=""
  good_files
  run_si A=good1 B=good2 || fails="$fails valid"
  run_si A=good1 B="  " && fails="$fails whitespace"
  run_si A=good1 B="$(printf '\v\f ')" && fails="$fails vt-ff"
  run_si A=good1 B=CHANGEME_X && fails="$fails changeme"
  run_si A=good1 B="LabDemo""Pass1!" && fails="$fails demo-default"
  run_si A=good1 && fails="$fails unset"
  for f in $SECRET_FILE_NAMES; do
    good_files; printf 'x-insecure-dev-only\n' > "$dir/run/$f"
    run_si A=good1 B=good2 && fails="$fails $f-placeholder"
    good_files; printf '\v\f\n' > "$dir/run/$f"
    run_si A=good1 B=good2 && fails="$fails $f-empty"
    good_files; rm -f "$dir/run/$f"
    run_si A=good1 B=good2 && fails="$fails $f-missing"
  done
  rm -rf -- "$dir"
  if [[ -n "$fails" ]]; then bad "$t" "wrong result for:$fails"; else ok "$t"; fi
}

test_padded_placeholders_are_rejected() {
  local t=${FUNCNAME[0]} key line old="LabDemo""Pass1!"
  while IFS='|' read -r key line; do
    new_fixture
    good_env_except "$key"
    printf '%s\n' "$line" >> "$FIX/.env"
    if check >/dev/null 2>&1; then bad "$t" "accepted: $key (padded/commented old value)"; else ok "$t ($key)"; fi
    cleanup
  done <<EOF
DEMO_USER_PASSWORD|DEMO_USER_PASSWORD=" ${old} "
OPENWEBUI_SECRET_KEY|OPENWEBUI_SECRET_KEY=" CHANGEME_x"
PGADMIN_PASSWORD|PGADMIN_PASSWORD=old-insecure-dev-only # note
EOF
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
test_whitespace_only_value_is_rejected
test_old_demo_default_is_rejected
test_placeholder_secret_files_are_rejected
test_secret_file_consumers_wait_for_secrets_init
test_secrets_init_mounts_exactly_the_file_secrets
test_secrets_init_checks_every_file_secret
test_secrets_init_script_behaviour
test_padded_placeholders_are_rejected
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
