#!/usr/bin/env bash
# =============================================================================
# Identity test suites — containerised entry point
# =============================================================================
#   make jml-test          all four suites
#   bash scripts/test-identity.sh lifecycle
#   bash scripts/test-identity.sh rbac
#   bash scripts/test-identity.sh access-review
#   bash scripts/test-identity.sh scim
#
# Runs against the RUNNING lab. These are integration tests by design: the
# things being verified are whether revocation actually revokes and whether the
# simulator reports what the services really contain, and neither is a question
# a mock can answer.
#
# Uses disposable identities. The seeded demo users are protected by
# model.PROTECTED_USERNAMES and are never modified.
# =============================================================================
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"

SUITE="${1:-all}"
STATUS=0

run_suite() {
  local name="$1" module="$2"
  heading "$name"
  if run_engine "$module"; then
    return 0
  fi
  STATUS=1
}

run_scim_suite() {
  heading "SCIM provisioning tests"
  # Relative to the container's root. Avoids Git Bash rewriting /engine into a
  # Windows host path before Compose receives it.
  if compose exec -T scim-provisioner python3 engine/test_scim.py; then
    return 0
  fi
  STATUS=1
}

case "$SUITE" in
  lifecycle)      run_suite "Lifecycle tests" test_lifecycle.py ;;
  rbac)           run_suite "RBAC simulator tests" test_rbac.py ;;
  access-review)  run_suite "Access review campaign tests" test_campaign.py ;;
  scim)           run_scim_suite ;;
  all)
    log "${C_DIM}Running against the live lab. This creates and offboards disposable users.${C_RESET}"
    run_suite "Lifecycle tests" test_lifecycle.py
    run_suite "RBAC simulator tests" test_rbac.py
    run_suite "Access review campaign tests" test_campaign.py
    run_scim_suite
    ;;
  *) die "unknown suite '${SUITE}'; expected: lifecycle, rbac, access-review, scim, all" ;;
esac

exit "$STATUS"
