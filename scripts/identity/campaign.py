"""
Access review campaigns  (roadmap v2-3).

    make access-review-create   NAME=quarterly-q3 [SCOPE=all|profile:X|user:X]
    make access-review-show     CAMPAIGN=<id>
    make access-review-list
    make access-review-decide   CAMPAIGN=<id> USER=erin ENTITLEMENT="Gitea:team developers" DECISION=approve
    make access-review-complete CAMPAIGN=<id>
    make access-review-remediate CAMPAIGN=<id>

A campaign answers: who currently has access to what, who should review it,
what decision was made, and what evidence proves the review happened.

This module owns the workflow. It does not discover access on its own -- every
entitlement a campaign reviews comes from rbac.Simulator, the same engine
`make rbac-show` uses. A campaign is a review layered on top of that engine,
not a second authorization model.

Lifecycle: DRAFT -> OPEN -> COMPLETED, with CANCELLED reachable from either of
the first two. `create` moves straight from DRAFT to OPEN in one step, because
no command in this feature needs a scope to sit undecided before its snapshot
is taken -- draft exists as a state so the model does not conflate "captured"
with "decided", not because there is a separate command that pauses there.

Decision is not remediation. Recording REVOKE on an item only marks it PENDING
remediation; nothing about the live systems changes until `remediate` runs, and
`remediate` reuses the exact Keycloak/Vault/Gitea adapters the JML commands use
-- there is no second implementation of "remove a group" or "drop a policy"
here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import rbac
from labhttp import HttpError, Unavailable
from model import (
    FAILED,
    PROTECTED_USERNAMES,
    UNCHANGED,
    ServiceResult,
    ValidationError,
    validate_username,
)
from record import redact

ARTIFACT_ROOT = Path(os.environ.get("LAB_ARTIFACT_DIR", "/artifacts")) / "access-review"

# -- campaign lifecycle --------------------------------------------------------

DRAFT = "draft"
OPEN = "open"
COMPLETED = "completed"
CANCELLED = "cancelled"

# -- review decisions -----------------------------------------------------------
#
# UNDECIDED is a real state, not an absence of one. It is written into every
# item explicitly and rendered explicitly, so an item nobody has looked at
# cannot be mistaken for one that was approved.

UNDECIDED = "UNDECIDED"
APPROVE = "APPROVE"
REVOKE = "REVOKE"
NOT_APPLICABLE = "NOT_APPLICABLE"

DECISIONS = {
    "approve": APPROVE,
    "revoke": REVOKE,
    "not-applicable": NOT_APPLICABLE,
}

# -- remediation status ---------------------------------------------------------
#
# A revoke decision is a governance decision, not a live-system change. This is
# the field that tracks the difference: PENDING means "decided but not yet
# acted on"; VERIFIED means a fresh read of the service confirms the access is
# actually gone, not merely that an API call returned success.

REM_NOT_REQUIRED = "NOT_REQUIRED"
REM_PENDING = "PENDING"
REM_VERIFIED = "VERIFIED"
REM_FAILED = "FAILED"
REM_MANUAL = "MANUAL_ACTION_REQUIRED"
REM_SKIPPED_PROTECTED = "SKIPPED_PROTECTED_IDENTITY"

# -- input validation -------------------------------------------------------
#
# Same defence-in-depth philosophy as model.validate_username: narrow enough to
# exclude path traversal and shell metacharacters by construction, because a
# campaign name becomes a directory name and nothing here ever reaches a shell.

CAMPAIGN_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,60}$")
# Matches exactly what slugify_id produces: <name>-<8 digits>T<6 digits>Z. The
# literal T and Z are the ISO-8601 basic-format markers slugify_id writes via
# strftime('%Y%m%dT%H%M%SZ'), the same convention record.py uses -- not
# ordinary name characters, so the general name charset above does not cover
# them and this needs its own pattern.
CAMPAIGN_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,60}-[0-9]{8}T[0-9]{6}Z$")
ENTITLEMENT_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9 _-]*:[\x20-\x7e]{1,180}$")


def validate_campaign_name(value: str) -> str:
    # Not lowercased: matches model.validate_username, which requires
    # lowercase as typed rather than silently folding it. Diverging from that
    # for campaign names would mean the same class of input is accepted here
    # and rejected there for no real reason.
    value = (value or "").strip()
    if not value:
        raise ValidationError("NAME is required")
    if not CAMPAIGN_NAME_PATTERN.match(value):
        raise ValidationError(
            f"invalid campaign name {value!r}\n"
            "  Must be 2-61 characters, start with a lowercase letter, and contain\n"
            "  only lowercase letters, digits and dashes."
        )
    return value


def validate_campaign_id(value: str) -> str:
    # Not lowercased, unlike validate_campaign_name: an id embeds the
    # uppercase T/Z timestamp markers slugify_id writes, and folding case would
    # turn a real id into one the pattern below can no longer match.
    value = (value or "").strip()
    if not value:
        raise ValidationError("CAMPAIGN is required")
    if not CAMPAIGN_ID_PATTERN.match(value):
        raise ValidationError(f"invalid campaign id {value!r}")
    return value


def validate_entitlement(value: str) -> str:
    value = (value or "").strip()
    if not value or ":" not in value:
        raise ValidationError(
            f"invalid ENTITLEMENT {value!r}\n"
            "  Expected 'Service:resource', e.g. 'Gitea:team developers' -- copy it\n"
            "  from the resource column of 'make access-review-show CAMPAIGN=...'."
        )
    if not ENTITLEMENT_PATTERN.match(value):
        raise ValidationError(f"ENTITLEMENT {value!r} contains characters that are not allowed")
    return value


def validate_decision(value: str) -> str:
    key = (value or "").strip().lower()
    if key not in DECISIONS:
        raise ValidationError(
            f"invalid DECISION {value!r}\n"
            f"  Expected one of: {', '.join(sorted(DECISIONS))}"
        )
    return DECISIONS[key]


def slugify_id(name: str, moment: datetime) -> str:
    return f"{name}-{moment.strftime('%Y%m%dT%H%M%SZ')}"


def compute_item_id(username: str, key: str) -> str:
    """
    Stable identifier for one (identity, entitlement) pairing within a
    campaign. Deterministic so the same review target always hashes the same
    way, which is useful for audit cross-referencing; it is not how items are
    addressed from the CLI -- USER plus ENTITLEMENT does that -- so it does not
    need to be typed by an operator.
    """
    return hashlib.sha256(f"{username}:{key}".encode()).hexdigest()[:12]


# -----------------------------------------------------------------------------
# Data model
# -----------------------------------------------------------------------------


@dataclass
class ReviewItem:
    item_id: str
    username: str
    service: str
    resource: str
    permission: str
    source: str
    inheritance: str
    expectation: str  # rbac.EXPECTED / UNEXPECTED / NOT_MODELED / NO_PROFILE
    decision: str = UNDECIDED
    note: str = ""
    decided_at: str | None = None
    decided_by: str | None = None
    remediation_status: str = REM_NOT_REQUIRED
    remediation_detail: str = ""

    def key(self) -> str:
        return f"{self.service}:{self.resource}"

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict) -> "ReviewItem":
        return ReviewItem(**data)


@dataclass
class Campaign:
    id: str
    name: str
    status: str
    scope: str
    reviewer: str
    created_at: str
    opened_at: str | None = None
    completed_at: str | None = None
    cancelled_at: str | None = None
    identities: list = field(default_factory=list)
    items: list = field(default_factory=list)          # list[ReviewItem]
    drift_context: dict = field(default_factory=dict)  # username -> [Drift-as-dict]
    post_snapshot_drift: dict = field(default_factory=dict)
    notes: list = field(default_factory=list)

    def find_item(self, username: str, entitlement_key: str) -> ReviewItem | None:
        for it in self.items:
            if it.username == username and it.key() == entitlement_key:
                return it
        return None

    def items_for(self, username: str) -> list:
        return [it for it in self.items if it.username == username]

    def undecided(self) -> list:
        return [it for it in self.items if it.decision == UNDECIDED]

    def summary(self) -> dict:
        by_decision: dict = {}
        for it in self.items:
            by_decision[it.decision] = by_decision.get(it.decision, 0) + 1
        by_remediation: dict = {}
        for it in self.items:
            if it.decision == REVOKE:
                by_remediation[it.remediation_status] = by_remediation.get(it.remediation_status, 0) + 1
        return {
            "identities": len(self.identities),
            "items": len(self.items),
            "byDecision": by_decision,
            "revokeRemediation": by_remediation,
        }

    def to_dict(self) -> dict:
        d = asdict(self)
        d["items"] = [it.to_dict() for it in self.items]
        return d

    @staticmethod
    def from_dict(data: dict) -> "Campaign":
        data = dict(data)
        items = [ReviewItem.from_dict(i) for i in data.pop("items", [])]
        campaign = Campaign(**{k: v for k, v in data.items() if k != "schemaVersion"})
        campaign.items = items
        return campaign


# -----------------------------------------------------------------------------
# Storage
#
# campaign.json is the live working document: created once, then rewritten by
# every decide/complete/remediate call. It is not itself the audit trail --
# `complete` and `remediate` additionally write a timestamped, never-rewritten
# copy alongside it, so a later edit to campaign.json cannot quietly change
# what the record once said.
# -----------------------------------------------------------------------------


def _campaign_dir(campaign_id: str) -> Path:
    return ARTIFACT_ROOT / campaign_id


def save(campaign: Campaign) -> Path:
    directory = _campaign_dir(campaign.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "campaign.json"
    document = redact({"schemaVersion": 1, **campaign.to_dict()})
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


def load(campaign_id: str) -> Campaign:
    campaign_id = validate_campaign_id(campaign_id)
    path = _campaign_dir(campaign_id) / "campaign.json"
    if not path.exists():
        raise ValidationError(
            f"no campaign {campaign_id!r}\n  Run 'make access-review-list' to see what exists."
        )
    return Campaign.from_dict(json.loads(path.read_text(encoding="utf-8")))


def list_campaigns() -> list:
    if not ARTIFACT_ROOT.exists():
        return []
    campaigns = []
    for entry in sorted(ARTIFACT_ROOT.iterdir()):
        path = entry / "campaign.json"
        if path.exists():
            try:
                campaigns.append(Campaign.from_dict(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
    return campaigns


def write_evidence(campaign: Campaign, event: str, payload: dict) -> Path:
    """
    An immutable, timestamped evidence record for one campaign transition.

    Separate from campaign.json (the live document) for the same reason
    record.py's lifecycle records are separate from live identity state: the
    working document keeps changing; the evidence must not.
    """
    now = datetime.now(timezone.utc)
    document = redact({
        "schemaVersion": 1,
        "campaignId": campaign.id,
        "campaignName": campaign.name,
        "event": event,
        "timestamp": now.isoformat(timespec="seconds"),
        **payload,
    })
    directory = _campaign_dir(campaign.id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{event}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path


# -----------------------------------------------------------------------------
# Scope resolution
# -----------------------------------------------------------------------------


def resolve_scope(scope: str, kc, catalogue) -> list:
    """
    Turn a SCOPE string into the list of usernames a campaign will review.

    Three forms, deliberately not a query language:
      all               every identity in the realm
      profile:<name>    everyone currently in that role profile's group
      user:<name>       exactly one identity
    """
    scope = (scope or "all").strip()

    if scope == "all":
        return kc.all_usernames()

    if scope.startswith("profile:"):
        profile_name = scope[len("profile:"):].strip()
        profile = catalogue.get(profile_name)  # raises ValidationError if unknown
        try:
            return kc.group_members(profile.keycloak_group)
        except LookupError as exc:
            raise ValidationError(str(exc)) from exc

    if scope.startswith("user:"):
        return [validate_username(scope[len("user:"):].strip())]

    raise ValidationError(
        f"invalid SCOPE {scope!r}\n"
        "  Expected 'all', 'profile:<name>' or 'user:<username>'."
    )


# -----------------------------------------------------------------------------
# Create / open
# -----------------------------------------------------------------------------


def create_campaign(sim: "rbac.Simulator", kc, catalogue, name: str, scope: str, reviewer: str) -> Campaign:
    name = validate_campaign_name(name)
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat(timespec="seconds")
    campaign_id = slugify_id(name, now)

    if _campaign_dir(campaign_id).exists():
        raise ValidationError(
            f"a campaign already exists at {campaign_id!r} (same name, same second)\n"
            "  Try again, or use a different NAME."
        )

    identities = sorted(set(resolve_scope(scope, kc, catalogue)))

    campaign = Campaign(
        id=campaign_id,
        name=name,
        status=DRAFT,
        scope=scope or "all",
        reviewer=reviewer or "unspecified",
        created_at=now_iso,
        identities=identities,
    )

    items: list = []
    drift_context: dict = {}
    for username in identities:
        report = sim.analyse(username)
        drift_context[username] = [asdict(d) for d in report.drift]
        for grant in report.allowed():
            expectation = sim.classify_expectation(report, grant)
            items.append(ReviewItem(
                item_id=compute_item_id(username, grant.key()),
                username=username,
                service=grant.service,
                resource=grant.resource,
                permission=grant.permission,
                source=grant.source,
                inheritance=grant.inheritance,
                expectation=expectation,
            ))

    campaign.items = items
    campaign.drift_context = drift_context
    campaign.status = OPEN
    campaign.opened_at = now_iso

    if not identities:
        campaign.notes.append(f"scope {scope!r} matched no identities")

    save(campaign)
    write_evidence(campaign, "opened", {
        "scope": campaign.scope,
        "identitiesReviewed": identities,
        "itemCount": len(items),
        "driftContext": drift_context,
    })
    return campaign


# -----------------------------------------------------------------------------
# Decide
# -----------------------------------------------------------------------------


def decide(campaign: Campaign, username: str, entitlement_key: str, decision: str,
           note: str, reviewer: str, force: bool) -> ReviewItem:
    if campaign.status != OPEN:
        raise ValidationError(
            f"campaign {campaign.id!r} is {campaign.status}, not open -- decisions can only be "
            "recorded on an open campaign"
        )

    item = campaign.find_item(username, entitlement_key)
    if item is None:
        available = ", ".join(it.key() for it in campaign.items_for(username)) or "(none)"
        raise ValidationError(
            f"{username!r} has no reviewable entitlement {entitlement_key!r} in this campaign\n"
            f"  Available for {username}: {available}"
        )

    if item.decision != UNDECIDED and not force:
        raise ValidationError(
            f"{username}/{entitlement_key} was already decided: {item.decision} "
            f"at {item.decided_at} by {item.decided_by}\n"
            "  Pass FORCE=1 to override."
        )

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    item.decision = decision
    item.note = (note or "").strip()
    item.decided_at = now_iso
    item.decided_by = reviewer or campaign.reviewer

    if decision == REVOKE:
        item.remediation_status = REM_PENDING
        item.remediation_detail = ""
    else:
        item.remediation_status = REM_NOT_REQUIRED
        item.remediation_detail = ""

    save(campaign)
    return item


# -----------------------------------------------------------------------------
# Complete
# -----------------------------------------------------------------------------


def complete(campaign: Campaign, sim: "rbac.Simulator", force: bool) -> Campaign:
    if campaign.status == COMPLETED:
        return campaign  # idempotent: already done, nothing to re-write
    if campaign.status == CANCELLED:
        raise ValidationError(f"campaign {campaign.id!r} was cancelled and cannot be completed")

    undecided = campaign.undecided()
    if undecided and not force:
        names = ", ".join(f"{it.username}/{it.key()}" for it in undecided[:10])
        more = f" and {len(undecided) - 10} more" if len(undecided) > 10 else ""
        raise ValidationError(
            f"{len(undecided)} item(s) are still undecided: {names}{more}\n"
            "  Decide every item first, or pass FORCE=1 to close the campaign anyway.\n"
            "  Forcing does not mark them approved -- they are recorded as UNDECIDED."
        )

    # Surface access that changed since the campaign opened. The original
    # snapshot is never rewritten; this is a separate, additional comparison.
    post_drift: dict = {}
    for username in campaign.identities:
        report = sim.analyse(username)
        current_keys = {g.key() for g in report.allowed()}
        original_keys = {it.key() for it in campaign.items_for(username)}
        gained = sorted(current_keys - original_keys)
        lost = sorted(original_keys - current_keys)
        if gained or lost:
            post_drift[username] = {"gained": gained, "lost": lost}

    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    campaign.post_snapshot_drift = post_drift
    campaign.status = COMPLETED
    campaign.completed_at = now_iso
    if undecided:
        campaign.notes.append(f"completed with {len(undecided)} item(s) left undecided (forced)")

    save(campaign)
    write_evidence(campaign, "completed", {
        "summary": campaign.summary(),
        "undecidedAtCompletion": len(undecided),
        "postSnapshotDrift": post_drift,
    })
    return campaign


def cancel(campaign: Campaign) -> Campaign:
    if campaign.status == CANCELLED:
        return campaign
    if campaign.status == COMPLETED:
        raise ValidationError(f"campaign {campaign.id!r} is already completed and cannot be cancelled")

    campaign.status = CANCELLED
    campaign.cancelled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save(campaign)
    write_evidence(campaign, "cancelled", {})
    return campaign


# -----------------------------------------------------------------------------
# Remediate
# -----------------------------------------------------------------------------
#
# The rule that makes this section safe: every action here mutates an
# ATTACHMENT (a group membership, a policy binding, a team membership), never a
# SHARED DEFINITION (a Vault policy document, a Keycloak group itself). A
# policy document or a group can be relied on by other identities; the
# attachment between one person and one of those objects cannot be.
#
# Every action re-uses an existing JML adapter method. There is no second
# implementation of "remove a group" here.


def _rebuild_report(sim: "rbac.Simulator", username: str):
    return sim.analyse(username)


def _remediate_one(item: ReviewItem, sim: "rbac.Simulator", kc, vault, gitea) -> None:
    result = ServiceResult(item.service)

    try:
        if item.service == "Keycloak" and item.resource.startswith("group "):
            user = kc.find_user(item.username)
            if user is None:
                result.record(UNCHANGED, "identity no longer exists in Keycloak")
            else:
                kc.remove_group(user["id"], item.resource[len("group "):], result)

        elif item.service == "Keycloak" and item.resource.startswith("role "):
            if item.inheritance != rbac.DIRECT:
                item.remediation_status = REM_MANUAL
                item.remediation_detail = (
                    "this role is granted by group membership, not assigned directly; "
                    "revoke the corresponding group item instead, or move the identity "
                    "to a different profile with jml-move"
                )
                return
            user = kc.find_user(item.username)
            if user is None:
                result.record(UNCHANGED, "identity no longer exists in Keycloak")
            else:
                kc.remove_direct_realm_role(user["id"], item.resource[len("role "):], result)

        elif item.service == "Vault" and item.resource.startswith("policy "):
            policy_name = item.resource[len("policy "):]
            current = vault.user_policies(item.username) or []
            vault.set_policies(item.username, [p for p in current if p != policy_name], result)

        elif item.service == "Vault":
            item.remediation_status = REM_MANUAL
            item.remediation_detail = (
                f"this permission comes from Vault policy '{item.source[len('policy '):]}'; "
                "revoke the policy item instead of one path inside it, so no other "
                "identity attached to that policy is affected"
            )
            return

        elif item.service == "Gitea" and item.resource.startswith("team "):
            gitea.remove_team_membership(item.username, item.resource[len("team "):], result)

        elif item.service == "Gitea" and item.resource.startswith("repository "):
            item.remediation_status = REM_MANUAL
            item.remediation_detail = (
                "repository ownership is not revoked one entitlement at a time; "
                "run make jml-leave to offboard and transfer ownership, or transfer "
                "the repository manually in Gitea"
            )
            return

        else:
            item.remediation_status = REM_MANUAL
            item.remediation_detail = (
                f"{item.service} '{item.resource}' has no automated remediation path; "
                "handle it manually and record the outcome in the campaign note"
            )
            return

    except (HttpError, Unavailable) as exc:
        item.remediation_status = REM_FAILED
        item.remediation_detail = f"remediation call failed: {exc}"
        return

    if result.failed:
        item.remediation_status = REM_FAILED
        item.remediation_detail = "; ".join(c.detail for c in result.changes if c.status == FAILED)
        return

    # Verify against a fresh read, never against the adapter's own report of
    # success. This is what catches an entitlement that survives through a
    # different inheritance path -- the key still shows up in the new report,
    # now with a different source, and that source is what explains the
    # failure instead of a bare "still present".
    try:
        fresh = _rebuild_report(sim, item.username)
    except (HttpError, Unavailable) as exc:
        item.remediation_status = REM_FAILED
        item.remediation_detail = f"could not verify after remediation: {exc}"
        return

    still_present = next((g for g in fresh.allowed() if g.key() == item.key()), None)
    if still_present is None:
        item.remediation_status = REM_VERIFIED
        item.remediation_detail = "; ".join(c.detail for c in result.changes) or "already absent"
    else:
        item.remediation_status = REM_FAILED
        item.remediation_detail = (
            f"still held after remediation, now via: {still_present.source} "
            f"({still_present.inheritance})"
        )


def remediate(campaign: Campaign, sim: "rbac.Simulator", kc, vault, gitea) -> Campaign:
    if campaign.status == CANCELLED:
        raise ValidationError(f"campaign {campaign.id!r} was cancelled; nothing to remediate")

    allow_protected = os.environ.get("LAB_ALLOW_PROTECTED") == "1"
    attempted = []

    for item in campaign.items:
        if item.decision != REVOKE:
            continue
        if item.remediation_status not in (REM_PENDING, REM_FAILED):
            continue  # NOT_REQUIRED, VERIFIED, MANUAL, SKIPPED: nothing to (re)do

        if item.username in PROTECTED_USERNAMES and not allow_protected:
            item.remediation_status = REM_SKIPPED_PROTECTED
            item.remediation_detail = (
                f"{item.username!r} is a protected seeded identity; remediation was not "
                "attempted. Set LAB_ALLOW_PROTECTED=1 to override deliberately."
            )
            attempted.append(item)
            continue

        _remediate_one(item, sim, kc, vault, gitea)
        attempted.append(item)

    save(campaign)

    if attempted:
        write_evidence(campaign, "remediated", {
            "attempted": [
                {
                    "username": it.username, "entitlement": it.key(),
                    "remediationStatus": it.remediation_status,
                    "remediationDetail": it.remediation_detail,
                }
                for it in attempted
            ],
        })
    return campaign
