#!/usr/bin/env bash
# Re-record docs/assets/demo.gif from cheap offline Lab-in-a-Box checks (no full stack).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/lab-in-a-box-demo-XXXXXX")"
VENV="$STAGE/venv"
WORK="$STAGE/work"

cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

command -v vhs >/dev/null || { echo "vhs is required" >&2; exit 1; }
command -v ttyd >/dev/null || { echo "ttyd is required by vhs" >&2; exit 1; }
command -v make >/dev/null || { echo "make is required" >&2; exit 1; }
command -v docker >/dev/null || { echo "docker CLI is required for compose config" >&2; exit 1; }

mkdir -p "$WORK"
cp "$ROOT/scripts/demo.tape" "$WORK/demo.tape"

python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install -q 'pytest==9.0.2'

BIN="$STAGE/bin"
mkdir -p "$BIN"
ln -sf "$(command -v make)" "$BIN/make"
ln -sf "$(command -v docker)" "$BIN/docker"
ln -sf "$(command -v sed)" "$BIN/sed"
ln -sf "$VENV/bin/python" "$BIN/python"
ln -sf "$VENV/bin/python" "$BIN/python3"

export LAB_BIN_DIR="$BIN:$VENV/bin"
export LAB_DEMO_HOME="$ROOT"
export PATH="$LAB_BIN_DIR:$PATH"

unset AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AZURE_CLIENT_SECRET GOOGLE_CREDENTIALS || true
make -C "$ROOT" help >/dev/null
docker compose -f "$ROOT/docker-compose.yml" config --quiet
( cd "$ROOT" && python3 -m pytest -q -p no:cacheprovider scripts/identity/unit >/dev/null )

( cd "$WORK" && vhs demo.tape )

mkdir -p "$ROOT/docs/assets"
cp "$WORK/demo.gif" "$ROOT/docs/assets/demo.gif"
echo "Wrote $ROOT/docs/assets/demo.gif"
