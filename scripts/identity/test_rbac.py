#!/usr/bin/env python3
"""
RBAC simulator test suite (roadmap v2-2).

Runs inside the engine container against the REAL lab. The simulator's whole
claim is that it reports what the services actually contain, so testing it
against mocks would test nothing.

Drift tests deliberately CREATE drift through the adapters, assert the
simulator finds it, then remove it again. That is the only honest way to test a
drift detector: a detector that has never seen drift is a detector nobody has
tested.

Disposable identity only (`rbactest`). The seeded demo users are read but never
modified, and a test at the end proves it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import rbac
from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError
from model import ServiceResult, load_catalogue
from rbac import ALLOWED, NOT_AUTHORIZED, NOT_INTEGRATED, Simulator
from vault import Vault

SUBJECT = "rbactest"
JML = "/engine/jml.py"
RBAC = "/engine/rbac_cli.py"

PASSED, FAILED = [], []


def check(name: str, condition: bool, detail: str = "") -> bool:
    if condition:
        PASSED.append(name)
        print(f"  \033[32mPASS\033[0m  {name}")
    else:
        FAILED.append(f"{name} — {detail}" if detail else name)
        print(f"  \033[31mFAIL\033[0m  {name}" + (f"\n        {detail}" if detail else ""))
    return bool(condition)


def section(title: str) -> None:
    print(f"\n\033[1m{title}\033[0m\n\033[2m{'─' * len(title)}\033[0m")


def run(script: str, *args, expect_rc: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run([sys.executable, script, *args], capture_output=True, text=True,
                          env={**os.environ, "NO_COLOR": "1"})
    if proc.returncode != expect_rc:
        print(f"    \033[2m[rc={proc.returncode}] {script} {' '.join(args)}\033[0m")
        print("    " + (proc.stdout or "")[-800:].replace("\n", "\n    "))
        print("    " + (proc.stderr or "")[-500:].replace("\n", "\n    "))
    return proc


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


def report_for(sim, username: str):
    return sim.analyse(username)


def drift_kinds(report) -> set:
    return {d.kind for d in report.drift}


def grant(report, service: str, prefix: str):
    for g in report.grants:
        if g.service == service and g.resource.startswith(prefix):
            return g
    return None


# =============================================================================


def move_to(profile_name: str) -> None:
    """
    Put the subject on a profile, whatever it is on now.

    Uses the mover between profiles rather than repeated joins, because the
    joiner deliberately refuses to stack a second profile onto an identity that
    already holds one — see test_join_refuses_second_profile.
    """
    catalogue, kc, _, _ = services()
    snap = kc.snapshot(SUBJECT)

    if not snap["exists"] or not snap["groups"]:
        run(JML, "join", "--user", SUBJECT, "--role", profile_name)
        return

    current = next(
        (p.name for p in catalogue.profiles.values() if p.keycloak_group in snap["groups"]),
        None,
    )
    if current == profile_name:
        run(JML, "join", "--user", SUBJECT, "--role", profile_name)  # reconcile in place
    elif current:
        run(JML, "move", "--user", SUBJECT, "--from", current, "--to", profile_name)
    else:
        run(JML, "join", "--user", SUBJECT, "--role", profile_name)


def test_profiles(sim, catalogue) -> None:
    """A–D: each role profile resolves to the access its mappings imply."""
    for profile_name in ("developer", "platform-admin", "security", "contractor"):
        section(f"Profile: {profile_name}")
        profile = catalogue.get(profile_name)

        move_to(profile_name)
        report = report_for(sim, SUBJECT)

        check(f"{profile_name}: identity enabled", report.enabled)
        check(f"{profile_name}: profile inferred from the group",
              report.inferred_profile == profile_name,
              f"inferred {report.inferred_profile!r}")
        check(f"{profile_name}: group resolved",
              profile.keycloak_group in report.groups, f"groups={report.groups}")

        for role in profile.effective_roles:
            g = grant(report, "Keycloak", f"role {role}")
            check(f"{profile_name}: holds realm role {role}", g is not None and g.decision == ALLOWED)
            check(f"{profile_name}: role {role} shown as group-inherited",
                  g is not None and g.inheritance == rbac.GROUP_INHERITED,
                  f"inheritance={(g.inheritance if g else 'n/a')}")

        vg = grant(report, "Vault", f"policy {profile.vault_policy}")
        check(f"{profile_name}: Vault policy {profile.vault_policy} attached",
              vg is not None and vg.decision == ALLOWED)
        check(f"{profile_name}: Vault grant explains its source",
              vg is not None and profile.name in (vg.source or ""),
              f"source={(vg.source if vg else 'n/a')}")

        paths = [g for g in report.grants if g.service == "Vault" and g.inheritance == rbac.DERIVED]
        check(f"{profile_name}: policy document resolved into paths", len(paths) > 0,
              "no Vault paths were parsed from the live policy document")

        tg = grant(report, "Gitea", f"team {profile.gitea_team}")
        check(f"{profile_name}: Gitea team {profile.gitea_team} present",
              tg is not None and tg.decision == ALLOWED)

        check(f"{profile_name}: no drift straight after provisioning",
              report.drift == [], f"drift={[d.kind for d in report.drift]}")


def test_contractor_is_least_privileged(sim, catalogue) -> None:
    section("Contractor is genuinely the smallest grant")

    move_to("contractor")
    contractor = report_for(sim, SUBJECT)
    contractor_allowed = {g.key() for g in contractor.allowed()}

    move_to("platform-admin")
    admin = report_for(sim, SUBJECT)
    admin_allowed = {g.key() for g in admin.allowed()}

    check("platform-admin reaches strictly more than contractor",
          len(admin_allowed) > len(contractor_allowed),
          f"contractor={len(contractor_allowed)} admin={len(admin_allowed)}")

    denied = [g for g in contractor.grants
              if g.service == "Vault" and g.decision == NOT_AUTHORIZED and g.permission == "deny"]
    check("contractor policy carries explicit deny rules", len(denied) > 0,
          "no deny paths were surfaced for the contractor policy")


def test_mover_reflected(sim, catalogue) -> None:
    """E: a lifecycle move changes what the simulator reports."""
    section("Mover is reflected in effective access")

    move_to("developer")
    before = report_for(sim, SUBJECT)

    run(JML, "move", "--user", SUBJECT, "--from", "developer", "--to", "security")
    after = report_for(sim, SUBJECT)

    check("group changed", before.groups != after.groups, f"{before.groups} -> {after.groups}")
    check("old realm role gone", "developer" not in after.realm_roles, f"roles={after.realm_roles}")
    check("new realm role present", "security-analyst" in after.realm_roles)
    check("profile re-inferred", after.inferred_profile == "security", after.inferred_profile)
    check("old Vault policy gone", grant(after, "Vault", "policy developer") is None)
    check("new Vault policy present", grant(after, "Vault", "policy security-analyst") is not None)
    check("old Gitea team gone", grant(after, "Gitea", "team developers") is None)
    check("new Gitea team present", grant(after, "Gitea", "team security") is not None)
    check("no drift after a clean move", after.drift == [],
          f"drift={[d.kind for d in after.drift]}")


def test_drift_extra_gitea_team(sim, catalogue, gt) -> None:
    """H, I: an extra team membership is drift, and is reported as such."""
    section("Drift: extra Gitea team")

    move_to("security")
    clean = report_for(sim, SUBJECT)
    check("baseline is drift-free", clean.drift == [], f"drift={[d.kind for d in clean.drift]}")

    # Create real drift through the adapter, not through the simulator.
    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "developers", "write", scratch)

    drifted = report_for(sim, SUBJECT)
    check("extra Gitea team is detected", "EXTRA_GITEA_TEAM" in drift_kinds(drifted),
          f"drift={drift_kinds(drifted)}")

    finding = next((d for d in drifted.drift if d.kind == "EXTRA_GITEA_TEAM"), None)
    check("drift finding names the offending team",
          finding is not None and "developers" in finding.actual, str(finding))
    check("drift finding states what was expected",
          finding is not None and finding.expected == "security", str(finding))

    gt.remove_team_membership(SUBJECT, "developers", scratch)
    restored = report_for(sim, SUBJECT)
    check("drift clears once the extra team is removed", restored.drift == [],
          f"drift={[d.kind for d in restored.drift]}")


def test_drift_unexpected_vault_policy(sim, catalogue, vt) -> None:
    """J: a Vault policy the profile does not grant is drift."""
    section("Drift: unexpected Vault policy")

    move_to("contractor")
    scratch = ServiceResult("fixture")

    # Attach a policy the contractor profile does not grant.
    vt._call("POST", f"/auth/userpass/users/{SUBJECT}/policies",
             json_body={"token_policies": "platform-admin"})

    drifted = report_for(sim, SUBJECT)
    check("unexpected Vault policy is detected",
          "UNEXPECTED_VAULT_POLICY" in drift_kinds(drifted), f"drift={drift_kinds(drifted)}")
    check("missing expected policy is also reported",
          "MISSING_VAULT_POLICY" in drift_kinds(drifted), f"drift={drift_kinds(drifted)}")

    finding = next((d for d in drifted.drift if d.kind == "UNEXPECTED_VAULT_POLICY"), None)
    check("Vault drift names the offending policy",
          finding is not None and "platform-admin" in finding.actual, str(finding))

    # Reconcile back through the lifecycle engine, which is the supported fix.
    run(JML, "join", "--user", SUBJECT, "--role", "contractor")
    restored = report_for(sim, SUBJECT)
    check("re-running the joiner clears the Vault drift",
          "UNEXPECTED_VAULT_POLICY" not in drift_kinds(restored),
          f"drift={drift_kinds(restored)}")


def test_leaver(sim) -> None:
    """F: a disabled identity reports honestly, not as 'not found'."""
    section("Leaver / disabled identity")

    move_to("developer")
    run(JML, "leave", "--user", SUBJECT)
    report = report_for(sim, SUBJECT)

    check("identity still exists (retained, not deleted)", report.exists)
    check("identity reported as disabled", not report.enabled)
    check("lifecycle state surfaced", report.lifecycle_state == "offboarded",
          report.lifecycle_state)
    check("no groups remain", report.groups == [], f"groups={report.groups}")
    check("no realm roles remain", report.realm_roles == [], f"roles={report.realm_roles}")

    kc_account = grant(report, "Keycloak", "account")
    check("Keycloak account shown as not authorized",
          kc_account is not None and kc_account.decision == NOT_AUTHORIZED)
    check("reason mentions the disabled state",
          kc_account is not None and "disabled" in kc_account.source)

    gitea_account = grant(report, "Gitea", "account")
    check("retained Gitea account is reported, not omitted", gitea_account is not None)
    check("Gitea account shown as deactivated",
          gitea_account is not None and gitea_account.decision == NOT_AUTHORIZED)

    check("no allowed grants remain in Keycloak, Vault or Gitea",
          not [g for g in report.allowed() if g.service in ("Keycloak", "Vault", "Gitea")],
          f"still allowed: {[g.key() for g in report.allowed()]}")


def test_stale_downstream_after_leave(sim, gt) -> None:
    """A retained downstream entitlement after offboarding must surface as drift."""
    section("Drift: stale entitlement after a leaver")

    move_to("developer")
    run(JML, "leave", "--user", SUBJECT)

    scratch = ServiceResult("fixture")
    gt.ensure_team_membership(SUBJECT, "developers", "write", scratch)

    report = report_for(sim, SUBJECT)
    check("stale Gitea team after offboarding is drift",
          "STALE_GITEA_TEAM" in drift_kinds(report), f"drift={drift_kinds(report)}")

    gt.remove_team_membership(SUBJECT, "developers", scratch)
    check("cleanup restores a drift-free offboarded state",
          "STALE_GITEA_TEAM" not in drift_kinds(report_for(sim, SUBJECT)))


def test_join_refuses_second_profile(sim, catalogue) -> None:
    """
    The joiner must not stack a second profile onto an existing identity.

    It only ever ADDS a group, so without a guard a second `jml-join` with a
    different role would leave the person holding both — the entitlement
    accumulation this milestone exists to prevent. The RBAC simulator found this
    while walking all four profiles, which is exactly the kind of thing an
    access review is supposed to catch.
    """
    section("Joiner refuses to stack profiles")

    move_to("developer")
    baseline = report_for(sim, SUBJECT)
    check("subject holds exactly one group before the attempt",
          len(baseline.groups) == 1, f"groups={baseline.groups}")

    proc = run(JML, "join", "--user", SUBJECT, "--role", "security", expect_rc=2)
    check("second profile is refused", proc.returncode == 2)
    check("error names the profile already held", "developer" in (proc.stdout + proc.stderr))
    check("error points at the mover",
          "jml-move" in (proc.stdout + proc.stderr),
          "the message should tell the operator what to do instead")

    after = report_for(sim, SUBJECT)
    check("refusal left access unchanged", after.groups == baseline.groups,
          f"{baseline.groups} -> {after.groups}")
    check("no accumulation drift was created", "EXTRA_KEYCLOAK_GROUP" not in drift_kinds(after))

    same = run(JML, "join", "--user", SUBJECT, "--role", "developer")
    check("re-joining the SAME profile still reconciles", same.returncode == 0)


def test_unknown_user(sim) -> None:
    """G: an identity that exists nowhere."""
    section("Unknown identity")

    report = report_for(sim, "nosuchperson")
    check("reports as non-existent", not report.exists)
    check("no groups", report.groups == [])
    check("explains that nothing was found", any("No Keycloak identity" in n for n in report.notes),
          f"notes={report.notes}")
    check("downstream services still evaluated",
          grant(report, "Gitea", "account") is not None,
          "an account outliving its identity would be missed")

    proc = run(RBAC, "show", "--user", "../etc/passwd", expect_rc=2)
    check("path traversal in USER is rejected", proc.returncode == 2)
    proc = run(RBAC, "show", "--user", "erin;whoami", expect_rc=2)
    check("shell metacharacters in USER are rejected", proc.returncode == 2)


def test_readonly(sim, kc, vt, gt) -> None:
    """K: running every command changes nothing, and repeats are deterministic."""
    section("Read-only and deterministic")

    move_to("security")

    def snapshot():
        return {
            "keycloak": kc.snapshot(SUBJECT),
            "vault": vt.snapshot(SUBJECT),
            "gitea": gt.snapshot(SUBJECT),
            "alice": kc.snapshot("alice"),
            "bob": kc.snapshot("bob"),
        }

    before = snapshot()

    first = run(RBAC, "show", "--user", SUBJECT)
    run(RBAC, "show", "--user", SUBJECT, "--format", "json")
    run(RBAC, "diff", "--user", "alice", "--other", "bob")
    run(RBAC, "who-can", "--permission", "vault:secret/data/security/")
    second = run(RBAC, "show", "--user", SUBJECT)

    after = snapshot()

    check("no identity state changed by any rbac command", before == after,
          "the simulator mutated something — it must be read-only")
    check("repeated runs produce identical output", first.stdout == second.stdout,
          "output is not deterministic")
    check("show exits 0", first.returncode == 0)


def test_json_output(sim) -> None:
    section("JSON output")

    move_to("developer")
    proc = run(RBAC, "show", "--user", SUBJECT, "--format", "json")

    try:
        doc = json.loads(proc.stdout)
        parsed = True
    except json.JSONDecodeError as exc:
        doc, parsed = {}, False
        check("output parses as JSON", False, str(exc))

    if parsed:
        check("output parses as JSON", True)
        for field in ("user", "exists", "enabled", "groups", "realm_roles", "grants", "drift", "summary"):
            check(f"JSON has {field!r}", field in doc)
        check("grants carry a reason",
              all("source" in g for g in doc.get("grants", [])))
        check("grants carry a decision",
              all("decision" in g for g in doc.get("grants", [])))
        check("summary marks the run read-only", doc.get("summary", {}).get("readOnly") is True)

    diff = run(RBAC, "diff", "--user", "alice", "--other", "bob", "--format", "json")
    try:
        ddoc = json.loads(diff.stdout)
        check("diff JSON parses", True)
        check("diff JSON has onlyLeft/onlyRight", "onlyLeft" in ddoc and "onlyRight" in ddoc)
    except json.JSONDecodeError as exc:
        check("diff JSON parses", False, str(exc))


def test_diff_and_who_can(sim) -> None:
    section("rbac-diff and rbac-who-can")

    diff = run(RBAC, "diff", "--user", "alice", "--other", "dave")
    check("diff exits 0", diff.returncode == 0)
    check("diff names both identities", "alice" in diff.stdout and "dave" in diff.stdout)
    check("diff shows something alice has that dave does not",
          "platform-admin" in diff.stdout, "expected the admin role to appear as a difference")

    who = run(RBAC, "who-can", "--permission", "keycloak:role platform-admin")
    check("who-can exits 0", who.returncode == 0)
    check("who-can finds alice for platform-admin", "alice" in who.stdout, who.stdout[-400:])
    check("who-can does not list dave for platform-admin", "dave" not in who.stdout.split("Matches")[-1])

    bad = run(RBAC, "who-can", "--permission", "no-colon-here", expect_rc=2)
    check("malformed PERMISSION is rejected", bad.returncode == 2)

    # Wildcard coverage, in both directions. A user holding secret/* can reach
    # secret/data/security/, and missing that would answer "nobody" — the most
    # dangerous wrong answer an access review can give.
    check("a wildcard grant covers a deeper query",
          rbac.resource_matches("secret/*", "secret/data/security/"))
    check("a deeper grant covers a shallower query",
          rbac.resource_matches("secret/data/security/*", "secret/"))
    check("unrelated paths do not match",
          not rbac.resource_matches("secret/data/apps/*", "transit/keys/"))
    check("an empty query matches anything in the service",
          rbac.resource_matches("anything", ""))

    wide = run(RBAC, "who-can", "--permission", "vault:secret/data/security/")
    check("who-can finds the wildcard holder", "alice" in wide.stdout,
          "alice holds secret/* and must be reported for a security path")
    check("who-can finds the explicit holder", "carol" in wide.stdout, wide.stdout[-300:])


def test_seeded_users_untouched(kc) -> None:
    """L: the demo realm must survive the suite intact."""
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
    print("\033[1mRBAC simulator test suite\033[0m")
    print(f"\033[2mDisposable subject: {SUBJECT}. Seeded demo users are read, never modified.\033[0m")

    catalogue, kc, vt, gt = services()
    sim = Simulator(kc, vt, gt, catalogue)

    test_profiles(sim, catalogue)
    test_contractor_is_least_privileged(sim, catalogue)
    test_mover_reflected(sim, catalogue)
    test_drift_extra_gitea_team(sim, catalogue, gt)
    test_drift_unexpected_vault_policy(sim, catalogue, vt)
    test_join_refuses_second_profile(sim, catalogue)
    test_leaver(sim)
    test_stale_downstream_after_leave(sim, gt)
    test_unknown_user(sim)
    test_readonly(sim, kc, vt, gt)
    test_json_output(sim)
    test_diff_and_who_can(sim)
    test_seeded_users_untouched(kc)

    # Leave the disposable identity offboarded so a re-run starts from a known
    # state rather than inheriting whatever the last test happened to set.
    run(JML, "leave", "--user", SUBJECT)

    section("Summary")
    total = len(PASSED) + len(FAILED)
    print(f"  {len(PASSED)}/{total} checks passed")
    if FAILED:
        print(f"\n  \033[31m{len(FAILED)} failed:\033[0m")
        for name in FAILED:
            print(f"    - {name}")
        return 1
    print("  \033[32mall RBAC simulator checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
