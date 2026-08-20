# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Because this project is infrastructure rather than a library, "breaking" means **a change that requires
action from someone with an existing lab**: a `make clean`, a manual migration, or an edit to their
`.env`. Those are always called out explicitly.

---

## [Unreleased]

Work in progress toward **v2.0, Identity governance**. The milestone is not
complete: `v2-1`, `v2-2` and `v2-3` have landed, `v2-4` through `v2-7` remain.

### Added

#### Access review campaigns (roadmap `v2-3`)

- **Campaign workflow built on the v2-2 RBAC simulator**, not a second
  authorization model. Every entitlement a campaign reviews comes from
  `rbac.Simulator.analyse()`, the same engine `make rbac-show` uses.
  - `make access-review-create NAME=… [SCOPE=all|profile:<name>|user:<name>]`:
    snapshots access for every identity in scope and opens the campaign in one
    step
  - `make access-review-decide CAMPAIGN=… USER=… ENTITLEMENT=… DECISION=approve|revoke|not-applicable`:
    records a decision. A second decision on the same item is refused unless
    `FORCE=1`
  - `make access-review-complete CAMPAIGN=…`: closes the campaign. Refuses if
    items are still undecided unless `FORCE=1`, and forcing never marks them
    approved
  - `make access-review-remediate CAMPAIGN=…`: the only command that mutates
    live access. Acts on `REVOKE` decisions by reusing the exact Keycloak/Vault/Gitea
    adapters the JML commands use, then verifies each removal against a fresh
    RBAC read rather than trusting the adapter's own report of success
  - `make access-review-show` / `access-review-list` / `access-review-cancel`
- **Snapshot immutability.** A campaign's captured items never change after
  the fact; re-showing the same campaign always returns what it saw at open
  time. If live access changes afterward, `access-review-complete` reports the
  difference as `post_snapshot_drift` alongside the untouched original record.
- **Expectation classification.** `rbac.Simulator.classify_expectation()`
  marks every held entitlement `EXPECTED`, `UNEXPECTED`, `NOT_MODELED` or
  `NO_PROFILE` against the identity's current role profile, reusing the exact
  comparison `_detect_drift` already makes rather than a second copy of it.
- **Remediation never touches a shared definition, only an attachment.**
  Revoking a Vault policy computes *current policies minus the one revoked*
  (`vault.set_policies`, new), never a blind replace, so an identity holding
  more than one policy keeps the others. A role granted by a group, a single
  Vault path inside a policy, and Gitea repository ownership all resolve to
  `MANUAL_ACTION_REQUIRED` rather than an automated guess, because none of
  them can be safely narrowed to one attachment without risking a shared
  object or someone else's access.
- **Protected identities can be reviewed but never mutated.** Remediation
  against a seeded demo identity is skipped by default (`SKIPPED_PROTECTED_IDENTITY`),
  reusing the same `LAB_ALLOW_PROTECTED` escape hatch the JML commands already have.
- **Redacted evidence**, immutable per transition (`opened`, `remediated`,
  `completed`), written under `artifacts/access-review/<id>/` alongside the
  live working document, through the same `record.py` redaction chokepoint the
  lifecycle records use.
- `keycloak.py`: `group_members`, `all_usernames`, `direct_realm_roles`,
  `remove_direct_realm_role`. The last two are needed because a role held with no
  group granting it (an anomaly `rbac.py` already detects) requires a
  different removal call than a group-granted one, and using the wrong one
  would silently no-op.
- 133 new integration checks (`make jml-test SUITE=access-review`), including
  an end-to-end join → inject drift → review → approve → revoke → remediate →
  verify → complete scenario, and a dedicated test proving a multi-policy
  Vault identity keeps its other policy when only one is revoked.

#### RBAC simulator (roadmap `v2-2`)

- **`make rbac-show USER=…`** shows effective access for an identity, with a reason
  and an inheritance kind (`direct`, `group-inherited`, `derived`) on every
  grant. `FORMAT=json` for scripting.
- **`make rbac-diff USER=… OTHER=…`** shows what one identity can reach that another
  cannot, with the source of each difference.
- **`make rbac-who-can PERMISSION=…`** performs a reverse lookup across the realm.
  Resource matching is wildcard-aware in both directions, so a holder of Vault
  `secret/*` is correctly reported for a question about `secret/data/security/`.
- **Resolution from live state, not configuration.** Vault policy documents are
  fetched from the running Vault and parsed into the paths and capabilities they
  actually grant; Keycloak roles come from the composite mapping; Grafana's role
  is computed from the deployment's own `role_attribute_path` and only when
  `GRAFANA_OIDC_ENABLED=true`.
- **Drift detection.** Actual access is compared with what the identity's role
  profile expects, and the difference reported as `EXTRA_GITEA_TEAM`,
  `UNEXPECTED_VAULT_POLICY`, `STALE_GITEA_TEAM`, `MISSING_REALM_ROLE` and
  similar. Read-only: drift is reported, never corrected.
- **Honest decisions.** `NOT_IDENTITY_INTEGRATED` and `UNKNOWN` are distinct
  from `NOT_AUTHORIZED`, so services with no Keycloak integration are labelled
  rather than silently reported as "no access".
- **Offboarded identities** report as disabled with their retained downstream
  accounts listed, never as "user not found".
- 133 new integration checks (`make jml-test SUITE=rbac`), including a
  before/after state snapshot proving the simulator mutates nothing.
- `scripts/lib/engine.sh`: one containerised runner shared by the lifecycle
  commands, the simulator and both test suites.

#### Identity lifecycle (roadmap `v2-1`)

- **Joiner/Mover/Leaver automation** across Keycloak, Vault and Gitea, driven by
  declarative role profiles in `identity/profiles.json`:
  - `make jml-join USER=… ROLE=…` provisions the Keycloak user and group
    membership, a Vault `userpass` identity bound to exactly one policy, and a
    Gitea account and team
  - `make jml-move USER=… FROM=… TO=…` removes obsolete access **before**
    adding new access, then prints a before/after diff resolved from live API
    reads rather than from the profile
  - `make jml-leave USER=…` disables both accounts (never deletes), revokes
    sessions and refresh tokens, deletes the Vault identity and revokes its
    outstanding leases, and transfers owned repositories to a custody account
  - `make jml-show USER=…` prints effective access across all three services
  - `make jml-test` runs 105 integration checks against the running lab
- Four role profiles mapped onto the groups the realm already had:
  `developer`, `platform-admin`, `security`, `contractor`. Authorization stays
  group-based; the engine never grants a realm role directly to a user
- `configs/vault/policies/contractor.hcl`: the one policy the seeded realm
  implied but did not ship, with explicit `deny` rules that survive future
  broadening
- Redacted JSON lifecycle records under `artifacts/identity/<user>/`, with a
  single recursive redactor as the chokepoint for "no secrets in artifacts"
- `docs/identity-governance.md`: the lifecycle model, the revocation semantics
  in detail, and an explicit list of what v2-1 deliberately does not cover

The engine runs in a throwaway `python:3.12-alpine` container using only the
standard library, so it adds no host dependency and leaves `docker compose up -d`
unchanged.

### Fixed

- **A long-running caller of the Keycloak adapter could hit a stale admin
  token.** `_authenticate()` caches its token "for the life of one command",
  true for every short-lived `jml`/`rbac` invocation, but not for the new
  access-review test suite, which runs for several minutes across dozens of
  API calls and can outlive the admin token's own expiry inside that single
  process. `_call()` now retries once after a fresh login on a 401.
- **`jml-join` could stack a second role profile onto an existing identity.**
  The joiner only ever added a group, so joining someone who already held a
  different profile left them in both, which is the entitlement accumulation this
  milestone exists to prevent. It now refuses and points at `jml-move`, which
  removes the old access first and records a diff. Found by the RBAC simulator
  while walking all four profiles.
- **A second engine invocation in one shell reported services as down.**
  The shared runner exported the MSYS path-conversion switches, which then
  leaked into the next `docker compose ps` and left its paths unresolvable.
  They are now scoped to the single `docker run`. Surfaced by running both test
  suites in one command.
- **Custom user attributes were silently discarded by the Keycloak Admin API.**
  Keycloak's declarative User Profile is enabled by default and drops any
  attribute that is not declared. There is no error: the write succeeds and the
  value never appears. The realm's seeded users carried `title` and `employeeId`
  only because realm import bypasses that validation; nothing could write them
  afterwards. The lifecycle engine now declares the attributes it manages before
  writing them.

---

## [1.0.0] - 2026-08-03

First release. A complete self-hosted lab that starts with one command.

### Added

#### Core

- Traefik v3.6.17 edge router with automatic Docker service discovery, TLS, per-IP rate limiting and a
  shared security-header middleware chain
- Read-only Docker socket proxy, so Traefik and Promtail never touch the real socket
- PostgreSQL 17 hosting three databases with a separate least-privilege role for each
- Redis 7.4 with an eviction policy, AOF persistence, and the destructive commands renamed away
- Static landing page with live per-service reachability probes and a `/healthz` endpoint

#### Identity and secrets

- Keycloak 26.1 with a seeded realm: 4 users, 4 groups, 6 realm roles, 4 OIDC clients, brute-force
  protection, a password policy, and audit events enabled
- Roles granted to groups rather than to users, modelling real RBAC indirection
- Vault 1.18 with KV v2, the transit engine, AppRole for machines and userpass for humans
- Four ACL policies demonstrating least privilege, including an explicit `deny` on audit paths for the
  administrator policy
- Audit device enabled before provisioning, so provisioning itself is recorded

#### Observability

- Prometheus 3.1 with 7 scrape jobs, time- and size-bounded retention, and 9 alert rules across
  availability, saturation and edge
- Grafana 11.5 with datasources and 3 dashboards provisioned from files: container overview, log
  explorer, and edge/identity
- Loki 3.3 and Promtail, collecting through the Docker API so log collection works identically on Linux,
  macOS and Windows
- cAdvisor and node-exporter for container and host metrics

#### AI

- Ollama with automatic model download on first boot, defaulting to a small model so the lab is usable in
  minutes
- Open WebUI wired to Ollama, with telemetry and outbound API access disabled
- Optional Qdrant vector database behind a Compose profile
- Optional NVIDIA GPU passthrough via `make up GPU=1`

#### Platform

- Gitea 1.23 with PostgreSQL, Redis-backed sessions and cache, Actions enabled, and an administrator
  created automatically
- MinIO with buckets, private-by-default policies, versioning, lifecycle rules and a least-privilege
  application policy, all created on first boot

#### Tools

- Portainer, pgAdmin (pre-registered with all three databases) and Adminer
- Watchtower behind a profile, off by default

#### Automation

- `make secrets`: CSPRNG credentials for every service, guaranteed to satisfy Keycloak's password policy
- `make health`: checks containers, provisioning jobs and HTTP routes separately
- `make backup` / `make restore`: `pg_dumpall` for the database, tarballs for volumes, with a manifest
- `make validate`: everything CI runs, locally, including `promtool` over the alert rules
- `make docs`: regenerates the service catalogue and dependency graph from the compose project itself
- `make creds`: reads live credentials and flags any still using a shipped default

#### Project

- Six-fragment compose structure via the Compose Spec `include:` directive
- Five networks, two of them `internal: true`
- CI: compose validation, healthcheck coverage, image-tag enforcement, YAML, JSON, shell, markdown, link
  checking, `promtool`, and a generated-docs drift check
- Security CI: Gitleaks over full history, Trivy image and configuration scanning
- Issue and pull request templates, CODEOWNERS, grouped Dependabot
- Documentation: architecture, security model, observability, AI, prompt library, troubleshooting

### Security

- Docker secrets for every image with real `_FILE` support
- Non-root users wherever the image allows; `no-new-privileges` on every container
- Exactly one capability grant in the whole stack: `IPC_LOCK` on Vault, to keep secrets out of swap
- Database ports published to `127.0.0.1` only
- Log rotation on every container

### Known limitations

Documented in [`docs/security.md`](docs/security.md#known-gaps). In brief: Vault runs in dev mode,
Keycloak runs `start-dev`, TLS is self-signed, and Portainer holds the Docker socket. All are appropriate
for a local development lab and wrong for production.

---

[Unreleased]: https://github.com/chriswayneh/lab-in-a-box/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/chriswayneh/lab-in-a-box/releases/tag/v1.0.0
