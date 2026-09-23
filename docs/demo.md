# Ten-minute portfolio demo

This walkthrough is for a technical reviewer, interviewer or teammate who wants
to see the engineering decisions rather than tour a list of product logos. It
demonstrates identity lifecycle, access reasoning, downstream provisioning and
auditable evidence on a laptop.

## Before the conversation

Start the lab and confirm its baseline:

```bash
make up
make health
make creds
```

Open these tabs:

- <https://lab.localhost> — service portal and live reachability
- <https://keycloak.lab.localhost/admin/master/console/> — identity source
- <https://grafana.lab.localhost/d/lab-identity-audit> — audit evidence

The local certificate warning is expected unless the development CA has been
trusted. Do not spend demo time installing certificates; explain the boundary
and continue.

## Minute 0–2: frame the system

Use the [architecture diagram](../README.md#architecture) to establish three
points:

1. This is one Compose project but five networks. Network membership is an
   authorization boundary, not decoration.
1. Provisioning jobs turn source-controlled intent into working service state;
   the operator does not configure dashboards, realms or buckets by hand.
1. Lab shortcuts are explicit. Vault dev mode and local development credentials
   are useful here and unacceptable production defaults.

The platform-engineering story is reproducibility and operability. The identity
story is the full control loop: grant, explain, review, revoke and prove.

## Minute 2–5: identity lifecycle and effective access

Create an identity from a declarative profile, then ask what it can actually do:

```bash
make jml-join USER=erin ROLE=developer
make rbac-show USER=erin
```

Point out that the RBAC answer comes from live Keycloak, Vault and Gitea state,
not from restating the intended profile. Then move the same person and show the
before/after access difference:

```bash
make jml-move USER=erin FROM=developer TO=security
make rbac-show USER=erin
```

Useful design decisions to discuss:

- obsolete grants are removed before new grants are added
- group-inherited and direct access remain distinguishable
- unknown integration is not misreported as denied access
- protected seeded identities cannot be mutated accidentally

## Minute 5–7: evidence and failure behavior

Run the audit integration test:

```bash
make audit-test
```

While it runs, open **Identity Audit Trail** in Grafana. The test creates five
rejected logins, a disposable Keycloak administrative change, Vault secret
access and a privileged policy change. It then proves:

- the events reached Loki with useful bounded labels
- the brute-force and policy-change rules reached `firing`
- the generated Vault secret and active root token are absent from the raw audit
  file, while HMAC values are present

This is the test story in miniature: validate observable behavior and failure
paths, not only whether YAML parses.

## Minute 7–9: downstream provisioning and revocation

Keycloak's SCIM API is the identity source; a persistent reconciler projects its
managed population into Gitea and Grafana with independent bounded retries.

```bash
make scim-status
make jml-leave USER=erin
make rbac-show USER=erin
```

The leaver flow disables rather than deletes the identity, revokes sessions and
refresh tokens, removes Vault access and transfers repository ownership. Keeping
the record while removing access is the important governance distinction.

## Minute 9–10: close with judgment

End with what you would change for production:

- replace Vault dev mode with durable storage, unseal controls and no standing
  root token
- use a trusted certificate and external secret distribution
- put Alertmanager, Prometheus and the Traefik dashboard behind role-aware
  forward auth in roadmap `v2-7` (routing and inhibition already ship as `v2-6`)
- move Loki chunks to object storage and define recovery objectives

The strongest portfolio claim is not “I ran 28 containers.” It is: “I designed
an identity-governance control loop, made its trade-offs explicit, and built
tests that prove the security-relevant outcomes against live systems.”

## Evidence map

| Claim | Evidence |
| --- | --- |
| One-command, cross-platform deployment | `docker-compose.yml`, Compose fragments, `make health` |
| Network least privilege | [Architecture](architecture.md#network-segmentation) and Compose network membership |
| Identity lifecycle and revocation | [Identity governance](identity-governance.md) and `make jml-test` |
| Effective-access reasoning | `make rbac-show`, drift classification and access-review evidence |
| Standards-based provisioning | Keycloak SCIM endpoints, reconciler state and `make scim-test` |
| Auditable identity changes | [Identity audit pipeline](observability.md#identity-audit-pipeline) and `make audit-test` |
| Honest production boundaries | [Security model](security.md) and the roadmap |
