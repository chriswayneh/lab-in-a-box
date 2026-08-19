"""
Gitea adapter — accounts, team membership and repository custody.

A stock v1 lab has one Gitea user (the administrator) and no organisation, so
the first lifecycle run creates the organisation and the per-profile teams named
in identity/profiles.json. Doing that here rather than in scripts/init-gitea.sh
keeps `docker compose up -d` unchanged for anyone not using these commands.

Authentication is HTTP basic as the lab administrator. Gitea's admin API is the
supported route for user lifecycle; nothing here touches its database.
"""

from __future__ import annotations

import base64

from labhttp import HttpError, request
from model import CREATED, FAILED, UNCHANGED, UPDATED, ServiceResult

# Gitea team permission levels, weakest first.
PERMISSIONS = ("read", "write", "admin")


class Gitea:
    def __init__(self, base_url: str, admin_user: str, admin_password: str, org: str, transfer_target: str):
        self.base = base_url.rstrip("/")
        self.org = org
        self.transfer_target = transfer_target
        self._admin_user = admin_user
        token = base64.b64encode(f"{admin_user}:{admin_password}".encode()).decode()
        self._auth = f"Basic {token}"

    def _call(self, method: str, path: str, **kwargs):
        return request(
            method,
            f"{self.base}/api/v1{path}",
            headers={"Authorization": self._auth},
            **kwargs,
        )

    def ping(self) -> None:
        me = self._call("GET", "/user")
        if not (me or {}).get("is_admin"):
            raise PermissionError(
                f"Gitea user {self._admin_user!r} is not an administrator; "
                "user lifecycle operations require admin rights"
            )

    # -- organisation and teams ---------------------------------------------

    def ensure_org(self, full_name: str, description: str, result: ServiceResult) -> None:
        try:
            self._call("GET", f"/orgs/{self.org}")
            return
        except HttpError as exc:
            if exc.status != 404:
                raise

        self._call(
            "POST",
            "/orgs",
            json_body={
                "username": self.org,
                "full_name": full_name,
                "description": description,
                "visibility": "private",
            },
        )
        result.record(CREATED, f"organisation {self.org!r} created")

    def find_team(self, name: str) -> dict | None:
        teams = self._call("GET", f"/orgs/{self.org}/teams") or []
        return next((t for t in teams if t["name"] == name), None)

    def ensure_team(self, name: str, permission: str, result: ServiceResult) -> dict:
        team = self.find_team(name)
        if team:
            return team

        if permission not in PERMISSIONS:
            raise ValueError(f"unsupported Gitea permission {permission!r}")

        team = self._call(
            "POST",
            f"/orgs/{self.org}/teams",
            json_body={
                "name": name,
                "description": f"Provisioned by the identity lifecycle engine ({permission}).",
                "permission": permission,
                "units": ["repo.code", "repo.issues", "repo.pulls", "repo.releases"],
                "includes_all_repositories": True,
                "can_create_org_repo": permission in ("write", "admin"),
            },
        )
        result.record(CREATED, f"team {name!r} created with {permission} permission")
        return team

    # -- users ---------------------------------------------------------------

    def find_user(self, username: str) -> dict | None:
        try:
            return self._call("GET", f"/users/{username}")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def team_memberships(self, username: str) -> list:
        """Teams in the managed organisation that this user belongs to."""
        teams = self._call("GET", f"/orgs/{self.org}/teams") or []
        member_of = []
        for team in teams:
            try:
                self._call("GET", f"/teams/{team['id']}/members/{username}")
                member_of.append(team["name"])
            except HttpError as exc:
                if exc.status not in (404, 403):
                    raise
        return sorted(member_of)

    def snapshot(self, username: str) -> dict:
        user = self.find_user(username)
        if not user:
            return {"exists": False, "active": False, "teams": [], "repositories": []}
        try:
            teams = self.team_memberships(username)
        except HttpError:
            teams = []
        return {
            "exists": True,
            "active": bool(user.get("active", True)),
            "teams": teams,
            "repositories": sorted(r["full_name"] for r in self.owned_repositories(username)),
        }

    # -- joiner --------------------------------------------------------------

    def reconcile_user(self, username: str, password: str, full_name: str, result: ServiceResult) -> dict:
        user = self.find_user(username)

        if user is None:
            user = self._call(
                "POST",
                "/admin/users",
                json_body={
                    "username": username,
                    "email": f"{username}@lab.example.com",
                    "full_name": full_name,
                    "password": password,
                    # The lab has no mail server, so a forced password change
                    # would strand the account behind a prompt nobody can clear.
                    "must_change_password": False,
                    "send_notify": False,
                },
            )
            result.record(CREATED, f"account {username!r} created")
            return user

        if not user.get("active", True):
            self._call("PATCH", f"/admin/users/{username}", json_body={"active": True, "login_name": username})
            result.record(UPDATED, f"account {username!r} reactivated")
            return self.find_user(username)

        result.record(UNCHANGED, f"account {username!r} already active")
        return user

    def ensure_team_membership(self, username: str, team_name: str, permission: str, result: ServiceResult) -> None:
        team = self.ensure_team(team_name, permission, result)
        if team_name in self.team_memberships(username):
            result.record(UNCHANGED, f"already a member of team {team_name!r}")
            return
        self._call("PUT", f"/teams/{team['id']}/members/{username}")
        result.record(UPDATED, f"added to team {team_name!r} ({permission})")

    # -- mover ---------------------------------------------------------------

    def remove_team_membership(self, username: str, team_name: str, result: ServiceResult) -> None:
        team = self.find_team(team_name)
        if not team or team_name not in self.team_memberships(username):
            result.record(UNCHANGED, f"not a member of team {team_name!r} (nothing to remove)")
            return
        self._call("DELETE", f"/teams/{team['id']}/members/{username}")
        result.record(UPDATED, f"removed from team {team_name!r}")

    def remove_all_team_memberships(self, username: str, result: ServiceResult) -> list:
        removed = []
        for team_name in self.team_memberships(username):
            team = self.find_team(team_name)
            if team:
                self._call("DELETE", f"/teams/{team['id']}/members/{username}")
                removed.append(team_name)
        if removed:
            result.record(UPDATED, f"removed from teams: {', '.join(removed)}")
        else:
            result.record(UNCHANGED, "held no team memberships")
        return removed

    # -- leaver --------------------------------------------------------------

    def owned_repositories(self, username: str) -> list:
        try:
            repos = self._call("GET", f"/users/{username}/repos") or []
        except HttpError as exc:
            if exc.status == 404:
                return []
            raise
        # Only repositories this user personally owns. Organisation-owned
        # repositories they merely had access to need no transfer.
        return [r for r in repos if (r.get("owner") or {}).get("login") == username]

    def transfer_repositories(self, username: str, result: ServiceResult) -> list:
        """
        Move personally-owned repositories to the custody account.

        Ownership transfer, never deletion. A leaver's repositories are usually
        the most valuable thing they leave behind, and an offboarding script that
        can delete them is a script one typo away from destroying work. There is
        no delete call anywhere in this module.
        """
        transferred = []
        for repo in self.owned_repositories(username):
            name = repo["name"]
            try:
                self._call(
                    "POST",
                    f"/repos/{username}/{name}/transfer",
                    json_body={"new_owner": self.transfer_target},
                )
                transferred.append(f"{username}/{name} -> {self.transfer_target}/{name}")
                result.record(UPDATED, f"repository {name!r} transferred to {self.transfer_target!r}")
            except HttpError as exc:
                # Loud and fatal to the service result: an untransferred repo
                # means the offboarding is incomplete, which the operator must
                # see rather than discover months later.
                #
                # The common cause is a name collision -- the custody account
                # already holds a repository of that name, often from a previous
                # offboarding. Say so, because "HTTP 422" alone sends the
                # operator to the wrong place.
                reason = f"HTTP {exc.status}"
                try:
                    self._call("GET", f"/repos/{self.transfer_target}/{name}")
                    reason = (
                        f"{self.transfer_target!r} already owns a repository named {name!r} — "
                        f"rename or archive it, then re-run"
                    )
                except HttpError:
                    pass
                result.record(
                    FAILED,
                    f"could not transfer repository {name!r} to {self.transfer_target!r}: {reason}",
                )
        if not transferred and not result.failed:
            result.record(UNCHANGED, "owns no personal repositories to transfer")
        return transferred

    def deactivate_user(self, username: str, result: ServiceResult) -> None:
        """
        Deactivate rather than delete.

        Gitea's DELETE removes the account and rewrites its commit attribution,
        which destroys history. Setting active=false blocks every login and API
        token while leaving authorship and audit trail intact.
        """
        user = self.find_user(username)
        if not user:
            result.record(UNCHANGED, "no Gitea account exists")
            return
        if not user.get("active", True):
            result.record(UNCHANGED, "account already deactivated")
            return

        self._call(
            "PATCH",
            f"/admin/users/{username}",
            # Gitea requires login_name on this endpoint even when unchanged.
            json_body={"active": False, "login_name": username, "prohibit_login": True},
        )
        result.record(UPDATED, "account deactivated and login prohibited (not deleted)")

    # -- verification --------------------------------------------------------

    def can_authenticate(self, username: str, password: str) -> bool:
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        try:
            data = request(
                "GET",
                f"{self.base}/api/v1/user",
                headers={"Authorization": f"Basic {token}"},
                retries=1,
            )
            return bool((data or {}).get("login"))
        except HttpError as exc:
            if exc.status in (401, 403):
                return False
            raise
