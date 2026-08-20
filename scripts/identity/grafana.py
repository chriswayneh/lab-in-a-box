"""Grafana user and organization-role reconciliation.

Grafana's server-admin user API requires basic authentication. The lab uses it
only from the private edge network and never places credentials in a URL.
Deprovisioning removes the user from the organization instead of deleting the
global account, preserving dashboard attribution and audit history.
"""

from __future__ import annotations

import base64
import urllib.parse

from labhttp import HttpError, request
from model import CREATED, UNCHANGED, UPDATED, ServiceResult

ROLES = frozenset({"Admin", "Editor", "Viewer"})


class Grafana:
    def __init__(self, base_url: str, admin_user: str, admin_password: str, org_id: int = 1):
        self.base = base_url.rstrip("/")
        self.org_id = int(org_id)
        token = base64.b64encode(f"{admin_user}:{admin_password}".encode()).decode()
        self._auth = f"Basic {token}"

    def _call(self, method: str, path: str, **kwargs):
        return request(
            method,
            f"{self.base}{path}",
            headers={"Authorization": self._auth},
            **kwargs,
        )

    def ping(self) -> None:
        user = self._call("GET", "/api/user") or {}
        if not user.get("isGrafanaAdmin"):
            raise PermissionError("Grafana provisioning requires a server administrator")

    def find_user(self, username: str) -> dict | None:
        query = urllib.parse.urlencode({"loginOrEmail": username})
        try:
            return self._call("GET", f"/api/users/lookup?{query}")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def _org_membership(self, user_id: int) -> dict | None:
        memberships = self._call("GET", f"/api/users/{user_id}/orgs") or []
        return next((item for item in memberships if int(item.get("orgId", 0)) == self.org_id), None)

    def reconcile_user(
        self,
        username: str,
        email: str,
        full_name: str,
        password: str,
        role: str,
        result: ServiceResult,
    ) -> dict:
        if role not in ROLES:
            raise ValueError(f"unsupported Grafana organization role {role!r}")

        user = self.find_user(username)
        if user is None:
            created = self._call(
                "POST",
                "/api/admin/users",
                json_body={
                    "name": full_name,
                    "email": email,
                    "login": username,
                    "password": password,
                    "OrgId": self.org_id,
                },
            ) or {}
            user = self._call("GET", f"/api/users/{created['id']}")
            result.record(CREATED, f"account {username!r} created")
        else:
            updates = {}
            if user.get("email") != email:
                updates["email"] = email
            if user.get("name") != full_name:
                updates["name"] = full_name
            if updates:
                self._call(
                    "PUT",
                    f"/api/users/{user['id']}",
                    json_body={
                        "login": username,
                        "email": updates.get("email", user.get("email", email)),
                        "name": updates.get("name", user.get("name", full_name)),
                    },
                )
                result.record(UPDATED, f"account {username!r} attributes updated")

        membership = self._org_membership(user["id"])
        if membership is None:
            self._call(
                "POST",
                f"/api/orgs/{self.org_id}/users",
                json_body={"loginOrEmail": username, "role": role},
            )
            result.record(UPDATED, f"added to Grafana organization as {role}")
        elif membership.get("role") != role:
            self._call(
                "PATCH",
                f"/api/orgs/{self.org_id}/users/{user['id']}",
                json_body={"role": role},
            )
            result.record(UPDATED, f"organization role changed to {role}")
        elif not result.changes:
            result.record(UNCHANGED, f"account already active with {role} role")
        return self.find_user(username) or user

    def remove_access(self, username: str, result: ServiceResult) -> None:
        user = self.find_user(username)
        if user is None:
            result.record(UNCHANGED, "no Grafana account exists")
            return
        if self._org_membership(user["id"]) is None:
            result.record(UNCHANGED, "account already has no organization access")
            return
        self._call("DELETE", f"/api/orgs/{self.org_id}/users/{user['id']}")
        result.record(UPDATED, "organization access removed; global account retained for audit")

    def snapshot(self, username: str) -> dict:
        user = self.find_user(username)
        if user is None:
            return {"exists": False, "has_access": False, "role": None}
        membership = self._org_membership(user["id"])
        return {
            "exists": True,
            "has_access": membership is not None,
            "role": membership.get("role") if membership else None,
        }
