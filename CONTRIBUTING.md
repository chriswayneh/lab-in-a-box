# Contributing

Contributions are welcome — particularly dashboards, hardening, platform-specific fixes, and
documentation corrections where something was unclear or wrong.

- [Before you start](#before-you-start)
- [Roadmap work](#roadmap-work)
- [Development setup](#development-setup)
- [The bar for a change](#the-bar-for-a-change)
- [Adding a service](#adding-a-service)
- [Adding a dashboard](#adding-a-dashboard)
- [Style](#style)
- [Commits and pull requests](#commits-and-pull-requests)
- [Review](#review)

---

## Before you start

**For a bug fix**, just open the pull request. Include how to reproduce it.

**For a new service or a structural change**, open an issue first. Every service added to this lab costs
memory on somebody's laptop, seconds on every cold boot, another image to keep patched, and another thing
that can break on a platform you do not have. The
[feature request template](.github/ISSUE_TEMPLATE/feature_request.yml) asks for the resource cost for
exactly this reason.

**For documentation**, no ceremony required. If something was confusing, fixing it is a real contribution.

---

## Roadmap work

The [roadmap](roadmap/README.md) is a commitment to depth over a growing list of containers. If your
idea belongs to an existing roadmap item, open or comment on that issue before implementation so the
scope and acceptance criteria stay shared.

For a new roadmap proposal, open a feature request first. Explain the user problem, resource cost,
cross-platform impact and why the existing services do not already solve it. New default services are
the exception: profile-gated services are preferred when a capability is optional or expensive.

`roadmap/issues.yml` is the source of truth for planned work. A roadmap pull request must update both
that file and the matching summary in `roadmap/README.md`, and include:

- a milestone and stable issue ID (for example, `v5-1`)
- clear acceptance criteria that can be verified
- the effect on the one-command default, resource budget and supported platforms
- dependencies on existing roadmap items, if any

Implementation pull requests should reference their roadmap issue and keep the scope to that issue's
acceptance criteria. If implementation changes the service graph, run `make docs` and commit the
generated catalogue and dependency graph as part of the same pull request.

---

## Development setup

```bash
git clone https://github.com/chriswayneh/lab-in-a-box.git
cd lab-in-a-box
make secrets
make up
make health
```

Then run the checks CI runs:

```bash
make validate
```

That covers the compose project (including profiles and the GPU override), every YAML and JSON file,
shell syntax, healthcheck coverage, and `promtool` over the Prometheus configuration and alert rules.

Useful during development:

```bash
SKIP_UPSTREAM=1 make validate    # skip the checks that pull a container image
make docs                        # regenerate the service catalogue and graph
make logs SERVICE=keycloak
make shell SERVICE=postgres
```

Optional, and worth having:

```bash
pip install yamllint
brew install shellcheck    # or: apt install shellcheck
npm install -g markdownlint-cli2
```

---

## The bar for a change

Three questions, in order:

1. **Does `docker compose up -d` still work with no prior step?** This is the project's central promise.
   A change that requires a manual step first is not a change to this project.

1. **Does it still work on Linux, macOS and Windows?** The lab avoids host bind mounts for data,
   collects logs through the Docker API rather than host paths, and forces LF line endings — all three
   exist because the alternative broke on one platform. Please do not undo them.

1. **Is the reasoning captured where someone will find it?** Comments in this repository explain *why*,
   not *what*. If you made a non-obvious choice, write down what you rejected and why.

---

## Adding a service

Work through this list; the [PR template](.github/PULL_REQUEST_TEMPLATE.md) repeats it as checkboxes.

**Placement.** Put it in the compose fragment that matches its role. If it does not fit any of the six,
that is worth discussing in the issue.

**Networks.** Join only what it actually needs. A service that does not use the database does not go on
`lab_data`. This is the single most valuable habit in the repository.

**Healthcheck.** Required — CI fails without one. Several images ship no shell and no HTTP client, so
verify what is actually available rather than assuming:

```bash
docker run --rm --entrypoint /bin/sh <image> -c "command -v wget curl"
docker inspect <image> --format '{{json .Config.Healthcheck}}'    # it may already have one
```

`docs/architecture.md` lists the unusual probes already in use and why each one is shaped that way.

**Hardening.** Add `security_opt: no-new-privileges:true`. Set a non-root `user:` if the image supports
one. Add `read_only: true` with tmpfs mounts if it writes nothing persistent.

**Credentials.** From `.env` or a Docker secret — never hard-coded. If it needs a new one:

- add it to `.env.example` as `CHANGEME_<NAME>` so `make secrets` fills it in
- add it to `scripts/show-credentials.sh` so `make creds` reports it
- if the image supports `_FILE`, use a Docker secret and add it to the `secrets:` block in
  `docker-compose.yml` and to `SECRET_FILES` in `scripts/generate-secrets.sh`

**State.** Named volume, never a bind mount. Add it to `VOLUMES` in `scripts/backup.sh`, or explain in
the pull request why it should not be backed up.

**Routing.** Traefik labels following the existing pattern, including
`middlewares=lab-default@file,lab-ratelimit@docker`. If your service streams responses, use
`lab-security-headers@file` instead — compression buffers the body and breaks streaming.

**Image tag.** Pin it. If it genuinely must float, say why in `.trivyignore`. CI rejects an image with no
tag at all.

**Provisioning.** If it needs setup, write an idempotent `-init` job following the pattern in
`scripts/init-*.sh`: wait for healthy, converge, exit. Prefer the tool's own declarative mechanism —
a mounted config file, a native import flag — over a script whenever one exists.

**Documentation.** Update the service and ports tables in the README, then run `make docs`.

---

## Adding a dashboard

1. Build it in Grafana.
1. **Share → Export → Save to file**, with *Export for sharing externally* **off**. That option replaces
   datasource uids with template inputs, which breaks provisioning.
1. Save to `monitoring/grafana/dashboards/`.
1. Give it a stable top-level `uid` — CI fails without one.
1. Reference datasources as `{"type": "prometheus", "uid": "prometheus"}` or
   `{"type": "loki", "uid": "loki"}`.
1. Give every panel a `description`. A panel that needs explaining in Slack should have explained itself.

---

## Style

**Comments explain why.** The code already shows what. A comment that restates the line below it is
noise; one that records a rejected alternative is worth its space forever.

```yaml
# Good — records a decision
# Runs unprivileged, which is why the entrypoints bind 8000/8443 instead of
# 80/443: an unprivileged process cannot bind a low port.
user: "1000:1000"

# Bad — restates the line
# Set the user to 1000
user: "1000:1000"
```

**Shell.** `set -euo pipefail` in bash. POSIX `sh` with `set -eu` for anything running inside an
Alpine-based image — those have no bash, and a bashism will pass local checks and fail at runtime. Source
`scripts/lib/common.sh` for logging and helpers.

**YAML.** Two-space indent, `yamllint --strict` clean. Section headers use the `# ===` banner style
already in the files.

**Markdown.** `markdownlint-cli2` clean. Dash bullets, underscore emphasis, fenced code blocks with a
language tag.

**Line endings.** LF everywhere. `.gitattributes` enforces it; do not override it.

---

## Commits and pull requests

[Conventional Commits](https://www.conventionalcommits.org/):

```text
feat(observability): add PostgreSQL exporter and dashboard
fix(keycloak): wait for the admin API rather than assuming it is ready
docs(security): explain why platform-admin cannot touch audit devices
chore(deps): bump grafana to 11.5.1
```

Explain **why** in the body. The diff shows what.

Before opening the pull request:

```bash
make validate
make docs                          # if you added, removed or renamed a service
make clean && make up && make health   # prove it works from nothing
```

That last one matters more than it looks. Most breakage in a project like this is invisible on a warm
machine and obvious on a cold one.

---

## Review

What a reviewer will look at, roughly in order:

1. Does the lab still come up clean from `make clean`?
1. Are the networks minimal?
1. Is there a healthcheck, and does it prove something real?
1. Are credentials generated rather than hard-coded?
1. Is the reasoning for anything non-obvious written down?
1. Are the docs and generated files current?

Review is about the code, never the person. If a change is not going to be merged you will get a clear
reason, not silence.

By contributing you agree that your work is licensed under the [MIT License](LICENSE).
