# Architecture

How the lab is put together, and why it is put together that way.

- [The one-command constraint](#the-one-command-constraint)
- [Compose structure](#compose-structure)
- [Networks](#networks)
- [The request path](#the-request-path)
- [Provisioning model](#provisioning-model)
- [Startup ordering](#startup-ordering)
- [State and persistence](#state-and-persistence)
- [Configuration flow](#configuration-flow)
- [Decisions and trade-offs](#decisions-and-trade-offs)

---

## The one-command constraint

Every design decision in this repository was made under one rule:

> `docker compose up -d` must produce a fully working lab, with no prior step and no manual configuration.

That constraint is more demanding than it sounds, and it rules out a lot of otherwise reasonable designs.

It means no `terraform apply` first. No "log in and click Add Datasource". No editing a config file to
insert a password. No `docker exec` to create the admin user. And critically: **no step that runs before
Compose parses the project**, because a user typing the documented command will not run it.

That last point is what shapes the credential model. Compose resolves environment variables and secret
file paths at *parse* time, before any container exists. Nothing running inside the lab can generate a
credential the lab itself needs to start. So the lab ships with fallback development values baked into
`${VAR:-default}` expressions. `make secrets`, which is what the README recommends, writes
random values before Compose runs, with file-backed values kept in `secrets/local/`.

The trade-off is stated plainly rather than hidden: the fallback passwords are in the repository, and
`make creds` prints them in red until they are replaced.

---

## Compose structure

```text
docker-compose.yml            networks, volumes, secrets, and six includes
│
├── compose/01-core.yml           Traefik, socket proxy, PostgreSQL, Redis, landing page
├── compose/02-iam.yml            Keycloak, Vault, + provisioning jobs
├── compose/03-observability.yml  Prometheus, Grafana, Loki, Promtail, cAdvisor, node-exporter
├── compose/04-ai.yml             Ollama, Open WebUI, Qdrant
├── compose/05-platform.yml       Gitea, MinIO, + provisioning jobs
├── compose/06-tools.yml          Portainer, pgAdmin, Adminer, Watchtower
│
└── compose/overrides/gpu.yml     opt-in NVIDIA passthrough
```

The root file uses the Compose Spec [`include:`](https://docs.docker.com/compose/how-tos/multiple-compose-files/include/)
directive. This is **not** the same as multiple `-f` flags: the fragments merge into a single project with
one network namespace, one dependency graph and one `up` command. `docker compose ps` shows all
28 available services (26 by default); `depends_on` works across fragment boundaries.

Each include sets `project_directory: .` so that a relative path inside a fragment resolves from the
repository root. Without it, Compose would resolve `./configs/...` relative to `compose/`, and every
volume mount would point somewhere that does not exist.

Shared declarations (networks, volumes and secrets) live in the root file only. They are the contract
between fragments: a service in `04-ai.yml` can join `lab_data` without that file knowing how `lab_data`
is defined.

### Why not one file?

A single file containing all 28 services would be around 1,500 lines. That is unreviewable in a pull
request and unmergeable when two people touch it at once. The split is by *responsibility*, so a change
to the AI stack touches one file and conflicts with nothing else.

The cost is that YAML anchors do not cross file boundaries, so each fragment repeats a small
`x-defaults` block. Six copies of eight lines is a price worth paying for six independently readable files.

---

## Networks

Five networks. A service can reach only what it shares one with.

| Network | `internal` | Members |
| --- | :---: | --- |
| `lab_edge` | no | Traefik plus every service with an HTTP route |
| `lab_data` | **yes** | PostgreSQL, Redis, and their clients |
| `lab_observability` | no | Prometheus, Grafana, Loki, Promtail, exporters, scrape targets |
| `lab_ai` | no | Ollama, Open WebUI, Qdrant |
| `lab_socket` | **yes** | Socket proxy, Traefik, Promtail |

`internal: true` severs the network from the host and the internet entirely. Containers on it can talk to
each other and to nothing else. PostgreSQL has no outbound route. The route is absent, not filtered.

Services join only the networks they use. Keycloak is on `lab_edge` (to be routed), `lab_data` (to reach
PostgreSQL) and `lab_observability` (to be scraped), and nothing else. Open WebUI is on `lab_ai` and
`lab_edge`, so it has no path to the database at all.

```text
                          ┌──────────────┐
     Browser ────443────▶ │   Traefik    │
                          └──────┬───────┘
                                 │  lab_edge
        ┌────────────┬───────────┼───────────┬────────────┐
        ▼            ▼           ▼           ▼            ▼
   ┌─────────┐  ┌─────────┐ ┌─────────┐ ┌─────────┐  ┌─────────┐
   │Keycloak │  │  Vault  │ │ Grafana │ │OpenWebUI│  │  Gitea  │
   └────┬────┘  └─────────┘ └────┬────┘ └────┬────┘  └────┬────┘
        │                        │           │            │
        │ lab_data               │ lab_obs   │ lab_ai     │ lab_data
        ▼                        ▼           ▼            ▼
   ┌─────────────────┐   ┌──────────────┐ ┌────────┐ ┌─────────┐
   │   PostgreSQL    │   │  Prometheus  │ │ Ollama │ │  Redis  │
   │    (isolated)   │   │     Loki     │ └────────┘ │(isolated)│
   └─────────────────┘   └──────────────┘            └─────────┘
```

---

## The request path

What happens when you open `https://grafana.lab.localhost`:

1. **DNS.** `*.lab.localhost` resolves to `127.0.0.1` with no configuration, under
   [RFC 6761 §6.3](https://www.rfc-editor.org/rfc/rfc6761#section-6.3). Browsers and resolvers treat
   `.localhost` as loopback by definition. This is why the lab needs no `hosts` file entry, a detail
   that removes the single most common setup failure in projects like this.

1. **Host port.** Traefik publishes `443 → 8443`. The container binds 8443 rather than 443 because it
   runs as UID 1000, and an unprivileged process cannot bind a port below 1024. The port mapping does
   the translation instead of granting `CAP_NET_BIND_SERVICE`.

1. **TLS.** No certificate matches the SNI, so Traefik generates and serves a self-signed one. HTTPS
   works out of the box; the browser warns. TLS 1.2 minimum, with a modern cipher suite list from
   `configs/traefik/dynamic/tls.yml`.

1. **Routing.** Traefik matched `Host(\`grafana.lab.localhost\`)` from a *label on the Grafana
   container*, discovered through the Docker API. Adding a service means adding labels. There is no
   central routing table to update.

1. **Middleware.** The request passes `lab-default@file` (security headers, compression) and
   `lab-ratelimit@docker` (100 req/s average, 200 burst, per client IP).

1. **Backend.** Traefik forwards to `grafana:3000` over `lab_edge`.

### Why static config is CLI flags and dynamic config is a file

Traefik treats its three static-configuration sources (file, CLI flags, environment variables) as
mutually exclusive. Only one is used.

The lab needs `${LAB_DOMAIN}` and `${TRAEFIK_LOG_LEVEL}` from `.env`, and a static `traefik.yml` cannot
interpolate them. So static configuration is CLI flags in `compose/01-core.yml`, where Compose does the
interpolation.

Dynamic configuration (middlewares, TLS options) *is* a file, in `configs/traefik/dynamic/`, watched and
hot-reloaded. The one exception is the rate-limit middleware, whose thresholds come from `.env`: the file
provider does not interpolate environment variables, so it is defined as a label on the Traefik container
instead, where Compose can. It is referenced as `lab-ratelimit@docker` rather than `@file` for exactly
that reason.

---

## Provisioning model

Six services have a companion `-init` container. They are ordinary Compose services with three
properties:

```yaml
restart: "no"                    # a clean exit is success, not a crash
depends_on:
  service: { condition: service_healthy }
entrypoint: ["/bin/sh", "/scripts/init-service.sh"]
```

They start as part of the normal `up`, wait for their dependency to be *healthy* (not merely started),
do their work, and exit. `docker compose ps` shows them as `Exited (0)`, which is the desired state.

| Job | Does |
| --- | --- |
| `keycloak-init` | Sets demo user passwords, injects the Grafana client secret, rewrites redirect URIs if `LAB_DOMAIN` is not the default |
| `vault-init` | Enables the audit device, seeds KV secrets, loads ACL policies, configures AppRole and userpass, creates a transit key |
| `minio-init` | Creates buckets, disables anonymous access, enables versioning and lifecycle rules, creates a least-privilege policy |
| `gitea-init` | Creates the first administrator |
| `ollama-init` | Pulls the configured models |

Every one is **idempotent**. Running the lab a second time re-runs them and converges to the same state
rather than failing on "already exists".

### Why not use each tool's native mechanism?

Where a native mechanism exists, the lab uses it:

- Keycloak's realm *structure* is imported by `--import-realm` from a JSON file, not scripted
- Grafana's datasources and dashboards are provisioned from files, not POSTed to its API
- Prometheus, Loki and Promtail are configured entirely by mounted files
- PostgreSQL's databases and roles are created by `docker-entrypoint-initdb.d`

Scripts handle only what those mechanisms cannot: injecting a secret that must not be committed, and
adapting to a `LAB_DOMAIN` that is not known when the file was written.

That split is deliberate. A declarative file that the tool itself reads is easier to review, cannot drift,
and does not need a retry loop.

---

## Startup ordering

`depends_on` with `condition: service_healthy` is what makes the ordering real. Waiting on
`service_started` only guarantees the process launched; waiting on a healthcheck guarantees it is
*answering*.

This matters most for Keycloak, which needs 30–90 seconds on first boot to run its database migrations
and import the realm. Anything that starts talking to it before then gets connection refused. The
alternative, a `sleep 60` in an entrypoint, is both slower on a fast machine and unreliable on a slow one.

Every long-running service defines a healthcheck, and CI fails the build if one is added without.
Writing them was not uniform, because several images ship no shell and no HTTP client:

| Service | Probe | Why |
| --- | --- | --- |
| Keycloak | `bash` + `/dev/tcp` speaking HTTP by hand | `ubi9-micro` base: no curl, no wget |
| Traefik | `traefik healthcheck` | Built from scratch; its own binary is the only tool present |
| Portainer | `/portainer --version` | No shell at all. Verifies the binary runs, not that the API serves. A known limitation |
| Adminer | `php -r 'fsockopen(...)'` | Has a shell but no curl or wget; PHP is guaranteed present |
| Ollama | `ollama list` | Its CLI talks to the local API |
| Watchtower | `/watchtower --health-check` | Mirrors the image's own built-in check |

These were verified by inspecting each image, not assumed.

---

## State and persistence

Sixteen named volumes hold every piece of persistent state. No bind mounts for data, only for
read-only configuration.

`make down` stops the lab and keeps all of it. Only `make clean` removes volumes, and it requires typing
`delete` to confirm.

### One database engine, three databases

PostgreSQL runs once and hosts three logical databases, each with its own role:

```text
postgres
├── lab        POSTGRES_USER      general purpose
├── keycloak   role: keycloak     NOCREATEDB NOCREATEROLE NOSUPERUSER
└── gitea      role: gitea        NOCREATEDB NOCREATEROLE NOSUPERUSER
```

Created by `configs/postgres/init/01-create-service-databases.sh` on first boot. The script also revokes
the permissive default that lets any role create objects in `public`, and creates a read-only role for
exploratory queries from pgAdmin.

One engine instead of three saves roughly 400 MB of RAM and two more things to keep patched. Separate
roles preserve the isolation that separate engines would have given.

---

## Configuration flow

```text
.env.example                    annotated template, committed
     │  make secrets
     ▼
.env                            generated, git-ignored
     │  interpolation at parse time
     ▼
docker-compose.yml ──▶ compose/*.yml ──▶ container environment
     │
     ├──▶ Traefik CLI flags        (LAB_DOMAIN, rate limits, log level)
     ├──▶ service environment      (database URLs, admin users, feature flags)
     └──▶ config file expansion    (Loki and Promtail, via -config.expand-env=true)

secrets/*.txt                   tracked development defaults
secrets/local/*.txt             generated, git-ignored credentials
     │ selected by LAB_SECRETS_DIR
     ▼
/run/secrets/<name> ──────────▶ POSTGRES_PASSWORD_FILE
                                  MINIO_ROOT_PASSWORD_FILE
                                  GF_SECURITY_ADMIN_PASSWORD__FILE
```

Two config files needed values from `.env` but are read by tools that do not interpolate: Loki and
Promtail. Both accept `-config.expand-env=true`, which is why `${LOKI_RETENTION_PERIOD}` inside
`loki-config.yml` resolves. Prometheus has no equivalent flag, so everything in `prometheus.yml` is
literal and anything tunable is a command-line argument instead.

---

## Decisions and trade-offs

Decisions where a reasonable engineer would have chosen differently, and the reasoning.

### Docker socket proxy instead of mounting the socket

Traefik needs the Docker API for service discovery. Mounting `/var/run/docker.sock`, which nearly every
Traefik example does, grants root on the host: anything that can reach that socket can start a
privileged container that mounts `/`.

`tecnativa/docker-socket-proxy` sits in front, on an `internal` network, allowing only the endpoints the
discovery loop calls, with `POST: 0`. A fully compromised Traefik can enumerate containers and nothing else.

Portainer and Watchtower still get the real socket, because they genuinely need to create and destroy
containers. That is a real, documented risk rather than an accidental one.

### SQLite for Open WebUI, not the lab's PostgreSQL

Open WebUI supports `DATABASE_URL`, PostgreSQL is already running, and wiring them together would be a
better story for "everything connects".

It stays on SQLite because the primary promise is that first boot works everywhere. Adding a driver and a
migration path to the critical path of the most-demoed service trades reliability for a talking point.
`docs/ai.md` documents the switch for anyone who wants it.

### Three images float on a moving tag

Every image is pinned except Ollama, Open WebUI and MinIO.

Ollama's runtime and the model formats it can load move in lockstep; pinning a six-month-old runtime
against a model published last week is the most common way this stack breaks. Open WebUI publishes
`:main` as its release channel. MinIO publishes date-stamped tags with no stable minor to track.

They are listed in `.trivyignore` with this reasoning, and CI still fails on any image with *no* tag.
an implicit `:latest` is unreproducible without saying so, which is worse than an explicit one.

### HTTPS redirect as a dynamic router

Forcing HTTP → HTTPS is normally an entrypoint-level redirect, which is *static* configuration,
toggling it would mean restarting Traefik and duplicating its entire flag list in an override file.

Instead, `make https-on` copies a catch-all router with a `redirectScheme` middleware into the watched
dynamic directory. Hot-reloaded, no restart, no duplication. The redirect is temporary (307) rather than
permanent (308) on purpose: a permanent redirect is cached by the browser almost forever, and on
`localhost` that cache outlives the lab.

### Promtail reads the Docker API, not host paths

The usual Promtail setup tails `/var/lib/docker/containers`. That path exists on a Linux host but lives
inside a VM on Docker Desktop for macOS and Windows, where the bind mount either fails or silently
collects nothing.

Using `docker_sd_configs` against the socket proxy works identically on all three platforms, and reuses
the read-only proxy rather than adding a second socket mount.

---

## See also

- [`docs/SERVICES.md`](SERVICES.md): generated catalogue of every service, network and volume
- [`docs/security.md`](security.md): threat model and trust boundaries
- [`docs/observability.md`](observability.md): the metrics and logs pipeline
- [`architecture/dependency-graph.mmd`](../architecture/dependency-graph.mmd): generated startup graph
