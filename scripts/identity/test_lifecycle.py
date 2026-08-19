#!/usr/bin/env python3
"""
Identity lifecycle test suite.

Runs inside the same container as the engine, against the REAL running lab.
Nothing here is mocked: every assertion is a live API call to Keycloak, Vault or
Gitea. A mocked version of this suite would prove only that the mocks agree with
themselves, and the entire point of v2-1 is whether revocation actually happens.

Disposable identities only. `model.PROTECTED_USERNAMES` guards the seeded demo
users, and this suite never touches alice, bob, carol or dave.

    make jml-test
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import jml
import record
from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError, request
from model import PROTECTED_USERNAMES, load_catalogue, validate_username, ValidationError
from vault import Vault

# Two disposable identities: one for the lifecycle walk, one used only by the
# revocation test so a failure there cannot be masked by earlier mutations.
SUBJECT = "jmltest"
TOKEN_SUBJECT = "jmltoken"

ENGINE = "/engine/jml.py"
PASSWORD = os.environ.get("DEMO_USER_PASSWORD", "demo-insecure-dev-only")
KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080")
REALM = os.environ.get("KEYCLOAK_REALM", "lab")

PASSED, FAILED = [], []


# -- harness ------------------------------------------------------------------


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


def run(*args, expect_rc: int = 0) -> subprocess.CompletedProcess:
    """Invoke the real CLI, exactly as `make` does."""
    proc = subprocess.run(
        [sys.executable, ENGINE, *args],
        capture_output=True,
        text=True,
        env={**os.environ, "NO_COLOR": "1"},
    )
    if proc.returncode != expect_rc:
        print(f"    \033[2m[rc={proc.returncode}] {' '.join(args)}\033[0m")
        print("    " + (proc.stdout or "").replace("\n", "\n    ")[-1500:])
        print("    " + (proc.stderr or "").replace("\n", "\n    ")[-800:])
    return proc


def services():
    catalogue = load_catalogue()
    kc = Keycloak(
        KEYCLOAK_URL,
        os.environ.get("KEYCLOAK_ADMIN", "admin"),
        os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""),
        REALM,
    )
    vt = Vault(os.environ.get("VAULT_ADDR", "http://vault:8200"), os.environ.get("VAULT_TOKEN", ""))
    gt = Gitea(
        os.environ.get("GITEA_URL", "http://gitea:3000"),
        os.environ.get("GITEA_ADMIN_USER", "labadmin"),
        os.environ.get("GITEA_ADMIN_PASSWORD", ""),
        catalogue.gitea_org,
        catalogue.gitea_transfer_target,
    )
    return catalogue, kc, vt, gt


# -- token helpers ------------------------------------------------------------


def get_tokens(username: str) -> dict | None:
    """Password grant against the public lab-cli client."""
    try:
        return request(
            "POST",
            f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
            form_body={
                "client_id": "lab-cli",
                "username": username,
                "password": PASSWORD,
                "grant_type": "password",
                "scope": "openid",
            },
            retries=1,
        )
    except HttpError:
        return None


def userinfo_works(access_token: str) -> bool:
    """
    Call the userinfo endpoint with a bearer token.

    This is the honest revocation probe. Keycloak validates the token against
    live session state here, so a revoked session fails immediately — unlike a
    resource server that only checks the JWT signature locally.
    """
    try:
        data = request(
            "GET",
            f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
            retries=1,
        )
        return bool((data or {}).get("sub"))
    except HttpError:
        return False


def refresh_works(refresh_token: str) -> bool:
    try:
        data = request(
            "POST",
            f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
            form_body={
                "client_id": "lab-cli",
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            retries=1,
        )
        return bool((data or {}).get("access_token"))
    except HttpError:
        return False


# =============================================================================
# Tests
# =============================================================================


def test_validation(catalogue) -> None:
    section("Input validation and injection resistance")

    hostile = [
        "../../etc/passwd",
        "erin;rm -rf /",
        "erin$(whoami)",
        "erin`id`",
        "erin&&curl evil.test",
        "erin with space",
        "Erin",           # uppercase rejected: services differ on case folding
        "1erin",          # must start with a letter
        "e" * 40,         # too long
        "",
    ]
    rejected = 0
    for candidate in hostile:
        try:
            validate_username(candidate)
        except ValidationError:
            rejected += 1
    check("hostile usernames are rejected", rejected == len(hostile),
          f"{len(hostile) - rejected} of {len(hostile)} slipped through")

    check("ordinary username is accepted", validate_username("erin") == "erin")

    try:
        catalogue.get("not-a-real-profile")
        bad_profile_rejected = False
    except ValidationError:
        bad_profile_rejected = True
    check("unknown role profile is rejected", bad_profile_rejected)

    proc = run("join", "--user", "erin", "--role", "wizard", expect_rc=2)
    check("CLI exits 2 on unknown profile", proc.returncode == 2)
    check("CLI names the valid profiles in its error",
          "developer" in (proc.stdout + proc.stderr))

    proc = run("join", "--user", "alice", "--role", "developer", expect_rc=2)
    check("seeded demo identity is protected from mutation", proc.returncode == 2)
    check("alice is in the protected set", "alice" in PROTECTED_USERNAMES)

    # Regression: employee IDs were derived from hash(), which Python seeds
    # randomly per process, so the same person got a different ID on every run.
    # Determinism is asserted across a SEPARATE interpreter, because within one
    # process a randomised hash still looks stable.
    first = jml.employee_id("erin")
    same_process = jml.employee_id("erin")
    other_process = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '/engine'); import jml; print(jml.employee_id('erin'))"],
        capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"},
    ).stdout.strip()

    check("employee ID is stable within a process", first == same_process)
    check("employee ID is stable ACROSS processes", first == other_process,
          f"{first} != {other_process} — identifier is not reproducible")
    check("employee ID has the expected shape", first.startswith("E-") and len(first) == 7, first)
    check("different users get different employee IDs",
          jml.employee_id("erin") != jml.employee_id("erica"))


def test_joiner(catalogue, kc, vt, gt) -> None:
    section("Joiner")

    profile = catalogue.get("contractor")
    proc = run("join", "--user", SUBJECT, "--role", "contractor")
    check("jml-join exits 0", proc.returncode == 0)
    check("output reports CREATED", "CREATED" in proc.stdout)

    user = kc.find_user(SUBJECT)
    check("Keycloak user exists", user is not None)
    if user:
        check("Keycloak user is enabled", user.get("enabled") is True)
        groups = kc.user_groups(user["id"])
        check("assigned the profile's group", profile.keycloak_group in groups,
              f"groups={groups}")
        roles = kc.effective_realm_roles(user["id"])
        check("inherits the group's realm roles",
              set(profile.effective_roles).issubset(set(roles)), f"roles={roles}")
        attrs = user.get("attributes") or {}
        check("employeeId attribute was set", bool(attrs.get("employeeId")))
        check("lifecycleState is active", attrs.get("lifecycleState") == ["active"])

    check("Vault userpass identity exists", vt.user_policies(SUBJECT) is not None)
    check("Vault holds exactly the profile's policy",
          vt.user_policies(SUBJECT) == [profile.vault_policy],
          f"policies={vt.user_policies(SUBJECT)}")

    check("Gitea account exists", gt.find_user(SUBJECT) is not None)
    check("Gitea team membership matches profile",
          profile.gitea_team in gt.team_memberships(SUBJECT),
          f"teams={gt.team_memberships(SUBJECT)}")

    check("provisioned identity can authenticate to Keycloak",
          kc.can_authenticate(SUBJECT, PASSWORD))
    check("provisioned identity can authenticate to Vault",
          vt.can_authenticate(SUBJECT, PASSWORD))


def test_joiner_idempotent(catalogue, kc, vt, gt) -> None:
    section("Joiner idempotency")

    before_groups = kc.user_groups(kc.require_user(SUBJECT)["id"])
    proc = run("join", "--user", SUBJECT, "--role", "contractor")

    check("second join exits 0", proc.returncode == 0)
    check("second join reports UNCHANGED", "UNCHANGED" in proc.stdout)
    check("second join creates nothing", "CREATED" not in proc.stdout,
          "a second run created something — not idempotent")

    after_groups = kc.user_groups(kc.require_user(SUBJECT)["id"])
    check("group membership is not duplicated", before_groups == after_groups,
          f"{before_groups} -> {after_groups}")
    check("exactly one group membership", len(after_groups) == 1, f"groups={after_groups}")
    check("Vault policy list still has exactly one entry",
          len(vt.user_policies(SUBJECT) or []) == 1)
    check("Gitea team membership still singular",
          len(gt.team_memberships(SUBJECT)) == 1,
          f"teams={gt.team_memberships(SUBJECT)}")
    check("password was not reset on the second run",
          "password already set" in proc.stdout)


def test_mover(catalogue, kc, vt, gt) -> None:
    section("Mover")

    source = catalogue.get("contractor")
    target = catalogue.get("developer")

    proc = run("move", "--user", SUBJECT, "--from", "contractor", "--to", "developer")
    check("jml-move exits 0", proc.returncode == 0)
    check("prints a Before snapshot", "Before" in proc.stdout)
    check("prints an After snapshot", "After" in proc.stdout)
    check("prints an access diff", "Access diff" in proc.stdout)
    check("diff shows a removal", "- keycloak:" in proc.stdout or "- vault:" in proc.stdout,
          "no removed entitlement appeared in the diff")
    check("diff shows an addition", "+ keycloak:" in proc.stdout or "+ vault:" in proc.stdout)

    user = kc.require_user(SUBJECT)
    groups = kc.user_groups(user["id"])
    roles = set(kc.effective_realm_roles(user["id"]))

    check("old group membership removed", source.keycloak_group not in groups, f"groups={groups}")
    check("new group membership added", target.keycloak_group in groups, f"groups={groups}")
    check("no entitlement accumulation", len(groups) == 1, f"groups={groups}")

    obsolete = set(source.effective_roles) - set(target.effective_roles)
    check("obsolete realm roles are gone", not (obsolete & roles),
          f"still holds {sorted(obsolete & roles)}")
    check("new realm roles are present", set(target.effective_roles).issubset(roles),
          f"roles={sorted(roles)}")

    check("Vault policy replaced, not appended",
          vt.user_policies(SUBJECT) == [target.vault_policy],
          f"policies={vt.user_policies(SUBJECT)}")

    teams = gt.team_memberships(SUBJECT)
    check("old Gitea team removed", source.gitea_team not in teams, f"teams={teams}")
    check("new Gitea team added", target.gitea_team in teams, f"teams={teams}")


def test_mover_idempotent(catalogue, kc) -> None:
    section("Mover idempotency")

    proc = run("move", "--user", SUBJECT, "--from", "contractor", "--to", "developer")
    check("repeat move exits 0", proc.returncode == 0)
    check("repeat move converges without error", "nothing to remove" in proc.stdout)

    groups = kc.user_groups(kc.require_user(SUBJECT)["id"])
    check("still exactly one group after repeat", len(groups) == 1, f"groups={groups}")

    proc = run("move", "--user", SUBJECT, "--from", "developer", "--to", "developer", expect_rc=2)
    check("moving to the same profile is rejected", proc.returncode == 2)


def test_repository_custody(catalogue, gt) -> None:
    section("Gitea repository custody")

    repo_name = "jmltest-artifact"

    # Fixture cleanup, so the suite is repeatable.
    #
    # A previous run transferred this repository to the custody account, and
    # Gitea refuses a transfer into a name that is already taken. Removing the
    # PREVIOUS RUN'S copy is test hygiene on a disposable fixture -- it is not
    # the engine deleting a user's work, which never happens anywhere in
    # gitea.py.
    try:
        gt._call("DELETE", f"/repos/{catalogue.gitea_transfer_target}/{repo_name}")
    except HttpError:
        pass  # nothing left over from a previous run

    # Create a repository owned by the subject so the leaver has something to
    # transfer. Done through the admin API, the same way a real user would end
    # up owning one.
    try:
        gt._call(
            "POST",
            f"/admin/users/{SUBJECT}/repos",
            json_body={"name": repo_name, "private": True, "auto_init": True,
                       "description": "Owned by a departing user; must survive offboarding."},
        )
    except HttpError as exc:
        if exc.status not in (409, 422):  # already owned by the subject
            check("could create a repository for the subject", False, str(exc))
            return

    owned = [r["name"] for r in gt.owned_repositories(SUBJECT)]
    check("subject owns a repository before offboarding", repo_name in owned, f"owned={owned}")


def test_leaver(catalogue, kc, vt, gt) -> None:
    section("Leaver")

    proc = run("leave", "--user", SUBJECT)
    check("jml-leave exits 0", proc.returncode == 0)

    user = kc.find_user(SUBJECT)
    check("Keycloak identity is RETAINED, not deleted", user is not None,
          "the account was deleted — audit history is lost")
    if user:
        check("Keycloak account is disabled", user.get("enabled") is False)
        check("lifecycleState marked offboarded",
              (user.get("attributes") or {}).get("lifecycleState") == ["offboarded"])
        check("all group memberships removed", kc.user_groups(user["id"]) == [],
              f"still in {kc.user_groups(user['id'])}")
        check("zero active sessions remain", kc.active_session_count(user["id"]) == 0)

    check("Keycloak password grant now refused", not kc.can_authenticate(SUBJECT, PASSWORD))
    check("Vault userpass identity removed", vt.user_policies(SUBJECT) is None)
    check("Vault login now refused", not vt.can_authenticate(SUBJECT, PASSWORD))

    gitea_user = gt.find_user(SUBJECT)
    check("Gitea account is RETAINED, not deleted", gitea_user is not None,
          "the account was deleted — commit attribution is lost")
    if gitea_user:
        check("Gitea account is deactivated", gitea_user.get("active") is False)
    check("Gitea team memberships removed", gt.team_memberships(SUBJECT) == [],
          f"teams={gt.team_memberships(SUBJECT)}")
    check("Gitea login now refused", not gt.can_authenticate(SUBJECT, PASSWORD))

    # Custody: the repository must still exist, under the transfer target.
    target = catalogue.gitea_transfer_target
    still_owned = [r["name"] for r in gt.owned_repositories(SUBJECT)]
    check("departing user no longer owns repositories", still_owned == [],
          f"still owns {still_owned}")
    try:
        gt._call("GET", f"/repos/{target}/jmltest-artifact")
        preserved = True
    except HttpError:
        preserved = False
    check("repository preserved under the custody account", preserved,
          f"repository not found under {target} — it may have been orphaned or lost")


def test_leaver_idempotent(kc) -> None:
    section("Leaver idempotency")

    proc = run("leave", "--user", SUBJECT)
    check("second leave exits 0", proc.returncode == 0)
    check("second leave reports already-disabled state",
          "already disabled" in proc.stdout or "UNCHANGED" in proc.stdout)

    proc = run("leave", "--user", "neverexisted")
    check("offboarding an unknown identity is a clean no-op", proc.returncode == 0)


def test_revocation() -> None:
    """
    The mandatory test: does a leaver's existing access actually stop working?

    Deliberately precise about what is and is not proven. Keycloak issues
    self-contained JWT access tokens, and no identity provider can reach into a
    resource server and un-issue one. What `jml-leave` does is:

      - end every session and invalidate the bound refresh tokens
      - bump the user's notBefore, so Keycloak rejects the token itself

    So the token stops working against anything that validates against Keycloak
    (userinfo, introspection, any forward-auth proxy). It would keep working, for
    up to accessTokenLifespan, against a resource server that only checks the
    signature offline. This test proves the first and documents the second.
    """
    section("Session and token revocation")

    run("join", "--user", TOKEN_SUBJECT, "--role", "developer")

    tokens = get_tokens(TOKEN_SUBJECT)
    if not check("obtained an access token before offboarding", tokens is not None):
        return

    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]

    check("access token works against a protected endpoint (userinfo)",
          userinfo_works(access_token))
    check("refresh token can mint new access tokens", refresh_works(refresh_token))

    # Keycloak's notBefore has one-second granularity. Without this the revoked
    # token can carry the same issued-at second as the revocation and slip
    # through — a real race, not a flaky test.
    time.sleep(2)

    proc = run("leave", "--user", TOKEN_SUBJECT)
    check("jml-leave exits 0 for the token subject", proc.returncode == 0)

    check("PRE-EXISTING access token is now REJECTED by userinfo",
          not userinfo_works(access_token),
          "the previously valid access token still works — revocation did not take effect")
    check("PRE-EXISTING refresh token can no longer mint tokens",
          not refresh_works(refresh_token),
          "refresh token survived offboarding — the holder retains indefinite access")
    check("new logins are refused", get_tokens(TOKEN_SUBJECT) is None)

    print("\n  \033[2mScope of this proof: the token is rejected by every check that consults")
    print("  Keycloak (userinfo, introspection, forward-auth). A resource server that")
    print("  validates the JWT purely offline would still accept it until exp — at most")
    print(f"  accessTokenLifespan ({os.environ.get('LAB_ACCESS_TOKEN_LIFESPAN', '300')}s).\033[0m")


def test_artifacts_have_no_secrets() -> None:
    section("Lifecycle records contain no secrets")

    root = Path(os.environ.get("LAB_ARTIFACT_DIR", "/artifacts")) / "identity"
    files = sorted(root.rglob("*.json"))
    check("lifecycle records were written", len(files) > 0, f"none found under {root}")

    haystack = "\n".join(f.read_text(encoding="utf-8") for f in files)

    secrets = {
        "demo user password": PASSWORD,
        "Keycloak admin password": os.environ.get("KEYCLOAK_ADMIN_PASSWORD", ""),
        "Vault root token": os.environ.get("VAULT_TOKEN", ""),
        "Gitea admin password": os.environ.get("GITEA_ADMIN_PASSWORD", ""),
        "Postgres password": os.environ.get("KEYCLOAK_DB_PASSWORD", ""),
    }
    for label, value in secrets.items():
        if not value or len(value) < 6:
            continue
        check(f"artifacts do not contain the {label}", value not in haystack,
              "a live credential was written to an audit artifact")

    for token in ("access_token", "refresh_token", "client_secret", "X-Vault-Token"):
        check(f"artifacts do not contain {token!r}", token not in haystack)

    # The redactor is the guarantee; test it directly rather than trusting that
    # today's payloads happen to be clean.
    scrubbed = record.redact(
        {
            "password": "hunter2",
            "nested": {"access_token": "abc", "client_secret": "def", "safe": "keep"},
            "list": [{"refresh_token": "ghi"}],
            "private_key": "-----BEGIN-----",
        }
    )
    check("redactor strips password", scrubbed["password"] == "<redacted>")
    check("redactor strips nested tokens", scrubbed["nested"]["access_token"] == "<redacted>")
    check("redactor strips nested client_secret", scrubbed["nested"]["client_secret"] == "<redacted>")
    check("redactor strips inside lists", scrubbed["list"][0]["refresh_token"] == "<redacted>")
    check("redactor strips private keys", scrubbed["private_key"] == "<redacted>")
    check("redactor preserves non-secret values", scrubbed["nested"]["safe"] == "keep")


def test_record_shape() -> None:
    section("Lifecycle record structure")

    root = Path(os.environ.get("LAB_ARTIFACT_DIR", "/artifacts")) / "identity" / SUBJECT
    leaves = sorted(root.glob("*-leave.json"))
    if not check("an offboarding record exists", bool(leaves), f"none under {root}"):
        return

    doc = json.loads(leaves[-1].read_text(encoding="utf-8"))
    for field in ("operation", "user", "timestamp", "servicesEvaluated", "results",
                  "accessBefore", "accessAfter", "outcome", "identityRetained"):
        check(f"offboarding record has {field!r}", field in doc)
    check("record names all three services",
          set(doc.get("servicesEvaluated", [])) == {"Keycloak", "Vault", "Gitea"},
          f"got {doc.get('servicesEvaluated')}")
    check("record captures verification results",
          any(r.get("verifications") for r in doc.get("results", [])))


# =============================================================================


def main() -> int:
    print("\033[1mIdentity lifecycle test suite\033[0m")
    print(f"\033[2mDisposable subjects: {SUBJECT}, {TOKEN_SUBJECT} — seeded demo users are never touched.\033[0m")

    catalogue, kc, vt, gt = services()

    test_validation(catalogue)
    test_joiner(catalogue, kc, vt, gt)
    test_joiner_idempotent(catalogue, kc, vt, gt)
    test_mover(catalogue, kc, vt, gt)
    test_mover_idempotent(catalogue, kc)
    test_repository_custody(catalogue, gt)
    test_leaver(catalogue, kc, vt, gt)
    test_leaver_idempotent(kc)
    test_revocation()
    test_artifacts_have_no_secrets()
    test_record_shape()

    section("Summary")
    total = len(PASSED) + len(FAILED)
    print(f"  {len(PASSED)}/{total} checks passed")
    if FAILED:
        print(f"\n  \033[31m{len(FAILED)} failed:\033[0m")
        for name in FAILED:
            print(f"    - {name}")
        return 1
    print("  \033[32mall identity lifecycle checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
