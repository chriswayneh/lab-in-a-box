#!/usr/bin/env bash
# =============================================================================
# Validation
# =============================================================================
#   make validate
#
# Runs the same checks CI runs, locally, before you push:
#
#   1. The compose project resolves — including profiles and the GPU override
#   2. Every YAML file parses
#   3. Every JSON file parses
#   4. Every shell script parses
#   5. Prometheus rules and Loki config are accepted by their own tooling
#
# Steps that need a tool you do not have are skipped with a note rather than
# failing, so this is useful on a machine with nothing but Docker installed.
# =============================================================================
set -uo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

FAILURES=0
SKIPPED=0

fail()    { error "$*"; (( FAILURES++ )) || true; }
skipped() { log "  ${C_DIM}skipped: $*${C_RESET}"; (( SKIPPED++ )) || true; }

# -----------------------------------------------------------------------------
check_compose() {
  heading "Compose project"

  if ! command -v docker >/dev/null 2>&1; then
    skipped "docker is not installed"
    return
  fi

  if docker compose -f "${LAB_ROOT}/docker-compose.yml" config --quiet 2>/tmp/lab-compose-err; then
    success "docker-compose.yml resolves"
  else
    fail "docker-compose.yml does not resolve"
    sed 's/^/    /' /tmp/lab-compose-err
  fi

  if docker compose -f "${LAB_ROOT}/docker-compose.yml" \
       --profile qdrant --profile watchtower config --quiet 2>/dev/null; then
    success "optional profiles resolve"
  else
    fail "the qdrant/watchtower profiles do not resolve"
  fi

  if docker compose -f "${LAB_ROOT}/docker-compose.yml" \
       -f "${LAB_ROOT}/compose/overrides/gpu.yml" config --quiet 2>/dev/null; then
    success "the GPU override resolves"
  else
    fail "compose/overrides/gpu.yml does not resolve"
  fi

  rm -f /tmp/lab-compose-err
}

# -----------------------------------------------------------------------------
# A container is only observable if it can say whether it is well.
# -----------------------------------------------------------------------------
check_healthchecks() {
  heading "Healthchecks"

  command -v docker >/dev/null 2>&1 || { skipped "docker is not installed"; return; }
  command -v python3 >/dev/null 2>&1 || { skipped "python3 is not installed"; return; }

  local missing
  missing="$(docker compose -f "${LAB_ROOT}/docker-compose.yml" config --format json 2>/dev/null \
    | python3 -c '
import json, sys
try:
    cfg = json.load(sys.stdin)
except Exception:
    sys.exit(0)
missing = [
    name for name, svc in sorted(cfg.get("services", {}).items())
    # One-shot provisioning jobs exit as soon as they are done; a healthcheck
    # on a container that is meant to stop is meaningless.
    if not svc.get("healthcheck") and not name.endswith("-init")
]
print(" ".join(missing))
')"

  if [[ -z "$missing" ]]; then
    success "every long-running service defines a healthcheck"
  else
    fail "services with no healthcheck: ${missing}"
  fi
}

# -----------------------------------------------------------------------------
check_yaml() {
  heading "YAML"

  if ! command -v python3 >/dev/null 2>&1; then
    skipped "python3 is not installed"
    return
  fi

  if ! python3 -c "import yaml" 2>/dev/null; then
    skipped "PyYAML is not installed (pip install pyyaml)"
    return
  fi

  local count=0 file
  while IFS= read -r file; do
    if python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1], encoding='utf-8')))" "$file" 2>/dev/null; then
      (( count++ )) || true
    else
      fail "invalid YAML: ${file#$LAB_ROOT/}"
      python3 -c "import yaml,sys; list(yaml.safe_load_all(open(sys.argv[1], encoding='utf-8')))" "$file" 2>&1 | tail -3 | sed 's/^/    /'
    fi
  done < <(find "$LAB_ROOT" -name '*.yml' -o -name '*.yaml' \
             | grep -v '/\.git/' | grep -v '/node_modules/' | sort)

  success "${count} YAML file(s) parsed"
}

# -----------------------------------------------------------------------------
check_json() {
  heading "JSON"

  if ! command -v python3 >/dev/null 2>&1; then
    skipped "python3 is not installed"
    return
  fi

  local count=0 file
  while IFS= read -r file; do
    if python3 -c "import json,sys; json.load(open(sys.argv[1], encoding='utf-8'))" "$file" 2>/dev/null; then
      (( count++ )) || true
    else
      fail "invalid JSON: ${file#$LAB_ROOT/}"
    fi
  done < <(find "$LAB_ROOT" -name '*.json' \
             | grep -v '/\.git/' | grep -v '/backups/' | grep -v '/artifacts/' | sort)

  success "${count} JSON file(s) parsed"
}

# -----------------------------------------------------------------------------
check_shell() {
  heading "Shell scripts"

  local count=0 file interpreter
  while IFS= read -r file; do
    # Honour the shebang: the init scripts target POSIX sh because they run in
    # Alpine-based images that have no bash, and checking them with bash would
    # accept bashisms that would fail at runtime.
    interpreter="bash"
    head -1 "$file" | grep -q '^#!/bin/sh' && interpreter="sh"

    if $interpreter -n "$file" 2>/dev/null; then
      (( count++ )) || true
    else
      fail "syntax error in ${file#$LAB_ROOT/}"
      $interpreter -n "$file" 2>&1 | sed 's/^/    /'
    fi
  done < <(find "$LAB_ROOT/scripts" "$LAB_ROOT/configs" -name '*.sh' 2>/dev/null | sort)

  success "${count} shell script(s) parsed"

  if command -v shellcheck >/dev/null 2>&1; then
    # Collected into an array so that a path containing a space survives.
    local -a shell_files=()
    while IFS= read -r script; do
      shell_files+=("$script")
    done < <(find "$LAB_ROOT/scripts" -name '*.sh')

    # Matches the severity CI enforces, so a clean local run means a clean CI run.
    if shellcheck --severity=warning --exclude=SC1091 "${shell_files[@]}"; then
      success "shellcheck found no warnings or errors"
    else
      fail "shellcheck reported problems"
    fi
  else
    skipped "shellcheck is not installed"
  fi
}

# -----------------------------------------------------------------------------
# Ask the tools themselves whether their configuration is valid. Nothing
# validates a Prometheus rule file like Prometheus.
# -----------------------------------------------------------------------------
check_with_upstream_tools() {
  heading "Upstream config checks"

  if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
    skipped "the Docker daemon is not available"
    return
  fi

  promtool_check "prometheus.yml" config /cfg/prometheus.yml
  promtool_check "alert rules"    rules  /cfg/rules/lab-alerts.yml
}

# Runs `promtool check <what> <path>` against the mounted monitoring directory.
#
# MSYS_NO_PATHCONV disables Git Bash's automatic POSIX-to-Windows path
# translation, which would otherwise rewrite the *container's* /cfg path into a
# Windows path that exists on neither side. It is an unset no-op everywhere
# except Git Bash.
promtool_check() {
  local label="$1" subcommand="$2" path="$3"
  local -a cmd=(
    docker run --rm --entrypoint promtool
    -v "${LAB_ROOT}/monitoring/prometheus:/cfg:ro"
    prom/prometheus:v3.1.0 check "$subcommand" "$path"
  )

  if MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "${cmd[@]}" >/dev/null 2>&1; then
    success "${label} accepted by promtool"
  else
    fail "promtool rejected ${label}"
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' "${cmd[@]}" 2>&1 | tail -12 | sed 's/^/    /'
  fi
}

# -----------------------------------------------------------------------------
check_secrets() {
  # Delegated so that the same logic backs `make validate`, the pre-commit hook
  # and CI — three callers, one definition of "is this safe to commit".
  if bash "${LAB_SCRIPT_DIR}/check-secrets.sh"; then
    return 0
  fi
  (( FAILURES++ )) || true
}

check_makefile_user_guard() {
  # Regression test for a cross-platform trap.
  #
  # `USER` is exported by the shell on macOS and Linux, and make imports the
  # environment as variables. Before the $(origin USER) guard in the Makefile,
  # `make jml-leave` with no USER= inherited the operator's own account name and
  # sailed straight past the usage check — an offboarding command aimed at
  # whoever happened to be logged in.
  #
  # This never reproduces on a machine where USER is unset, which is why it is
  # asserted explicitly rather than left to chance.
  heading "Makefile argument guards"

  if ! command -v make >/dev/null 2>&1; then
    skipped "make is not installed"
    return 0
  fi

  local output
  if output="$(cd "$LAB_ROOT" && USER=someone-else make jml-leave 2>&1)"; then
    fail "make jml-leave ran with no USER= — the environment's USER leaked in"
    log "  ${C_DIM}${output}${C_RESET}"
    return 1
  fi

  if [[ "$output" != *"Usage:"* ]]; then
    fail "make jml-leave failed without printing usage"
    return 1
  fi

  success "jml targets ignore an inherited USER and require an explicit one"
}

check_markdown() {
  # Delegated to the same script CI runs, so a clean `make validate` genuinely
  # means a clean Markdown job rather than "clean on whichever linter version
  # this machine happens to have".
  heading "Markdown"

  if bash "${LAB_SCRIPT_DIR}/check-markdown.sh" >/dev/null 2>&1; then
    success "markdownlint clean"
    return 0
  fi

  fail "markdownlint reported problems"
  bash "${LAB_SCRIPT_DIR}/check-markdown.sh" 2>&1 | sed 's/^/  /' || true
}

main() {
  log "${C_BOLD}Validating Lab-in-a-Box${C_RESET}"

  check_secrets
  check_makefile_user_guard
  check_compose
  check_healthchecks
  check_yaml
  check_json
  check_shell
  check_markdown

  # Pulls a container image, so it is opt-out for a fast inner loop.
  if [[ "${SKIP_UPSTREAM:-0}" != "1" ]]; then
    check_with_upstream_tools
  fi

  heading "Result"
  if (( FAILURES == 0 )); then
    success "all checks passed${SKIPPED:+ (${SKIPPED} skipped)}"
    exit 0
  fi

  error "${FAILURES} check(s) failed"
  exit 1
}

main "$@"
