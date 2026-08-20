<!--
  GENERATED FILE — do not edit by hand.
  Regenerate with: make docs
  Source of truth: docker-compose.yml and compose/*.yml
-->

# Service catalogue

Every container in the lab, grouped by the compose fragment that defines it. Hostnames assume `LAB_DOMAIN=lab.localhost`.

**28 available services** across **6 layers**. **26 start by default**; `qdrant`, `watchtower` require their optional Compose profiles. Services whose name ends in `-init` are one-shot provisioning jobs: they run once, do their work, and exit. A stopped `-init` container is a success, not a fault.

## Core

_Edge routing, data stores and the landing page_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `traefik` | `traefik:v3.6.17` | `traefik.lab.localhost` | `80→8000`, `443→8443` | edge, socket | ✅ | uid `1000:1000` |
| `socket-proxy` | `tecnativa/docker-socket-proxy:0.2.0` | — | — | socket | ✅ | docker socket (ro) |
| `postgres` | `postgres:17-alpine` | — | `5432→5432` | data | ✅ | — |
| `redis` | `redis:7.4-alpine` | — | `6379→6379` | data | ✅ | uid `redis` |
| `landing` | `nginxinc/nginx-unprivileged:1.27-alpine` | `lab.localhost` | — | edge | ✅ | read-only fs |

## Identity & Secrets

_Who you are, and what you are allowed to know_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `keycloak` | `quay.io/keycloak/keycloak:26.7.1` | `keycloak.lab.localhost` | — | data, edge, observability | ✅ | — |
| `keycloak-init` | `quay.io/keycloak/keycloak:26.7.1` | — | — | edge | n/a | — |
| `vault` | `hashicorp/vault:1.18` | `vault.lab.localhost` | — | edge, observability | ✅ | cap `IPC_LOCK` |
| `vault-init` | `hashicorp/vault:1.18` | — | — | edge | n/a | — |

## Observability

_Metrics, logs and dashboards_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `prometheus` | `prom/prometheus:v3.1.0` | `prometheus.lab.localhost` | — | edge, observability | ✅ | uid `65534:65534` |
| `grafana` | `grafana/grafana:11.5.0` | `grafana.lab.localhost` | — | edge, observability | ✅ | uid `472:472` |
| `loki` | `grafana/loki:3.3.2` | — | — | observability | ✅ | uid `10001:10001` |
| `promtail` | `grafana/promtail:3.3.2` | — | — | observability, socket | ✅ | — |
| `cadvisor` | `gcr.io/cadvisor/cadvisor:v0.49.1` | — | — | observability | ✅ | **privileged** |
| `node-exporter` | `prom/node-exporter:v1.8.2` | — | — | observability | ✅ | — |

## AI

_Local model serving and chat_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `ollama` | `ollama/ollama:latest` | `ollama.lab.localhost` | — | ai, edge | ✅ | — |
| `ollama-init` | `ollama/ollama:latest` | — | — | ai | n/a | — |
| `open-webui` | `ghcr.io/open-webui/open-webui:main` | `chat.lab.localhost` | — | ai, edge | ✅ | — |
| `qdrant` | `qdrant/qdrant:v1.12.5` | `qdrant.lab.localhost` | — | ai, edge | ✅ | — |

## Platform

_Source control and object storage_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `gitea` | `gitea/gitea:1.23` | `git.lab.localhost` | `2222→22` | data, edge | ✅ | — |
| `gitea-init` | `gitea/gitea:1.23` | — | — | data | n/a | uid `1000:1000` |
| `minio` | `minio/minio:latest` | `minio.lab.localhost`, `s3.lab.localhost` | — | edge, observability | ✅ | — |
| `minio-init` | `minio/mc:latest` | — | — | edge | n/a | — |

## Tools

_Operator conveniences_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `portainer` | `portainer/portainer-ce:lts` | `portainer.lab.localhost` | — | edge | ✅ | **docker socket (rw)** |
| `pgadmin` | `dpage/pgadmin4:8.14` | `pgadmin.lab.localhost` | — | data, edge | ✅ | — |
| `adminer` | `adminer:4.8.1` | `adminer.lab.localhost` | — | data, edge | ✅ | — |
| `watchtower` | `containrrr/watchtower:1.7.1` | — | — | edge | ✅ | **docker socket (rw)** |

## Other

_Not yet categorised_

| Service | Image | URL | Host ports | Networks | Health | Privileges |
| --- | --- | --- | --- | --- | :---: | --- |
| `scim-provisioner` | `python:3.12-alpine` | — | — | edge | ✅ | read-only fs |

## Networks

| Network | Isolated | Purpose |
| --- | :---: | --- |
| `lab_ai` | — | Model serving. Needs egress to pull model weights. |
| `lab_data` | 🔒 | PostgreSQL, Redis and their clients. No internet access. |
| `lab_edge` | — | Public-facing. Traefik and everything it routes. |
| `lab_observability` | — | Scrape targets, log shippers and their backends. |
| `lab_socket` | 🔒 | Read-only Docker API, via the socket proxy. |

## Volumes

17 named volumes hold all persistent state: `gitea_data`, `grafana_data`, `keycloak_data`, `loki_data`, `minio_data`, `ollama_models`, `openwebui_data`, `pgadmin_data`, `portainer_data`, `postgres_data`, `prometheus_data`, `promtail_positions`, `qdrant_data`, `redis_data`, `scim_state`, `vault_data`, `vault_logs`.

`make down` preserves every one of them. Only `make clean` removes them, and it asks first.
