# Roadmap

Where this project is going, and how to file the work.

- [Milestones](#milestones)
- [v1: Foundation](#v1-foundation-shipped)
- [v2: Identity governance](#v2-identity-governance)
- [v3: Infrastructure as code](#v3-infrastructure-as-code)
- [v4: AI operations](#v4-ai-operations)
- [v5: Homelab operations](#v5-homelab-operations)
- [Filing the issues](#filing-the-issues)
- [Principles](#principles)

---

## Milestones

| Milestone | Theme | Status | Issues |
| --- | --- | --- | :---: |
| **v1.0** | Foundation, the working lab | ✅ Shipped | n/a |
| **v2.0** | Identity governance | 🚧 In progress, 4 of 7 done | 7 |
| **v3.0** | Infrastructure as code | Planned | 7 |
| **v4.0** | AI operations | Planned | 6 |
| **v5.0** | Homelab operations | Planned | 3 |

Full detail for every planned issue (description, acceptance criteria, labels) is in
[`issues.yml`](issues.yml), which is the source of truth. This page is the summary.

---

## v1: Foundation (shipped)

Twenty-five default services, provisioned automatically, starting with one command. Identity, secrets,
observability, object storage, Git hosting and a local LLM, behind a single reverse proxy with network
segmentation and generated credentials. Qdrant and Watchtower bring the catalogue to twenty-seven when
their optional Compose profiles are enabled.

See [`CHANGELOG.md`](../CHANGELOG.md) for the full inventory.

---

## v2: Identity governance

The theme: **turn the seeded Keycloak realm into a working demonstration of identity lifecycle and
access governance.** This is the area where most self-hosted labs stop at "we installed an IdP", and it
is where the genuinely interesting problems are.

**Status: in progress.** `v2-1` through `v2-4` are complete; `v2-5` is next. The milestone itself is not done.

| | Issue | Status |
| --- | --- | --- |
| `v2-1` | Joiner/Mover/Leaver automation: provision, transfer and deprovision an identity end to end | ✅ **Complete**. See [docs/identity-governance.md](../docs/identity-governance.md) |
| `v2-2` | RBAC simulator: answer "what can this person actually reach?" across every service | ✅ **Complete**. See [docs/identity-governance.md](../docs/identity-governance.md#rbac-simulator) |
| `v2-3` | Access review campaign: periodic recertification with an approve/revoke workflow | ✅ **Complete**. See [docs/identity-governance.md](../docs/identity-governance.md#access-review-campaigns) |
| `v2-4` | SCIM provisioning endpoint: propagate identity changes to downstream services | ✅ **Complete**. See [docs/identity-governance.md](../docs/identity-governance.md#scim-provisioning) |
| `v2-5` | Audit event pipeline: ship Keycloak and Vault audit events into Loki with a dashboard | ⬅ **Next** |
| `v2-6` | Alertmanager: route the alert rules that currently evaluate into nothing | |
| `v2-7` | Forward-auth for unauthenticated services: Prometheus and the Traefik dashboard behind SSO | |

**Why this first.** The lab already models RBAC correctly, with roles granted to groups and users joining
groups. What it does not yet show is what happens over *time*: someone joins, changes team, leaves.
That lifecycle is the actual work of identity management, and it is what an IAM interviewer asks about.

---

## v3: Infrastructure as code

The theme: **the same lab, expressed in the tools people actually deploy with.** One Compose definition,
several deployment targets, no drift between them.

| | Issue |
| --- | --- |
| `v3-1` | Terraform module: the whole lab as declarative infrastructure |
| `v3-2` | Ansible playbook: provision the lab onto an existing host |
| `v3-3` | Kubernetes edition: Helm chart or Kustomize overlays with the same service graph |
| `v3-4` | AWS deployment: ECS or EKS with managed RDS and S3 in place of the containers |
| `v3-5` | Azure deployment: Container Apps or AKS with managed PostgreSQL and Blob Storage |
| `v3-6` | GCP deployment: Cloud Run or GKE with Cloud SQL and Cloud Storage |
| `v3-7` | Shared configuration model: one source of truth across all deployment targets |

**`v3-7` matters more than the individual targets.** Four deployments that drift apart are worse than
one that works. Merging any of the others before configuration sharing is settled will produce that
drift.

---

## v4: AI operations

The theme: **use the local model for operational work, not demos.** The lab already has logs, metrics,
configuration and an LLM in the same place, which is what makes this work possible locally.

| | Issue |
| --- | --- |
| `v4-1` | Log analysis pipeline: summarise anomalies from Loki on a schedule |
| `v4-2` | Incident response copilot: assemble context from metrics, logs and config for a firing alert |
| `v4-3` | Automatic infrastructure documentation: generate and refresh service docs from live state |
| `v4-4` | RAG over your own runbooks: index `docs/` and operational history into Qdrant |
| `v4-5` | Security review agent: analyse configuration changes for weakened posture |
| `v4-6` | Grafana LLM panel: ask questions about a dashboard from inside it |

**Constraint:** a 7B model running on CPU will not triage a production incident. These
issues are worth doing because they explore where local models are useful (summarisation, correlation,
first-draft documentation) and where they are not. Any issue in this
milestone that ships should say plainly what it got wrong in testing.

---

## v5: Homelab operations

The theme: **make the lab easier to trust and size for daily use, without adding a second monitoring or
dashboard stack.** These are planned operational improvements, not services added to the default lab.

| | Issue |
| --- | --- |
| `v5-1` | Backup verification and restore drill: prove a backup can restore into a healthy lab |
| `v5-2` | External uptime monitoring profile: test the browser-facing routes independently of the stack |
| `v5-3` | Resource presets: make the laptop-friendly, standard and full-AI footprints explicit |

**Why this next.** The lab already has backup commands, health checks and a resource budget. The next
step is making those guarantees continuously verifiable and easy to choose, without turning the default
installation into a larger collection of overlapping tools.

---

## Filing the issues

Everything in [`issues.yml`](issues.yml) can be filed against your own repository:

```bash
# See what would be created, no changes made
bash scripts/create-roadmap-issues.sh

# Create the milestones and issues for real
bash scripts/create-roadmap-issues.sh --apply

# One milestone at a time
bash scripts/create-roadmap-issues.sh --apply --milestone v2.0
```

Requires the [GitHub CLI](https://cli.github.com/), authenticated (`gh auth login`), run from inside the
repository.

The script is idempotent: it skips any issue whose title already exists, so re-running it after adding to
`issues.yml` files only the new ones.

---

## Principles

Constraints any roadmap item has to respect. They are what keeps the lab usable.

**One command still has to work.** No milestone may introduce a required manual step before
`docker compose up -d`.

**It must still run on a laptop.** New services need a resource budget, and a big one needs to go behind
a Compose profile. The whole lab fitting in 8 GB is a feature.

**Cross-platform, still.** Linux, macOS and Windows. If something only works on Linux, it goes behind a
profile and says so.

**Every service explains itself.** A healthcheck that proves something real, comments that record *why*,
and documentation that admits what does not work.

**Depth over breadth.** One well-implemented governance workflow is worth more than five half-wired
services. The failure mode for a project like this is becoming a list of logos.

---

## Suggesting something

Open a [feature request](../.github/ISSUE_TEMPLATE/feature_request.yml). The template asks for the
resource cost, because that is the part that determines whether a good idea is a good idea *here*.
