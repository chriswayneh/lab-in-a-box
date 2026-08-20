#!/usr/bin/env python3
"""Live SCIM CRUD and downstream propagation checks."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

from gitea import Gitea
from grafana import Grafana
from labhttp import HttpError, request
from model import load_catalogue
from scim_worker import StateStore

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
SUBJECT = "scimtest"
GROUP = "SCIM Test Group"
RENAMED_GROUP = "SCIM Test Group Updated"

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
REALM = os.environ.get("KEYCLOAK_REALM", "lab")
SCIM = f"{KEYCLOAK_URL}/realms/{REALM}/scim/v2"
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


def access_token() -> str:
    data = request(
        "POST",
        f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token",
        form_body={
            "grant_type": "client_credentials",
            "client_id": "lab-scim",
            "client_secret": os.environ.get(
                "KEYCLOAK_SCIM_CLIENT_SECRET", "scim-insecure-dev-only"
            ),
        },
        retries=1,
    ) or {}
    return data.get("access_token", "")


class Client:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/scim+json",
            "Content-Type": "application/scim+json",
        }

    def call(self, method: str, path: str, body=None):
        return request(
            method,
            f"{SCIM}{path}",
            headers=self.headers,
            json_body=body,
            retries=1,
        )

    def find(self, resource: str, attribute: str, value: str) -> dict | None:
        import urllib.parse

        query = urllib.parse.urlencode({"filter": f'{attribute} eq "{value}"'})
        result = self.call("GET", f"/{resource}?{query}") or {}
        resources = result.get("Resources") or []
        return resources[0] if resources else None


def cleanup(client: Client) -> None:
    for resource, attribute, value in (
        ("Users", "userName", SUBJECT),
        ("Groups", "displayName", GROUP),
        ("Groups", "displayName", RENAMED_GROUP),
    ):
        found = client.find(resource, attribute, value)
        if found:
            client.call("DELETE", f"/{resource}/{found['id']}")


def test_authentication(token: str) -> None:
    section("Authentication and discovery")
    try:
        request("GET", f"{SCIM}/ServiceProviderConfig", retries=1)
        unauthorized = False
    except HttpError as exc:
        unauthorized = exc.status == 401
    check("anonymous SCIM request is rejected", unauthorized)
    check("client-credentials grant returns a bearer token", bool(token))
    config = Client(token).call("GET", "/ServiceProviderConfig") or {}
    check("service advertises PATCH support", (config.get("patch") or {}).get("supported") is True)
    check("service advertises filter support", (config.get("filter") or {}).get("supported") is True)
    check("bulk is honestly advertised as unsupported", (config.get("bulk") or {}).get("supported") is False)


def test_user_crud(client: Client) -> dict:
    section("Users: create, read, filter, update and PATCH")
    user = client.call(
        "POST",
        "/Users",
        {
            "schemas": [USER_SCHEMA],
            "userName": SUBJECT,
            "name": {"givenName": "Scim", "familyName": "Test"},
            "emails": [{"value": "scimtest@lab.example.com", "type": "work", "primary": True}],
            "active": True,
        },
    )
    check("POST /Users creates a user", bool(user.get("id")))
    fetched = client.call("GET", f"/Users/{user['id']}")
    check("GET /Users/{id} reads it", fetched.get("userName") == SUBJECT)
    filtered = client.find("Users", "userName", SUBJECT)
    check("Users filter finds the exact user", (filtered or {}).get("id") == user["id"])

    replaced = client.call(
        "PUT",
        f"/Users/{user['id']}",
        {
            "schemas": [USER_SCHEMA],
            "id": user["id"],
            "userName": SUBJECT,
            "name": {"givenName": "SCIM", "familyName": "Integration"},
            "emails": [{"value": "scimtest@lab.example.com", "type": "work", "primary": True}],
            "active": True,
        },
    )
    check("PUT /Users updates the resource", replaced.get("name", {}).get("familyName") == "Integration")
    patched = client.call(
        "PATCH",
        f"/Users/{user['id']}",
        {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": True}]},
    )
    check("PATCH /Users is accepted", patched.get("active") is True)
    return patched


def test_group_crud(client: Client) -> None:
    section("Groups: create, read, update, PATCH and delete")
    group = client.call("POST", "/Groups", {"schemas": [GROUP_SCHEMA], "displayName": GROUP})
    check("POST /Groups creates a group", bool(group.get("id")))
    fetched = client.call("GET", f"/Groups/{group['id']}")
    check("GET /Groups/{id} reads it", fetched.get("displayName") == GROUP)
    replaced = client.call(
        "PUT",
        f"/Groups/{group['id']}",
        {"schemas": [GROUP_SCHEMA], "id": group["id"], "displayName": RENAMED_GROUP, "members": []},
    )
    check("PUT /Groups updates it", replaced.get("displayName") == RENAMED_GROUP)
    patched = client.call(
        "PATCH",
        f"/Groups/{group['id']}",
        {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "displayName", "value": GROUP}]},
    )
    check("PATCH /Groups updates it", patched.get("displayName") == GROUP)
    client.call("DELETE", f"/Groups/{group['id']}")
    try:
        client.call("GET", f"/Groups/{group['id']}")
        gone = False
    except HttpError as exc:
        gone = exc.status == 404
    check("DELETE /Groups removes it", gone)


def services():
    catalogue = load_catalogue()
    gitea = Gitea(
        os.environ.get("GITEA_URL", "http://gitea:3000"),
        os.environ.get("GITEA_ADMIN_USER", "labadmin"),
        os.environ.get("GITEA_ADMIN_PASSWORD", ""),
        catalogue.gitea_org,
        catalogue.gitea_transfer_target,
    )
    password = Path("/run/secrets/grafana_admin_password").read_text().strip()
    grafana = Grafana(
        os.environ.get("GRAFANA_URL", "http://grafana:3000"),
        os.environ.get("GRAFANA_ADMIN_USER", "admin"),
        password,
    )
    return gitea, grafana


def wait_for(predicate, timeout: int = 75) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except HttpError:
            pass
        time.sleep(3)
    return False


def test_propagation(client: Client, user: dict) -> None:
    section("Downstream propagation and deprovisioning")
    marker = client.find("Groups", "displayName", "SCIM Managed")
    group = client.find("Groups", "displayName", "Application Engineering")
    check("SCIM ownership group is visible", bool(marker))
    check("seeded developer group is visible through SCIM", bool(group))
    if not marker or not group:
        return
    for target in (marker, group):
        client.call(
            "PATCH",
            f"/Groups/{target['id']}",
            {
                "schemas": [PATCH_SCHEMA],
                "Operations": [{"op": "add", "path": "members", "value": [{"value": user["id"]}]}],
            },
        )
    gitea, grafana = services()
    check(
        "SCIM group membership propagates to Gitea",
        wait_for(lambda: "developers" in gitea.team_memberships(SUBJECT)),
    )
    check(
        "SCIM user and role propagate to Grafana",
        wait_for(lambda: grafana.snapshot(SUBJECT).get("role") == "Editor"),
    )

    client.call(
        "PATCH",
        f"/Users/{user['id']}",
        {"schemas": [PATCH_SCHEMA], "Operations": [{"op": "replace", "path": "active", "value": False}]},
    )
    check(
        "SCIM deactivation removes Gitea access",
        wait_for(lambda: not gitea.snapshot(SUBJECT).get("active")),
    )
    check(
        "SCIM deactivation removes Grafana organization access",
        wait_for(lambda: not grafana.snapshot(SUBJECT).get("has_access")),
    )
    check("Gitea account is retained for audit", gitea.snapshot(SUBJECT).get("exists"))
    check("Grafana account is retained for audit", grafana.snapshot(SUBJECT).get("exists"))


def test_retry_state() -> None:
    section("Retry state")
    with tempfile.TemporaryDirectory() as directory:
        store = StateStore(Path(directory) / "state.json")
        now = time.time()
        store.failed("subject:gitea", "subject", "Gitea", RuntimeError("simulated outage"), now)
        check("failed target is retained", "subject:gitea" in store.data["failures"])
        check("first retry is delayed", not store.due("subject:gitea", now))
        check("retry becomes due after its deadline", store.due("subject:gitea", now + 16))
        store.failed("subject:gitea", "subject", "Gitea", RuntimeError("still down"), now + 16)
        check("retry attempt count increments", store.data["failures"]["subject:gitea"]["attempts"] == 2)
        check("backoff increases", store.data["failures"]["subject:gitea"]["retry_in_seconds"] == 30)
        store.succeeded("subject:gitea")
        check("success clears the pending failure", "subject:gitea" not in store.data["failures"])


def main() -> int:
    print("\033[1mSCIM provisioning test suite\033[0m")
    token = access_token()
    test_authentication(token)
    client = Client(token)
    cleanup(client)
    try:
        user = test_user_crud(client)
        test_group_crud(client)
        test_propagation(client, user)
        client.call("DELETE", f"/Users/{user['id']}")
        try:
            client.call("GET", f"/Users/{user['id']}")
            gone = False
        except HttpError as exc:
            gone = exc.status == 404
        check("DELETE /Users removes the source identity", gone)
        test_retry_state()
    finally:
        cleanup(client)

    section("Summary")
    total = len(PASSED) + len(FAILED)
    print(f"  {len(PASSED)}/{total} checks passed")
    if FAILED:
        for failure in FAILED:
            print(f"    - {failure}")
        return 1
    print("  \033[32mall SCIM provisioning checks passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(main())
