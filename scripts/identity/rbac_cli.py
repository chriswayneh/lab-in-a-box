#!/usr/bin/env python3
"""
RBAC simulator — command line interface (roadmap v2-2).

Rendering and the two comparison commands. The analysis itself lives in
rbac.py; this file only turns reports into something a person or a script can
read.

Read-only throughout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict

import rbac
from gitea import Gitea
from keycloak import Keycloak
from labhttp import HttpError, Unavailable
from model import ValidationError, load_catalogue, validate_username
from rbac import ALLOWED, NOT_AUTHORIZED, NOT_INTEGRATED, UNKNOWN, Simulator
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
    ALLOWED: GREEN,
    NOT_AUTHORIZED: DIM,
    NOT_INTEGRATED: YELLOW,
    UNKNOWN: RED,
}

DECISION_LABEL = {
    ALLOWED: "ALLOW",
    NOT_AUTHORIZED: "deny",
    NOT_INTEGRATED: "n/a",
    UNKNOWN: "?",
}


def heading(text: str) -> None:
    print(f"\n{BOLD(text)}\n{DIM('─' * len(text))}")


# -----------------------------------------------------------------------------
# Rendering
# -----------------------------------------------------------------------------


def render(report) -> None:
    state = "does not exist"
    if report.exists:
        state = "enabled" if report.enabled else f"DISABLED ({report.lifecycle_state or 'no lifecycle state'})"

    print(f"\n{BOLD('Effective access')} — {CYAN(report.user)}")
    print(f"  {'Identity':<16} {state}")
    print(f"  {'Groups':<16} {', '.join(report.groups) or DIM('(none)')}")
    print(f"  {'Realm roles':<16} {', '.join(report.realm_roles) or DIM('(none)')}")
    print(f"  {'Role profile':<16} {report.inferred_profile or DIM('(none inferred)')}")

    by_service: dict = {}
    for grant in report.grants:
        by_service.setdefault(grant.service, []).append(grant)

    for service in sorted(by_service):
        grants = by_service[service]
        # A service that is entirely unintegrated collapses to one line; there
        # is nothing per-resource to say about it.
        if all(g.decision == NOT_INTEGRATED for g in grants):
            heading(service)
            for g in grants:
                print(f"  {YELLOW('NOT IDENTITY-INTEGRATED')}  {g.resource}")
                print(f"      {DIM(g.source)}")
            continue

        heading(service)
        for g in grants:
            style = DECISION_STYLE.get(g.decision, str)
            label = style(DECISION_LABEL.get(g.decision, g.decision).ljust(5))
            perm = f" [{g.permission}]" if g.permission else ""
            print(f"  {label} {g.resource}{perm}")
            detail = g.source
            if g.inheritance != rbac.DIRECT:
                detail = f"{detail}  ({g.inheritance})"
            print(f"      {DIM('why: ' + detail)}")
            if g.evidence:
                print(f"      {DIM('     ' + g.evidence)}")

    if report.drift:
        heading("ACCESS DRIFT")
        for d in report.drift:
            print(f"  {RED(d.kind)}")
            print(f"      {d.detail}")
            print(f"      {DIM('expected: ' + d.expected)}")
            print(f"      {DIM('actual:   ' + d.actual)}")
        print(f"\n  {YELLOW('Drift is reported, never corrected.')} This tool is read-only —")
        print(f"  {DIM('use the jml-* commands to change access deliberately.')}")
    else:
        heading("ACCESS DRIFT")
        print(f"  {GREEN('none')} — actual access matches what the role profile expects")

    if report.notes:
        heading("Notes")
        for note in report.notes:
            print(f"  {DIM('· ' + note)}")

    allowed = len(report.allowed())
    print(f"\n  {DIM(f'{allowed} allowed grant(s), {len(report.drift)} drift finding(s)')}")


def to_json(report) -> str:
    payload = asdict(report)
    payload["summary"] = {
        "allowedGrants": len(report.allowed()),
        "driftFindings": len(report.drift),
        "readOnly": True,
    }
    return json.dumps(payload, indent=2)


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


def cmd_show(args, sim) -> int:
    report = sim.analyse(validate_username(args.user))
    if args.format == "json":
        print(to_json(report))
    else:
        render(report)
    return 0


def cmd_diff(args, sim) -> int:
    left = sim.analyse(validate_username(args.user))
    right = sim.analyse(validate_username(args.other))

    def allowed_map(report):
        return {g.key(): g for g in report.grants if g.decision == ALLOWED}

    lmap, rmap = allowed_map(left), allowed_map(right)
    only_left = sorted(set(lmap) - set(rmap))
    only_right = sorted(set(rmap) - set(lmap))
    shared = sorted(set(lmap) & set(rmap))

    if args.format == "json":
        print(json.dumps({
            "left": left.user, "right": right.user,
            "onlyLeft": [asdict(lmap[k]) for k in only_left],
            "onlyRight": [asdict(rmap[k]) for k in only_right],
            "shared": shared,
            "readOnly": True,
        }, indent=2))
        return 0

    print(f"\n{BOLD('Access difference')} — {CYAN(left.user)} vs {CYAN(right.user)}")
    print(f"  {left.user:<12} groups: {', '.join(left.groups) or '(none)'}")
    print(f"  {right.user:<12} groups: {', '.join(right.groups) or '(none)'}")

    heading(f"Only {left.user}")
    for key in only_left or []:
        print(f"  {GREEN('+ ' + key)}")
        print(f"      {DIM('why: ' + lmap[key].source)}")
    if not only_left:
        print(f"  {DIM('(nothing)')}")

    heading(f"Only {right.user}")
    for key in only_right or []:
        print(f"  {GREEN('+ ' + key)}")
        print(f"      {DIM('why: ' + rmap[key].source)}")
    if not only_right:
        print(f"  {DIM('(nothing)')}")

    heading("Shared")
    print(f"  {DIM(str(len(shared)) + ' grant(s) held by both')}")
    return 0


def cmd_who_can(args, sim, kc) -> int:
    """
    Reverse lookup: every identity whose effective access matches a permission.

    The permission is matched as `service:resource-prefix`, so
    `vault:secret/data/security/` matches any Vault path beginning that way.
    """
    wanted = args.permission.strip()
    if ":" not in wanted:
        raise ValidationError(
            f"PERMISSION must be 'service:resource', e.g. vault:secret/data/security/*\n"
            f"  got: {wanted!r}"
        )
    service, _, resource = wanted.partition(":")

    users = [u["username"] for u in (kc._call("GET", "/users?briefRepresentation=true&max=200") or [])]

    matches = []
    for username in sorted(users):
        report = sim.analyse(username)
        for grant in report.grants:
            if grant.decision != ALLOWED:
                continue
            if grant.service.lower() != service.lower():
                continue
            if resource and not rbac.resource_matches(grant.resource, resource):
                continue
            matches.append((username, grant))
            break

    if args.format == "json":
        print(json.dumps({
            "permission": wanted,
            "matches": [{"user": u, "grant": asdict(g)} for u, g in matches],
            "usersEvaluated": len(users),
            "readOnly": True,
        }, indent=2))
        return 0

    print(f"\n{BOLD('Who can reach')} {CYAN(wanted)}")
    print(f"  {DIM(f'evaluated {len(users)} identities in the realm')}")
    heading("Matches")
    if not matches:
        print(f"  {DIM('nobody')}")
    for username, grant in matches:
        print(f"  {GREEN(username)}  {grant.resource} [{grant.permission}]")
        print(f"      {DIM('why: ' + grant.source)}")
    return 0


def main(argv=None) -> int:
    # --format is declared on a shared parent AND on the top-level parser, so it
    # is accepted on either side of the subcommand. The Makefile appends it
    # after the subcommand; a person typing by hand naturally puts it before.
    # Only accepting one position turns a reasonable command into an error.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--format", choices=["text", "json"], default=None)

    parser = argparse.ArgumentParser(prog="rbac", description="RBAC simulator (read-only)")
    # A separate dest, because a subparser writing its own default of None into
    # a shared dest would silently discard a value given before the subcommand.
    parser.add_argument("--format", choices=["text", "json"], default="text", dest="global_format")
    sub = parser.add_subparsers(dest="command", required=True)

    p_show = sub.add_parser("show", parents=[common], help="effective access for one identity")
    p_show.add_argument("--user", required=True)

    p_diff = sub.add_parser("diff", parents=[common],
                            help="what one identity has that another does not")
    p_diff.add_argument("--user", required=True)
    p_diff.add_argument("--other", required=True)

    p_who = sub.add_parser("who-can", parents=[common],
                           help="every identity that can reach a resource")
    p_who.add_argument("--permission", required=True)

    args = parser.parse_args(argv)

    # The subcommand's value wins when given; otherwise the one before the
    # subcommand, which defaults to text.
    args.format = getattr(args, "format", None) or args.global_format

    try:
        catalogue = load_catalogue()
        kc, vt, gt = build(catalogue)
        preflight(kc, vt, gt)
        sim = Simulator(kc, vt, gt, catalogue)

        if args.command == "show":
            return cmd_show(args, sim)
        if args.command == "diff":
            return cmd_diff(args, sim)
        return cmd_who_can(args, sim, kc)

    except ValidationError as exc:
        print(f"\n{RED('✗')} {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
