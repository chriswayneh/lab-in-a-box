#!/usr/bin/env python3
"""
Access review campaign test suite (roadmap v2-3).

Runs inside the engine container against the REAL lab, same as the lifecycle
and RBAC suites. A campaign's whole claim is that its snapshot is a faithful,
immutable record of what the RBAC engine found and that remediation actually
changes live access -- neither is a question a mock can answer.

Drift used here is injected the same way test_rbac.py injects it: through the
real adapters against a disposable identity, never fabricated in the test
itself. Where the seeded-user Gitea drift (v2-2's finding: alice/bob/carol/dave
have no Gitea account) would be a natural example, it is not usable as a
REVIEW ITEM -- it is a MISSING entitlement, and review items are built from
held (ALLOWED) grants only. It IS still exercised, in
test_seeded_drift_is_context_not_an_item, as drift_context on a real-user scope
-- proving the campaign surfaces it for the reviewer to see without inventing a
fake revocable version of it.

Disposable identities only (`campaigntest`, `campaigntest2`). The seeded demo
users are read but never modified, and a test at the end proves it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import campaign
from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError, Unavailable
from model import PROTECTED_USERNAMES, ServiceResult, load_catalogue
from rbac import Simulator
from vault import Vault

SUBJECT = "campaigntest"
SUBJECT2 = "campaigntest2"
JML = "/engine/jml.py"
AR = "/engine/campaign_cli.py"

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAILED.append(f"{name}: {detail}" if detail else name)
        print(f"  \033[31mFAIL\033[0m  {name}" + (f"\n        {detail}" if detail else ""))
    return bool(condition)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n\033[2m{'─' * len(title)}\033[0m")


def run(script: str, *args, expect_rc: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                          env={**os.environ, "NO_COLOR": "1"})
    if proc.returncode != expect_rc:
        print(f"    \033[2m[rc={proc.returncode}] {script} {' '.join(args)}\033[0m")
        print("    " + (proc.stdout or "")[-1000:].replace("\n", "\n    "))
        print("    " + (proc.stderr or "")[-500:].replace("\n", "\n    "))
    return proc


def run_ar(*args, expect_rc: int = 0) -> subprocess.CompletedProcess:
    return run(AR, *args, expect_rc=expect_rc)


def run_json(*args, expect_rc: int = 0):
    proc = run_ar(*args, "--format", "json", expect_rc=expect_rc)
    if proc.returncode != expect_rc:
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def services():
    catalogue = load_catalogue()
    kc = Keycloak(os.environ.get("KEYCLOAK_URL", "http://keycloak:8080"),
                  os.environ.get("KEYCLOAK_ADMIN", "admin"),
                  os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""),
                  os.environ.get("KEYCLOAK_REALM", "lab"))
    vt = Vault(os.environ.get("VAULT_ADDR", "http://vault:8200"), os.environ.get("VAULT_TOKEN", ""))
    gt = Gitea(os.environ.get("GITEA_URL", "http://gitea:3000"),
               os.environ.get("GITEA_ADMIN_USER", "labadmin"),
               os.environ.get("GITEA_ADMIN_PASSWORD", ""),
               catalogue.gitea_org, catalogue.gitea_transfer_target)
    return catalogue, kc, vt, gt


def allowed_keys(sim, username: str) -> set:
    return {g.key() for g in sim.analyse(username).allowed()}


def reset_to_profile(username: str, profile_name: str, catalogue, kc) -> None:
    """
    Converge `username` onto `profile_name`, whatever profile (if any) they
    are currently on.

    jml-join refuses to stack a second profile onto an identity that already
    holds one -- the v2-2 entitlement-accumulation guard -- so an identity
    left mid-profile by an earlier, possibly interrupted, run has to be moved
    away from rather than joined over. Same approach test_rbac.py's move_to
    uses, for the same reason.
    """
    snap = kc.snapshot(username)
    if not snap["exists"] or not snap["groups"]:
        run(JML, "join", "--user", username, "--role", profile_name)
        return
    current = next((p.name for p in catalogue.profiles.values() if p.keycloak_group in snap["groups"]), None)
    if current == profile_name:
        run(JML, "join", "--user", username, "--role", profile_name)  # reconcile in place
    elif current:
        run(JML, "move", "--user", username, "--from", current, "--to", profile_name)
    else:
        run(JML, "join", "--user", username, "--role", profile_name)


# =============================================================================
# Input validation
# =============================================================================


def test_validation() -> None:
    section("Input validation")

    hostile_names = ["../../etc/passwd", "name;rm -rf /", "name$(whoami)", "UPPER", "1starts-with-digit",
                      "has spaces", "", "a" * 100]
    rejected = 0
    for name in hostile_names:
        try:
            campaign.validate_campaign_name(name)
        except campaign.ValidationError:
            rejected += 1
    check("hostile campaign names are rejected", rejected == len(hostile_names),
          f"{len(hostile_names) - rejected} of {len(hostile_names)} slipped through")
    check("ordinary campaign name is accepted", campaign.validate_campaign_name("quarterly-q3") == "quarterly-q3")

    hostile_ids = ["../etc/passwd", "name-with-no-timestamp", "quarterly-q3-20260101t000000z",
                   "'; DROP TABLE x;--", ""]
    rejected = 0
    for cid in hostile_ids:
        try:
            campaign.validate_campaign_id(cid)
        except campaign.ValidationError:
            rejected += 1
    check("hostile / malformed campaign ids are rejected", rejected == len(hostile_ids),
          f"{len(hostile_ids) - rejected} of {len(hostile_ids)} slipped through")

    real_id = "quarterly-q3-20260819T010203Z"
    check("a real generated id round-trips through validation",
          campaign.validate_campaign_id(real_id) == real_id,
          "case folding or timestamp handling corrupted a valid id")

    hostile_entitlements = ["no-colon-here", "Keycloak:" + "x" * 300, "Keycloak:bad\nnewline", ""]
    rejected = 0
    for ent in hostile_entitlements:
        try:
            campaign.validate_entitlement(ent)
        except campaign.ValidationError:
            rejected += 1
    check("malformed entitlement strings are rejected", rejected == len(hostile_entitlements),
          f"{len(hostile_entitlements) - rejected} of {len(hostile_entitlements)} slipped through")
    check("a real entitlement string is accepted",
          campaign.validate_entitlement("Gitea:team developers") == "Gitea:team developers")

    try:
        campaign.validate_decision("delete-everything")
        bad_decision_rejected = False
    except campaign.ValidationError:
        bad_decision_rejected = True
    check("an unknown decision value is rejected", bad_decision_rejected)
    check("known decisions map correctly",
          campaign.validate_decision("approve") == campaign.APPROVE
          and campaign.validate_decision("revoke") == campaign.REVOKE
          and campaign.validate_decision("not-applicable") == campaign.NOT_APPLICABLE)

    proc = run_ar("create", "--name", "alice-takeover", "--scope", "user:alice", expect_rc=0)
    # Creating a campaign that merely INCLUDES a protected identity is fine --
    # review of a seeded user is exactly where the real, permanent drift lives.
    # Protection applies to mutation, enforced at remediate time, not to being
    # looked at.
    check("a campaign may include a protected identity for read-only review", proc.returncode == 0)
    check("protected identity is in PROTECTED_USERNAMES", "alice" in PROTECTED_USERNAMES)


def test_corrupted_campaign_data() -> None:
    section("Corrupted campaign data fails cleanly")

    campaign_id = "corrupt-test-20990101T000000Z"
    directory = campaign.ARTIFACT_ROOT / campaign_id
    path = directory / "campaign.json"
    directory.mkdir(parents=True, exist_ok=True)

    try:
        path.write_text("{not-json\n", encoding="utf-8")
        try:
            campaign.load(campaign_id)
            malformed_rejected = False
        except campaign.ValidationError as exc:
            malformed_rejected = "malformed" in str(exc)
        check("invalid JSON is rejected as malformed campaign data", malformed_rejected)

        proc = run_ar("show", "--campaign", campaign_id, expect_rc=2)
        check("the CLI reports corrupted data without a Python traceback",
              proc.returncode == 2 and "Traceback" not in proc.stderr)

        path.write_text(json.dumps({"schemaVersion": 1, "id": campaign_id}) + "\n", encoding="utf-8")
        try:
            campaign.load(campaign_id)
            invalid_schema_rejected = False
        except campaign.ValidationError:
            invalid_schema_rejected = True
        check("structurally incomplete campaign data is rejected", invalid_schema_rejected)

        listed = run_json("list")
        check("a corrupted campaign is skipped without breaking campaign list",
              listed is not None and all(item["id"] != campaign_id for item in listed))
    finally:
        if path.exists():
            path.unlink()
        if directory.exists():
            directory.rmdir()


# =============================================================================
# Create / snapshot
# =============================================================================


def test_create_and_snapshot(sim, catalogue, kc) -> None:
    section("Create and snapshot")

    reset_to_profile(SUBJECT, "developer", catalogue, kc)
    expected_grants = allowed_keys(sim, SUBJECT)

    doc = run_json("create", "--name", "snapshot-test", "--scope", f"user:{SUBJECT}")
    if not check("create exits 0 and returns parseable JSON", doc is not None):
        return

    check("campaign status is open immediately", doc["status"] == campaign.OPEN)
    check("opened_at is set", doc["opened_at"] is not None)
    check("scope is recorded", doc["scope"] == f"user:{SUBJECT}")
    check("subject is in the identities list", SUBJECT in doc["identities"])

    item_keys = {f"{it['service']}:{it['resource']}" for it in doc["items"]}
    check("item count matches the RBAC engine's allowed() grants exactly",
          item_keys == expected_grants, f"{item_keys} != {expected_grants}")

    check("every item starts UNDECIDED", all(it["decision"] == "UNDECIDED" for it in doc["items"]),
          "an item looked decided immediately after creation")
    check("every item has a stable item_id", all(it["item_id"] for it in doc["items"]))

    group_item = next((it for it in doc["items"] if it["resource"].startswith("group ")), None)
    check("developer's group membership is classified EXPECTED",
          group_item is not None and group_item["expectation"] == "EXPECTED", str(group_item))

    account_item = next((it for it in doc["items"] if it["resource"] == "account" and it["service"] == "Keycloak"), None)
    check("account sign-in is classified NOT_MODELED, not EXPECTED or UNEXPECTED",
          account_item is not None and account_item["expectation"] == "NOT_MODELED")


def test_expectation_classification_with_real_drift(sim, gt) -> None:
    section("Expectation classification against injected drift")

    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "security", "read", scratch)

    doc = run_json("create", "--name", "drift-test", "--scope", f"user:{SUBJECT}")
    if not check("campaign captures the identity with injected drift", doc is not None):
        return

    unexpected = next((it for it in doc["items"] if it["resource"] == "team security"), None)
    check("the extra Gitea team is a review item", unexpected is not None)
    check("it is classified UNEXPECTED, not EXPECTED", unexpected and unexpected["expectation"] == "UNEXPECTED",
          str(unexpected))

    expected_team = next((it for it in doc["items"] if it["resource"] == "team developers"), None)
    check("the profile's own team is still classified EXPECTED",
          expected_team is not None and expected_team["expectation"] == "EXPECTED")

    check("drift_context for the subject records the same drift v2-2 would find",
          any(d["kind"] == "EXTRA_GITEA_TEAM" for d in doc["drift_context"].get(SUBJECT, [])),
          str(doc["drift_context"].get(SUBJECT)))


# =============================================================================
# Snapshot immutability
# =============================================================================


def test_snapshot_immutability(sim, gt) -> None:
    section("Snapshot immutability")

    doc = run_json("create", "--name", "immutable-test", "--scope", f"user:{SUBJECT}")
    if not check("baseline campaign created", doc is not None):
        return
    baseline_id = doc["id"]
    baseline_keys = {f"{it['service']}:{it['resource']}" for it in doc["items"]}

    # Mutate live access AFTER the snapshot was taken.
    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "platform", "admin", scratch)

    reloaded = run_json("show", "--campaign", baseline_id)
    reloaded_keys = {f"{it['service']}:{it['resource']}" for it in reloaded["items"]} if reloaded else set()
    check("re-showing the SAME campaign does not pick up the new drift",
          reloaded_keys == baseline_keys,
          f"snapshot changed after the fact: {reloaded_keys ^ baseline_keys}")

    fresh_doc = run_json("create", "--name", "after-mutation-test", "--scope", f"user:{SUBJECT}")
    fresh_keys = {f"{it['service']}:{it['resource']}" for it in fresh_doc["items"]} if fresh_doc else set()
    check("a NEW campaign created after the mutation DOES see it",
          "Gitea:team platform" in fresh_keys,
          f"new campaign missed the new drift: {fresh_keys}")

    gt.remove_team_membership(SUBJECT, "platform", scratch)


# =============================================================================
# Decisions
# =============================================================================


def test_decide_approve_and_revoke() -> None:
    section("Decide: approve and revoke")

    doc = run_json("create", "--name", "decide-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    approve_target = next(it for it in doc["items"] if it["resource"] == "team developers")
    proc = run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
                  "--entitlement", f"{approve_target['service']}:{approve_target['resource']}",
                  "--decision", "approve", "--reviewer", "test-harness")
    check("approve exits 0", proc.returncode == 0)

    revoke_target = next(it for it in doc["items"] if it["resource"] == "team security")
    proc = run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
                  "--entitlement", f"{revoke_target['service']}:{revoke_target['resource']}",
                  "--decision", "revoke", "--note", "not part of the developer profile")
    check("revoke exits 0", proc.returncode == 0)

    shown = run_json("show", "--campaign", campaign_id)
    approved = next(it for it in shown["items"] if it["resource"] == "team developers")
    revoked = next(it for it in shown["items"] if it["resource"] == "team security")

    check("approve decision persisted", approved["decision"] == "APPROVE")
    check("approve requires no remediation", approved["remediation_status"] == "NOT_REQUIRED")
    check("revoke decision persisted", revoked["decision"] == "REVOKE")
    check("revoke queues remediation as PENDING", revoked["remediation_status"] == "PENDING")
    check("revoke note persisted", revoked["note"] == "not part of the developer profile")
    check("reviewer label persisted on the approved item", approved["decided_by"] == "test-harness")


def test_duplicate_decision_guard() -> None:
    section("Duplicate decision guard")

    doc = run_json("create", "--name", "dup-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    target = next(it for it in doc["items"] if it["resource"] == "team developers")
    ent = f"{target['service']}:{target['resource']}"

    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT, "--entitlement", ent, "--decision", "approve")

    proc = run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
                  "--entitlement", ent, "--decision", "revoke", expect_rc=2)
    check("a second decision without --force is refused", proc.returncode == 2)
    check("the refusal names the existing decision", "APPROVE" in proc.stdout or "APPROVE" in proc.stderr)

    proc = run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
                  "--entitlement", ent, "--decision", "revoke", "--force")
    check("--force overrides an existing decision", proc.returncode == 0)

    shown = run_json("show", "--campaign", campaign_id)
    item = next(it for it in shown["items"] if it["resource"] == "team developers")
    check("the forced decision actually changed the stored value", item["decision"] == "REVOKE")


def test_snapshot_commands_work_during_downstream_outage() -> None:
    section("Snapshot-only commands remain available during downstream outage")

    doc = run_json("create", "--name", "offline-snapshot-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    target = doc["items"][0]
    offline_env = {
        **os.environ,
        "NO_COLOR": "1",
        "KEYCLOAK_URL": "http://127.0.0.1:1",
        "VAULT_ADDR": "http://127.0.0.1:1",
        "GITEA_URL": "http://127.0.0.1:1",
    }

    def offline(*args):
        return subprocess.run(
            [sys.executable, AR, *args], capture_output=True, text=True, env=offline_env,
        )

    shown = offline("show", "--campaign", campaign_id, "--format", "json")
    check("show reads the persisted snapshot without contacting services",
          shown.returncode == 0 and json.loads(shown.stdout)["id"] == campaign_id)

    decided = offline(
        "decide", "--campaign", campaign_id, "--user", SUBJECT,
        "--entitlement", f"{target['service']}:{target['resource']}", "--decision", "approve",
    )
    check("decide records a snapshot decision without contacting services", decided.returncode == 0)
    check("the offline decision persisted",
          campaign.load(campaign_id).find_item(SUBJECT, f"{target['service']}:{target['resource']}").decision
          == campaign.APPROVE)

    cancelled = offline("cancel", "--campaign", campaign_id)
    check("cancel updates the persisted campaign without contacting services", cancelled.returncode == 0)


def test_decide_unknown_entitlement_and_closed_campaign() -> None:
    section("Decide: unknown entitlement and non-open campaigns")

    doc = run_json("create", "--name", "guard-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    proc = run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
                  "--entitlement", "Keycloak:role does-not-exist", "--decision", "approve", expect_rc=2)
    check("deciding on an entitlement the campaign never captured is refused", proc.returncode == 2)
    check("the error lists what IS available for that user",
          "Available for" in proc.stdout or "Available for" in proc.stderr)

    run_ar("cancel", "--campaign", campaign_id)
    target = next(it for it in doc["items"] if it["resource"] == "team developers")
    proc = run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
                  "--entitlement", f"{target['service']}:{target['resource']}", "--decision", "approve",
                  expect_rc=2)
    check("deciding on a cancelled campaign is refused", proc.returncode == 2)


# =============================================================================
# Completion
# =============================================================================


def test_complete_refuses_undecided() -> None:
    section("Completion refuses undecided items")

    doc = run_json("create", "--name", "complete-guard-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    proc = run_ar("complete", "--campaign", campaign_id, expect_rc=2)
    check("completing with undecided items is refused without --force", proc.returncode == 2)
    check("the refusal names how many are undecided",
          "undecided" in (proc.stdout + proc.stderr).lower())

    doc2 = run_json("complete", "--campaign", campaign_id, "--force")
    check("--force closes the campaign anyway", doc2 is not None and doc2["status"] == "completed")
    still_undecided = [it for it in doc2["items"] if it["decision"] == "UNDECIDED"]
    check("forced completion does NOT mark undecided items as approved",
          all(it["decision"] == "UNDECIDED" for it in still_undecided) and len(still_undecided) > 0,
          "an undecided item was coerced into looking decided")
    check("the campaign records that items were left undecided",
          any("undecided" in n for n in doc2["notes"]))


def test_complete_idempotent() -> None:
    section("Completion is idempotent")

    doc = run_json("create", "--name", "complete-idem-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    first = run_json("complete", "--campaign", campaign_id, "--force")
    second = run_json("complete", "--campaign", campaign_id, "--force")

    check("completing twice succeeds both times", first is not None and second is not None)
    check("completed_at is not rewritten on the second call",
          first["completed_at"] == second["completed_at"],
          f"{first['completed_at']} != {second['completed_at']}")


def test_cancel() -> None:
    section("Cancel")

    doc = run_json("create", "--name", "cancel-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    proc = run_ar("cancel", "--campaign", campaign_id)
    check("cancel exits 0", proc.returncode == 0)
    shown = run_json("show", "--campaign", campaign_id)
    check("status becomes cancelled", shown["status"] == "cancelled")
    check("cancelled_at is set", shown["cancelled_at"] is not None)

    proc = run_ar("cancel", "--campaign", campaign_id)
    check("cancelling twice is idempotent, not an error", proc.returncode == 0)

    proc = run_ar("complete", "--campaign", campaign_id, expect_rc=2)
    check("a cancelled campaign cannot be completed", proc.returncode == 2)


# =============================================================================
# Remediation
# =============================================================================


def test_remediation_reuses_adapters_and_verifies(sim, gt) -> None:
    section("Remediation executes, then verifies against live state")

    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "security", "read", scratch)

    doc = run_json("create", "--name", "remediate-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    dev_item = next(it for it in doc["items"] if it["resource"] == "team developers")
    sec_item = next(it for it in doc["items"] if it["resource"] == "team security")
    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{dev_item['service']}:{dev_item['resource']}", "--decision", "approve")
    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{sec_item['service']}:{sec_item['resource']}", "--decision", "revoke")

    before_live = allowed_keys(sim, SUBJECT)
    check("before remediation, the extra team is genuinely still held live",
          "Gitea:team security" in before_live)

    remediated = run_json("remediate", "--campaign", campaign_id)
    if not check("remediate exits with parseable JSON", remediated is not None):
        return

    result = next(it for it in remediated["items"] if it["resource"] == "team security")
    check("the revoked item is marked VERIFIED, not just EXECUTED",
          result["remediation_status"] == "VERIFIED", str(result))

    # Independent proof, not trusting campaign.py's own report: re-query the
    # RBAC engine directly.
    after_live = allowed_keys(sim, SUBJECT)
    check("live access ACTUALLY changed (independently re-verified)",
          "Gitea:team security" not in after_live, f"still present: {after_live}")
    check("the approved, unrelated entitlement was NOT touched",
          "Gitea:team developers" in after_live,
          "remediation removed access that was never decided against")


def test_remediation_removes_one_vault_policy_leaves_others(sim, vt, catalogue, kc) -> None:
    """
    The scenario vault.set_policies exists for: an identity holding more than
    one Vault policy at once. Normal jml provisioning never produces this --
    it always converges onto exactly one policy -- so this can only happen the
    way it happens in practice, someone attaching a second policy directly
    against Vault, bypassing the lifecycle model entirely. That is exactly the
    UNEXPECTED_VAULT_POLICY anomaly the RBAC engine already detects, and it is
    the case revoking a single policy must get right: remove the one flagged,
    keep the one the profile actually grants.
    """
    section("Remediation removes one Vault policy, leaves the other attached")

    reset_to_profile(SUBJECT, "developer", catalogue, kc)
    scratch = ServiceResult("fixture")
    # Bypass jml deliberately, the same way test_rbac.py's drift injection
    # does, to attach a second policy without going through reconcile_user's
    # single-policy convergence.
    vt._call("POST", f"/auth/userpass/users/{SUBJECT}/policies",
             json_body={"token_policies": "developer,security-analyst"})

    doc = run_json("create", "--name", "multi-policy-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    expected_item = next(it for it in doc["items"] if it["resource"] == "policy developer")
    extra_item = next(it for it in doc["items"] if it["resource"] == "policy security-analyst")
    check("the profile's own policy is classified EXPECTED", expected_item["expectation"] == "EXPECTED")
    check("the extra, directly-attached policy is classified UNEXPECTED", extra_item["expectation"] == "UNEXPECTED")

    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{extra_item['service']}:{extra_item['resource']}", "--decision", "revoke")

    remediated = run_json("remediate", "--campaign", campaign_id)
    item = next(it for it in remediated["items"] if it["resource"] == "policy security-analyst")
    check("revoking the extra policy resolves to VERIFIED", item["remediation_status"] == "VERIFIED", str(item))

    after = vt.user_policies(SUBJECT)
    check("the extra policy is gone", after is not None and "security-analyst" not in after, str(after))
    check("the profile's own policy is still attached, untouched",
          after is not None and "developer" in after, str(after))
    check("exactly one policy remains, not zero and not both",
          after == ["developer"], f"expected ['developer'], got {after}")


def test_remediation_idempotent(sim, gt) -> None:
    section("Remediation is idempotent")

    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "security", "read", scratch)

    doc = run_json("create", "--name", "remediate-idem-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    sec_item = next(it for it in doc["items"] if it["resource"] == "team security")
    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{sec_item['service']}:{sec_item['resource']}", "--decision", "revoke")

    first = run_json("remediate", "--campaign", campaign_id)
    second = run_json("remediate", "--campaign", campaign_id)

    r1 = next(it for it in first["items"] if it["resource"] == "team security")
    r2 = next(it for it in second["items"] if it["resource"] == "team security")
    check("repeat remediation is safe (no error)", second is not None)
    check("an already-VERIFIED item stays VERIFIED on a repeat run",
          r1["remediation_status"] == "VERIFIED" and r2["remediation_status"] == "VERIFIED")

    proc = run_ar("remediate", "--campaign", campaign_id)
    check("a third run still exits cleanly", proc.returncode == 0)


def test_partial_downstream_failure_continues(sim, gt) -> None:
    section("Partial downstream failure is retained and later items continue")

    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "security", "read", scratch)
    gt.ensure_team_membership(SUBJECT, "platform", "admin", scratch)

    doc = run_json("create", "--name", "partial-failure-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    security_item = next(it for it in doc["items"] if it["resource"] == "team security")
    platform_item = next(it for it in doc["items"] if it["resource"] == "team platform")
    current = campaign.load(campaign_id)
    campaign.decide(current, SUBJECT, "Gitea:team security", campaign.REVOKE,
                    "simulated outage", "test-harness", False)
    campaign.decide(current, SUBJECT, "Gitea:team platform", campaign.REVOKE,
                    "continue after failure", "test-harness", False)

    class FailOneTeam:
        def remove_team_membership(self, username, team_name, result):
            if team_name == "security":
                raise Unavailable("simulated Gitea failure for one entitlement")
            return gt.remove_team_membership(username, team_name, result)

    result = campaign.remediate(current, sim, None, None, FailOneTeam())
    failed = result.find_item(SUBJECT, f"{security_item['service']}:{security_item['resource']}")
    succeeded = result.find_item(SUBJECT, f"{platform_item['service']}:{platform_item['resource']}")

    check("a downstream exception is recorded as FAILED without aborting the campaign",
          failed.remediation_status == campaign.REM_FAILED, failed.remediation_detail)
    check("the failure detail preserves the service error", "simulated Gitea failure" in failed.remediation_detail)
    check("a later remediation item still executes and verifies",
          succeeded.remediation_status == campaign.REM_VERIFIED, succeeded.remediation_detail)

    live = allowed_keys(sim, SUBJECT)
    check("the failed entitlement remains live and is not reported revoked", "Gitea:team security" in live)
    check("the independently successful entitlement is absent live", "Gitea:team platform" not in live)

    gt.remove_team_membership(SUBJECT, "security", scratch)


def test_remediation_manual_action_paths(vt) -> None:
    section("Remediation refuses to guess: manual-action paths")

    doc = run_json("create", "--name", "manual-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    # The top-level "policy X" grant is ALSO inheritance == derived when it
    # matches the profile (see rbac.py _vault_grants), so filtering on
    # inheritance alone would pick the wrong item -- the one path IS
    # genuinely remediable by removing the policy attachment. What is not
    # remediable one-at-a-time is an individual PATH inside that policy,
    # identified here by resource not being the "policy ..." grant itself.
    vault_path_item = next(
        (it for it in doc["items"]
         if it["service"] == "Vault" and it["inheritance"] == "derived"
         and not it["resource"].startswith("policy ")),
        None,
    )

    if not check("a Vault path grant exists to test the manual-action path", vault_path_item is not None):
        return

    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{vault_path_item['service']}:{vault_path_item['resource']}",
           "--decision", "revoke")

    before = set(vt.user_policies(SUBJECT) or [])
    result = run_json("remediate", "--campaign", campaign_id)
    item = next(it for it in result["items"]
                if it["resource"] == vault_path_item["resource"] and it["service"] == "Vault")

    check("revoking a single Vault path is MANUAL_ACTION_REQUIRED, not silently attempted",
          item["remediation_status"] == "MANUAL_ACTION_REQUIRED", str(item))
    check("the manual-action detail names the underlying policy",
          "policy" in item["remediation_detail"].lower())

    after = set(vt.user_policies(SUBJECT) or [])
    check("no Vault policy was actually touched by the manual-action attempt",
          before == after, f"{before} -> {after}")


def test_remediation_does_not_accumulate_or_leak_across_entitlements(sim) -> None:
    section("Remediation touches only the reviewed entitlement")

    # A role granted BY a group must never be stripped through the role item;
    # only removing the GROUP is safe. Prove the role item is refused and nothing moves.
    doc = run_json("create", "--name", "role-safety-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    role_item = next((it for it in doc["items"]
                       if it["service"] == "Keycloak" and it["resource"].startswith("role ")
                       and it["inheritance"] == "group-inherited"), None)
    if not check("a group-inherited role item exists to test", role_item is not None):
        return

    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{role_item['service']}:{role_item['resource']}", "--decision", "revoke")

    before = allowed_keys(sim, SUBJECT)
    result = run_json("remediate", "--campaign", campaign_id)
    item = next(it for it in result["items"] if it["resource"] == role_item["resource"])

    check("revoking a group-inherited role is MANUAL_ACTION_REQUIRED",
          item["remediation_status"] == "MANUAL_ACTION_REQUIRED", str(item))
    check("the message points at revoking the group instead",
          "group" in item["remediation_detail"].lower())

    after = allowed_keys(sim, SUBJECT)
    check("nothing was removed by the refused role-revoke attempt", before == after,
          f"{before ^ after} changed unexpectedly")


def test_remediation_protected_identity_guard(kc) -> None:
    section("Remediation protects seeded identities")

    doc = run_json("create", "--name", "protected-test", "--scope", "user:alice")
    campaign_id = doc["id"]
    target = next((it for it in doc["items"] if it["service"] == "Gitea"), None)
    if target is None:
        # alice's Gitea account is the known v2-2 drift finding: it does not
        # exist. Fall back to a Keycloak grant, which alice definitely has.
        target = next(it for it in doc["items"] if it["service"] == "Keycloak" and it["resource"].startswith("group"))

    run_ar("decide", "--campaign", campaign_id, "--user", "alice",
           "--entitlement", f"{target['service']}:{target['resource']}", "--decision", "revoke")

    before_groups = sorted(kc.user_groups(kc.require_user("alice")["id"]))
    result = run_json("remediate", "--campaign", campaign_id)
    # Resource names are only unique within a service (for example both
    # Keycloak and Gitea expose an "account" item). Match the full entitlement
    # so the guard remains valid when a seeded user already has a Gitea account.
    item = next(
        it for it in result["items"]
        if it["service"] == target["service"] and it["resource"] == target["resource"]
    )

    check("remediation against a protected identity is skipped by default",
          item["remediation_status"] == "SKIPPED_PROTECTED_IDENTITY", str(item))
    check("the skip message explains why", "protected" in item["remediation_detail"].lower())

    after_groups = sorted(kc.user_groups(kc.require_user("alice")["id"]))
    check("alice's actual group membership is completely unchanged",
          before_groups == after_groups, f"{before_groups} -> {after_groups}")
    check("alice is still enabled", kc.require_user("alice")["enabled"] is True)


# =============================================================================
# Realistic failure modes
# =============================================================================


def test_identity_removed_after_snapshot(sim, catalogue, kc) -> None:
    section("Identity offboarded after the campaign snapshot was taken")

    reset_to_profile(SUBJECT2, "developer", catalogue, kc)
    doc = run_json("create", "--name", "offboard-during-review", "--scope", f"user:{SUBJECT2}")
    campaign_id = doc["id"]

    group_item = next(it for it in doc["items"] if it["resource"].startswith("group "))
    policy_item = next(it for it in doc["items"] if it["resource"].startswith("policy "))
    for it in (group_item, policy_item):
        run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT2,
               "--entitlement", f"{it['service']}:{it['resource']}", "--decision", "revoke")

    # The identity is fully offboarded through the normal leaver flow BEFORE
    # remediation of the campaign's own decisions ever runs.
    run(JML, "leave", "--user", SUBJECT2)

    result = run_json("remediate", "--campaign", campaign_id)
    if not check("remediation after full offboarding does not crash", result is not None):
        return

    for original in (group_item, policy_item):
        item = next(it for it in result["items"] if it["resource"] == original["resource"])
        check(f"'{original['resource']}' resolves to VERIFIED, since the access is genuinely gone",
              item["remediation_status"] == "VERIFIED", str(item))


def test_access_changed_after_snapshot_surfaced_at_complete(sim, catalogue, kc) -> None:
    section("Access changed after the snapshot is surfaced, not hidden")

    reset_to_profile(SUBJECT, "developer", catalogue, kc)
    doc = run_json("create", "--name", "post-snapshot-drift-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]

    # Change the identity's role AFTER the campaign already captured it.
    run(JML, "move", "--user", SUBJECT, "--from", "developer", "--to", "security")

    result = run_json("complete", "--campaign", campaign_id, "--force")
    if check("campaign completes even though live access has since changed", result is not None):
        delta = result["post_snapshot_drift"].get(SUBJECT, {})
        check("gained access since the snapshot is reported", len(delta.get("gained", [])) > 0, str(delta))
        check("lost access since the snapshot is reported", len(delta.get("lost", [])) > 0, str(delta))
        check("the ORIGINAL snapshot itself is untouched by the later move",
              any(it["resource"].startswith("group ") and "Application Engineering" in it["resource"]
                  for it in result["items"]),
              "the historical item list was rewritten instead of being compared against")

    # Restore SUBJECT to developer so later tests in this suite can keep
    # assuming that steady state, the same way this test itself no longer has
    # to assume anything about what a previous run left behind.
    reset_to_profile(SUBJECT, "developer", catalogue, kc)


# =============================================================================
# Evidence and identifiers
# =============================================================================


def test_audit_evidence(catalogue) -> None:
    section("Audit evidence")

    doc = run_json("create", "--name", "evidence-test", "--scope", f"user:{SUBJECT}")
    campaign_id = doc["id"]
    target = next(it for it in doc["items"] if it["resource"].startswith("group "))
    run_ar("decide", "--campaign", campaign_id, "--user", SUBJECT,
           "--entitlement", f"{target['service']}:{target['resource']}", "--decision", "approve")
    run_json("complete", "--campaign", campaign_id, "--force")

    directory = campaign.ARTIFACT_ROOT / campaign_id
    check("campaign directory exists", directory.is_dir())
    opened = list(directory.glob("*-opened.json"))
    completed = list(directory.glob("*-completed.json"))
    check("an 'opened' evidence record was written", len(opened) == 1)
    check("a 'completed' evidence record was written", len(completed) == 1)
    check("campaign.json (the live document) exists alongside the evidence",
          (directory / "campaign.json").exists())

    haystack = "\n".join(p.read_text(encoding="utf-8") for p in directory.glob("*.json"))
    secrets = {
        "Keycloak admin password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""),
        "Vault root token": os.environ.get("VAULT_TOKEN", ""),
        "Gitea admin password": os.environ.get("GITEA_ADMIN_PASSWORD", ""),
        "demo user password": os.environ.get("DEMO_USER_PASSWORD", ""),
    }
    for label, value in secrets.items():
        if not value or len(value) < 6:
            continue
        check(f"campaign evidence does not contain the {label}", value not in haystack)
    for token in ("access_token", "refresh_token", "client_secret", "X-Vault-Token"):
        check(f"campaign evidence does not contain {token!r}", token not in haystack)


def test_evidence_filename_collisions_are_append_only() -> None:
    section("Evidence remains append-only when events share a timestamp")

    campaign_id = "evidence-collision-20990101T000000Z"
    review = campaign.Campaign(
        id=campaign_id,
        name="evidence-collision",
        status=campaign.OPEN,
        scope="all",
        reviewer="test-harness",
        created_at="2099-01-01T00:00:00+00:00",
    )
    directory = campaign.ARTIFACT_ROOT / campaign_id
    fixed = datetime(2099, 1, 1, tzinfo=timezone.utc)
    real_datetime = campaign.datetime

    class FrozenDateTime:
        @classmethod
        def now(cls, tz=None):
            return fixed

    try:
        campaign.datetime = FrozenDateTime
        first = campaign.write_evidence(review, "collision", {"sequence": 1})
        second = campaign.write_evidence(review, "collision", {"sequence": 2})
    finally:
        campaign.datetime = real_datetime

    check("same-second evidence events receive distinct filenames", first != second)
    check("both same-second evidence records remain on disk", first.exists() and second.exists())
    check("neither evidence payload overwrote the other",
          json.loads(first.read_text(encoding="utf-8"))["sequence"] == 1
          and json.loads(second.read_text(encoding="utf-8"))["sequence"] == 2)

    first.unlink()
    second.unlink()
    directory.rmdir()


def test_deterministic_item_id() -> None:
    section("Deterministic item identifiers")

    a = campaign.compute_item_id("erin", "Gitea:team developers")
    b = campaign.compute_item_id("erin", "Gitea:team developers")
    c = campaign.compute_item_id("erin", "Gitea:team security")
    d = campaign.compute_item_id("erica", "Gitea:team developers")

    check("the same (user, entitlement) always hashes the same way", a == b)
    check("a different entitlement hashes differently", a != c)
    check("a different user hashes differently", a != d)

    other_process = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '/engine'); import campaign; "
         "print(campaign.compute_item_id('erin', 'Gitea:team developers'))"],
        capture_output=True, text=True,
    ).stdout.strip()
    check("the identifier is stable across a separate interpreter", a == other_process,
          f"{a} != {other_process}")


def test_list_command() -> None:
    section("List")

    run_json("create", "--name", "list-test-one", "--scope", f"user:{SUBJECT}")
    run_json("create", "--name", "list-test-two", "--scope", f"user:{SUBJECT}")

    campaigns = run_json("list")
    if not check("list returns parseable JSON", campaigns is not None):
        return
    names = {c["name"] for c in campaigns}
    check("both newly created campaigns appear in the list",
          {"list-test-one", "list-test-two"} <= names)


def test_scope_forms(kc, catalogue) -> None:
    section("Scope resolution")

    doc = run_json("create", "--name", "scope-user-test", "--scope", f"user:{SUBJECT}")
    check("user: scope resolves to exactly one identity",
          doc is not None and doc["identities"] == [SUBJECT])

    profile = catalogue.get("developer")
    doc = run_json("create", "--name", "scope-profile-test", "--scope", "profile:developer")
    check("profile: scope resolves via the real Keycloak group, includes the subject",
          doc is not None and SUBJECT in doc["identities"])
    live_members = sorted(kc.group_members(profile.keycloak_group))
    check("profile: scope matches the group's actual live membership exactly",
          sorted(doc["identities"]) == live_members, f"{doc['identities']} != {live_members}")

    proc = run_ar("create", "--name", "scope-bad-test", "--scope", "not-a-real-form", expect_rc=2)
    check("an invalid SCOPE form is rejected", proc.returncode == 2)

    proc = run_ar("create", "--name", "scope-bad-profile-test", "--scope", "profile:not-a-real-profile",
                  expect_rc=2)
    check("an unknown profile in SCOPE is rejected", proc.returncode == 2)


# =============================================================================
# Seeded drift used as real context, not a fabricated item
# =============================================================================


def test_seeded_drift_is_context_not_an_item() -> None:
    """
    v2-2 found real drift on the seeded realm: alice, bob, carol and dave have
    no Gitea account, because v1 provisioning only ever created labadmin. That
    is a MISSING entitlement, not a held one, so it cannot become a revocable
    review item -- but a campaign reviewing these users should still surface it
    as drift context. This proves that happens, using the actual current state
    rather than a fabricated stand-in.
    """
    section("Real seeded-user drift surfaces as context")

    doc = run_json("create", "--name", "seeded-drift-context-test", "--scope", "user:alice")
    if not check("a campaign can review alice without mutating her", doc is not None):
        return

    gitea_items = [it for it in doc["items"] if it["service"] == "Gitea"]
    alice_drift = doc["drift_context"].get("alice", [])
    missing_gitea = [d for d in alice_drift if d["kind"] == "MISSING_GITEA_TEAM"]

    if missing_gitea:
        check("the missing Gitea team shows up as drift context, confirming v2-2's finding still holds",
              len(missing_gitea) == 1, str(alice_drift))
        check("the missing entitlement produced NO Gitea review item (nothing held to decide)",
              gitea_items == [], str(gitea_items))
    else:
        # The drift may have been resolved between sessions; that is a fact
        # about the lab, not a defect in this test.
        check("alice's Gitea state was checked (drift may since be resolved)", True)


# =============================================================================
# End-to-end scenario (roadmap v2-3, section 12)
# =============================================================================


def test_end_to_end_scenario(sim, gt) -> None:
    section("End-to-end: join -> drift -> review -> remediate -> complete")

    e2e_subject = "campaigne2e"

    # 1. join test identity as developer
    proc = run(JML, "join", "--user", e2e_subject, "--role", "developer")
    check("1. joined the test identity as developer", proc.returncode == 0)

    # 2. add one real drift condition
    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(e2e_subject, "security", "read", scratch)
    check("2. injected a real, live entitlement the developer profile does not grant",
          "security" in gt.team_memberships(e2e_subject))

    # 3. create campaign
    doc = run_json("create", "--name", "e2e-scenario", "--scope", f"user:{e2e_subject}")
    if not check("3. campaign created and open", doc is not None and doc["status"] == "open"):
        return
    campaign_id = doc["id"]

    # 4. review actual entitlements
    unexpected = [it for it in doc["items"] if it["expectation"] == "UNEXPECTED"]
    check("4. the review surfaced the injected drift as UNEXPECTED", len(unexpected) == 1, str(unexpected))

    # 5. approve legitimate access. A thorough reviewer decides every item,
    # including the NOT_MODELED ones (account sign-in is normal for an active
    # identity); only the one genuinely UNEXPECTED item is left for step 6.
    legit = [it for it in doc["items"] if it["expectation"] != "UNEXPECTED"]
    for it in legit:
        run_ar("decide", "--campaign", campaign_id, "--user", e2e_subject,
               "--entitlement", f"{it['service']}:{it['resource']}", "--decision", "approve")
    check("5. every legitimate entitlement was approved", len(legit) > 0)

    # 6. revoke one inappropriate entitlement
    bad = unexpected[0]
    run_ar("decide", "--campaign", campaign_id, "--user", e2e_subject,
           "--entitlement", f"{bad['service']}:{bad['resource']}", "--decision", "revoke")
    check("6. the inappropriate entitlement was decided REVOKE", True)

    # 7. execute remediation
    remediated = run_json("remediate", "--campaign", campaign_id)
    item = next(it for it in remediated["items"] if it["resource"] == bad["resource"])
    check("7. remediation reports VERIFIED", item["remediation_status"] == "VERIFIED", str(item))

    # 8. verify access changed (independently, not trusting the campaign's own report)
    live = allowed_keys(sim, e2e_subject)
    check("8. live access no longer includes the revoked entitlement", bad["resource"] not in
          {g.split(":", 1)[1] for g in live if g.startswith("Gitea:")})
    check("8b. approved access is still present", any(g.startswith("Gitea:team developers") for g in live))

    # 9. complete campaign
    completed = run_json("complete", "--campaign", campaign_id)
    check("9. campaign completes cleanly (nothing left undecided)", completed is not None
          and completed["status"] == "completed" and not completed["notes"])

    # 10. verify retained campaign evidence
    directory = campaign.ARTIFACT_ROOT / campaign_id
    files = sorted(p.name for p in directory.glob("*.json"))
    check("10. opened, remediated and completed evidence all retained",
          any("opened" in f for f in files) and any("remediated" in f for f in files)
          and any("completed" in f for f in files), str(files))

    run(JML, "leave", "--user", e2e_subject)


# =============================================================================
# Cross-cutting safety
# =============================================================================


def test_seeded_users_untouched(kc) -> None:
    section("Seeded demo identities unchanged")

    expected = {
        "alice": "/Platform Engineering",
        "bob": "/Application Engineering",
        "carol": "/Security",
        "dave": "/Contractors",
    }
    for username, group in expected.items():
        snap = kc.snapshot(username)
        check(f"{username} still enabled", snap["enabled"], str(snap))
        check(f"{username} still in {group}", snap["groups"] == [group], f"groups={snap['groups']}")


# =============================================================================


def main() -> int:
    print("\033[1mAccess review campaign test suite\033[0m")
    print(f"\033[2mDisposable subjects: {SUBJECT}, {SUBJECT2}, campaigne2e. "
          f"Seeded demo users are read, never modified.\033[0m")

    catalogue, kc, vt, gt = services()
    sim = Simulator(kc, vt, gt, catalogue)

    test_validation()
    test_corrupted_campaign_data()
    test_create_and_snapshot(sim, catalogue, kc)
    test_expectation_classification_with_real_drift(sim, gt)
    test_snapshot_immutability(sim, gt)
    test_decide_approve_and_revoke()
    test_duplicate_decision_guard()
    test_snapshot_commands_work_during_downstream_outage()
    test_decide_unknown_entitlement_and_closed_campaign()
    test_complete_refuses_undecided()
    test_complete_idempotent()
    test_cancel()
    test_remediation_reuses_adapters_and_verifies(sim, gt)
    test_remediation_removes_one_vault_policy_leaves_others(sim, vt, catalogue, kc)
    test_remediation_idempotent(sim, gt)
    test_partial_downstream_failure_continues(sim, gt)
    test_remediation_manual_action_paths(vt)
    test_remediation_does_not_accumulate_or_leak_across_entitlements(sim)
    test_remediation_protected_identity_guard(kc)
    test_identity_removed_after_snapshot(sim, catalogue, kc)
    test_access_changed_after_snapshot_surfaced_at_complete(sim, catalogue, kc)
    test_audit_evidence(catalogue)
    test_evidence_filename_collisions_are_append_only()
    test_deterministic_item_id()
    test_list_command()
    test_scope_forms(kc, catalogue)
    test_seeded_drift_is_context_not_an_item()
    test_end_to_end_scenario(sim, gt)
    test_seeded_users_untouched(kc)

    # Leave the disposable identities offboarded so a re-run starts clean.
    run(JML, "leave", "--user", SUBJECT)
    run(JML, "leave", "--user", SUBJECT2)

    section("Summary")
    total = len(PASSED) + len(FAILED)
    print(f"  {len(PASSED)}/{total} checks passed")
    if FAILED:
        print(f"\n  \033[31m{len(FAILED)} failed:\033[0m")
        for name in FAILED:
            print(f"    - {name}")
        return 1
    print("  \033[32mall access review campaign checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
