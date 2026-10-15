#!/usr/bin/env bash
# =============================================================================
# Fixture tests for check-secrets.sh's inline Compose credential guard.
# Each fixture copies the guard into a throwaway git repo with one Compose
# file and checks the exit code: inline --admin-password forms must fail (1),
# --admin-password-file must pass (0).
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILS=0

run_fixture() {
  local name="$1" expected="$2" body="$3" tmp rc
  tmp="$(mktemp -d)"
  mkdir -p "$tmp/scripts/lib" "$tmp/compose" "$tmp/secrets"
  cp "$HERE/check-secrets.sh" "$tmp/scripts/"
  cp "$HERE/lib/common.sh" "$tmp/scripts/lib/"
  printf 'services: {}\n' > "$tmp/docker-compose.yml"
  printf '%s\n' "$body" > "$tmp/compose/06-tools.yml"
  git -C "$tmp" init -q
  bash "$tmp/scripts/check-secrets.sh" >/dev/null 2>&1; rc=$?
  rm -rf "$tmp"
  if [[ "$rc" == "$expected" ]]; then
    printf 'ok   %s (exit %s)\n' "$name" "$rc"
  else
    printf 'FAIL %s: expected exit %s, got %s\n' "$name" "$expected" "$rc"
    (( FAILS++ )) || true
  fi
}

run_fixture "equals form" 1 '    command:
      - --admin-password=$$2y$$05$$abc'
run_fixture "space form" 1 '    command: --admin-password $$2y$$05$$abc'
run_fixture "separate list argument" 1 '    command:
      - --admin-password
      - $$2y$$05$$abc'
run_fixture "quoted list argument" 1 '    command: ["-H", "unix:///var/run/docker.sock", "--admin-password", "$$2y$$05$$abc"]'
run_fixture "password file is allowed" 0 '    command:
      - --admin-password-file=/run/secrets/portainer_admin_password'

(( FAILS == 0 )) || exit 1
