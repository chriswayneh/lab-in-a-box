#!/bin/sh
# =============================================================================
# Ollama model provisioning
# =============================================================================
# Pulls the models listed in OLLAMA_DEFAULT_MODELS so that Open WebUI has
# something to talk to the first time it is opened. Without this the chat UI
# comes up empty and the first-run experience is a model picker with no models.
#
# Runs inside the Ollama image, which ships the CLI. The CLI talks to the
# server over HTTP via $OLLAMA_HOST, so this container needs no GPU, no model
# storage and no privileges — it is a remote control, not a second daemon.
#
# Idempotent: a model already present is skipped, so restarting the lab does
# not re-download several gigabytes.
# =============================================================================
set -eu

OLLAMA_HOST="${OLLAMA_HOST:-http://ollama:11434}"
export OLLAMA_HOST
MODELS="${OLLAMA_DEFAULT_MODELS:-llama3.2:1b}"

log()  { printf '[ollama-init] %s\n' "$*"; }
warn() { printf '[ollama-init] WARNING: %s\n' "$*" >&2; }

# -----------------------------------------------------------------------------
# Wait for the server. Compose gates this container on Ollama being healthy,
# but the healthcheck passes the moment the API answers, which can be slightly
# before it is ready to accept a pull.
# -----------------------------------------------------------------------------
wait_for_ollama() {
  attempt=1
  while [ "$attempt" -le 60 ]; do
    if ollama list >/dev/null 2>&1; then
      log "connected to Ollama at ${OLLAMA_HOST}"
      return 0
    fi
    log "waiting for Ollama (attempt ${attempt}/60)"
    sleep 5
    attempt=$((attempt + 1))
  done
  warn "Ollama did not become reachable at ${OLLAMA_HOST}; no models were pulled."
  warn "Pull one by hand once it is up:  docker compose exec ollama ollama pull llama3.2:1b"
  exit 0
}

model_present() {
  # `ollama list` prints a table; the first column is the model tag. Matching
  # on the exact first field avoids "llama3.2:1b" matching "llama3.2:1b-extra".
  ollama list 2>/dev/null | awk 'NR > 1 { print $1 }' | grep -qx "$1"
}

pull_model() {
  model="$1"

  if model_present "$model"; then
    log "'${model}' is already present; skipping"
    return 0
  fi

  log "pulling '${model}' — this is the slow part of first boot"
  if ollama pull "$model"; then
    log "'${model}' ready"
  else
    # One bad entry in the list should not stop the others from downloading.
    warn "could not pull '${model}'. Check the name against https://ollama.com/library"
  fi
}

main() {
  wait_for_ollama

  # Split the comma-separated list without relying on bash arrays.
  echo "$MODELS" | tr ',' '\n' | while IFS= read -r model; do
    model="$(echo "$model" | tr -d '[:space:]')"
    [ -z "$model" ] && continue
    pull_model "$model"
  done

  log "model provisioning complete. Available models:"
  ollama list 2>/dev/null || true
}

main "$@"
