#!/usr/bin/env python3
"""
Joiner / Mover / Leaver lifecycle engine  (roadmap v2-1).

Runs inside a throwaway container; the operator never invokes it directly.

    make jml-join  USER=erin ROLE=developer
    make jml-move  USER=erin FROM=contractor TO=developer
    make jml-leave USER=erin
    make jml-show  USER=erin

Design: every flow is RECONCILIATION. It reads what each service currently
reports, compares that with the desired state a role profile declares, and
applies only the differences. That is what makes the commands idempotent, and
it is closer to how real provisioning works than firing create calls and hoping.

There are no transactions across Keycloak, Vault and Gitea. Each service reports
its own outcome, and the command exits PARTIAL_FAILURE if some succeeded and
others did not -- see docs/identity-governance.md for the reconciliation story.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys

import record
from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError, Unavailable
from vault import Vault
from model import (
    FAILED,
    FAILURE,
    PARTIAL_FAILURE,
    PROTECTED_USERNAMES,
    SUCCESS,
    UNCHANGED,
    UPDATED,
    ServiceResult,
    ValidationError,
    load_catalogue,
    validate_username,
)

# -- presentation -------------------------------------------------------------

USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text


BOLD = lambda t: _c("1", t)      # noqa: E731
DIM = lambda t: _c("2", t)       # noqa: E731
GREEN = lambda t: _c("32", t)    # noqa: E731
YELLOW = lambda t: _c("33", t)   # noqa: E731
RED = lambda t: _c("31", t)      # noqa: E731
CYAN = lambda t: _c("36", t)     # noqa: E731

STATUS_STYLE = {
    "CREATED": GREEN,
    "UPDATED": CYAN,
    "UNCHANGED": DIM,
    "SKIPPED": YELLOW,
    "FAILED": RED,
}


def heading(text: str) -> None:
    print(f"\n{BOLD(text)}\n{DIM('─' * len(text))}")


def emit(result: ServiceResult) -> None:
    heading(result.service)
    for change in result.changes:
        style = STATUS_STYLE.get(change.status, str)
        print(f"  {style(change.status.ljust(9))} {change.detail}")
    for check in result.verifications:
        mark = GREEN("verified ") if check["ok"] else RED("FAILED   ")
        print(f"  {mark} {check['detail']}")
    if result.error:
        print(f"  {RED('ERROR')}     {result.error}")


# -- service wiring -----------------------------------------------------------


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def build_services(catalogue):
    keycloak = Keycloak(
        base_url=env("KEYCLOAK_URL", "http://keycloak:8080"),
        admin_user=env("KEYCLOAK_ADMIN", "admin"),
        admin_password=env("KEYCLOAK_ADMIN_PASSWORD"),
        realm=env("KEYCLOAK_REALM", "lab"),
    )
    vault = Vault(
        base_url=env("VAULT_ADDR", "http://vault:8200"),
        token=env("VAULT_TOKEN"),
    )
    gitea = Gitea(
        base_url=env("GITEA_URL", "http://gitea:3000"),
        admin_user=env("GITEA_ADMIN_USER", "labadmin"),
        admin_password=env("GITEA_ADMIN_PASSWORD"),
        org=catalogue.gitea_org,
        transfer_target=catalogue.gitea_transfer_target,
    )
    return keycloak, vault, gitea


def preflight(keycloak, vault, gitea) -> None:
    """
    Fail before changing anything if a service is unreachable.

    Especially important for the leaver flow: a half-finished offboarding that
    disabled Keycloak but never reached Vault is worse than one that refused to
    start, because the operator believes it is done.
    """
    for name, probe in (("Keycloak", keycloak.ping), ("Vault", vault.ping), ("Gitea", gitea.ping)):
        try:
            probe()
        except (Unavailable, HttpError, PermissionError) as exc:
            raise SystemExit(
                f"{RED('✗')} {name} is not usable: {exc}\n"
                f"  The lifecycle command stopped before making any change.\n"
                f"  Check {CYAN('make health')} and try again."
            )


# -- access snapshots and diffing ---------------------------------------------


def capture(keycloak, vault, gitea, username: str) -> dict:
    return {
        "keycloak": keycloak.snapshot(username),
        "vault": vault.snapshot(username),
        "gitea": gitea.snapshot(username),
    }


def flatten(snapshot: dict) -> set:
    """Reduce a snapshot to comparable entitlement strings."""
    items = set()
    for group in snapshot["keycloak"]["groups"]:
        items.add(f"keycloak:group {group}")
    for role in snapshot["keycloak"]["roles"]:
        items.add(f"keycloak:role {role}")
    for policy in snapshot["vault"]["policies"]:
        items.add(f"vault:policy {policy}")
    for team in snapshot["gitea"]["teams"]:
        items.add(f"gitea:team {team}")
    return items


def print_snapshot(title: str, snapshot: dict) -> None:
    heading(title)
    kc, vt, gt = snapshot["keycloak"], snapshot["vault"], snapshot["gitea"]

    state = "enabled" if kc["enabled"] else "DISABLED"
    if not kc["exists"]:
        state = "absent"
    print(f"  {'Keycloak account':<20} {state}")
    print(f"  {'Keycloak groups':<20} {', '.join(kc['groups']) or DIM('(none)')}")
    print(f"  {'Effective roles':<20} {', '.join(kc['roles']) or DIM('(none)')}")
    print(f"  {'Active sessions':<20} {kc['sessions']}")
    print(f"  {'Vault identity':<20} {', '.join(vt['policies']) or DIM('(none)')}")
    gitea_state = "active" if gt["active"] else ("DEACTIVATED" if gt["exists"] else "absent")
    print(f"  {'Gitea account':<20} {gitea_state}")
    print(f"  {'Gitea teams':<20} {', '.join(gt['teams']) or DIM('(none)')}")
    print(f"  {'Gitea repositories':<20} {', '.join(gt['repositories']) or DIM('(none)')}")


def print_diff(before: dict, after: dict) -> dict:
    removed = sorted(flatten(before) - flatten(after))
    added = sorted(flatten(after) - flatten(before))

    heading("Access diff")
    if not removed and not added:
        print(f"  {DIM('no entitlement changes')}")
    for item in removed:
        print(f"  {RED('- ' + item)}")
    for item in added:
        print(f"  {GREEN('+ ' + item)}")

    return {"removed": removed, "added": added}


# -- outcome ------------------------------------------------------------------


def summarise(results: list) -> str:
    failed = [r for r in results if r.failed]
    if not failed:
        overall = SUCCESS
    elif len(failed) == len(results):
        overall = FAILURE
    else:
        overall = PARTIAL_FAILURE

    heading("Result")
    for r in results:
        style = RED if r.failed else (CYAN if r.touched else DIM)
        print(f"  {r.service:<12} {style(FAILED if r.failed else r.status)}")

    style = GREEN if overall == SUCCESS else (YELLOW if overall == PARTIAL_FAILURE else RED)
    print(f"\n  {BOLD('Overall')}      {style(overall)}")

    if overall != SUCCESS:
        print(
            f"\n  {YELLOW('This operation is not atomic across services.')}\n"
            f"  Fix the underlying problem and re-run the same command — it\n"
            f"  reconciles, so it will complete only the parts still outstanding."
        )
    return overall


def employee_id(username: str) -> str:
    """
    A stable employee identifier derived from the username.

    Deliberately not `hash()`: Python seeds its string hash randomly per
    process, so the same person would receive a different employee ID on every
    run. An identifier that changes when you look at it twice is worse than no
    identifier at all — it silently breaks any downstream correlation.

    SHA-256 is not used here for secrecy, only for a stable, well-distributed
    mapping that produces the same value on every machine and every run.
    """
    digest = hashlib.sha256(username.encode()).hexdigest()
    return f"E-{int(digest[:8], 16) % 90000 + 10000}"


def initial_password() -> str:
    """
    The credential a joiner starts with.

    Reuses DEMO_USER_PASSWORD, which is what init-keycloak.sh and init-vault.sh
    already give the seeded users, so a provisioned identity behaves exactly like
    alice or bob. `make creds` prints it. It is never written to an artifact.
    """
    return env("DEMO_USER_PASSWORD", "demo-insecure-dev-only")


def guard_protected(username: str, operation: str) -> None:
    if username in PROTECTED_USERNAMES and env("LAB_ALLOW_PROTECTED") != "1":
        raise ValidationError(
            f"refusing to {operation} {username!r}: it is a seeded demo identity.\n"
            "  The demo realm is part of the lab. Use a disposable name such as 'erin'."
        )


# -- flows --------------------------------------------------------------------


def do_join(args, catalogue, services) -> int:
    keycloak, vault, gitea = services
    username = validate_username(args.user)
    guard_protected(username, "provision over")
    profile = catalogue.get(args.role)
    password = initial_password()

    print(f"\n{BOLD('Joiner')} — provisioning {CYAN(username)} as {CYAN(profile.name)}")
    print(f"  {DIM(profile.summary)}")

    before = capture(keycloak, vault, gitea, username)

    kc = ServiceResult("Keycloak")
    vt = ServiceResult("Vault")
    gt = ServiceResult("Gitea")

    try:
        user = keycloak.reconcile_user(username, profile, employee_id(username), kc)
        keycloak.set_password_if_absent(user["id"], username, password, kc)
        keycloak.ensure_group(user["id"], username, profile.keycloak_group, kc)
        roles = keycloak.effective_realm_roles(user["id"])
        kc.verify(
            set(profile.effective_roles).issubset(set(roles)),
            f"effective roles include {', '.join(profile.effective_roles)} (actual: {', '.join(roles) or 'none'})",
        )
    except (HttpError, Unavailable, LookupError) as exc:
        kc.error = str(exc)

    try:
        vault.reconcile_user(username, profile.vault_policy, password, vt)
        vt.verify(
            vault.user_policies(username) == [profile.vault_policy],
            f"userpass identity holds exactly [{profile.vault_policy}]",
        )
    except (HttpError, Unavailable, LookupError) as exc:
        vt.error = str(exc)

    try:
        gitea.ensure_org(catalogue.gitea_org_full_name, catalogue.gitea_org_description, gt)
        gitea.reconcile_user(username, password, f"{username.capitalize()} (lab)", gt)
        gitea.ensure_team_membership(username, profile.gitea_team, profile.gitea_permission, gt)
        gt.verify(
            profile.gitea_team in gitea.team_memberships(username),
            f"member of team {profile.gitea_team}",
        )
    except (HttpError, Unavailable, PermissionError) as exc:
        gt.error = str(exc)

    results = [kc, vt, gt]
    for r in results:
        emit(r)

    after = capture(keycloak, vault, gitea, username)
    diff = print_diff(before, after)
    overall = summarise(results)

    if overall == SUCCESS:
        heading("Welcome summary")
        print(f"  {'User':<14} {username}")
        print(f"  {'Profile':<14} {profile.name} — {profile.summary}")
        print(f"  {'Keycloak':<14} {profile.keycloak_group}  ({', '.join(profile.effective_roles)})")
        for grant in profile.vault_grants:
            print(f"  {'Vault':<14} {grant}" if grant == profile.vault_grants[0] else f"  {'':<14} {grant}")
        print(f"  {'Gitea':<14} team {profile.gitea_team} ({profile.gitea_permission}) in {catalogue.gitea_org}")
        print(f"\n  {DIM('Credential: the shared lab demo password — see')} {CYAN('make creds')}")

    path = record.write(
        "join",
        username,
        {
            "profile": profile.name,
            "desiredState": {
                "keycloakGroup": profile.keycloak_group,
                "vaultPolicy": profile.vault_policy,
                "giteaTeam": profile.gitea_team,
            },
            "servicesEvaluated": [r.service for r in results],
            "results": [r.as_dict() for r in results],
            "accessBefore": before,
            "accessAfter": after,
            "entitlementsAdded": diff["added"],
            "entitlementsRemoved": diff["removed"],
            "outcome": overall,
        },
    )
    print(f"\n  {DIM('Lifecycle record:')} {path}")
    return 0 if overall == SUCCESS else 1


def do_move(args, catalogue, services) -> int:
    keycloak, vault, gitea = services
    username = validate_username(args.user)
    guard_protected(username, "move")
    source = catalogue.get(args.source)
    target = catalogue.get(args.target)

    if source.name == target.name:
        raise ValidationError(f"FROM and TO are both {source.name!r}; nothing to move")

    print(f"\n{BOLD('Mover')} — {CYAN(username)}: {CYAN(source.name)} → {CYAN(target.name)}")

    try:
        user = keycloak.require_user(username)
    except LookupError as exc:
        raise SystemExit(f"{RED('✗')} {exc}\n  Run {CYAN(f'make jml-join USER={username} ROLE={target.name}')} first.")

    before = capture(keycloak, vault, gitea, username)
    print_snapshot("Before", before)

    kc = ServiceResult("Keycloak")
    vt = ServiceResult("Vault")
    gt = ServiceResult("Gitea")
    password = initial_password()

    try:
        # Remove first, then add. A mover must never hold both entitlements,
        # even for the few milliseconds between two API calls.
        keycloak.remove_group(user["id"], source.keycloak_group, kc)
        keycloak.ensure_group(user["id"], username, target.keycloak_group, kc)

        roles = keycloak.effective_realm_roles(user["id"])
        obsolete = set(source.effective_roles) - set(target.effective_roles)
        kc.verify(not (obsolete & set(roles)), f"obsolete roles gone: {', '.join(sorted(obsolete)) or '(none)'}")
        kc.verify(set(target.effective_roles).issubset(set(roles)), f"new roles present: {', '.join(target.effective_roles)}")

        # Re-authorisation only takes effect on a new token, so end the old
        # sessions rather than leave the person holding their previous access.
        keycloak.revoke_sessions(user["id"], kc)
    except (HttpError, Unavailable, LookupError) as exc:
        kc.error = str(exc)

    try:
        # replace, not append -- this is where entitlement accumulation dies
        vault.reconcile_user(username, target.vault_policy, password, vt)
        vt.verify(
            vault.user_policies(username) == [target.vault_policy],
            f"Vault policy replaced by [{target.vault_policy}], {source.vault_policy} gone",
        )
    except (HttpError, Unavailable, LookupError) as exc:
        vt.error = str(exc)

    try:
        gitea.ensure_org(catalogue.gitea_org_full_name, catalogue.gitea_org_description, gt)
        gitea.remove_team_membership(username, source.gitea_team, gt)
        gitea.ensure_team_membership(username, target.gitea_team, target.gitea_permission, gt)
        teams = gitea.team_memberships(username)
        gt.verify(source.gitea_team not in teams, f"no longer in team {source.gitea_team}")
        gt.verify(target.gitea_team in teams, f"now in team {target.gitea_team}")
    except (HttpError, Unavailable, PermissionError) as exc:
        gt.error = str(exc)

    results = [kc, vt, gt]
    for r in results:
        emit(r)

    after = capture(keycloak, vault, gitea, username)
    print_snapshot("After", after)
    diff = print_diff(before, after)
    overall = summarise(results)

    path = record.write(
        "move",
        username,
        {
            "fromProfile": source.name,
            "toProfile": target.name,
            "servicesEvaluated": [r.service for r in results],
            "results": [r.as_dict() for r in results],
            "accessBefore": before,
            "accessAfter": after,
            "entitlementsRemoved": diff["removed"],
            "entitlementsAdded": diff["added"],
            "outcome": overall,
        },
    )
    print(f"\n  {DIM('Lifecycle record:')} {path}")
    return 0 if overall == SUCCESS else 1


def do_leave(args, catalogue, services) -> int:
    keycloak, vault, gitea = services
    username = validate_username(args.user)
    guard_protected(username, "offboard")

    print(f"\n{BOLD('Leaver')} — offboarding {CYAN(username)}")

    before = capture(keycloak, vault, gitea, username)
    if not before["keycloak"]["exists"] and not before["gitea"]["exists"] and not before["vault"]["exists"]:
        print(f"\n  {DIM('Nothing to offboard: this identity does not exist in any managed service.')}")
        print(f"  {DIM('Leaver is idempotent — this is a successful no-op.')}")
        return 0

    print_snapshot("Before", before)

    kc = ServiceResult("Keycloak")
    vt = ServiceResult("Vault")
    gt = ServiceResult("Gitea")
    password = initial_password()

    try:
        user = keycloak.find_user(username)
        if user is None:
            kc.record(UNCHANGED, "no Keycloak account exists")
        else:
            keycloak.disable_user(user, kc)
            keycloak.revoke_sessions(user["id"], kc)
            keycloak.strip_all_groups(user["id"], kc)
            kc.verify(not keycloak.can_authenticate(username, password), "password grant now refused")
            kc.verify(keycloak.active_session_count(user["id"]) == 0, "zero active sessions remain")
    except (HttpError, Unavailable, LookupError) as exc:
        kc.error = str(exc)

    try:
        vault.revoke_identity(username, vt)
        vt.verify(not vault.can_authenticate(username, password), "Vault userpass login now refused")
    except (HttpError, Unavailable) as exc:
        vt.error = str(exc)

    try:
        # Custody before deactivation: a deactivated account can still own
        # repositories, but transferring them is clearer while it is intact.
        gitea.transfer_repositories(username, gt)
        gitea.remove_all_team_memberships(username, gt)
        gitea.deactivate_user(username, gt)
        gt.verify(not gitea.can_authenticate(username, password), "Gitea login now refused")
        gt.verify(gitea.team_memberships(username) == [], "no team memberships remain")
    except (HttpError, Unavailable, PermissionError) as exc:
        gt.error = str(exc)

    results = [kc, vt, gt]
    for r in results:
        emit(r)

    after = capture(keycloak, vault, gitea, username)
    diff = print_diff(before, after)
    overall = summarise(results)

    path = record.write(
        "leave",
        username,
        {
            "servicesEvaluated": [r.service for r in results],
            "results": [r.as_dict() for r in results],
            "accessBefore": before,
            "accessAfter": after,
            "entitlementsRemoved": diff["removed"],
            "repositoriesTransferredTo": catalogue.gitea_transfer_target,
            "identityRetained": True,
            "retentionRationale": (
                "Accounts are disabled, not deleted, so commit attribution and audit "
                "history survive. See docs/identity-governance.md."
            ),
            "outcome": overall,
        },
    )
    print(f"\n  {DIM('Offboarding record:')} {path}")
    return 0 if overall == SUCCESS else 1


def do_show(args, catalogue, services) -> int:
    keycloak, vault, gitea = services
    username = validate_username(args.user)
    snapshot = capture(keycloak, vault, gitea, username)
    print_snapshot(f"Effective access — {username}", snapshot)
    if not snapshot["keycloak"]["exists"]:
        print(f"\n  {DIM('No Keycloak identity. Provision one with')} {CYAN(f'make jml-join USER={username} ROLE=developer')}")
    return 0


# -- entry point --------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="jml", description="Identity lifecycle automation")
    sub = parser.add_subparsers(dest="command", required=True)

    p_join = sub.add_parser("join", help="provision an identity")
    p_join.add_argument("--user", required=True)
    p_join.add_argument("--role", required=True)

    p_move = sub.add_parser("move", help="change an identity's role profile")
    p_move.add_argument("--user", required=True)
    p_move.add_argument("--from", dest="source", required=True)
    p_move.add_argument("--to", dest="target", required=True)

    p_leave = sub.add_parser("leave", help="offboard an identity")
    p_leave.add_argument("--user", required=True)

    p_show = sub.add_parser("show", help="print effective access")
    p_show.add_argument("--user", required=True)

    args = parser.parse_args(argv)

    try:
        catalogue = load_catalogue()
        services = build_services(catalogue)
        preflight(*services)

        handler = {"join": do_join, "move": do_move, "leave": do_leave, "show": do_show}[args.command]
        return handler(args, catalogue, services)

    except ValidationError as exc:
        print(f"\n{RED('✗')} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"\n{YELLOW('interrupted')} — re-run the same command to finish reconciling.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
