# Security model

What this lab defends against, what it does not, and where the line is drawn.

> **Warning**
> Lab-in-a-Box is a **development environment designed to run on `localhost`**. It is not hardened for
> internet exposure, and several deliberate choices would be wrong in production. Those choices are
> listed below rather than buried.

- [Threat model](#threat-model)
- [Trust boundaries](#trust-boundaries)
- [Credential handling](#credential-handling)
- [Network segmentation](#network-segmentation)
- [Container hardening](#container-hardening)
- [The Docker socket](#the-docker-socket)
- [Identity and access](#identity-and-access)
- [Secrets management](#secrets-management)
- [Supply chain](#supply-chain)
- [Known gaps](#known-gaps)
- [Hardening for exposure](#hardening-for-exposure)
- [Reporting a vulnerability](#reporting-a-vulnerability)

---

## Threat model

**In scope.** The lab is designed to be resilient against:

- **Lateral movement between services.** A compromised container should not be able to reach services
  it has no legitimate business with.
- **Credential leakage through inspection.** Passwords should not be readable via `docker inspect`,
  the process table, or shell history.
- **Privilege escalation inside a container.** A process should not be able to gain more than it started
  with.
- **Container escape via the Docker API.** A compromised web-facing service should not be able to start
  a privileged container.
- **Accidental credential commits.** A contributor should not be able to push a real secret without CI
  noticing.

**Out of scope.** The lab does *not* attempt to defend against:

- A hostile actor on your local network or with access to your machine
- Malicious container images from the upstream registries it pulls from
- Denial of service beyond basic per-IP rate limiting
- A compromised host kernel or Docker daemon
- Anyone who can read `.env`, which by design contains every credential in the lab

---

## Trust boundaries

```text
┌─────────────────────────────────────────────────────────────────┐
│ HOST                                                            │
│  Docker daemon: root-equivalent. Everything below trusts it.    │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ EDGE                              (lab_edge, reachable)   │  │
│  │  Traefik · Landing · Keycloak · Vault · Grafana ·         │  │
│  │  Open WebUI · Gitea · MinIO · Portainer · pgAdmin          │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ DATA                      (lab_data, internal, no egress) │  │
│  │  PostgreSQL · Redis                                        │  │
│  │  Reachable only from services that explicitly join.        │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │ CONTROL                 (lab_socket, internal, no egress) │  │
│  │  Socket proxy: read-only Docker API, POST denied.         │  │
│  └───────────────────────────────────────────────────────────┘  │
│                                                                 │
│  ⚠ Portainer and Watchtower hold the REAL socket (read/write).  │
│    They sit inside the host trust boundary by necessity.         │
└─────────────────────────────────────────────────────────────────┘

```

The important boundary is between EDGE and DATA. Open WebUI is the most exposed and fastest-moving
component in the lab. Compromising it yields no network path to PostgreSQL, because Open WebUI is not
attached to `lab_data`.

---

## Credential handling

### Three tiers, by capability of the image

| Tier | Mechanism | Used for | Why |
| --- | --- | --- | --- |
| **1** | Docker secret file | PostgreSQL, MinIO, Grafana | Images support `_FILE` natively. The value never enters the environment |
| **2** | Environment variable from `.env` | Keycloak, Gitea, Redis, pgAdmin, Vault | Images have no `_FILE` support. Wrapping them in an entrypoint that reads a file and re-exports it would give up the benefit entirely |
| **3** | Generated in-place | Open WebUI, Portainer admin | Created by the user on first visit; the lab never holds them |

Tier 1 uses each image's documented convention, which is not uniform:

```yaml
POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password        # postgres
MINIO_ROOT_PASSWORD_FILE: /run/secrets/minio_root_password    # minio
GF_SECURITY_ADMIN_PASSWORD__FILE: /run/secrets/...            # grafana: two underscores

```

Tier 2 is a genuine weakness: those values appear in `docker inspect`. It is accepted rather than papered
over. The alternative, a custom entrypoint per service, adds a maintenance burden and a new failure
mode without removing the secret from the process's environment anyway.

### Generation

`make secrets` produces 32 characters of CSPRNG output per credential, from `openssl rand`,
`/dev/urandom` or Python's `secrets` module, whichever is available.

It writes environment-variable credentials to the git-ignored `.env` file and file-backed Docker
credentials to the git-ignored `secrets/local/` directory. The generated `.env` sets
`LAB_SECRETS_DIR=./secrets/local`, so Compose selects the local files automatically.

Generated passwords are guaranteed to contain upper case, lower case and a digit. This is not decorative:
the Keycloak realm enforces a 12-character policy with mixed case and digits, and a non-compliant value
surfaces much later as a confusing provisioning failure rather than an obvious rejection.

### The shipped defaults

The repository contains fallback credentials (`changeme-postgres-insecure-dev-only` and similar) in
the tracked `secrets/*.txt` files and as `${VAR:-default}` expressions in the compose files.

This is a conscious trade. The project's central promise is that `docker compose up -d` works with no
prior step; Compose resolves secret file paths at parse time, so a missing file is a hard error before
any container starts. Shipping placeholders is what makes the promise true.

The mitigations are visibility, not obscurity:

- Every default contains the literal string `insecure-dev-only`
- `make creds` prints them in red with a fix command
- `make secrets` leaves the tracked defaults alone and writes real file-backed credentials to
  git-ignored `secrets/local/`
- `scripts/check-secrets.sh`, CI and the optional pre-commit hook reject a replacement of any tracked
  fallback secret
- CI runs Gitleaks over the full history to catch a *real* credential being committed

---

## Network segmentation

Five networks, with `internal: true` on two of them. An internal network has no route to the host or
the internet. The route is absent, not filtered.

| Network | Internal | Egress |
| --- | :---: | --- |
| `lab_edge` | no | yes |
| `lab_data` | **yes** | **none** |
| `lab_observability` | no | yes |
| `lab_ai` | no | yes (required to pull model weights) |
| `lab_socket` | **yes** | **none** |

Consequences worth stating explicitly:

- PostgreSQL cannot exfiltrate to the internet even if fully compromised
- The socket proxy cannot be reached from any service that is not explicitly on `lab_socket`
  (Traefik and Promtail only)
- Open WebUI, which processes untrusted input by definition, has no path to any database

PostgreSQL and Redis additionally publish to `127.0.0.1` only, not `0.0.0.0`, so they are not reachable
from other machines on your network.

---

## Container hardening

| Control | Coverage |
| --- | --- |
| `no-new-privileges:true` | Every container except cAdvisor (which is privileged by necessity) |
| Non-root user | Traefik `1000`, Prometheus `65534`, Grafana `472`, Loki `10001`, Gitea `1000`, Redis `redis`, nginx unprivileged variant. Keycloak, Vault and PostgreSQL drop privileges in their own entrypoints |
| Read-only root filesystem | Landing page, with tmpfs for nginx scratch state |
| Capability grants | Exactly one: `IPC_LOCK` on Vault, so it can `mlock()` its memory and keep secret material out of swap |
| Log rotation | Every container: 10 MB × 3 files. Without it a chatty container fills the disk |
| Resource discipline | Redis capped at 256 MB with LRU eviction; `FLUSHALL` and `CONFIG` renamed to nothing |

Traefik running as UID 1000 is why its entrypoints bind 8000/8443 rather than 80/443. An unprivileged
process cannot bind a low port. The host port mapping does the translation, rather than granting
`CAP_NET_BIND_SERVICE`.

---

## The Docker socket

The single largest privilege in any Compose stack. Anything that can write to `/var/run/docker.sock`
can start a privileged container that mounts the host filesystem, which is root on your machine.

**Traefik does not get it.** It talks to `tecnativa/docker-socket-proxy` over `tcp://socket-proxy:2375`,
on an internal network, configured as:

```yaml
CONTAINERS: 1     NETWORKS: 1     EVENTS: 1     PING: 1     VERSION: 1
POST: 0           EXEC: 0         IMAGES: 0     VOLUMES: 0    SECRETS: 0
SWARM: 0          SERVICES: 0     TASKS: 0      NODES: 0      SYSTEM: 0

```

`POST: 0` makes the entire API read-only. A fully compromised Traefik can list containers and nothing
else. Promtail uses the same proxy for log collection.

**Portainer and Watchtower do get it**, read/write. Portainer's purpose is creating, destroying and
exec-ing into containers; Watchtower's is recreating them. Neither can work through a read-only proxy.

Treat the Portainer administrator password as equivalent to root on the host. If that is unacceptable
for your use, remove the service:

```bash
docker compose stop portainer && docker compose rm -f portainer

```

Nothing else depends on it.

---

## Identity and access

The Keycloak realm is a working RBAC example, not decoration.

**Roles are granted to groups; users join groups.** Nobody holds a role directly. Revoking access means
removing someone from a group, which is auditable and reversible. Unpicking a dozen direct grants is
neither.

| Group | Roles | Models |
| --- | --- | --- |
| Platform Engineering | `platform-admin`, `developer`, `ai-user` | Infrastructure owners |
| Application Engineering | `developer`, `ai-user` | Service teams |
| Security | `security-analyst`, `auditor`, `ai-user` | Detection and review |
| Contractors | `contractor` | External, minimal, with a `contractEndDate` attribute |

Realm settings that matter:

- **Brute-force protection.** 5 failures, then a 60-second wait that increments to a 900-second cap
- **Password policy.** 12 characters, mixed case, a digit, not the username, 3-generation history
- **Token lifetimes.** 5-minute access tokens, 30-minute idle SSO session
- **Audit events.** Both user and admin events enabled, with details, retained 7 days
- **CSP and security headers** set at the realm level

Grafana can federate to it (`GRAFANA_OIDC_ENABLED=true`), mapping realm roles onto Grafana roles: a
`platform-admin` lands as Admin, a `developer` as Editor, everyone else as Viewer. Token and userinfo
calls go container-to-container over HTTP, so SSO does not depend on the self-signed edge certificate.

---

## Secrets management

Vault is provisioned with a policy model that demonstrates least privilege rather than asserting it:

| Policy | Can | Cannot |
| --- | --- | --- |
| `platform-admin` | Full control over `secret/*`, manage transit keys | **Export key material. Touch audit devices.** |
| `developer` | Read/write `secret/apps/*`, read `secret/infrastructure/*`, use transit | **See `secret/security/*` at all** (explicit `deny`) |
| `security-analyst` | Read everything, write only `secret/security/*`, read audit config | **Disable audit logging** |
| `ci-pipeline` | Read `secret/ci/*` and two named infrastructure secrets | Everything else |

Two details worth noticing:

- `platform-admin` is deliberately **not** root. Root can disable audit devices and erase its own trail;
  an operator account should not be able to.
- Vault's `deny` beats every other rule during policy evaluation, so `developer`'s exclusion from
  `secret/security/*` holds even if a broader grant is added later.

The audit device is enabled **first** by `init-vault.sh`, so the provisioning itself is recorded.

AppRole models machine identity: the role id is public, the secret id is single-use with a 10-minute TTL.
A leaked pipeline credential should be boring.

---

## Supply chain

- **Every image carries an explicit tag.** CI fails on any image with none. An implicit `:latest` is
  unreproducible without saying so.
- **Three images float deliberately** (Ollama, Open WebUI, MinIO) with the reasoning recorded in
  `.trivyignore`. Every other image is pinned to a specific version.
- **Dependabot** proposes grouped weekly updates for images and Actions. Grouping is intentional:
  fifteen individual bump PRs a week trains maintainers to stop reading them.
- **CI security workflow**, on every change and again weekly:
  - Gitleaks over full history. A secret that was committed and then removed is still compromised
  - Trivy image scanning for critical, *fixable* CVEs (unfixable findings are noise)
  - Trivy configuration scanning, uploaded to GitHub code scanning

---

## Known gaps

Honest list. These are known and accepted for a local development lab.

| Gap | Impact | Production fix |
| --- | --- | --- |
| **Vault in dev mode** | In-memory storage, auto-unseal, a root token in `.env`. All secrets are lost on restart | Durable storage backend, Shamir or auto-unseal with a KMS, no root token in circulation |
| **Keycloak `start-dev`** | Relaxed hostname and TLS checks; development-mode caching | `start --optimized` with a real hostname, certificate and cache configuration |
| **Self-signed TLS** | Browser warnings; no certificate validation between browser and edge | Real certificates via ACME or your CA |
| **Tier-2 credentials in environment** | Visible in `docker inspect` | An external secret manager injecting at runtime |
| **Portainer holds the socket** | Portainer compromise = host compromise | Remove it, or put authentication and network restriction in front |
| **cAdvisor is privileged** | Container escape from cAdvisor = host access | Unavoidable for cgroup metrics; treat the host as inside the boundary |
| **No authentication in front of Prometheus** | Metrics are readable by anyone who can reach the edge | Traefik forward-auth against Keycloak |
| **No Alertmanager** | Alert rules evaluate but nothing routes them | Add Alertmanager (v2 roadmap) |
| **No network policy inside networks** | Any container on `lab_edge` can reach any other on it | Service mesh, or finer-grained networks |
| **Shipped fallback credentials** | A lab run without `make secrets` uses published passwords | Always run `make secrets` |

---

## Hardening for exposure

If you must reach the lab from outside `localhost`, this is the minimum. None of it is optional.

1. **Generate credentials.**

   ```bash
   make secrets && make clean && make up

   ```

1. **Real certificates**, then force HTTPS.

   ```bash
   # .env
   LAB_FORCE_HTTPS=true

   ```

   Place `cert.pem` and `key.pem` in `configs/traefik/certs/` and uncomment the `certificates` block in
   `configs/traefik/dynamic/tls.yml`.

1. **Remove or protect the high-privilege services.** Portainer at minimum; consider Adminer and
   pgAdmin too.

1. **Put authentication in front of everything unauthenticated.** Prometheus and the Traefik dashboard
   both need it, using Traefik forward-auth against Keycloak.

1. **Stop publishing database ports.** Delete the `ports:` blocks for `postgres` and `redis` in
   `compose/01-core.yml`. Services reach them over `lab_data` regardless.

1. **Tighten rate limits.** The defaults (100/s average, 200 burst) are sized for a single developer.

1. **Move Vault out of dev mode.** At that point you are running production Vault and should follow
   [HashiCorp's production hardening guide](https://developer.hashicorp.com/vault/tutorials/operations/production-hardening).

1. **Turn on Keycloak's production mode**, with a real hostname and certificate.

If several of these feel like too much work for what you need, that is a meaningful signal that the lab
should stay on `localhost` and be reached over a VPN or SSH tunnel instead.

---

## Reporting a vulnerability

Please report privately rather than opening a public issue. See
[`SECURITY.md`](../SECURITY.md).
