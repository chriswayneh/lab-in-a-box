# AI stack

Ollama and Open WebUI, running entirely on your machine. No API key, no account, no data leaving the host
after the initial model download.

- [How it fits together](#how-it-fits-together)
- [Models](#models)
- [Hardware and speed](#hardware-and-speed)
- [GPU acceleration](#gpu-acceleration)
- [Using the API directly](#using-the-api-directly)
- [Retrieval-augmented generation](#retrieval-augmented-generation)
- [Using PostgreSQL for Open WebUI](#using-postgresql-for-open-webui)
- [Single sign-on](#single-sign-on)
- [Privacy](#privacy)

See [`ai-prompts.md`](ai-prompts.md) for the prompt library.

---

## How it fits together

```text
Browser ──443──▶ Traefik ──▶ Open WebUI ──▶ Ollama ──▶ model weights
                                  │                    (ollama_models volume)
                                  └──▶ Qdrant  (optional, for retrieval)
```

Both services sit on `lab_ai`. Open WebUI is also on `lab_edge` so Traefik can route it. Neither has any
path to `lab_data` — the chat interface, which processes untrusted input by definition, cannot reach the
identity provider's database.

`ollama-init` runs once on first boot, pulls the configured models and exits. It is a remote control, not
a second daemon: it uses the Ollama CLI over HTTP against the server container, so it needs no GPU, no
model storage and no privileges.

---

## Models

The default is deliberately small:

```bash
OLLAMA_DEFAULT_MODELS=llama3.2:1b     # ~1.3 GB
```

A 1B model is not going to impress anyone with its reasoning, but it downloads in a couple of minutes on
an ordinary connection and runs acceptably on CPU. The alternative — defaulting to something genuinely
capable — means a 20 GB download standing between a new user and a working lab.

Swap it for something better as soon as you have the disk and the RAM:

| Model | Size | RAM (CPU) | Good for |
| --- | --- | --- | --- |
| `llama3.2:1b` | 1.3 GB | ~2 GB | The default. Summaries, simple questions |
| `llama3.2:3b` | 2.0 GB | ~4 GB | Noticeably better, still fast on CPU |
| `mistral:7b` | 4.1 GB | ~8 GB | Strong general reasoning |
| `llama3.1:8b` | 4.7 GB | ~10 GB | Best all-rounder in this range |
| `gemma2:9b` | 5.4 GB | ~11 GB | Good at structured output |
| `qwen2.5-coder:7b` | 4.7 GB | ~8 GB | Code and configuration review |

```bash
# .env
OLLAMA_DEFAULT_MODELS=llama3.1:8b,qwen2.5-coder:7b
```

```bash
docker compose up -d ollama-init          # pulls anything missing
docker compose logs -f ollama-init        # watch it
```

Or pull one directly without touching `.env`:

```bash
docker compose exec ollama ollama pull mistral:7b
docker compose exec ollama ollama list
docker compose exec ollama ollama rm llama3.2:1b     # reclaim disk
```

The full library is at [ollama.com/library](https://ollama.com/library).

---

## Hardware and speed

CPU inference is the default and works everywhere. It is slow, and how slow depends almost entirely on
memory bandwidth rather than core count.

Rough expectations for a 7–8B model on CPU: single-digit tokens per second. Enough for a paragraph of
summary, frustrating for a long conversation. A 1–3B model is several times faster.

Two settings control the memory/latency trade-off:

```yaml
OLLAMA_KEEP_ALIVE: 5m          # how long a model stays resident after last use
OLLAMA_MAX_LOADED_MODELS: 1    # how many can be resident at once
```

Loading a model costs seconds; keeping it warm costs gigabytes of RAM. Five minutes and one model is the
right compromise on a machine that is also running twenty-three other containers. With a GPU, the
trade-off inverts — the GPU override raises both.

---

## GPU acceleration

```bash
make up GPU=1
```

Requires an NVIDIA GPU, a current driver, and the
[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html).

Verify the toolkit before blaming the lab:

```bash
docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi
```

Then confirm Ollama sees it:

```bash
docker compose logs ollama | grep -i "gpu\|cuda"
```

`compose/overrides/gpu.yml` also raises `OLLAMA_KEEP_ALIVE` to 30 minutes and allows two loaded models,
because on a GPU the expensive part is loading, not holding.

AMD ROCm and Apple Metal are not wired up. Metal in particular cannot work here — Docker on macOS runs
Linux containers in a VM with no access to the Metal API. On an Apple Silicon machine, run Ollama
natively and point Open WebUI at it via `OLLAMA_BASE_URL=http://host.docker.internal:11434`.

---

## Using the API directly

Ollama's API is routed at `ollama.lab.localhost` and speaks both its own protocol and an
OpenAI-compatible one.

```bash
curl -sk https://ollama.lab.localhost/api/generate -d '{
  "model": "llama3.2:1b",
  "prompt": "Explain what a reverse proxy does, in two sentences.",
  "stream": false
}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["response"])'
```

OpenAI-compatible, which means most existing SDKs work unchanged:

```bash
curl -sk https://ollama.lab.localhost/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "llama3.2:1b",
    "messages": [{"role": "user", "content": "Summarise these logs."}]
  }'
```

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:11434/v1",   # or https://ollama.lab.localhost/v1
    api_key="not-used",                     # required by the SDK, ignored by Ollama
)

response = client.chat.completions.create(
    model="llama3.2:1b",
    messages=[{"role": "user", "content": "What does this Compose file do?"}],
)
```

Note that the Ollama route has compression middleware deliberately omitted — buffering a response to
compress it destroys token streaming.

---

## Retrieval-augmented generation

Open WebUI can answer questions about documents you upload. By default it uses an embedded Chroma store,
which needs nothing else running and is enough to try the idea.

For something more capable, enable Qdrant:

```bash
# .env
COMPOSE_PROFILES=qdrant
OPENWEBUI_VECTOR_DB=qdrant
```

```bash
make up
```

Both settings are required. Setting `OPENWEBUI_VECTOR_DB=qdrant` without the profile leaves Open WebUI
trying to reach a container that is not running.

Qdrant's own dashboard is at <https://qdrant.lab.localhost/dashboard>.

Good documents to feed it: your runbooks, this repository's `docs/` directory, your team's architecture
decision records. Retrieval works well for "what did we decide about X" and poorly for questions that
need reasoning across many documents at once.

---

## Using PostgreSQL for Open WebUI

Open WebUI defaults to SQLite in its own volume. The lab's PostgreSQL is right there, and Open WebUI
supports `DATABASE_URL`.

The default stays SQLite because the primary promise is that first boot works everywhere, and adding a
database driver and a migration path to the critical path of the most-demoed service trades reliability
for a talking point.

To switch:

1. Add an `openwebui` database and role to `configs/postgres/init/01-create-service-databases.sh`:

   ```bash
   SERVICES=(
     "keycloak:keycloak:KEYCLOAK_DB_PASSWORD"
     "gitea:gitea:GITEA_DB_PASSWORD"
     "openwebui:openwebui:OPENWEBUI_DB_PASSWORD"
   )
   ```

1. Add `OPENWEBUI_DB_PASSWORD=CHANGEME_OPENWEBUI_DB_PASSWORD` to `.env.example`.

1. In `compose/04-ai.yml`, add `lab_data` to Open WebUI's networks and set:

   ```yaml
   DATABASE_URL: postgresql://openwebui:${OPENWEBUI_DB_PASSWORD}@postgres:5432/openwebui
   ```

1. Recreate from scratch — the init script only runs on an empty data directory:

   ```bash
   make secrets && make clean && make up
   ```

Existing chat history in the SQLite volume is not migrated.

---

## Single sign-on

The Keycloak realm ships an `openwebui` OIDC client, ready to use. Open WebUI's OIDC support is
configured through its own environment variables:

```yaml
ENABLE_OAUTH_SIGNUP: "true"
OAUTH_CLIENT_ID: openwebui
OAUTH_CLIENT_SECRET: ${KEYCLOAK_OPENWEBUI_CLIENT_SECRET}
OPENID_PROVIDER_URL: https://keycloak.lab.localhost/realms/lab/.well-known/openid-configuration
```

You will also need to extend `scripts/init-keycloak.sh` to inject the client secret, following the
pattern already there for Grafana.

This is left unconfigured by default because it makes the first visit to the chat interface a redirect
chain through a self-signed certificate, which is a poor introduction.

---

## Privacy

The AI stack is configured to stay local:

| Setting | Effect |
| --- | --- |
| `ENABLE_OPENAI_API: false` | No outbound calls to a paid endpoint, accidental or otherwise |
| `ENABLE_COMMUNITY_SHARING: false` | No sharing prompts or conversations upstream |
| `ANONYMIZED_TELEMETRY`, `SCARF_NO_ANALYTICS`, `DO_NOT_TRACK` | Telemetry off |
| `QDRANT__TELEMETRY_DISABLED: true` | Same for the vector store |

After the initial model pull, the only outbound traffic from `lab_ai` is whatever you deliberately ask
for — pulling another model, or fetching a URL you paste into a chat.

Conversations are stored in the `openwebui_data` volume and are included in `make backup`.
