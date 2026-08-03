# Prompt library

Prompts that do something useful with a model running next to your infrastructure.

The point of a local LLM in a lab like this is not that it is better than a hosted one — it is not. The
point is that you can paste your actual configuration, real logs and internal architecture into it
without a data-handling conversation first.

> **Note**
> Most of these work acceptably on a 7–8B model. The default `llama3.2:1b` will manage summarisation and
> explanation but will struggle with anything needing multi-step reasoning. Prompts that particularly
> want a larger model are marked **[8B+]**.

- [Understanding infrastructure](#understanding-infrastructure)
- [Log analysis](#log-analysis)
- [Security review](#security-review)
- [Debugging](#debugging)
- [Documentation](#documentation)
- [Identity and access](#identity-and-access)
- [Getting better answers](#getting-better-answers)

---

## Understanding infrastructure

**Explain the stack.** Paste `docker-compose.yml` plus one fragment.

```text
This is a Docker Compose stack. Explain what it does service by service, then
tell me:

1. Which containers hold the most privilege, and why they need it
2. Which services could be removed without breaking anything else
3. What the blast radius is if the web-facing service is compromised
```

**Explain one service's configuration.**

```text
Here is the Traefik service definition from my Compose file. Explain each
command-line flag in plain language, and flag any that look like they were
copied from an example without being understood.
```

**Map the dependency graph.** Paste `architecture/dependency-graph.mmd`.

```text
This is a Mermaid dependency graph of my container stack. Which services are
on the critical path for a cold start? If I wanted to reduce time-to-ready,
where would I look first?
```

**Compare against a reference.** **[8B+]**

```text
Here is my Prometheus scrape configuration. Compare it against what you would
expect for a Docker-based stack with a reverse proxy, an identity provider and
object storage. What am I not monitoring that I probably should be?
```

---

## Log analysis

Get the input with:

```bash
docker compose logs --tail 200 keycloak | pbcopy      # macOS
docker compose logs --tail 200 keycloak | clip        # Windows
docker compose logs --tail 200 keycloak | xclip -sel c  # Linux
```

**Summarise.**

```text
Here are the last 200 lines from my Keycloak container. Summarise what
happened in chronological order. Separate normal startup activity from
anything that indicates a real problem. If it is all normal, say so plainly
rather than inventing concerns.
```

That last sentence matters. Without it, models will reliably manufacture a problem because they infer
that you asked for a reason.

**Find the first cause.**

```text
These logs are from a container that is restarting in a loop. Work backwards
from the failure to the earliest line that indicates something was already
wrong, and tell me which line that is.
```

**Correlate across services.** **[8B+]**

```text
Here are logs from three containers over the same five-minute window: Traefik,
Keycloak and PostgreSQL. A user reported a failed login at roughly 14:32.
Trace the request through all three and tell me where it failed.
```

**Turn a log pattern into a query.**

```text
I want a Loki query that finds failed login attempts in Keycloak, grouped by
source IP, over the last hour. My logs are labelled with `service` and
`container`. Give me the LogQL and explain each stage.
```

**Explain an error you have never seen.**

```text
What does this error mean, what typically causes it, and what should I check
first?

<paste the error>
```

---

## Security review

**Review the Compose configuration.**

```text
Review this Docker Compose file from a security perspective. For each finding,
tell me the concrete risk, not just the rule that was broken, and rank them by
how much they actually matter for a lab running on localhost.
```

**Review a Vault policy.**

```text
Given this Vault ACL policy, what could a holder of a token with this policy
actually do that the author probably did not intend? Pay attention to path
wildcards and to capabilities that combine into something larger.

<paste policy>
```

**Review Traefik middleware.**

```text
Here are my Traefik middleware definitions. What attacks do these mitigate,
and — more usefully — what do they not cover that someone might assume they do?
```

**Reason about a trust boundary.** **[8B+]**

```text
My stack has five Docker networks, two of them internal. Here are the network
memberships per service. If an attacker achieves remote code execution in the
chat interface container, what can they reach, and what is the most valuable
thing they could get to?
```

**Check a change before it merges.**

```text
This diff adds a new service to my Compose stack. Does it follow the same
security posture as the existing services — non-root user, no-new-privileges,
minimal networks, healthcheck, no hard-coded credentials? List what is missing.
```

---

## Debugging

**Interpret a healthcheck failure.**

```text
This container is marked unhealthy. Here is its healthcheck definition and the
last 50 lines of its logs. Is the service actually broken, or is the
healthcheck wrong?
```

That distinction is worth asking for explicitly — a wrong healthcheck and a broken service look identical
from the outside, and models default to assuming the service is at fault.

**Explain a networking problem.**

```text
Container A cannot reach container B by hostname. Here are both service
definitions including their networks. What is wrong?
```

**Decode a PromQL expression.**

```text
Explain this PromQL query step by step, including why each function is there
and what would break if it were removed:

<paste query>
```

**Write an alert rule.**

```text
I want a Prometheus alert that fires when a container's memory working set
exceeds 90% of its limit for ten minutes, but only for containers that have a
limit set. Write the rule with annotations that tell the reader what to do.
```

---

## Documentation

**Document a service.**

```text
Write a short operational runbook for this service based on its Compose
definition: what it does, what it depends on, what its healthcheck actually
proves, how to check whether it is working, and the two or three most likely
failure modes.
```

**Explain a decision for a newcomer.**

```text
Explain to someone who has used Docker but never Traefik why this stack uses a
Docker socket proxy instead of mounting the socket directly. Assume they will
push back that the proxy is over-engineering.
```

**Generate an architecture decision record.** **[8B+]**

```text
Turn this into an ADR with Context, Decision, Consequences and Alternatives
Considered:

We chose to run one PostgreSQL instance with three databases and three roles,
rather than three PostgreSQL containers, to save memory and patching overhead
while keeping isolation between services.
```

**Write a commit message.**

```text
Write a conventional-commits message for this diff. Explain why in the body,
not what — the diff already shows what.
```

---

## Identity and access

**Explain the RBAC model.** Paste `configs/keycloak/realm-export.json`.

```text
This is a Keycloak realm export. Describe the access model: which groups exist,
what roles they grant, and what each user can therefore do. Then tell me
whether any group has more access than its description suggests it needs.
```

**Design a joiner/mover/leaver flow.** **[8B+]**

```text
Given this group and role structure, design the identity lifecycle:

- What happens when a contractor joins
- What changes when they move from Contractors to Application Engineering
- What must happen on their last day, and in what order

Call out anything that would be missed by only disabling the account.
```

**Review an OIDC client.**

```text
Here is an OIDC client configuration from my Keycloak realm. Is it configured
appropriately for a confidential server-side application? Look specifically at
the flows enabled, the redirect URIs, and whether any wildcard is dangerous.
```

---

## Getting better answers

A local 7B model is not a hosted frontier model, and prompting it as though it were produces
disappointing results. What actually helps:

**Give it the artefact, not a description of the artefact.** Paste the actual Compose file, the actual
logs, the actual policy. These models are much better at analysing text in front of them than at
recalling how a tool works.

**Ask for one thing.** "Explain this and find security issues and suggest improvements" produces three
shallow answers. Three separate prompts produce three good ones.

**Give it permission to find nothing.** Add *"If there is nothing wrong, say so"* to any review prompt.
Models infer from being asked that a problem exists, and will invent one.

**Ask for reasoning before conclusions.** *"Work through this step by step before giving your answer"*
measurably improves results on anything involving more than one inference.

**Set the frame.** *"You are reviewing this as a security engineer who has to justify each finding to a
sceptical team"* produces sharper output than an unframed question.

**Verify anything specific.** Model output about exact flag names, version numbers and API paths is
frequently wrong and always confident. It is a fast way to form a hypothesis, not a source of truth. Check
it against the actual documentation before acting on it.

**Adjust the temperature for the task.** In Open WebUI, under a chat's settings: lower (0.1–0.3) for
analysis and code, higher (0.7–0.9) for brainstorming.
