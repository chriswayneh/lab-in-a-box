#!/usr/bin/env python3
"""
Access review campaign command line interface (roadmap v2-3).

Rendering and argument handling live here; the workflow itself is in
campaign.py. Same split as rbac.py / rbac_cli.py.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import campaign
from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError, Unavailable
from model import ValidationError, load_catalogue
from rbac import Simulator
from vault import Vault

USE_COLOUR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if USE_COLOUR else text


BOLD = lambda t: _c("1", t)     # noqa: E731
DIM = lambda t: _c("2", t)      # noqa: E731
GREEN = lambda t: _c("32", t)   # noqa: E731
YELLOW = lambda t: _c("33", t)  # noqa: E731
RED = lambda t: _c("31", t)     # noqa: E731
CYAN = lambda t: _c("36", t)    # noqa: E731

DECISION_STYLE = {
    campaign.APPROVE: GREEN,
    campaign.REVOKE: RED,
    campaign.NOT_APPLICABLE: DIM,
    campaign.UNDECIDED: YELLOW,
}

def heading(text: str) -> None:
    print(f"\n{BOLD(text)}\n{DIM('─' * len(text))}")


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------


def render_campaign(c: "campaign.Campaign") -> None:
    print(f"\n{BOLD('Access review campaign')}: {CYAN(c.name)}  ({c.id})")
    print(f"  {'Status':<14} {c.status}")
    print(f"  {'Scope':<14} {c.scope}")
    print(f"  {'Reviewer':<14} {c.reviewer}")
    print(f"  {'Created':<14} {c.created_at}")
    print(f"  {'Opened':<14} {c.opened_at or DIM('(not opened)')}")
    print(f"  {'Completed':<14} {c.completed_at or DIM('(not completed)')}")
    if c.cancelled_at:
        print(f"  {'Cancelled':<14} {c.cancelled_at}")
    print(f"  {'Identities':<14} {', '.join(c.identities) or DIM('(none)')}")

    summary = c.summary()
    heading("Summary")
    print(f"  {summary['items']} entitlement(s) across {summary['identities']} identit{'y' if summary['identities']==1 else 'ies'}")
    for decision, count in sorted(summary["byDecision"].items()):
        style = DECISION_STYLE.get(decision, str)
        print(f"    {style(decision.ljust(14))} {count}")
    if summary["revokeRemediation"]:
        print(f"  {DIM('remediation, of revoked items:')}")
        for status, count in sorted(summary["revokeRemediation"].items()):
            print(f"    {status.ljust(28)} {count}")

    by_user: dict = {}
    for item in c.items:
        by_user.setdefault(item.username, []).append(item)

    for username in c.identities:
        items = by_user.get(username, [])
        heading(f"{username}  ({len(items)} entitlement(s))")
        if not items:
            print(f"  {DIM('no active entitlements found in scope')}")
        for item in items:
            dstyle = DECISION_STYLE.get(item.decision, str)
            print(f"  {dstyle(item.decision.ljust(14))} {item.service}:{item.resource} [{item.permission}]")
            print(f"      {DIM('why: ' + item.source + '  (' + item.inheritance + ')')}")
            print(f"      {DIM('expectation: ' + item.expectation)}")
            if item.note:
                print(f"      {DIM('note: ' + item.note)}")
            if item.decision == campaign.REVOKE:
                print(f"      {YELLOW('remediation: ' + item.remediation_status)}")
                if item.remediation_detail:
                    print(f"      {DIM('  ' + item.remediation_detail)}")

        drift = c.drift_context.get(username, [])
        if drift:
            print(f"\n  {YELLOW('Drift (context, not a decidable item):')}")
            for d in drift:
                print(f"    {d['kind']}: {d['detail']}")

    if c.post_snapshot_drift:
        heading("Access changed since the campaign opened")
        for username, delta in c.post_snapshot_drift.items():
            for key in delta.get("gained", []):
                print(f"  {username}  {GREEN('+ ' + key)}  {DIM('(gained after snapshot)')}")
            for key in delta.get("lost", []):
                print(f"  {username}  {RED('- ' + key)}  {DIM('(lost after snapshot)')}")

    if c.notes:
        heading("Notes")
        for note in c.notes:
            print(f"  {DIM('· ' + note)}")


def render_list(campaigns: list) -> None:
    heading("Access review campaigns")
    if not campaigns:
        print(f"  {DIM('none yet; make access-review-create NAME=...')}")
        return
    for c in campaigns:
        s = c.summary()
        undecided = len(c.undecided())
        print(f"  {c.id}")
        print(f"    {'status':<10} {c.status}    {'items':<8} {s['items']}    {'undecided':<10} {undecided}")


# -----------------------------------------------------------------------------
# Wiring
# -----------------------------------------------------------------------------


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


def build(catalogue):
    kc = Keycloak(env("KEYCLOAK_URL", "http://keycloak:8080"), env("KEYCLOAK_ADMIN", "admin"),
                  env("KEYCLOAK_ADMIN_PASSWORD"), env("KEYCLOAK_REALM", "lab"))
    vt = Vault(env("VAULT_ADDR", "http://vault:8200"), env("VAULT_TOKEN"))
    gt = Gitea(env("GITEA_URL", "http://gitea:3000"), env("GITEA_ADMIN_USER", "labadmin"),
               env("GITEA_ADMIN_PASSWORD"), catalogue.gitea_org, catalogue.gitea_transfer_target)
    return kc, vt, gt


def preflight(kc, vt, gt) -> None:
    for name, probe in (("Keycloak", kc.ping), ("Vault", vt.ping), ("Gitea", gt.ping)):
        try:
            probe()
        except (Unavailable, HttpError, PermissionError) as exc:
            raise SystemExit(f"{RED('✗')} {name} is not usable: {exc}\n"
                             f"  Check {CYAN('make health')} and try again.")


# -----------------------------------------------------------------------------
# Commands
# -----------------------------------------------------------------------------


def cmd_create(args, sim, kc, catalogue) -> int:
    c = campaign.create_campaign(sim, kc, catalogue, args.name, args.scope, args.reviewer)
    if args.format == "json":
        print(json.dumps(c.to_dict(), indent=2))
    else:
        render_campaign(c)
        print(f"\n  {DIM('Decide each item with:')} {CYAN(f'make access-review-decide CAMPAIGN={c.id} USER=<u> ENTITLEMENT=\"Service:resource\" DECISION=approve')}")
    return 0


def cmd_show(args) -> int:
    c = campaign.load(args.campaign)
    if args.format == "json":
        print(json.dumps(c.to_dict(), indent=2))
    else:
        render_campaign(c)
    return 0


def cmd_list(args) -> int:
    campaigns = campaign.list_campaigns()
    if args.format == "json":
        print(json.dumps([c.to_dict() for c in campaigns], indent=2))
    else:
        render_list(campaigns)
    return 0


def cmd_decide(args) -> int:
    c = campaign.load(args.campaign)
    entitlement = campaign.validate_entitlement(args.entitlement)
    item = campaign.decide(
        c, args.user, entitlement, campaign.validate_decision(args.decision),
        args.note, args.reviewer, args.force,
    )
    print(f"\n{BOLD('Decision recorded')}")
    print(f"  {args.user}  {item.service}:{item.resource}  ->  {DECISION_STYLE.get(item.decision, str)(item.decision)}")
    if item.decision == campaign.REVOKE:
        print(f"  {DIM('remediation status: ' + item.remediation_status)}")
        print(f"  {DIM('run: make access-review-remediate CAMPAIGN=' + c.id)}")
    return 0


def cmd_complete(args, sim) -> int:
    c = campaign.load(args.campaign)
    before_status = c.status
    c = campaign.complete(c, sim, args.force)
    if args.format == "json":
        print(json.dumps(c.to_dict(), indent=2))
        return 0
    if before_status == campaign.COMPLETED:
        print(f"\n{DIM('campaign already completed at ' + str(c.completed_at))}")
    else:
        print(f"\n{GREEN('Campaign completed')}: {c.id}")
    render_campaign(c)
    return 0


def cmd_cancel(args) -> int:
    c = campaign.load(args.campaign)
    c = campaign.cancel(c)
    print(f"\n{YELLOW('Campaign cancelled')}: {c.id}")
    return 0


def cmd_remediate(args, sim, kc, vt, gt) -> int:
    c = campaign.load(args.campaign)
    c = campaign.remediate(c, sim, kc, vt, gt)
    if args.format == "json":
        print(json.dumps(c.to_dict(), indent=2))
        return 0

    heading("Remediation")
    revoked = [it for it in c.items if it.decision == campaign.REVOKE]
    if not revoked:
        print(f"  {DIM('no revoke decisions in this campaign')}")
        return 0
    for it in revoked:
        style = GREEN if it.remediation_status == campaign.REM_VERIFIED else (
            YELLOW if it.remediation_status in (campaign.REM_PENDING, campaign.REM_MANUAL, campaign.REM_SKIPPED_PROTECTED)
            else RED
        )
        print(f"  {it.username}  {it.service}:{it.resource}  {style(it.remediation_status)}")
        if it.remediation_detail:
            print(f"      {DIM(it.remediation_detail)}")

    failed = sum(1 for it in revoked if it.remediation_status == campaign.REM_FAILED)
    return 1 if failed else 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=["text", "json"], default=None)

    parser = argparse.ArgumentParser(prog="access-review", description="Access review campaigns")
    parser.add_argument("--format", choices=["text", "json"], default="text", dest="global_format")
    sub = parser.add_subparsers(dest="command", required=True)

    p_create = sub.add_parser("create", parents=[common])
    p_create.add_argument("--name", required=True)
    p_create.add_argument("--scope", default="all")
    p_create.add_argument("--reviewer", default="")

    p_show = sub.add_parser("show", parents=[common])
    p_show.add_argument("--campaign", required=True)

    p_list = sub.add_parser("list", parents=[common])

    p_decide = sub.add_parser("decide", parents=[common])
    p_decide.add_argument("--campaign", required=True)
    p_decide.add_argument("--user", required=True)
    p_decide.add_argument("--entitlement", required=True)
    p_decide.add_argument("--decision", required=True)
    p_decide.add_argument("--note", default="")
    p_decide.add_argument("--reviewer", default="")
    p_decide.add_argument("--force", action="store_true")

    p_complete = sub.add_parser("complete", parents=[common])
    p_complete.add_argument("--campaign", required=True)
    p_complete.add_argument("--force", action="store_true")

    p_cancel = sub.add_parser("cancel", parents=[common])
    p_cancel.add_argument("--campaign", required=True)

    p_remediate = sub.add_parser("remediate", parents=[common])
    p_remediate.add_argument("--campaign", required=True)

    args = parser.parse_args(argv)
    args.format = getattr(args, "format", None) or args.global_format

    try:
        catalogue = load_catalogue()
        kc, vt, gt = build(catalogue)

        if args.command == "list":
            return cmd_list(args)

        # show, decide and cancel only touch the persisted snapshot. Keeping
        # them available during a downstream outage is part of the value of an
        # auditable review record. Commands that read or mutate live state still
        # require every integrated service up front.
        if args.command in ("create", "complete", "remediate"):
            preflight(kc, vt, gt)
        sim = Simulator(kc, vt, gt, catalogue)

        if args.command == "create":
            return cmd_create(args, sim, kc, catalogue)
        if args.command == "show":
            return cmd_show(args)
        if args.command == "decide":
            return cmd_decide(args)
        if args.command == "complete":
            return cmd_complete(args, sim)
        if args.command == "cancel":
            return cmd_cancel(args)
        return cmd_remediate(args, sim, kc, vt, gt)

    except ValidationError as exc:
        print(f"\n{RED('✗')} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
