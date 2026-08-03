# Pull request

## What does this change?

<!-- One or two sentences. The "why" matters more than the "what" — the diff
     already shows what changed. -->

## Why?

<!-- The problem this solves. Link the issue if there is one: Closes #123 -->

---

## Type of change

- [ ] Bug fix
- [ ] New service or integration
- [ ] Observability (dashboard, alert, datasource)
- [ ] Security hardening
- [ ] Documentation
- [ ] Tooling (Makefile, scripts, CI)
- [ ] Breaking change — existing labs need action to upgrade

## Checks

- [ ] `make validate` passes locally
- [ ] `make docs` was run if any service was added, removed or renamed
- [ ] I started the lab from scratch (`make clean && make up`) and it came up healthy

## If a service was added or changed

- [ ] It has a healthcheck
- [ ] It joins only the networks it actually needs
- [ ] Any credential it needs comes from `.env` or a Docker secret — nothing hard-coded
- [ ] It declares `security_opt: no-new-privileges` and runs as a non-root user where the image allows it
- [ ] Persistent state is on a named volume, and that volume is in the backup list in `scripts/backup.sh`
- [ ] Its image tag is pinned (or the reason it floats is documented in `.trivyignore`)
- [ ] The README service table and ports table are updated

## If credentials or trust boundaries changed

- [ ] `docs/security.md` reflects the new posture
- [ ] `scripts/generate-secrets.sh` generates any new secret
- [ ] `scripts/show-credentials.sh` reports it

## Resource impact

<!-- Roughly how much memory, disk and first-boot time does this add?
     "None" is a perfectly good answer. -->

## Screenshots

<!-- For anything with a UI — a dashboard, a page, a CLI output change. -->

---

<!-- By opening this pull request you agree that your contribution is licensed
     under the MIT License, the same as the rest of this project. -->
