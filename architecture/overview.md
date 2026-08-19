# System overview

Hand-maintained diagrams. For the generated startup graph see
[`dependency-graph.mmd`](dependency-graph.mmd); for the reasoning see
[`docs/architecture.md`](../docs/architecture.md).

---

## The whole lab

```text
                                    ┌──────────┐
                                    │ Browser  │
                                    └────┬─────┘
                                         │ :80 / :443
╔════════════════════════════════════════▼═══════════════════════════════════════╗
║                                   TRAEFIK                                      ║
║           service discovery · TLS · rate limiting · security headers           ║
╚══╤═════════╤═════════╤═════════╤═════════╤═════════╤═════════╤═════════╤═══════╝
   │         │         │         │         │         │         │         │
   ▼         ▼         ▼         ▼         ▼         ▼         ▼         ▼
┌──────┐ ┌────────┐ ┌──────┐ ┌────────┐ ┌────────┐ ┌──────┐ ┌───────┐ ┌────────┐
│Landing│ │Keycloak│ │Vault │ │Grafana │ │OpenWebUI│ │Gitea │ │ MinIO │ │Portainer│
│ page  │ │  IdP   │ │      │ │        │ │  chat   │ │      │ │       │ │pgAdmin │
└──────┘ └───┬────┘ └──────┘ └───┬────┘ └────┬────┘ └──┬───┘ └───┬───┘ └───┬────┘
             │                   │           │         │         │         │
             │ lab_data          │ lab_obs   │ lab_ai  │ lab_data│         │
             ▼                   ▼           ▼         ▼         ▼         ▼
 ┌───────────────────┐  ┌────────────────┐ ┌────────┐ ┌───────────────┐ ┌──────────┐
 │   PostgreSQL      │  │   Prometheus   │ │ Ollama │ │     Redis     │ │  Docker  │
 │  lab · keycloak   │  │      Loki      │ │  LLM   │ │ cache/session │ │  socket  │
 │      gitea        │  │                │ └────────┘ │               │ │  (rw) ⚠  │
 │  ── isolated ──   │  └───────┬────────┘            │ ── isolated ──│ └──────────┘
 └───────────────────┘          │                     └───────────────┘
                                │ scrape / collect
                    ┌───────────┴────────────┐
                    ▼                        ▼
             ┌─────────────┐         ┌──────────────┐
             │  cAdvisor   │         │   Promtail   │
             │node-exporter│         │              │
             └─────────────┘         └──────┬───────┘
                                            │ lab_socket
                                     ┌──────▼───────┐
                                     │ Socket proxy │
                                     │  read-only   │
                                     └──────────────┘
```

---

## Network segmentation

```text
┌─────────────────────────────────────────────────────────────────────┐
│ lab_edge                                          egress: yes       │
│ traefik landing keycloak vault grafana prometheus open-webui        │
│ ollama gitea minio portainer pgadmin adminer qdrant                 │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ lab_data                        🔒 internal      egress: NONE       │
│ postgres  redis                                                     │
│ clients: keycloak  gitea  pgadmin  adminer  gitea-init              │
│                                                                     │
│ Open WebUI is deliberately absent. The service that processes       │
│ untrusted input has no network path to any database.                │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ lab_observability                                 egress: yes       │
│ prometheus grafana loki promtail cadvisor node-exporter             │
│ scrape targets: keycloak  vault  minio  traefik                     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ lab_ai                                            egress: yes       │
│ ollama  ollama-init  open-webui  qdrant                             │
│ Egress is required: model weights are pulled from the registry.     │
└─────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────┐
│ lab_socket                      🔒 internal      egress: NONE       │
│ socket-proxy  traefik  promtail                                     │
│ Read-only Docker API. POST denied: no container can be created.     │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Request path

```text
  Browser
     │
     │  1. DNS:  *.lab.localhost → 127.0.0.1
     │           RFC 6761 §6.3: no hosts file, no resolver config
     ▼
  Host :443
     │
     │  2. Port mapping 443 → 8443
     │     Traefik runs as UID 1000 and cannot bind a low port
     ▼
  Traefik
     │
     │  3. TLS: self-signed if no certificate matches the SNI
     │           TLS 1.2 minimum, modern cipher suites
     │
     │  4. Router match: Host(`grafana.lab.localhost`)
     │           discovered from a label on the container itself,
     │           read through the read-only socket proxy
     │
     │  5. Middleware chain
     │           lab-security-headers@file   nosniff, frame-deny, HSTS, referrer
     │           lab-compress@file           except on streaming routes
     │           lab-ratelimit@docker        100/s average, 200 burst, per IP
     ▼
  grafana:3000    over lab_edge
```

---

## Provisioning sequence

```text
 t=0    docker compose up -d
        │
        ├─ socket-proxy ──────────────▶ traefik
        │
        ├─ postgres ─── healthy ──┬──▶ keycloak ─── healthy ──▶ keycloak-init ─▶ exit 0
        │  (runs init SQL:         │                              · demo passwords
        │   3 databases,           │                              · client secrets
        │   3 roles)               │                              · redirect URIs
        │                          │
        ├─ redis ─── healthy ──────┴──▶ gitea ────── healthy ──▶ gitea-init ───▶ exit 0
        │                                                          · administrator
        │
        ├─ vault ─── healthy ────────────────────────────────────▶ vault-init ──▶ exit 0
        │                                                          · audit device
        │                                                          · KV secrets
        │                                                          · ACL policies
        │                                                          · AppRole, userpass
        │                                                          · transit key
        │
        ├─ minio ─── healthy ────────────────────────────────────▶ minio-init ──▶ exit 0
        │                                                          · buckets
        │                                                          · policies
        │                                                          · lifecycle rules
        │
        ├─ ollama ── healthy ────────────────────────────────────▶ ollama-init ─▶ exit 0
        │                          └────────────────────────────▶ open-webui     · model pull
        │
        └─ cadvisor ─▶ prometheus ─┬─▶ grafana
           loki ─────▶ promtail    │
           loki ───────────────────┘

 Every arrow labelled "healthy" waits on a Docker healthcheck, not a sleep.
```

An `-init` container that shows as `Exited (0)` has succeeded. That is the intended end state.

---

## State

```text
 Persistent (16 named volumes)          Backed up      Notes
 ────────────────────────────────────   ───────────    ─────────────────────────
 postgres_data                          ✅ pg_dumpall  logical dump, not a file copy
 redis_data                             ✅
 keycloak_data                          ✅
 vault_data · vault_logs                ✅             dev mode: memory anyway
 grafana_data                           ✅
 gitea_data                             ✅
 minio_data                             ✅
 openwebui_data                         ✅             chat history
 portainer_data · pgadmin_data          ✅
 prometheus_data                        ❌             stale on restore
 loki_data · promtail_positions         ❌             stale on restore
 ollama_models                          ❌             gigabytes, re-downloadable
 qdrant_data                            ❌             rebuilt from source documents

 make down   → keeps everything
 make clean  → deletes everything, after typing "delete"
```
