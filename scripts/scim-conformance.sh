#!/usr/bin/env bash
# Run the published scim2/test-suite at a reviewed commit against Keycloak.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"

SCIM_TEST_SUITE_COMMIT="3d80a46970a02ce114efeeb38f3f112b5d736d16"
network="$(engine_preflight)"
host_root="$(engine_host_path "$LAB_ROOT")"
mkdir -p "${LAB_ROOT}/artifacts/scim-conformance"

SCIM_TOKEN="$(run_engine scim_cli.py token)"
export SCIM_TOKEN

MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' docker run --rm \
  --network "$network" \
  --env SCIM_TOKEN \
  --env "SCIM_TEST_SUITE_COMMIT=${SCIM_TEST_SUITE_COMMIT}" \
  --volume "${host_root}/artifacts/scim-conformance:/report" \
  golang:1.26-bookworm \
  sh -ec '
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends git >/dev/null
    git init -q /suite
    cd /suite
    git remote add origin https://github.com/scim2/test-suite.git
    git fetch -q --depth 1 origin "$SCIM_TEST_SUITE_COMMIT"
    git checkout -q FETCH_HEAD
    go test ./compliance/ -v -count=1 \
      -scim.url=http://keycloak:8080/realms/lab/scim/v2 \
      -scim.token="$SCIM_TOKEN" \
      -scim.report=/report/compliance-report.txt
  '

success "SCIM conformance report: artifacts/scim-conformance/compliance-report.txt"
