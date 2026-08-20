#!/usr/bin/env bash
# Keycloak SCIM operator entry point. Python remains containerized so the host
# still needs only Docker, Compose and Make/Git Bash.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "$(dirname "${BASH_SOURCE[0]}")/lib/engine.sh"

[[ $# -eq 1 && "$1" == "token" ]] || die "usage: scim.sh token"
run_engine scim_cli.py token
