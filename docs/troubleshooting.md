# Troubleshooting

Start with `make health`. It checks containers, provisioning jobs and HTTP routes as three separate
things, which usually identifies which of the three is wrong before you read any logs.

- [Reading the output](#reading-the-output)
- [First boot](#first-boot)
- [Ports and networking](#ports-and-networking)
- [Credentials](#credentials)
- [Provisioning jobs](#provisioning-jobs)
- [Specific services](#specific-services)
- [Performance](#performance)
- [Windows and macOS](#windows-and-macos)
- [Starting over](#starting-over)
- [Reporting a bug](#reporting-a-bug)

---

## Reading the output

```bash
make health
```

Three sections:

**Containers** — anything long-running. `starting` means the healthcheck has not passed yet and is
normal for the first minute or two. `UNHEALTHY` means it started and then failed its own check.

**Provisioning** — the one-shot `-init` jobs. `completed` is what you want. These containers are
*supposed* to exit; `docker compose ps` showing them as `Exited (0)` is success, not a fault.

**HTTP routes** — whether Traefik actually routes each hostname. A `401` or `403` counts as healthy: a
service demanding credentials is a service that is up and enforcing them.

For a specific service:

```bash
docker compose logs --tail 100 keycloak
docker compose logs -f ollama-init          # follow a running job
docker inspect lab-keycloak --format '{{json .State.Health}}' | python3 -m json.tool
```

---

## First boot

### It has been five minutes and things are still unhealthy

Expected. A cold first boot pulls roughly 6 GB of images and downloads a language model. On a slow
connection this takes a while.

Watch the two slowest things directly:

```bash
docker compose logs -f ollama-init      # the model download
docker compose logs -f keycloak         # database migrations and realm import
```

Keycloak's healthcheck has a 90-second grace period and 20 retries for exactly this reason.

### `docker compose up` fails immediately with a secret error

```text
error while creating mount source path ... secrets/postgres_password.txt
```

If `.env` points to `secrets/local/`, recreate the local credentials:

```bash
make secrets
```

If a tracked fallback file under `secrets/` is missing, restore only those public development defaults:

```bash
git restore secrets/
```

### `include` is not recognised

```text
services.include must be a mapping
```

Your Docker Compose predates v2.20. Check with `docker compose version` and update — on Docker Desktop,
update the whole application.

---

## Ports and networking

### Port 80 or 443 is already in use

```text
Bind for 0.0.0.0:443 failed: port is already allocated
```

Something else — often another Traefik, a local nginx, or IIS on Windows — has it. Either stop that, or
move the lab:

```bash
# .env
LAB_HTTP_PORT=8080
LAB_HTTPS_PORT=8443
```

```bash
make restart
```

Then use `https://lab.localhost:8443`. The landing page derives every link from the address you visit it
on, so its links follow automatically.

Find the culprit:

```bash
# Linux/macOS
sudo lsof -i :443
# Windows PowerShell
Get-NetTCPConnection -LocalPort 443 | Select-Object OwningProcess
```

### `*.lab.localhost` does not resolve

Rare — `.localhost` is loopback by definition under RFC 6761 — but some corporate DNS and some VPN
clients intercept it anyway.

Test:

```bash
curl -skI https://lab.localhost
ping keycloak.lab.localhost
```

If it fails, either add entries to your hosts file:

```text
127.0.0.1 lab.localhost traefik.lab.localhost keycloak.lab.localhost vault.lab.localhost
127.0.0.1 grafana.lab.localhost prometheus.lab.localhost chat.lab.localhost ollama.lab.localhost
127.0.0.1 git.lab.localhost minio.lab.localhost s3.lab.localhost portainer.lab.localhost
127.0.0.1 pgadmin.lab.localhost adminer.lab.localhost qdrant.lab.localhost
```

…or set `LAB_DOMAIN` to a domain you control that resolves to `127.0.0.1`.

### 404 from Traefik

The route does not exist. Usually the container is not on `lab_edge`, or its labels are wrong.

```bash
docker compose logs traefik | grep -i error
```

Open <https://traefik.lab.localhost> and check the Routers tab — if the router is not listed, Traefik
never saw the labels.

### 502 or 503 from Traefik

The route exists but the backend is not answering. Usually the service is still starting.

```bash
docker compose ps <service>
docker compose logs --tail 50 <service>
```

If the container is healthy and you still get 502, the port in
`traefik.http.services.<name>.loadbalancer.server.port` does not match what the service is listening on.

### Certificate warning in the browser

Expected. The lab serves a self-signed certificate so HTTPS works with no setup. Either accept it, or
install a locally-trusted one with [mkcert](https://github.com/FiloSottile/mkcert) — see the
[README](../README.md#https-and-certificates).

---

## Credentials

### A password does not work

Almost always the same cause: `.env` was regenerated *after* a service had already stored the old
password in its own volume. Keycloak, Gitea, Grafana and MinIO all hash their administrator password on
first boot; changing `.env` afterwards changes what the lab offers, not what those services expect.

```bash
make creds            # what the lab currently believes
make clean && make up # make the new credentials take effect
```

### `make creds` shows red placeholder values

The lab is running with the development credentials committed to this repository.

```bash
make secrets && make clean && make up
```

### Keycloak demo users cannot log in

`keycloak-init` sets their passwords on every boot, and the realm enforces a 12-character policy with
mixed case and a digit. A hand-edited `DEMO_USER_PASSWORD` that violates the policy is rejected.

```bash
docker compose logs keycloak-init | grep -i "password policy"
```

`make secrets` always generates a compliant value.

---

## Provisioning jobs

Each `-init` container is idempotent and safe to re-run:

```bash
docker compose up --force-recreate keycloak-init
docker compose up --force-recreate vault-init
docker compose up --force-recreate minio-init
docker compose up --force-recreate gitea-init
docker compose up --force-recreate ollama-init
```

| Job | Failure usually means |
| --- | --- |
| `keycloak-init` | The realm import did not land, or `DEMO_USER_PASSWORD` violates the password policy |
| `vault-init` | Vault sealed or restarted — in dev mode a restart wipes everything, so re-run this |
| `minio-init` | Wrong root password, i.e. the secret file changed after MinIO's first boot |
| `gitea-init` | `app.ini` had not been written yet. Non-fatal — finish setup in the browser |
| `ollama-init` | Model name typo, or no disk space. Check against <https://ollama.com/library> |

Vault is worth repeating: **dev mode stores everything in memory**. Restarting the Vault container loses
every secret and policy. Re-run `vault-init` after any Vault restart.

---

## Specific services

<details>
<summary><strong>Keycloak restarts or never becomes healthy</strong></summary>

Nearly always the database.

```bash
docker compose logs keycloak | grep -i "database\|connection\|migration"
docker compose exec postgres psql -U lab -c "\l"     # is the keycloak DB there?
```

If the `keycloak` database is missing, the PostgreSQL init script did not run — it only runs on an empty
data directory. A `make clean` recreates it.

Keycloak also needs around 1 GB of RAM. If Docker is constrained to 4 GB with Ollama also running, it
will be OOM-killed:

```bash
docker inspect lab-keycloak --format '{{.State.OOMKilled}}'
```

</details>

<details>
<summary><strong>Grafana shows "No data" everywhere</strong></summary>

Check Prometheus has targets first:

```bash
curl -s http://localhost:9090/api/v1/targets | python3 -m json.tool | grep health
```

Or open <https://prometheus.lab.localhost/targets>.

If cAdvisor is down, every container panel will be empty — it is the source of all of them. cAdvisor
needs privileged mode and several host mounts, and is the service most likely to be blocked by a
hardened or unusual host.

</details>

<details>
<summary><strong>No logs in Loki</strong></summary>

```bash
docker compose logs promtail | tail -30
curl -s http://localhost:3100/ready
```

Promtail reads logs through the socket proxy. If the proxy is down, discovery finds nothing and Promtail
reports no error — it simply has no targets. Check the proxy is running and that Promtail is on
`lab_socket`.

</details>

<details>
<summary><strong>Open WebUI cannot see any models</strong></summary>

```bash
docker compose exec ollama ollama list          # is anything downloaded?
docker compose logs ollama-init | tail -20      # did the pull succeed?
```

If the list is empty, pull one directly:

```bash
docker compose exec ollama ollama pull llama3.2:1b
```

Then refresh Open WebUI — it queries Ollama for the model list on page load.

</details>

<details>
<summary><strong>MinIO console redirects to the wrong address</strong></summary>

`MINIO_BROWSER_REDIRECT_URL` must match how you reach it. If you changed `LAB_DOMAIN` or the HTTPS port
after first boot, `make restart` picks up the new value.

Remember the split: `minio.lab.localhost` is the console for humans, `s3.lab.localhost` is the S3 API for
tooling. Pointing an S3 client at the console port is the most common MinIO mistake.

</details>

<details>
<summary><strong>Gitea shows the installation page</strong></summary>

`gitea-init` could not create the administrator. This is non-fatal by design — complete the setup in the
browser, or re-run the job:

```bash
docker compose logs gitea-init
docker compose up --force-recreate gitea-init
```

</details>

---

## Performance

### The whole lab is slow

Check Docker's memory allocation. 8 GB is the floor for the full stack; Keycloak wants ~1 GB and Ollama
will take whatever a model needs.

- **Docker Desktop** → Settings → Resources → Memory
- **Linux** — no limit by default; check the host has headroom with `free -h`

Free the largest consumer:

```bash
docker compose stop ollama open-webui
```

### Which container is using everything

```bash
docker stats --format "table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}"
```

Or open the **Lab Overview** dashboard in Grafana, which shows the same thing over time rather than as an
instant.

### Disk is filling up

```bash
docker system df -v | grep lab_
```

The usual culprits, in order: `ollama_models` (gigabytes per model), `prometheus_data` (capped at 4 GB by
default), `loki_data`, and old images from `make update`.

```bash
docker compose exec ollama ollama rm <model>    # reclaim model weights
docker image prune                              # reclaim superseded layers
```

---

## Windows and macOS

### Windows: `make` is not recognised

Use Git Bash rather than PowerShell — GNU make ships with Git for Windows. Or install it with
`choco install make`, or use WSL. Every target's raw `docker compose` equivalent is in the
[README](../README.md#commands).

### Windows: scripts fail with "no such file or directory"

A shell script was checked out with CRLF line endings, so the kernel reads the shebang as `/bin/sh\r`.
The repository's `.gitattributes` prevents this, but a checkout from before it was added can still be
affected:

```bash
git rm --cached -r .
git reset --hard
```

### macOS: `make up` is very slow the first time

Docker Desktop's file sharing is slower than native Linux. It affects the initial image extraction more
than steady-state operation. [OrbStack](https://orbstack.dev/) is noticeably faster if this bothers you.

### macOS: Ollama does not use the GPU

It cannot. Docker on macOS runs Linux containers in a VM with no access to Apple's Metal API. For GPU
acceleration on Apple Silicon, run Ollama natively and point Open WebUI at it — see
[`docs/ai.md`](ai.md#gpu-acceleration).

---

## Starting over

```bash
make down          # stop, keep all data
make clean         # delete every volume — asks first
make clean && make secrets && make up      # completely fresh, new credentials
```

Nuclear option, if the Docker state itself is suspect:

```bash
docker compose down -v --remove-orphans
docker system prune -a --volumes          # ⚠ affects ALL Docker projects on this machine
```

---

## Reporting a bug

If none of this helped, please open an issue. Include:

```bash
make health   > health.txt
make version  > version.txt
docker compose logs --tail 100 <failing-service> > service.log
```

Skim the logs before pasting — container logs can contain tokens, and the issue tracker is public. Never
paste `.env`.

The [bug report template](https://github.com/OWNER/lab-in-a-box/issues/new?template=bug_report.yml) asks
for exactly these.
