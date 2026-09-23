# Observability

The metrics and logs pipeline: what collects what, how it is provisioned, and how to extend it.

- [The pipeline](#the-pipeline)
- [Dashboards](#dashboards)
- [Alert rules](#alert-rules)
- [Alertmanager](#alertmanager)
- [Metrics](#metrics)
- [Logs](#logs)
- [Identity audit pipeline](#identity-audit-pipeline)
- [Audit alerts](#audit-alerts)
- [Adding a scrape target](#adding-a-scrape-target)
- [Adding a dashboard](#adding-a-dashboard)
- [Enabling Vault metrics](#enabling-vault-metrics)
- [Moving Loki to object storage](#moving-loki-to-object-storage)
- [Retention and disk](#retention-and-disk)

---

## The pipeline

```text
                  metrics                                logs
                     │                                     │
   ┌─────────────┐   │   ┌──────────────┐              ┌────────────┐
   │  cAdvisor   │───┤   │              │              │   Docker   │
   │  container  │   ├──▶│  Prometheus  │              │    API     │
   │  CPU/mem/IO │   │   │              │              └──────┬─────┘
   └─────────────┘   │   │  15s scrape  │                     │
   ┌─────────────┐   │   │  15d / 4GB   │              ┌──────▼─────┐
   │node-exporter│───┤   │  9 alert     │              │  Promtail  │
   │ host system │   │   │    rules     │              │  discovery │
   └─────────────┘   │   └───────┬──────┘              │  + filter  │
   ┌─────────────┐   │           │                     └──────┬─────┘
   │   Traefik   │───┤           ▼                            │
   │  requests   │   │   ┌──────────────┐              ┌──────▼─────┐
   └─────────────┘   │   │ Alertmanager │              │    Loki    │
   ┌─────────────┐   │   │ group/inhibit│              │  7d / 30d  │
   │  Keycloak   │───┤   └───────┬──────┘              └──────┬─────┘
   │  JVM/logins │   │           │                            │
   └─────────────┘   │           ▼                            │
   ┌─────────────┐   │   alert-webhook                 ┌──────┘
   │ Loki, MinIO │───┘   (local sink)                  │
   └─────────────┘                  ┌──────────────────▼─┐
                                    │      Grafana       │
                                    │   4 dashboards     │
                                    │    provisioned     │
                                    └────────────────────┘
```

Nothing in this diagram requires a click to set up. Datasources, dashboards, scrape configuration and
alert rules are all files in this repository, mounted read-only.

---

## Dashboards

Four, provisioned into a **Lab-in-a-Box** folder and read-only in the UI. `allowUiUpdates: false` is
deliberate: the files are the source of truth, and an edit made in the browser that vanishes on the next
deploy is worse than one the UI refused to make.

### Lab Overview (`lab-overview`)

The default home dashboard. Opening Grafana lands here.

- **At a glance.** Containers running, restarts in the last hour, host CPU / memory / disk gauges
- **Compute.** CPU and memory per container, with a `$container` template variable to filter
- **I/O.** Network and disk throughput per container, drawn symmetrically around zero (receive positive,
  transmit negative)
- **Health.** A sortable table of uptime and 24-hour restart counts, colour-coded, plus a live Loki panel
  of errors across every container

Restart events are annotated onto every time-series panel, so a latency spike and the restart that caused
it line up visually.

### Lab Logs (`lab-logs`)

- Log volume by service, stacked
- Share of total volume, as a donut, showing which service is doing the most talking
- Errors and warnings charted separately from total volume, because a 2% error rate is invisible next to
  total traffic
- A filterable log explorer driven by three template variables: **Service**, **Level** and a free-text
  **Contains** box

### Lab Edge (`lab-edge`)

- Request rate, error rate, p95 latency and open connections, as stat panels
- Requests by service and responses by status class
- p50 / p95 / p99 latency together. p50 shows the typical experience, p99 shows the worst one somebody
  actually had
- Keycloak JVM heap, and a live panel of identity events (logins, token grants, admin changes)

### Identity Audit Trail (`lab-identity-audit`)

- Keycloak authentication volume split by event type and outcome
- Failures grouped by source, plus focused administrative-change records
- Vault KV secret reads with request paths and operations
- The complete Keycloak and Vault evidence stream for investigation

---

## Alert rules

Nine rules in three groups, in `monitoring/prometheus/rules/lab-alerts.yml`. Every one carries a
description that says what to *do*. An alert that does not is a noise generator.

| Group | Alert | Fires when |
| --- | --- | --- |
| availability | `TargetDown` | A scrape target has been unreachable for 3m |
| | `ContainerUnhealthy` | A container is registered but producing no CPU samples for 5m |
| | `ContainerRestartLoop` | More than 3 restarts in 15m |
| saturation | `ContainerHighCPU` | Above 85% of a core for 10m |
| | `ContainerHighMemory` | Above 90% of its limit for 10m, **only where a limit is set** |
| | `HostMemoryPressure` | Host above 90% for 10m |
| | `HostDiskFillingUp` | A real filesystem above 85% for 15m |
| edge | `HighErrorRate` | Over 5% 5xx for a service for 5m |
| | `EdgeLatencyHigh` | p95 above 2s for 10m |

Thresholds are generous and `for` windows are long, because this runs on a laptop where a model download
or a container restart is normal.

`ContainerHighMemory` guards against a zero limit (`and container_spec_memory_limit_bytes > 0`). Without
that, every unlimited container divides by zero and reports `+Inf`, and the alert fires constantly.

Two additional Loki-managed audit rules evaluate brute-force login patterns and
privileged Vault policy changes. All eleven rules evaluate in Prometheus or Loki,
appear in Grafana's alert list, and route through Alertmanager (see below).

Every Prometheus and Loki rule carries a `runbook_url` annotation pointing back
at this document so a firing alert links to the explanation of what to do.

Validate rule and Alertmanager changes before starting anything:

```bash
make validate          # includes promtool check rules and amtool check-config
```

`promtool` parses every PromQL expression, so a typo is caught at commit time rather than at 3am when the
rule silently never fires. `amtool check-config` does the same for the routing
tree, inhibition rules and receivers.

---

## Alertmanager

Alertmanager receives every firing alert from Prometheus, groups related ones,
suppresses children when a parent is already firing, and delivers notifications.

| | |
| --- | --- |
| UI | <https://alertmanager.lab.localhost> |
| Config | `monitoring/alertmanager/alertmanager.yml` |
| Default receiver | `local-webhook` → in-lab `alert-webhook` container |

### Grouping

Alerts are grouped by `category` and `severity` (the labels every lab rule
already sets). Criticals re-notify after one hour; warnings after four; infos
after twelve. `group_wait` / `group_interval` are deliberately generous so a
model download does not look like an incident.

### Inhibition

When `HostMemoryPressure` is firing, Alertmanager suppresses the per-container
symptoms it usually causes:

- `ContainerHighCPU`
- `ContainerHighMemory`
- `ContainerUnhealthy`
- `ContainerRestartLoop`

The operator sees the cause once rather than a cascade of children.

### Local webhook (works out of the box)

No Slack workspace, SMTP relay or Discord bot is required. Alertmanager POSTs
to `http://alert-webhook:8080/`, an echo container on `lab_observability` that
logs the JSON body to stdout:

```bash
docker compose logs -f alert-webhook
```

Resolved alerts are included (`send_resolved: true`), so a quiet period after a
firing burst is visible in the same stream.

### Optional receivers (off by default)

Email, Slack and Discord receivers are stubbed as commented examples in
`monitoring/alertmanager/alertmanager.yml`. To enable one:

1. Uncomment the receiver block and fill in the real endpoint (SMTP host, Slack
   incoming-webhook URL, or Discord webhook URL with the `/slack` suffix).
2. Either change the root `receiver:` to that name, or add a child route that
   selects it (for example by severity).
3. Restart Alertmanager: `docker compose up -d alertmanager`.

Do not commit real webhook URLs or SMTP passwords. Keep them in an ignored
local override or a secret manager.

Discord accepts Slack-compatible payloads at `…/slack` on the webhook URL, which
is why the Discord example reuses `slack_configs`.

### Validate

```bash
make validate
# or directly:
docker run --rm --entrypoint amtool \
  -v "$PWD/monitoring/alertmanager:/cfg:ro" \
  prom/alertmanager:v0.28.1 check-config /cfg/alertmanager.yml
```

---

## Metrics

Scrape targets are in `monitoring/prometheus/prometheus.yml`, addressed by Docker service name.

| Job | Target | Provides |
| --- | --- | --- |
| `prometheus` | `localhost:9090` | Self-monitoring |
| `cadvisor` | `cadvisor:8080` | Per-container CPU, memory, network, disk |
| `node` | `node-exporter:9100` | Host CPU, memory, disk, network |
| `traefik` | `traefik:8083` | Request rate, duration histograms, status codes |
| `keycloak` | `keycloak:9000` | JVM, login and token metrics |
| `loki` | `loki:3100` | Ingestion and query performance |
| `minio` | `minio:9000` | Bucket usage, request rates |

Two things worth knowing about the cAdvisor job:

- `container_blkio_device_usage_total` is dropped. cAdvisor emits a series per container *per device*
  per metric, and no dashboard reads it.
- Container names are relabelled from `name` to `container`, stripping Docker's leading slash, so legends
  read `lab-grafana` rather than `/lab-grafana`.

Traefik's metrics are published on a dedicated entrypoint (`:8083`) that is never routed publicly, so
scraping does not pass through the edge it is measuring.

MinIO is scraped because `MINIO_PROMETHEUS_AUTH_TYPE: public` is set. Without it, the endpoint requires a
signed JWT, which Prometheus cannot mint from a static configuration file. The endpoint is only reachable
from inside the lab's networks.

---

## Logs

Promtail collects through the **Docker API**, not by tailing `/var/lib/docker/containers`. That path
exists on a Linux host but lives inside a VM on Docker Desktop for macOS and Windows, where the bind
mount either fails or silently collects nothing. Using `docker_sd_configs` against the read-only socket
proxy works identically everywhere and adds no second socket mount.

Discovery is dynamic and filtered to this Compose project, so a container started five minutes from now
is picked up on the next 15-second refresh with no configuration change.

### Labels

| Label | Source |
| --- | --- |
| `container` | Container name, leading slash stripped |
| `service` | Compose service name, stable across recreations |
| `project` | Compose project name |
| `stream` | `stdout` or `stderr` |
| `detected_level` | Parsed from the line content |
| `component` | The lab's own taxonomy, where a service declares it |

### Pipeline stages

Three things happen to every line before it reaches Loki:

1. **Promtail's own output is dropped.** Without this, every shipped line generates a log line about
   shipping a line, which generates another.
1. **Healthcheck noise is dropped.** Probes run every 15 seconds against every service and would
   otherwise dominate ingestion volume.
1. **A level is promoted to a label**, so dashboards can chart error rates without a full-text search.

Then `labeldrop` removes `filename` and the container id. This is the important one: a label with
unbounded values turns every log line into its own stream, and high-cardinality labels are the classic
way to bring Loki down.

### Useful queries

```logql
{job="lab-containers"} |~ "(?i)(error|fatal|panic)"

{service="keycloak"} |= "LOGIN_ERROR"

sum by (service) (rate({job="lab-containers"}[5m]))

{service="traefik"} | json | status >= 500

{job="lab-containers", detected_level="error"} != "healthcheck"
```

---

## Identity audit pipeline

The audit path deliberately uses the native formats of both identity systems:

```text
Keycloak jboss-logging ──JSON stdout──┐
                                     ├── Promtail ──labelled streams── Loki ── Grafana
Vault file audit device ─JSON volume─┘
```

Keycloak's realm has the built-in `jboss-logging` listener enabled. Successful
events are raised from `DEBUG` to `INFO`, the console emits JSON, and Promtail
selects only `org.keycloak.events` records. Vault continues writing its native
JSON audit file. Promtail mounts the named `vault_logs` volume read-only, which
works the same way on Docker Desktop and Linux.

Only bounded fields become labels:

| Source | Label | Meaning |
| --- | --- | --- |
| Both | `audit_source` | `keycloak` or `vault` |
| Both | `event_type` | Keycloak user event, or Vault `request` / `response` |
| Both | `outcome` | `success` or `failure` |
| Keycloak | `operation_type` | Administrative `CREATE`, `UPDATE`, `DELETE` or `ACTION` |
| Vault | `operation` | Request operation such as `read`, `list`, `create`, `update` or `delete` |

User ids, IP addresses, resource paths and entity names stay in the log body or
structured metadata. Promoting them would create an unbounded number of Loki
streams. This is an intentional cardinality boundary, not missing parsing.

Vault HMAC-hashes sensitive request and response values before it writes the
file. Promtail forwards the original JSON; it never sees or reconstructs the
plaintext. `make audit-test` proves that behavior with a unique secret and also
checks that the active root token is absent from the raw audit file.

Useful investigation queries:

```logql
{audit_source="keycloak", event_type="LOGIN_ERROR", outcome="failure"}

{audit_source="keycloak", operation_type=~"CREATE|UPDATE|DELETE"}

{audit_source="vault", event_type="request", operation=~"read|list"}
| json request_path="request.path"
| request_path=~"secret/.*"

sum by (audit_source) (
  count_over_time({audit_source=~"keycloak|vault", outcome="failure"}[5m])
)
```

Run the end-to-end test against a live lab:

```bash
make audit-test
```

The test generates rejected logins, a disposable Keycloak admin change, Vault
secret access and a policy change. It waits for parsed records in Loki, verifies
HMAC redaction and confirms both audit rules reach `firing` before cleaning up.

---

## Audit alerts

Loki loads `monitoring/loki/rules/fake/identity-audit.yml`; `fake` is Loki's
tenant id when authentication is disabled.

| Alert | Fires when | First response |
| --- | --- | --- |
| `KeycloakBruteForcePattern` | At least 5 `LOGIN_ERROR` events occur in 5 minutes | Inspect usernames and source addresses, then check Keycloak brute-force lockout state |
| `VaultPrivilegedPolicyChange` | An ACL or password policy is created, updated or deleted | Identify the token accessor and path, compare the policy with source control, then revoke unexplained credentials |

These rules evaluate locally in Loki today. Notification routing, grouping and
inhibition arrive with roadmap `v2-6`; until then, inspect rule state in Grafana
or with Loki's `/prometheus/api/v1/rules` endpoint.

---

## Adding a scrape target

1. Make sure the service is on `lab_observability` in its compose fragment.
1. Add a job to `monitoring/prometheus/prometheus.yml`:

   ```yaml
   - job_name: my-service
     static_configs:
       - targets: ["my-service:8080"]
         labels:
           component: platform
   ```

1. Validate and reload. No restart is needed, because Prometheus has `--web.enable-lifecycle`:

   ```bash
   make validate
   docker compose exec prometheus kill -HUP 1
   ```

1. Confirm at <https://prometheus.lab.localhost/targets>.

---

## Adding a dashboard

1. Build it in the Grafana UI.
1. Export via **Share → Export → Save to file**, with *Export for sharing externally* **off**. That
   option replaces datasource uids with template inputs, which breaks provisioning.
1. Save it to `monitoring/grafana/dashboards/`.
1. Set a stable `uid` at the top level. CI fails without one, because provisioned dashboards are
   referenced by uid from the home-dashboard setting and from cross-dashboard links.
1. Confirm datasources are referenced as `{"type": "prometheus", "uid": "prometheus"}` or
   `{"type": "loki", "uid": "loki"}`.

It appears within 30 seconds, because the provisioner re-reads the directory on an interval.

---

## Enabling Vault metrics

Vault's job is commented out in `prometheus.yml` rather than shipped broken. Its telemetry endpoint needs
either an authenticated token or `unauthenticated_metrics_access = true` in the listener stanza, and dev
mode provides neither.

To enable it:

```bash
docker compose exec vault vault policy write prometheus-metrics - <<'EOF'
path "sys/metrics" {
  capabilities = ["read"]
}
EOF

docker compose exec vault vault token create \
  -policy=prometheus-metrics -period=768h -field=token \
  > monitoring/prometheus/vault-token
```

Then uncomment the `vault` job in `prometheus.yml`, add the token as a mount in
`compose/03-observability.yml`, and reload Prometheus.

Add `monitoring/prometheus/vault-token` to `.gitignore` first.

---

## Moving Loki to object storage

The lab writes Loki chunks to a local volume, which is right for a single node. MinIO is already running
and the `loki-chunks` bucket already exists, so the upgrade is configuration only.

In `monitoring/loki/loki-config.yml`, replace the `common.storage` and `storage_config` blocks:

```yaml
common:
  storage:
    s3:
      endpoint: minio:9000
      bucketnames: loki-chunks
      access_key_id: ${MINIO_ROOT_USER}
      secret_access_key: ${MINIO_ROOT_PASSWORD}
      s3forcepathstyle: true
      insecure: true

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: s3        # was: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h
```

Then add `lab_edge` to Loki's networks so it can reach MinIO, and pass the MinIO credentials into its
environment. `-config.expand-env=true` is already set, so `${VAR}` resolves.

Use a scoped MinIO service account rather than the root credentials. `init-minio.sh` already creates a
`lab-app` policy that shows the shape.

---

## Retention and disk

| Store | Setting | Default |
| --- | --- | --- |
| Prometheus | `PROMETHEUS_RETENTION_TIME` | `15d` |
| Prometheus | `PROMETHEUS_RETENTION_SIZE` | `4GB` |
| Loki | `LOKI_RETENTION_PERIOD` | `168h` (7 days) |
| Loki identity audit streams | `LOKI_AUDIT_RETENTION_PERIOD` | `720h` (30 days) |

The Prometheus **size** limit matters more than the time limit on a laptop: without it, a lab left
running over a long weekend quietly consumes every free gigabyte.

Loki's retention only takes effect because `compactor.retention_enabled: true` is set. Without that,
`retention_period` is advisory and nothing is ever deleted, which is a common and expensive misconfiguration.

Check what is actually being used:

```bash
docker system df -v | grep lab_
```
