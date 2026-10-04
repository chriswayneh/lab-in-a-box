#!/usr/bin/env bash
# Exercise the real launcher and Compose parser without starting lab services.
set -euo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
source "${LAB_SCRIPT_DIR}/lib/engine.sh"
original_root="$LAB_ROOT"
fixture="$(mktemp -d "${TMPDIR:-/tmp}/lab-engine-config.XXXXXXXX")"
cleanup() {
  case "$fixture" in */lab-engine-config.*) rm -rf -- "$fixture" ;; esac
}
trap cleanup EXIT
git -C "$original_root" archive HEAD | tar -x -C "$fixture"
cp "$original_root/scripts/lib/engine.sh" "$fixture/scripts/lib/engine.sh"
LAB_ROOT="$fixture"
# Only service readiness is bypassed; both Docker invocations remain real.
engine_preflight() { printf 'none'; }
unset KEYCLOAK_ADMIN KEYCLOAK_ADMIN_PASSWORD KEYCLOAK_REALM
unset DEMO_USER_PASSWORD GITEA_ADMIN_USER GITEA_ADMIN_PASSWORD
unset VAULT_DEV_ROOT_TOKEN KEYCLOAK_DB_PASSWORD
cat >"$fixture/scripts/identity/launcher_probe.py" <<'PY'
import json
import os
from pathlib import Path

expected = json.loads(Path("/artifacts/expected.json").read_text())
for name, value in expected.items():
    if os.environ.get(name) != value:
        raise SystemExit("Launcher configuration mismatch: " + name)
print("authentication settings match Compose")
PY
mkdir -p "$fixture/artifacts"
host_fixture="$(engine_host_path "$fixture")"
case_name=""
docker() {
  if [[ "${1:-}" == run ]]; then
    local arg
    for arg in "$@"; do
      case "$arg" in
        KEYCLOAK_ADMIN_PASSWORD=*|GITEA_ADMIN_PASSWORD=*|VAULT_TOKEN=*|DEMO_USER_PASSWORD=*)
          die "authentication value was exposed in Docker arguments" ;;
      esac
    done
  fi
  command docker "$@"
}
check_case() {
  case_name="$1"
  compose config --format json | MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
    docker run --rm -i --network none \
    -v "$host_fixture/artifacts:/artifacts" "$LAB_ENGINE_IMAGE" python3 -c '
import json, sys
from pathlib import Path
s = json.load(sys.stdin)["services"]
kc = s["keycloak-init"]["environment"]
gt = s["gitea-init"]["environment"]
expected = {
    "KEYCLOAK_ADMIN": kc["KC_ADMIN"],
    "KEYCLOAK_ADMIN_PASSWORD": kc["KC_ADMIN_PASSWORD"],
    "KEYCLOAK_REALM": kc["KC_REALM"],
    "DEMO_USER_PASSWORD": kc["DEMO_USER_PASSWORD"],
    "GITEA_ADMIN_USER": gt["GITEA_ADMIN_USER"],
    "GITEA_ADMIN_PASSWORD": gt["GITEA_ADMIN_PASSWORD"],
    "VAULT_TOKEN": s["vault-init"]["environment"]["VAULT_TOKEN"],
    "KEYCLOAK_DB_PASSWORD": s["keycloak"]["environment"]["KC_DB_PASSWORD"],
}
Path("/artifacts/expected.json").write_text(json.dumps(expected))
'
  run_engine launcher_probe.py >/dev/null
  printf 'PASS: %s\n' "$case_name"
}
check_case "absent .env"
: >"$fixture/.env"
check_case "empty .env"
cat >"$fixture/.env" <<'ENV'
KEYCLOAK_ADMIN=fixture-admin
KEYCLOAK_ADMIN_PASSWORD='quoted $literal value'
KEYCLOAK_REALM=fixture-realm
DEMO_USER_PASSWORD='FixtureDemo1!'
GITEA_ADMIN_USER=fixture-gitea
GITEA_ADMIN_PASSWORD='fixture $literal gitea'
VAULT_DEV_ROOT_TOKEN='fixture $literal vault'
KEYCLOAK_DB_PASSWORD=fixture-db
ENV
check_case "present .env with Compose quoting"
export KEYCLOAK_ADMIN_PASSWORD='explicit $literal override'
export GITEA_ADMIN_PASSWORD='explicit gitea override'
export VAULT_DEV_ROOT_TOKEN='explicit vault override'
check_case "shell overrides .env"
export KEYCLOAK_ADMIN_PASSWORD=''
export GITEA_ADMIN_PASSWORD=''
export VAULT_DEV_ROOT_TOKEN=''
check_case "empty shell overrides use Compose defaults"
