"""
Profiles, input validation and the change log.

Three small things that every adapter needs and none of them owns.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

PROFILES_PATH = Path("/identity/profiles.json")

# -----------------------------------------------------------------------------
# Input validation
#
# Usernames reach Keycloak, Vault and Gitea as URL path segments, and reach the
# filesystem as an artifact directory name. This pattern is the single gate in
# front of all four.
#
# It is deliberately narrower than what any of those services would accept:
# lowercase alphanumerics, dot, dash and underscore, starting with a letter.
# That excludes path traversal ("../"), URL-encoded escapes, shell metacharacters
# and whitespace by construction rather than by escaping them afterwards.
#
# The engine never builds a shell command from user input -- everything is an
# HTTP call with the value in a path segment or a JSON field -- so this is
# defence in depth rather than the only protection.
# -----------------------------------------------------------------------------
USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{1,30}$")

# Seeded identities the automated tests must never touch. The demo realm is part
# of the lab's value; a test that disables alice has broken the product to prove
# a point.
PROTECTED_USERNAMES = frozenset({"alice", "bob", "carol", "dave", "admin", "labadmin"})


class ValidationError(ValueError):
    """Bad operator input. Distinct from a service failure."""


def validate_username(value: str) -> str:
    value = (value or "").strip()
    if not value:
        raise ValidationError("USER is required")
    if not USERNAME_PATTERN.match(value):
        raise ValidationError(
            f"invalid username {value!r}\n"
            "  Must be 2-31 characters, start with a lowercase letter, and contain\n"
            "  only lowercase letters, digits, dot, dash or underscore."
        )
    return value


@dataclass(frozen=True)
class Profile:
    """One role profile, resolved from profiles.json."""

    name: str
    summary: str
    keycloak_group: str
    keycloak_attributes: dict
    effective_roles: list
    vault_policy: str
    vault_grants: list
    gitea_team: str
    gitea_permission: str
    gitea_grants: list


@dataclass
class Catalogue:
    """Everything profiles.json declares."""

    profiles: dict
    gitea_org: str
    gitea_org_full_name: str
    gitea_org_description: str
    gitea_transfer_target: str

    def get(self, name: str) -> Profile:
        key = (name or "").strip().lower()
        if key not in self.profiles:
            known = ", ".join(sorted(self.profiles))
            raise ValidationError(
                f"unknown role profile {name!r}\n  Known profiles: {known}"
            )
        return self.profiles[key]

    @property
    def names(self) -> list:
        return sorted(self.profiles)


def load_catalogue(path: Path = PROFILES_PATH) -> Catalogue:
    if not path.exists():
        raise ValidationError(f"role profiles not found at {path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    gitea = raw.get("gitea", {})
    profiles = {}

    for name, spec in raw.get("profiles", {}).items():
        kc = spec.get("keycloak", {})
        vault = spec.get("vault", {})
        git = spec.get("gitea", {})

        missing = [
            label
            for label, value in (
                ("keycloak.group", kc.get("group")),
                ("vault.policy", vault.get("policy")),
                ("gitea.team", git.get("team")),
            )
            if not value
        ]
        if missing:
            raise ValidationError(
                f"profile {name!r} is missing required field(s): {', '.join(missing)}"
            )

        profiles[name] = Profile(
            name=name,
            summary=spec.get("summary", ""),
            keycloak_group=kc["group"],
            keycloak_attributes=kc.get("attributes", {}),
            effective_roles=kc.get("effective_roles", []),
            vault_policy=vault["policy"],
            vault_grants=vault.get("grants", []),
            gitea_team=git["team"],
            gitea_permission=git.get("permission", "read"),
            gitea_grants=git.get("grants", []),
        )

    if not profiles:
        raise ValidationError("profiles.json declares no profiles")

    return Catalogue(
        profiles=profiles,
        gitea_org=gitea.get("organization", "lab-engineering"),
        gitea_org_full_name=gitea.get("organization_full_name", "Lab Engineering"),
        gitea_org_description=gitea.get("organization_description", ""),
        gitea_transfer_target=gitea.get("repository_transfer_target", "labadmin"),
    )


# -----------------------------------------------------------------------------
# Change tracking
#
# Every adapter reports what it did in these terms, so the terminal summary and
# the JSON artifact are generated from one source rather than assembled twice.
# -----------------------------------------------------------------------------
CREATED = "CREATED"
UPDATED = "UPDATED"
UNCHANGED = "UNCHANGED"
SKIPPED = "SKIPPED"
FAILED = "FAILED"

SUCCESS = "SUCCESS"
PARTIAL_FAILURE = "PARTIAL_FAILURE"
FAILURE = "FAILURE"


@dataclass
class Change:
    status: str
    detail: str


@dataclass
class ServiceResult:
    """What one service reports back from one lifecycle operation."""

    service: str
    changes: list = field(default_factory=list)
    verifications: list = field(default_factory=list)
    error: str | None = None

    def record(self, status: str, detail: str) -> None:
        self.changes.append(Change(status, detail))

    def verify(self, ok: bool, detail: str) -> None:
        self.verifications.append({"ok": bool(ok), "detail": detail})

    @property
    def failed(self) -> bool:
        return self.error is not None or any(c.status == FAILED for c in self.changes)

    @property
    def touched(self) -> bool:
        """True when this run actually changed something."""
        return any(c.status in (CREATED, UPDATED) for c in self.changes)

    @property
    def status(self) -> str:
        if self.failed:
            return FAILED
        if self.touched:
            return UPDATED
        return UNCHANGED

    def as_dict(self) -> dict:
        return {
            "service": self.service,
            "status": self.status,
            "changes": [{"status": c.status, "detail": c.detail} for c in self.changes],
            "verifications": self.verifications,
            "error": self.error,
        }
