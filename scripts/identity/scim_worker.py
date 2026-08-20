#!/usr/bin/env python3
"""Reconcile Keycloak's SCIM-managed identities into Gitea and Grafana.

Keycloak 26.7 exposes the standards-compliant SCIM API. It does not yet emit
outbound SCIM events, so this worker compares live Keycloak state with the last
successful snapshot. Each downstream system is reconciled independently;
failures receive bounded exponential retry and remain visible in state.json.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from gitea import Gitea
from grafana import Grafana
from keycloak import Keycloak
from labhttp import HttpError, Unavailable
from model import PROTECTED_USERNAMES, ServiceResult, load_catalogue
from rbac import grafana_role

STATE_VERSION = 2
DEFAULT_STATE_PATH = Path("/state/state.json")
MAX_HISTORY = 100
SCIM_MANAGED_GROUP = "/SCIM Managed"
STOP = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default) or default


class StateStore:
    def __init__(self, path: Path = DEFAULT_STATE_PATH):
        self.path = path
        self.data = self._load()

    def _empty(self) -> dict:
        return {
            "schema_version": STATE_VERSION,
            "last_run": None,
            "last_successful_run": None,
            "managed_users": {},
            "failures": {},
            "history": [],
            "summary": {"users_seen": 0, "changes": 0, "failures": 0},
        }

    def _load(self) -> dict:
        if not self.path.exists():
            return self._empty()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"SCIM state is unreadable: {exc}") from exc
        # Version 1 briefly treated every Keycloak account as SCIM-owned. Drop
        # that pre-release state rather than letting the narrowed controller
        # deprovision identities owned by the JML workflow.
        if data.get("schema_version") == 1:
            return self._empty()
        if data.get("schema_version") != STATE_VERSION:
            raise RuntimeError("SCIM state schema version is unsupported")
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(self.path)

    def due(self, key: str, now: float) -> bool:
        return now >= float((self.data["failures"].get(key) or {}).get("next_retry_epoch", 0))

    def failed(self, key: str, username: str, service: str, error: Exception, now: float) -> None:
        old = self.data["failures"].get(key) or {}
        attempts = int(old.get("attempts", 0)) + 1
        delay = min(15 * (2 ** (attempts - 1)), 300)
        self.data["failures"][key] = {
            "username": username,
            "service": service,
            "attempts": attempts,
            "last_error": str(error)[:600],
            "last_attempt": now_iso(),
            "next_retry_epoch": int(now + delay),
            "retry_in_seconds": delay,
        }

    def succeeded(self, key: str) -> None:
        self.data["failures"].pop(key, None)

    def event(self, username: str, result: ServiceResult) -> int:
        changed = sum(change.status in ("CREATED", "UPDATED") for change in result.changes)
        if changed:
            self.data["history"].append(
                {
                    "timestamp": now_iso(),
                    "username": username,
                    "service": result.service,
                    "changes": [
                        {"status": change.status, "detail": change.detail}
                        for change in result.changes
                        if change.status in ("CREATED", "UPDATED")
                    ],
                }
            )
            self.data["history"] = self.data["history"][-MAX_HISTORY:]
        return changed


class Reconciler:
    def __init__(self, keycloak, gitea, grafana, catalogue, store: StateStore, password: str):
        self.keycloak = keycloak
        self.gitea = gitea
        self.grafana = grafana
        self.catalogue = catalogue
        self.store = store
        self.password = password
        self.group_to_profile = {
            profile.keycloak_group: profile for profile in catalogue.profiles.values()
        }
        self.managed_teams = {profile.gitea_team for profile in catalogue.profiles.values()}

    def _profile(self, groups: list[str]):
        matches = [self.group_to_profile[group] for group in groups if group in self.group_to_profile]
        if len(matches) > 1:
            names = ", ".join(profile.name for profile in matches)
            raise ValueError(f"identity holds multiple managed profiles: {names}")
        return matches[0] if matches else None

    @staticmethod
    def _identity(user: dict) -> tuple[str, str, str]:
        username = user["username"]
        email = user.get("email") or f"{username}@lab.example.com"
        full_name = " ".join(
            part for part in (user.get("firstName"), user.get("lastName")) if part
        ) or f"{username.capitalize()} Lab"
        return username, email, full_name

    def _run_service(self, username: str, service: str, operation) -> int:
        key = f"{username}:{service.lower()}"
        current = time.time()
        if not self.store.due(key, current):
            return 0
        result = ServiceResult(service)
        try:
            operation(result)
            self.store.succeeded(key)
            return self.store.event(username, result)
        except (HttpError, Unavailable, PermissionError, ValueError, KeyError) as exc:
            self.store.failed(key, username, service, exc, current)
            return 0

    def _active_gitea(self, username: str, full_name: str, profile, result: ServiceResult) -> None:
        self.gitea.ensure_org(
            self.catalogue.gitea_org_full_name,
            self.catalogue.gitea_org_description,
            result,
        )
        self.gitea.reconcile_user(username, self.password, full_name, result)
        desired_team = profile.gitea_team if profile else None
        for team in self.gitea.team_memberships(username):
            if team in self.managed_teams and team != desired_team:
                self.gitea.remove_team_membership(username, team, result)
        if profile:
            self.gitea.ensure_team_membership(
                username, profile.gitea_team, profile.gitea_permission, result
            )

    def _inactive_gitea(self, username: str, result: ServiceResult) -> None:
        self.gitea.remove_all_team_memberships(username, result)
        self.gitea.deactivate_user(username, result)

    def run(self) -> dict:
        started = time.time()
        users = self.keycloak.all_users()
        current = {}
        source_users_seen = 0
        changes = 0

        availability = {}
        for name, adapter in (("Gitea", self.gitea), ("Grafana", self.grafana)):
            key = f"_service:{name.lower()}"
            try:
                adapter.ping()
                self.store.succeeded(key)
                availability[name] = True
            except (HttpError, Unavailable, PermissionError) as exc:
                self.store.failed(key, "*", name, exc, time.time())
                availability[name] = False

        for user in users:
            username, email, full_name = self._identity(user)
            groups = self.keycloak.user_groups(user["id"])
            if SCIM_MANAGED_GROUP not in groups:
                continue
            source_users_seen += 1
            roles = self.keycloak.effective_realm_roles(user["id"])
            try:
                profile = self._profile(groups)
            except ValueError as exc:
                for service in ("Gitea", "Grafana"):
                    self.store.failed(
                        f"{username}:{service.lower()}", username, service, exc, time.time()
                    )
                continue

            active = bool(user.get("enabled", True))
            current[username] = {
                "active": active,
                "profile": profile.name if profile else None,
                "last_seen": now_iso(),
                "deleted": False,
            }
            if active:
                if availability["Gitea"]:
                    changes += self._run_service(
                        username,
                        "Gitea",
                        lambda result, u=username, n=full_name, p=profile: self._active_gitea(u, n, p, result),
                    )
                role, _ = grafana_role(roles)
                if availability["Grafana"]:
                    changes += self._run_service(
                        username,
                        "Grafana",
                        lambda result, u=username, e=email, n=full_name, r=role: self.grafana.reconcile_user(
                            u, e, n, self.password, r, result
                        ),
                    )
            else:
                if availability["Gitea"]:
                    changes += self._run_service(
                        username,
                        "Gitea",
                        lambda result, u=username: self._inactive_gitea(u, result),
                    )
                if availability["Grafana"]:
                    changes += self._run_service(
                        username,
                        "Grafana",
                        lambda result, u=username: self.grafana.remove_access(u, result),
                    )

        # A SCIM DELETE removes the source object. Preserve downstream accounts
        # but remove their access. Seeded identities retain the repository's
        # stronger mutation safeguard even if someone deletes one accidentally.
        previous = self.store.data.get("managed_users", {})
        for username, old in previous.items():
            if username in current:
                continue
            current[username] = {**old, "active": False, "deleted": True, "last_seen": old.get("last_seen")}
            if username in PROTECTED_USERNAMES:
                continue
            if availability["Gitea"]:
                changes += self._run_service(
                    username,
                    "Gitea",
                    lambda result, u=username: self._inactive_gitea(u, result),
                )
            if availability["Grafana"]:
                changes += self._run_service(
                    username,
                    "Grafana",
                    lambda result, u=username: self.grafana.remove_access(u, result),
                )

        completed = now_iso()
        self.store.data["managed_users"] = current
        self.store.data["last_run"] = completed
        if not self.store.data["failures"]:
            self.store.data["last_successful_run"] = completed
        self.store.data["summary"] = {
            "users_seen": source_users_seen,
            "managed_users": len(current),
            "changes": changes,
            "failures": len(self.store.data["failures"]),
            "duration_ms": int((time.time() - started) * 1000),
        }
        self.store.save()
        return self.store.data["summary"]


def build_reconciler(store: StateStore) -> Reconciler:
    catalogue = load_catalogue()
    keycloak = Keycloak(
        env("KEYCLOAK_URL", "http://keycloak:8080"),
        "",
        "",
        env("KEYCLOAK_REALM", "lab"),
        client_id="lab-scim",
        client_secret=env("KEYCLOAK_SCIM_CLIENT_SECRET"),
    )
    gitea = Gitea(
        env("GITEA_URL", "http://gitea:3000"),
        env("GITEA_ADMIN_USER", "labadmin"),
        env("GITEA_ADMIN_PASSWORD"),
        catalogue.gitea_org,
        catalogue.gitea_transfer_target,
    )
    password_file = Path(env("GRAFANA_ADMIN_PASSWORD_FILE", "/run/secrets/grafana_admin_password"))
    grafana = Grafana(
        env("GRAFANA_URL", "http://grafana:3000"),
        env("GRAFANA_ADMIN_USER", "admin"),
        password_file.read_text(encoding="utf-8").strip(),
    )
    return Reconciler(
        keycloak,
        gitea,
        grafana,
        catalogue,
        store,
        env("DEMO_USER_PASSWORD"),
    )


def serve(args) -> int:
    global STOP

    def stop(_signum, _frame):
        global STOP
        STOP = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    store = StateStore(Path(args.state))
    reconciler = build_reconciler(store)
    while not STOP:
        try:
            summary = reconciler.run()
            print(json.dumps({"timestamp": now_iso(), **summary}, sort_keys=True), flush=True)
        except Exception as exc:  # the daemon must surface and retry total failures
            store.data["last_run"] = now_iso()
            store.data["daemon_error"] = str(exc)[:600]
            store.save()
            print(f"SCIM reconciliation failed: {exc}", file=sys.stderr, flush=True)
        deadline = time.time() + args.interval
        while not STOP and time.time() < deadline:
            time.sleep(min(1, deadline - time.time()))
    return 0


def status(args) -> int:
    store = StateStore(Path(args.state))
    print(json.dumps(store.data, indent=2, sort_keys=True))
    return 1 if store.data["failures"] else 0


def once(args) -> int:
    store = StateStore(Path(args.state))
    summary = build_reconciler(store).run()
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 1 if summary["failures"] else 0


def health(args) -> int:
    store = StateStore(Path(args.state))
    last_run = store.data.get("last_run")
    if not last_run:
        return 1
    age = datetime.now(timezone.utc) - datetime.fromisoformat(last_run)
    return 0 if age.total_seconds() <= args.max_age else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Keycloak SCIM downstream reconciler")
    parser.add_argument("--state", default=str(DEFAULT_STATE_PATH))
    sub = parser.add_subparsers(dest="command", required=True)
    server = sub.add_parser("serve")
    server.add_argument("--interval", type=int, default=int(env("SCIM_SYNC_INTERVAL", "15")))
    sub.add_parser("once")
    sub.add_parser("status")
    probe = sub.add_parser("health")
    probe.add_argument("--max-age", type=int, default=90)
    args = parser.parse_args()
    return {"serve": serve, "once": once, "status": status, "health": health}[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
