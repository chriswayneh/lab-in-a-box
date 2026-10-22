#!/usr/bin/env bash
# =============================================================================
# Failure-path tests for backup.sh and restore.sh
# =============================================================================
#   bash scripts/test-backup-restore.sh
#
# Runs the real scripts against a throwaway copy of the repository with a
# fake `docker` on PATH, so nothing touches a running lab. Each case sets the
# fake up to fail in one specific way and checks the exit code, the output
# and what happened to the files.
#
# If PostgreSQL server binaries are installed (initdb/pg_ctl), two extra cases
# load a real pg_dumpall dump into a disposable cluster through restore.sh;
# otherwise they are skipped.
# =============================================================================
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FAILS=0
PASSES=0
SKIPS=0

ok()   { printf 'ok   %s\n' "$1"; (( PASSES++ )) || true; }
bad()  { printf 'FAIL %s: %s\n' "$1" "$2"; (( FAILS++ )) || true; }
skip() { printf 'skip %s (%s)\n' "$1" "$2"; (( SKIPS++ )) || true; }

# -- fixture -------------------------------------------------------------------
new_fixture() {
  FIX="$(mktemp -d "${TMPDIR:-/tmp}/lab-br-test.XXXXXXXX")"
  mkdir -p "$FIX/repo/scripts/lib" "$FIX/bin"
  cp "$HERE/backup.sh" "$HERE/restore.sh" "$HERE/check-admin-credentials.sh" "$FIX/repo/scripts/"
  cp "$HERE/lib/common.sh" "$FIX/repo/scripts/lib/"
  printf 'services: {}\n' > "$FIX/repo/docker-compose.yml"
  printf 'POSTGRES_USER=%s\nCOMPOSE_PROJECT_NAME=lab\n' "${FIX_PG_USER:-lab}" > "$FIX/repo/.env"
  # A valid value for every required credential, so restore's guard passes.
  sed -n '/^REQUIRED=(/,/^)/p' "$HERE/check-admin-credentials.sh" | sed '1d;$d' | tr -d ' ' \
    | while IFS= read -r n; do printf '%s=Fixture-Restore-%s\n' "$n" "${#n}"; done >> "$FIX/repo/.env"
  : > "$FIX/calls"
  cat > "$FIX/bin/docker" <<'SH'
#!/usr/bin/env bash
# Fake docker. Behaviour is controlled by FAKE_* variables.
printf '%s\n' "$*" >> "$FAKE_DIR/calls"
args=" $* "
case "$args" in
  " compose version "|" info ") exit 0 ;;
esac
if [[ "$1" == compose ]]; then
  case "$args" in
    *" ps --status running --services "*) echo postgres; exit 0 ;;
    *" up -d postgres "*) exit "${FAKE_PG_UP_RC:-0}" ;;
    *" up -d ") exit "${FAKE_UP_RC:-0}" ;;
    *" pg_isready "*) exit 0 ;;
    *" pg_dumpall "*)
      [[ -n "${FAKE_DUMP_FAIL:-}" ]] && exit 1
      echo "SELECT 1;"; exit 0 ;;
    *" psql "*)
      if [[ -n "${FAKE_REAL_PG:-}" ]]; then
        # Forward to a real local cluster, keeping restore.sh's psql flags.
        shift; while [[ "$1" != psql ]]; do shift; done; shift
        exec psql "$@"
      fi
      cat > "$FAKE_DIR/psql_stdin"
      exit "${FAKE_PSQL_RC:-0}" ;;
    *) exit 0 ;;
  esac
fi
if [[ "$1" == volume ]]; then exit 0; fi
if [[ "$1" == run ]]; then
  backup_dir=""; prev=""
  for a in "$@"; do
    [[ "$prev" == -v && "$a" == *:/backup ]] && backup_dir="${a%:/backup}"
    prev="$a"
  done
  if [[ "$args" == *" tar czf /backup/"* ]]; then
    name="${args##* tar czf /backup/}"; name="${name%%.tar.gz*}"
    echo "$name" >> "$FAKE_DIR/volumes_attempted"
    [[ "$name" == "${FAKE_FAIL_VOLUME:-}" ]] && exit 1
    echo data > "$backup_dir/$name.tar.gz"; exit 0
  fi
  exit 0
fi
exit 0
SH
  chmod +x "$FIX/bin/docker"
  export FAKE_DIR="$FIX"
}
cleanup_fixture() { case "${FIX:-}" in */lab-br-test.*) rm -rf -- "$FIX" ;; esac; }

old_backups() {
  local i
  mkdir -p "$FIX/repo/backups"
  for i in 1 2 3 4 5 6; do mkdir -p "$FIX/repo/backups/20200101-00000$i"; done
}

run_backup() {
  ( PATH="$FIX/bin:$PATH" BACKUP_KEEP=3 NO_COLOR=1 bash "$FIX/repo/scripts/backup.sh" ) >"$FIX/out" 2>&1
}
run_restore() {
  ( unset KEYCLOAK_ADMIN_PASSWORD VAULT_DEV_ROOT_TOKEN
    PATH="$FIX/bin:$PATH" ASSUME_YES=1 NO_COLOR=1 BACKUP="$1" bash "$FIX/repo/scripts/restore.sh" ) \
    </dev/null >"$FIX/out" 2>&1
}
count_old() { find "$FIX/repo/backups" -maxdepth 1 -mindepth 1 -type d -name '2020*' | wc -l | tr -d ' '; }

# -- backup cases --------------------------------------------------------------
test_volume_failure_preserves_all_old_backups() {
  local t=${FUNCNAME[0]}; new_fixture; old_backups
  FAKE_FAIL_VOLUME=keycloak_data run_backup; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 after a failed volume"
  elif [[ "$(count_old)" != 6 ]]; then bad "$t" "old backups were pruned ($(count_old)/6 left)"
  elif ! grep -q "nothing pruned" "$FIX/out"; then bad "$t" "no 'nothing pruned' warning"
  else ok "$t"; fi
  cleanup_fixture
}

test_postgres_failure_preserves_all_old_backups() {
  local t=${FUNCNAME[0]}; new_fixture; old_backups
  FAKE_DUMP_FAIL=1 run_backup; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 after a failed dump"
  elif [[ "$(count_old)" != 6 ]]; then bad "$t" "old backups were pruned ($(count_old)/6 left)"
  else ok "$t"; fi
  cleanup_fixture
}

test_success_prunes_to_backup_keep() {
  local t=${FUNCNAME[0]}; new_fixture; old_backups
  run_backup; local rc=$?
  local total; total="$(find "$FIX/repo/backups" -maxdepth 1 -mindepth 1 -type d | wc -l | tr -d ' ')"
  if (( rc != 0 )); then bad "$t" "exit $rc on a clean run"
  elif [[ "$total" != 3 ]]; then bad "$t" "expected 3 backups kept, found $total"
  else ok "$t"; fi
  cleanup_fixture
}

test_failure_does_not_stop_remaining_volume_attempts() {
  local t=${FUNCNAME[0]}; new_fixture
  FAKE_FAIL_VOLUME=redis_data run_backup
  # redis_data is second in VOLUMES; the last entry must still be attempted.
  local last; last="$(grep -A30 '^VOLUMES=(' "$FIX/repo/scripts/backup.sh" | sed -n '/^)/q;p' | grep -v VOLUMES | tail -n1 | tr -d ' ')"
  if grep -qx "$last" "$FIX/volumes_attempted" 2>/dev/null; then ok "$t"
  else bad "$t" "volume '$last' was not attempted after redis_data failed"; fi
  cleanup_fixture
}

# -- restore cases -------------------------------------------------------------
make_backup() {
  mkdir -p "$FIX/repo/backups/$1"
  printf '%s\n' "${2:-SELECT 1;}" | gzip > "$FIX/repo/backups/$1/postgres.sql.gz"
}

test_psql_error_propagates_to_main() {
  local t=${FUNCNAME[0]}; new_fixture; make_backup b1
  FAKE_PSQL_RC=3 run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 after psql failed"
  elif grep -qE "PostgreSQL restored|restore complete" "$FIX/out"; then bad "$t" "a success message was printed"
  elif ! grep -q "restore FAILED" "$FIX/out"; then bad "$t" "no failure summary"
  else ok "$t"; fi
  cleanup_fixture
}

test_psql_uses_strict_flags() {
  local t=${FUNCNAME[0]}; new_fixture; make_backup b1
  run_restore b1
  local line; line="$(grep ' psql ' "$FIX/calls" | tail -n1)"
  if [[ "$line" == *" -X "* && "$line" == *"-v ON_ERROR_STOP=1"* ]]; then ok "$t"
  else bad "$t" "psql was called without -X and -v ON_ERROR_STOP=1"; fi
  cleanup_fixture
}

test_corrupt_gzip_fails_restore() {
  local t=${FUNCNAME[0]}; new_fixture
  mkdir -p "$FIX/repo/backups/b1"; echo "not gzip" > "$FIX/repo/backups/b1/postgres.sql.gz"
  run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 with a corrupt archive (psql mock succeeded)"
  else ok "$t"; fi
  cleanup_fixture
}

test_postgres_start_failure_is_reported() {
  local t=${FUNCNAME[0]}; new_fixture; make_backup b1
  FAKE_PG_UP_RC=1 run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 when PostgreSQL could not start"
  elif grep -q ' psql ' "$FIX/calls"; then bad "$t" "the SQL import was attempted anyway"
  elif ! grep -q "could not start PostgreSQL" "$FIX/out"; then bad "$t" "no error message"
  else ok "$t"; fi
  cleanup_fixture
}

test_successful_restore_reports_success() {
  local t=${FUNCNAME[0]}; new_fixture; make_backup b1
  run_restore b1; local rc=$?
  if (( rc != 0 )); then bad "$t" "exit $rc on a clean restore"
  elif ! grep -q "restore complete" "$FIX/out"; then bad "$t" "no success message"
  else ok "$t"; fi
  cleanup_fixture
}

test_only_bootstrap_role_lines_are_stripped() {
  local t=${FUNCNAME[0]}; new_fixture
  make_backup b1 $'DROP ROLE IF EXISTS lab;\nCREATE ROLE lab;\nALTER ROLE lab WITH SUPERUSER;\nDROP ROLE IF EXISTS app;\nCREATE ROLE app;\nCREATE ROLE lab2;'
  run_restore b1
  local got; got="$(cat "$FIX/psql_stdin" 2>/dev/null)"
  if [[ "$got" == $'ALTER ROLE lab WITH SUPERUSER;\nDROP ROLE IF EXISTS app;\nCREATE ROLE app;\nCREATE ROLE lab2;' ]]; then ok "$t"
  else bad "$t" "unexpected SQL reached psql"; fi
  cleanup_fixture
}

test_missing_dump_fails_before_changes() {
  local t=${FUNCNAME[0]}; new_fixture
  mkdir -p "$FIX/repo/backups/b1"; echo data > "$FIX/repo/backups/b1/redis_data.tar.gz"
  run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 with no postgres.sql.gz"
  elif grep -qE ' down| run | psql ' "$FIX/calls"; then bad "$t" "the lab was stopped or data touched before refusing"
  else ok "$t"; fi
  cleanup_fixture
}

test_legacy_archive_only_backup_fails() {
  local t=${FUNCNAME[0]}; new_fixture
  mkdir -p "$FIX/repo/backups/b1"; echo data > "$FIX/repo/backups/b1/postgres_data.tar.gz"
  run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 for a postgres_data-only backup"
  elif grep -q "restore complete" "$FIX/out"; then bad "$t" "success was reported"
  elif grep -qE ' down| run ' "$FIX/calls"; then bad "$t" "volumes were touched before refusing"
  else ok "$t"; fi
  cleanup_fixture
}

test_lookalike_error_is_not_filtered() {
  # A real error whose text mentions the bootstrap messages must still fail.
  local t=${FUNCNAME[0]}
  start_real_pg || { skip "$t" "PostgreSQL server binaries not available"; stop_real_pg; return; }
  new_fixture
  make_backup b1 $'SELECT * FROM "current user cannot be dropped";\nCREATE TABLE sentinel2(x int);'
  FAKE_REAL_PG=1 run_restore b1; local rc=$?
  local sentinel; sentinel="$(psql -X -tAq -U lab -d postgres -c "select to_regclass('sentinel2') is not null")"
  if (( rc == 0 )); then bad "$t" "a look-alike SQL error was ignored"
  elif [[ "$sentinel" == t ]]; then bad "$t" "statement after the error ran"
  else ok "$t"; fi
  cleanup_fixture; stop_real_pg
}

test_sql_errors_do_not_leak_data() {
  local t=${FUNCNAME[0]}
  start_real_pg || { skip "$t" "PostgreSQL server binaries not available"; stop_real_pg; return; }
  new_fixture
  make_backup b1 $'CREATE TABLE leak(x int);\nINSERT INTO leak VALUES (\'Sensitive-Canary-9876\');'
  FAKE_REAL_PG=1 run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "invalid data restored without error"
  elif grep -q "Sensitive-Canary-9876" "$FIX/out"; then bad "$t" "restored data appeared in the output"
  else ok "$t"; fi
  cleanup_fixture; stop_real_pg
}

test_role_with_dot_is_refused_not_matched() {
  # POSTGRES_USER=lab.x must not strip "CREATE ROLE labax;" (regex dot) and
  # must be refused outright before the lab is touched.
  local t=${FUNCNAME[0]}; FIX_PG_USER=lab.x new_fixture; unset FIX_PG_USER
  make_backup b1 $'CREATE ROLE labax;'
  run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "restore ran with POSTGRES_USER=lab.x"
  elif grep -qE ' down| psql ' "$FIX/calls"; then bad "$t" "the lab was touched before refusing"
  elif ! grep -q "not a plain identifier" "$FIX/out"; then bad "$t" "no clear refusal message"
  else ok "$t"; fi
  cleanup_fixture
}

test_role_with_slash_is_refused() {
  local t=${FUNCNAME[0]}; FIX_PG_USER='lab/x' new_fixture; unset FIX_PG_USER
  make_backup b1
  run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "restore ran with POSTGRES_USER=lab/x"
  elif grep -qE ' down| psql ' "$FIX/calls"; then bad "$t" "the lab was touched before refusing"
  else ok "$t"; fi
  cleanup_fixture
}

test_strip_is_exact_string_match() {
  local t=${FUNCNAME[0]}; new_fixture
  make_backup b1 $'CREATE ROLE labax;\nDROP ROLE IF EXISTS lab ;\nCREATE ROLE lab;\n  CREATE ROLE lab;'
  run_restore b1
  if [[ "$(cat "$FIX/psql_stdin" 2>/dev/null)" == $'CREATE ROLE labax;\nDROP ROLE IF EXISTS lab ;\n  CREATE ROLE lab;' ]]; then ok "$t"
  else bad "$t" "lines other than the exact bootstrap statements were removed"; fi
  cleanup_fixture
}

test_missing_credential_refuses_before_stopping_lab() {
  local t=${FUNCNAME[0]}; new_fixture; make_backup b1
  sed -i '/^REDIS_PASSWORD=/d' "$FIX/repo/.env"
  run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "restore ran with REDIS_PASSWORD missing"
  elif grep -qE ' down| psql ' "$FIX/calls"; then bad "$t" "the lab was stopped before refusing"
  else ok "$t"; fi
  cleanup_fixture
}

test_final_compose_up_failure_fails_restore() {
  local t=${FUNCNAME[0]}; new_fixture; make_backup b1
  FAKE_UP_RC=1 run_restore b1; local rc=$?
  if (( rc == 0 )); then bad "$t" "exit 0 when the lab did not start again"
  elif grep -q "restore complete" "$FIX/out"; then bad "$t" "success was reported"
  else ok "$t"; fi
  cleanup_fixture
}

# -- real PostgreSQL cases (optional) ------------------------------------------
pg_bin() {
  local d
  for d in /usr/lib/postgresql/*/bin /usr/local/pgsql/bin; do
    [[ -x "$d/initdb" ]] && { printf '%s' "$d"; return 0; }
  done
  command -v initdb >/dev/null 2>&1 && { dirname "$(command -v initdb)"; return 0; }
  return 1
}

start_real_pg() {
  PGB="$(pg_bin)" || return 1
  PGD="$(mktemp -d "${TMPDIR:-/tmp}/lab-br-pg.XXXXXXXX")"
  "$PGB/initdb" -D "$PGD/data" -U lab --auth=trust >/dev/null 2>&1 || return 1
  "$PGB/pg_ctl" -D "$PGD/data" -o "-k $PGD -p 54329 -c listen_addresses=" -l "$PGD/log" -w start >/dev/null 2>&1 || return 1
  export PGHOST="$PGD" PGPORT=54329
  PATH="$PGB:$PATH"
}
stop_real_pg() {
  [[ -n "${PGD:-}" ]] || return 0
  "$PGB/pg_ctl" -D "$PGD/data" -m immediate stop >/dev/null 2>&1 || true
  case "$PGD" in */lab-br-pg.*) rm -rf -- "$PGD" ;; esac
  unset PGHOST PGPORT PGD
}

test_real_sql_error_stops_before_later_statement() {
  local t=${FUNCNAME[0]}
  start_real_pg || { skip "$t" "PostgreSQL server binaries not available"; stop_real_pg; return; }
  new_fixture
  make_backup b1 $'CREATE TABLE before_err(x int);\nSELECT * FROM no_such_table;\nCREATE TABLE sentinel(x int);'
  FAKE_REAL_PG=1 run_restore b1; local rc=$?
  local sentinel; sentinel="$(psql -X -tAq -U lab -d postgres -c "select to_regclass('sentinel') is not null")"
  if (( rc == 0 )); then bad "$t" "exit 0 after a real SQL error"
  elif [[ "$sentinel" == t ]]; then bad "$t" "a statement after the error still ran"
  else ok "$t"; fi
  cleanup_fixture; stop_real_pg
}

test_generated_dump_round_trip() {
  local t=${FUNCNAME[0]}
  start_real_pg || { skip "$t" "PostgreSQL server binaries not available"; stop_real_pg; return; }
  new_fixture
  psql -X -q -U lab -d postgres -c "CREATE ROLE app LOGIN" -c "CREATE DATABASE appdb OWNER app" >/dev/null
  psql -X -q -U lab -d appdb -c "CREATE TABLE t(x int); INSERT INTO t VALUES (42); GRANT SELECT ON t TO app;" >/dev/null
  mkdir -p "$FIX/repo/backups/b1"
  pg_dumpall -U lab --clean --if-exists | gzip > "$FIX/repo/backups/b1/postgres.sql.gz"
  # Change state after the backup; the restore must bring it back.
  psql -X -q -U lab -d appdb -c "INSERT INTO t VALUES (7)" >/dev/null
  FAKE_REAL_PG=1 run_restore b1; local rc=$?
  local rows grant
  rows="$(psql -X -tAq -U lab -d appdb -c "select string_agg(x::text, ',' order by x) from t")"
  grant="$(psql -X -tAq -U lab -d appdb -c "select has_table_privilege('app','t','SELECT')")"
  if (( rc != 0 )); then bad "$t" "restore of a real pg_dumpall dump failed (exit $rc)"
  elif [[ "$rows" != 42 ]]; then bad "$t" "data not restored (got '$rows')"
  elif [[ "$grant" != t ]]; then bad "$t" "role grant not restored"
  else ok "$t"; fi
  cleanup_fixture; stop_real_pg
}

# -----------------------------------------------------------------------------
test_volume_failure_preserves_all_old_backups
test_postgres_failure_preserves_all_old_backups
test_success_prunes_to_backup_keep
test_failure_does_not_stop_remaining_volume_attempts
test_psql_error_propagates_to_main
test_psql_uses_strict_flags
test_corrupt_gzip_fails_restore
test_postgres_start_failure_is_reported
test_successful_restore_reports_success
test_only_bootstrap_role_lines_are_stripped
test_missing_dump_fails_before_changes
test_role_with_dot_is_refused_not_matched
test_role_with_slash_is_refused
test_strip_is_exact_string_match
test_missing_credential_refuses_before_stopping_lab
test_final_compose_up_failure_fails_restore
test_legacy_archive_only_backup_fails
test_real_sql_error_stops_before_later_statement
test_lookalike_error_is_not_filtered
test_sql_errors_do_not_leak_data
test_generated_dump_round_trip

printf '\n%d passed, %d failed, %d skipped\n' "$PASSES" "$FAILS" "$SKIPS"
(( FAILS == 0 ))
