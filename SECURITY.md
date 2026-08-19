# Security policy

## Scope

Lab-in-a-Box is a **development environment intended to run on `localhost`**. Several of its defaults
would be wrong in production, and those are documented deliberately rather than treated as
vulnerabilities. Before reporting, please check [`docs/security.md`](docs/security.md), which lists the
known gaps and the reasoning for each.

### In scope

- A way to escape a container or reach the host that the documentation does not already describe
- A path between networks that should be isolated. For example, reaching `lab_data` from a service that
  is not on it
- Credentials exposed somewhere unexpected: a log, an HTTP response, a container label
- A privilege escalation within the lab: a role or policy granting more than its description implies
- A flaw in the provisioning scripts: command injection, a race, a credential written somewhere durable
- Anything in the shipped Keycloak realm or Vault policies that grants more access than intended

### Not in scope

These are documented characteristics, not vulnerabilities:

- The fallback development credentials committed to this repository. They exist so that
  `docker compose up -d` works with no prior step, every one contains `insecure-dev-only`, and
  `make creds` prints them in red until they are replaced.
- Vault running in dev mode: in-memory, auto-unsealed, with a root token
- Keycloak running `start-dev`
- The self-signed TLS certificate
- Portainer and Watchtower holding the Docker socket
- cAdvisor running privileged
- Prometheus and the Traefik dashboard having no authentication
- Anything requiring the attacker to already have access to your machine or your `.env`
- Vulnerabilities in upstream images. Report those upstream; we will bump the tag

---

## Reporting

**Please do not open a public issue for a security problem.**

Use GitHub's private reporting:

**[Report a vulnerability →](https://github.com/chriswayneh/lab-in-a-box/security/advisories/new)**

Include:

- What the issue is, and which files or services are involved
- How to reproduce it, ideally from a clean `make up`
- What an attacker gains. The impact matters more than the mechanism
- Any suggested fix, if you have one

## What to expect

| | |
| --- | --- |
| Acknowledgement | Within 3 days |
| Initial assessment | Within 7 days |
| Fix for a confirmed high-severity issue | Within 30 days |
| Disclosure | Coordinated with you, after a fix is available |

This is a volunteer-maintained project, so these are targets rather than guarantees. If a report goes
unacknowledged past the stated window, please follow up. It was missed, not ignored.

## Recognition

Reporters are credited in the security advisory and in [`CHANGELOG.md`](CHANGELOG.md) unless they prefer
otherwise. There is no bug bounty.

## Supported versions

The `main` branch is the only supported version. Fixes land there.

---

## Hardening your own deployment

If you intend to run this anywhere other than `localhost`, the required steps are in
[`docs/security.md`](docs/security.md#hardening-for-exposure). At minimum: generated credentials, real
certificates, `LAB_FORCE_HTTPS=true`, no published database ports, authentication in front of every
unauthenticated service, and either removing Portainer or restricting access to it.
