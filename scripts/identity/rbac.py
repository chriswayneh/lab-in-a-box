#!/usr/bin/env python3
"""
RBAC simulator — "what can this person actually reach?"  (roadmap v2-2)

    make rbac-show    USER=erin
    make rbac-show    USER=erin FORMAT=json
    make rbac-diff    USER=alice OTHER=bob
    make rbac-who-can PERMISSION=vault:secret/data/security/*

STRICTLY READ-ONLY. Every call this module makes is a GET. It never adds a
group, changes a policy, touches a team or disables an account -- and the test
suite snapshots identity state before and after a full run to prove it.

Two things separate this from a prettier `jml-show`:

  1. It RESOLVES the chain rather than restating it. Vault policy documents are
     fetched and parsed into the paths they actually grant; Grafana's role is
     computed from the role_attribute_path expression the deployment really
     uses. Nothing is copied out of profiles.json and presented as fact.

  2. It compares EXPECTED against ACTUAL and reports the difference as drift.
     Expected comes from the role profile implied by the user's current group;
     actual comes from the services. An access review that only reads intended
     state finds nothing, because drift is by definition the part nobody
     intended.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict

from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError, Unavailable
from model import ValidationError, load_catalogue, validate_username
from vault import Vault

# -- decisions ----------------------------------------------------------------
#
# Deliberately more than allow/deny. "I cannot tell" and "nothing enforces this"
# are different answers, and collapsing them into "denied" is how access reviews
# end up confidently wrong.

ALLOWED = "ALLOWED"
NOT_AUTHORIZED = "NOT_AUTHORIZED"
NOT_INTEGRATED = "NOT_IDENTITY_INTEGRATED"
UNKNOWN = "UNKNOWN"

# How a grant arrived.
DIRECT = "direct"
GROUP_INHERITED = "group-inherited"
DERIVED = "derived"

DECISION_ORDER = [ALLOWED, NOT_AUTHORIZED, NOT_INTEGRATED, UNKNOWN]


@dataclass
class Grant:
    """One access fact, with the reason it is true."""

    service: str
    resource: str
    decision: str
    permission: str = ""
    source: str = ""
    inheritance: str = DIRECT
    evidence: str = ""

    def key(self) -> str:
        return f"{self.service}:{self.resource}"


@dataclass
class Drift:
    kind: str
    detail: str
    expected: str
    actual: str


@dataclass
class Report:
    user: str
    exists: bool = False
    enabled: bool = False
    lifecycle_state: str = ""
    inferred_profile: str = ""
    groups: list = field(default_factory=list)
    realm_roles: list = field(default_factory=list)
    grants: list = field(default_factory=list)
    drift: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    def add(self, grant: Grant) -> None:
        self.grants.append(grant)

    def allowed(self) -> list:
        return [g for g in self.grants if g.decision == ALLOWED]


# -----------------------------------------------------------------------------
# Vault policy resolution
# -----------------------------------------------------------------------------

# Matches:  path "secret/data/apps/*" { capabilities = ["read", "update"] }
_VAULT_PATH = re.compile(
    r'path\s+"([^"]+)"\s*\{[^}]*capabilities\s*=\s*\[([^\]]*)\]',
    re.MULTILINE | re.DOTALL,
)


def resource_matches(granted: str, query: str) -> bool:
    """
    Does a grant on `granted` cover a question about `query`?

    Matching runs in BOTH directions, which matters more than it sounds.

    A user holding `secret/*` can reach `secret/data/security/foo`, so asking
    "who can reach secret/data/security/" must return them even though the
    granted string is shorter than the query. Plain prefix matching finds only
    the opposite case, and the resulting silence reads as "nobody can reach
    this" — the most dangerous possible wrong answer for an access review.

    Vault treats a trailing `*` as "everything below here", so both sides are
    stripped of it and compared as prefixes each way.
    """
    g = granted.rstrip("*")
    q = query.rstrip("*")
    if not q:
        return True
    return g.startswith(q) or q.startswith(g)


def parse_vault_policy(document: str) -> list:
    """
    Turn a policy document into (path, capabilities) pairs.

    Reading the document Vault is actually serving, rather than the .hcl file in
    the repository, is the point: if someone edited a policy at runtime the
    review must show what is live, not what is committed.
    """
    grants = []
    for path, caps in _VAULT_PATH.findall(document or ""):
        capabilities = [c.strip().strip('"') for c in caps.split(",") if c.strip()]
        grants.append((path, capabilities))
    return grants


# -----------------------------------------------------------------------------
# Grafana role resolution
#
# The deployment maps realm roles to Grafana roles with this expression:
#
#   contains(roles[*], 'platform-admin') && 'Admin'
#   || contains(roles[*], 'developer')   && 'Editor'
#   || 'Viewer'
#
# Encoded here rather than guessed. If compose changes, this must change with
# it — which is why the mapping is named and commented rather than inlined.
# -----------------------------------------------------------------------------
GRAFANA_ROLE_RULES = [("platform-admin", "Admin"), ("developer", "Editor")]
GRAFANA_FALLBACK_ROLE = "Viewer"


def grafana_role(realm_roles: list) -> tuple:
    for role, grafana in GRAFANA_ROLE_RULES:
        if role in realm_roles:
            return grafana, f"realm role '{role}'"
    return GRAFANA_FALLBACK_ROLE, "no matching realm role; falls through to the default"


# -----------------------------------------------------------------------------
# Services with no identity integration
#
# Listed explicitly so the report can say "nothing enforces this" instead of
# silently omitting them, which would read as "no access".
# -----------------------------------------------------------------------------
UNINTEGRATED_SERVICES = [
    ("Prometheus", "web UI and API", "reachable by anyone who can reach the lab; forward-auth is roadmap v2-7"),
    ("Traefik", "dashboard", "reachable by anyone who can reach the lab; forward-auth is roadmap v2-7"),
    ("Portainer", "console", "local Portainer accounts only, not wired to Keycloak"),
    ("pgAdmin", "console", "single shared login from PGADMIN_EMAIL, not wired to Keycloak"),
    ("Adminer", "console", "no authentication of its own; credentials are the database's"),
    ("MinIO", "object storage", "local MinIO users and policies; no Keycloak integration in this lab"),
    ("Open WebUI", "chat", "a Keycloak client exists in the realm but the deployment does not set OAUTH_* , so sign-in is local"),
]


class Simulator:
    def __init__(self, keycloak: Keycloak, vault: Vault, gitea: Gitea, catalogue):
        self.kc = keycloak
        self.vault = vault
        self.gitea = gitea
        self.catalogue = catalogue
        self._policy_cache: dict = {}

    # -- helpers ------------------------------------------------------------

    def _vault_policy_document(self, name: str) -> str:
        if name not in self._policy_cache:
            try:
                data = self.vault._call("GET", f"/sys/policies/acl/{name}")
                self._policy_cache[name] = (data or {}).get("data", {}).get("policy", "")
            except HttpError:
                self._policy_cache[name] = ""
        return self._policy_cache[name]

    def profile_for_group(self, group_path: str):
        """Reverse-resolve a Keycloak group back to the role profile that targets it."""
        for profile in self.catalogue.profiles.values():
            if profile.keycloak_group == group_path:
                return profile
        return None

    # -- the analysis -------------------------------------------------------

    def analyse(self, username: str) -> Report:
        report = Report(user=username)

        user = self.kc.find_user(username)
        if user is None:
            report.notes.append(
                "No Keycloak identity exists. Downstream accounts are still checked below, "
                "because an account that outlives its identity is exactly the thing worth finding."
            )
        else:
            report.exists = True
            report.enabled = bool(user.get("enabled"))
            attrs = user.get("attributes") or {}
            report.lifecycle_state = (attrs.get("lifecycleState") or [""])[0]
            report.groups = sorted(self.kc.user_groups(user["id"]))
            report.realm_roles = self.kc.effective_realm_roles(user["id"])

        expected = None
        if len(report.groups) == 1:
            expected = self.profile_for_group(report.groups[0])
            if expected:
                report.inferred_profile = expected.name
        elif len(report.groups) > 1:
            report.notes.append(
                "Member of more than one group, so no single role profile describes this identity."
            )

        self._keycloak_grants(report, user, expected)
        self._vault_grants(report, username, expected)
        self._gitea_grants(report, username, expected)
        self._grafana_grants(report)
        self._unintegrated(report)
        self._detect_drift(report, expected)

        return report

    def _keycloak_grants(self, report: Report, user, expected) -> None:
        if not report.exists:
            report.add(Grant("Keycloak", "account", NOT_AUTHORIZED,
                             source="no identity exists in the realm"))
            return

        if not report.enabled:
            report.add(Grant(
                "Keycloak", "account", NOT_AUTHORIZED,
                permission="disabled",
                source=f"account is disabled (lifecycleState={report.lifecycle_state or 'unset'})",
                evidence="retained for audit rather than deleted",
            ))
        else:
            report.add(Grant("Keycloak", "account", ALLOWED, permission="sign-in",
                             source="account is enabled"))

        for group in report.groups:
            report.add(Grant("Keycloak", f"group {group}", ALLOWED, permission="member",
                             source="group membership", inheritance=DIRECT))

        for role in report.realm_roles:
            granting = [g for g in report.groups if role in self._group_roles(g)]
            if granting:
                report.add(Grant(
                    "Keycloak", f"role {role}", ALLOWED, permission="realm role",
                    source=f"granted by group {', '.join(granting)}",
                    inheritance=GROUP_INHERITED,
                    evidence="read from the composite role mapping",
                ))
            else:
                # A role held without a group granting it is a direct assignment,
                # which this lab's model says should not happen.
                report.add(Grant(
                    "Keycloak", f"role {role}", ALLOWED, permission="realm role",
                    source="assigned directly to the user, not via a group",
                    inheritance=DIRECT,
                    evidence="no group in this realm grants this role",
                ))

    def _group_roles(self, group_path: str) -> list:
        try:
            return self.kc.group_by_path(group_path).get("realmRoles", []) or []
        except LookupError:
            return []

    def _vault_grants(self, report: Report, username: str, expected) -> None:
        try:
            policies = self.vault.user_policies(username)
        except (HttpError, Unavailable) as exc:
            report.add(Grant("Vault", "userpass identity", UNKNOWN,
                             source=f"Vault could not be queried: {exc}"))
            return

        if policies is None:
            report.add(Grant("Vault", "userpass identity", NOT_AUTHORIZED,
                             source="no userpass identity exists"))
            return

        for policy in policies:
            source = "Vault userpass identity"
            inheritance = DIRECT
            if expected and policy == expected.vault_policy:
                source = f"role profile '{expected.name}' via group {expected.keycloak_group}"
                inheritance = DERIVED

            report.add(Grant("Vault", f"policy {policy}", ALLOWED, permission="attached",
                             source=source, inheritance=inheritance))

            for path, caps in parse_vault_policy(self._vault_policy_document(policy)):
                deny = caps == ["deny"]
                report.add(Grant(
                    "Vault", path,
                    NOT_AUTHORIZED if deny else ALLOWED,
                    permission="deny" if deny else ", ".join(caps),
                    source=f"policy {policy}",
                    inheritance=DERIVED,
                    evidence="parsed from the policy document Vault is serving",
                ))

    def _gitea_grants(self, report: Report, username: str, expected) -> None:
        try:
            user = self.gitea.find_user(username)
        except (HttpError, Unavailable) as exc:
            report.add(Grant("Gitea", "account", UNKNOWN,
                             source=f"Gitea could not be queried: {exc}"))
            return

        if user is None:
            report.add(Grant("Gitea", "account", NOT_AUTHORIZED,
                             source="no Gitea account exists"))
            return

        active = bool(user.get("active", True))
        report.add(Grant(
            "Gitea", "account",
            ALLOWED if active else NOT_AUTHORIZED,
            permission="sign-in" if active else "deactivated",
            source="account is active" if active else "account is deactivated",
            evidence="retained so commit attribution survives" if not active else "",
        ))

        teams = self.gitea.team_memberships(username)
        all_teams = {t["name"]: t for t in (self.gitea._call("GET", f"/orgs/{self.gitea.org}/teams") or [])}

        for team in teams:
            permission = (all_teams.get(team) or {}).get("permission", "unknown")
            source = f"team membership in {self.gitea.org}"
            inheritance = DIRECT
            if expected and team == expected.gitea_team:
                source = f"role profile '{expected.name}' via group {expected.keycloak_group}"
                inheritance = DERIVED
            report.add(Grant(
                "Gitea", f"team {team}", ALLOWED, permission=permission,
                source=source, inheritance=inheritance,
                evidence=f"repositories in {self.gitea.org}",
            ))

        owned = [r["full_name"] for r in self.gitea.owned_repositories(username)]
        for repo in owned:
            report.add(Grant("Gitea", f"repository {repo}", ALLOWED, permission="owner",
                             source="personally owned", inheritance=DIRECT))

    def _grafana_grants(self, report: Report) -> None:
        """
        Grafana is the one service whose access is genuinely derivable.

        It reads roles from the Keycloak token and maps them with a
        role_attribute_path expression, so the resulting Grafana role follows
        from the realm roles already resolved above -- provided single sign-on
        is switched on at all.
        """
        oidc_enabled = os.environ.get("GRAFANA_OIDC_ENABLED", "false").strip().lower() == "true"

        if not oidc_enabled:
            report.add(Grant(
                "Grafana", "sign-in", NOT_INTEGRATED,
                source="GRAFANA_OIDC_ENABLED is false; Grafana uses its local admin account",
                evidence="set GRAFANA_OIDC_ENABLED=true in .env to route Grafana through Keycloak",
            ))
            return

        if not report.enabled:
            report.add(Grant("Grafana", "sign-in", NOT_AUTHORIZED,
                             source="Keycloak account is disabled, so the OIDC login fails"))
            return

        role, why = grafana_role(report.realm_roles)
        report.add(Grant(
            "Grafana", "organisation role", ALLOWED, permission=role,
            source=why, inheritance=DERIVED,
            evidence="computed from GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH",
        ))

    def _unintegrated(self, report: Report) -> None:
        for service, resource, why in UNINTEGRATED_SERVICES:
            report.add(Grant(service, resource, NOT_INTEGRATED, source=why))

    # -- drift ---------------------------------------------------------------

    def _detect_drift(self, report: Report, expected) -> None:
        """
        Compare what the identity SHOULD have against what it HAS.

        Read-only: drift is reported, never corrected. Remediation is a
        deliberate act with an audit trail, which is the lifecycle commands'
        job, not an analysis tool's.
        """
        actual_vault = [g.resource.replace("policy ", "")
                        for g in report.grants
                        if g.service == "Vault" and g.resource.startswith("policy ")]
        actual_teams = [g.resource.replace("team ", "")
                        for g in report.grants
                        if g.service == "Gitea" and g.resource.startswith("team ")]

        # An offboarded or group-less identity should hold nothing downstream.
        if not report.groups:
            state = "offboarded" if report.exists and not report.enabled else "no group membership"
            for policy in actual_vault:
                report.drift.append(Drift(
                    "STALE_VAULT_POLICY",
                    f"Vault policy '{policy}' remains after {state}",
                    expected="no Vault identity", actual=f"policy {policy}"))
            for team in actual_teams:
                report.drift.append(Drift(
                    "STALE_GITEA_TEAM",
                    f"Gitea team '{team}' remains after {state}",
                    expected="no team membership", actual=f"team {team}"))
            if report.exists and report.enabled and not report.groups:
                report.drift.append(Drift(
                    "NO_GROUP",
                    "Account is enabled but belongs to no group, so it inherits no roles",
                    expected="membership of one group", actual="none"))
            return

        if len(report.groups) > 1:
            report.drift.append(Drift(
                "EXTRA_KEYCLOAK_GROUP",
                f"Member of {len(report.groups)} groups: {', '.join(report.groups)}",
                expected="exactly one group", actual=", ".join(report.groups)))

        if not expected:
            report.notes.append(
                "Current group does not correspond to any role profile, so expected "
                "downstream access cannot be derived and drift detection is limited."
            )
            return

        if actual_vault != [expected.vault_policy]:
            for policy in actual_vault:
                if policy != expected.vault_policy:
                    report.drift.append(Drift(
                        "UNEXPECTED_VAULT_POLICY",
                        f"Holds Vault policy '{policy}', which profile '{expected.name}' does not grant",
                        expected=expected.vault_policy, actual=policy))
            if expected.vault_policy not in actual_vault:
                report.drift.append(Drift(
                    "MISSING_VAULT_POLICY",
                    f"Profile '{expected.name}' expects Vault policy '{expected.vault_policy}', which is absent",
                    expected=expected.vault_policy, actual=", ".join(actual_vault) or "none"))

        for team in actual_teams:
            if team != expected.gitea_team:
                report.drift.append(Drift(
                    "EXTRA_GITEA_TEAM",
                    f"Member of Gitea team '{team}', which profile '{expected.name}' does not grant",
                    expected=expected.gitea_team, actual=team))
        if expected.gitea_team not in actual_teams:
            report.drift.append(Drift(
                "MISSING_GITEA_TEAM",
                f"Profile '{expected.name}' expects Gitea team '{expected.gitea_team}', which is absent",
                expected=expected.gitea_team, actual=", ".join(actual_teams) or "none"))

        for role in expected.effective_roles:
            if role not in report.realm_roles:
                report.drift.append(Drift(
                    "MISSING_REALM_ROLE",
                    f"Profile '{expected.name}' expects realm role '{role}', which is not effective",
                    expected=role, actual="absent"))
