"""
Keycloak adapter — Admin API only.

Nothing here touches Keycloak's PostgreSQL database. Every operation goes
through the supported Admin REST API, so the realm cache, the event log and the
admin console all stay consistent with what the engine did.

The interesting method is `revoke_sessions`. See its docstring.
"""

from __future__ import annotations

import urllib.parse

from labhttp import HttpError, Unavailable, request
from model import CREATED, UNCHANGED, UPDATED, ServiceResult


class Keycloak:
    def __init__(self, base_url: str, admin_user: str, admin_password: str, realm: str):
        self.base = base_url.rstrip("/")
        self.realm = realm
        self._admin_user = admin_user
        self._admin_password = admin_password
        self._token: str | None = None

    # -- auth ----------------------------------------------------------------

    def _authenticate(self) -> str:
        """
        Obtain an admin token from the master realm.

        Called lazily and cached for the life of one command. The token is never
        logged, never written to an artifact, and never leaves this object.
        """
        if self._token:
            return self._token

        data = request(
            "POST",
            f"{self.base}/realms/master/protocol/openid-connect/token",
            form_body={
                "client_id": "admin-cli",
                "username": self._admin_user,
                "password": self._admin_password,
                "grant_type": "password",
            },
        )
        token = (data or {}).get("access_token")
        if not token:
            raise Unavailable("Keycloak returned no admin access token")
        self._token = token
        return token

    def _call(self, method: str, path: str, **kwargs):
        return request(
            method,
            f"{self.base}/admin/realms/{self.realm}{path}",
            headers={"Authorization": f"Bearer {self._authenticate()}"},
            **kwargs,
        )

    def ping(self) -> None:
        """Fail early and clearly if Keycloak is unreachable or credentials are wrong."""
        self._authenticate()

    # -- reads ---------------------------------------------------------------

    def find_user(self, username: str) -> dict | None:
        # `exact=true` matters: without it Keycloak substring-matches, and
        # searching for "erin" would also return "erina".
        q = urllib.parse.urlencode({"username": username, "exact": "true"})
        found = self._call("GET", f"/users?{q}") or []
        return found[0] if found else None

    def require_user(self, username: str) -> dict:
        user = self.find_user(username)
        if not user:
            raise LookupError(f"Keycloak has no user named {username!r}")
        return user

    def user_groups(self, user_id: str) -> list:
        return [g["path"] for g in (self._call("GET", f"/users/{user_id}/groups") or [])]

    def effective_realm_roles(self, user_id: str) -> list:
        """
        Roles the user actually holds, composite and group-inherited included.

        Read back from Keycloak rather than inferred from the profile, so the
        access diff reflects reality even if someone edited the realm by hand.
        """
        roles = self._call("GET", f"/users/{user_id}/role-mappings/realm/composite") or []
        return sorted(
            r["name"]
            for r in roles
            # Keycloak's built-ins are noise in an access review.
            if r["name"] not in ("default-roles-" + self.realm, "offline_access", "uma_authorization")
        )

    def group_by_path(self, path: str) -> dict:
        groups = self._call("GET", "/groups?briefRepresentation=false") or []

        def walk(nodes):
            for node in nodes:
                if node.get("path") == path:
                    return node
                hit = walk(node.get("subGroups", []) or [])
                if hit:
                    return hit
            return None

        group = walk(groups)
        if not group:
            available = ", ".join(sorted(g.get("path", "?") for g in groups))
            raise LookupError(f"realm has no group at path {path!r}. Available: {available}")
        return group

    def active_session_count(self, user_id: str) -> int:
        sessions = self._call("GET", f"/users/{user_id}/sessions") or []
        return len(sessions)

    def snapshot(self, username: str) -> dict:
        """Everything the access diff needs, in one place."""
        user = self.find_user(username)
        if not user:
            return {"exists": False, "enabled": False, "groups": [], "roles": [], "sessions": 0}
        return {
            "exists": True,
            "enabled": bool(user.get("enabled")),
            "groups": sorted(self.user_groups(user["id"])),
            "roles": self.effective_realm_roles(user["id"]),
            "sessions": self.active_session_count(user["id"]),
        }

    # -- user profile --------------------------------------------------------

    def ensure_profile_attributes(self, names: list, result: ServiceResult) -> None:
        """
        Declare the attributes the lifecycle engine writes.

        Keycloak's declarative User Profile is enabled by default from v24, and
        an attribute that is not declared is silently DROPPED on write -- no
        error, the PUT succeeds and the value simply never appears. The realm's
        seeded users carry `title` and `employeeId` only because realm import
        bypasses this validation; nothing could write them through the API.

        Declaring each attribute explicitly is preferred over flipping
        `unmanagedAttributePolicy` to ENABLED: declared attributes are validated,
        appear in the admin console, and say what the model intends. The blanket
        switch would let any typo become a permanent attribute.

        Idempotent -- only missing declarations are added.
        """
        profile = self._call("GET", "/users/profile") or {}
        declared = {a["name"] for a in profile.get("attributes", [])}
        missing = [n for n in names if n not in declared]
        if not missing:
            return

        for name in missing:
            profile.setdefault("attributes", []).append(
                {
                    "name": name,
                    "displayName": name,
                    "multivalued": False,
                    # Administrators manage these; the user cannot edit their own
                    # employeeId or lifecycleState, which is the whole point.
                    "permissions": {"view": ["admin", "user"], "edit": ["admin"]},
                    "validations": {},
                    "annotations": {},
                }
            )

        self._call("PUT", "/users/profile", json_body=profile)
        result.record(UPDATED, f"declared user-profile attribute(s): {', '.join(missing)}")

    # -- joiner --------------------------------------------------------------

    def reconcile_user(self, username: str, profile, employee_id: str, result: ServiceResult) -> dict:
        """Create the user if absent, then converge attributes. Never resets a password."""
        desired_attrs = {k: [str(v)] for k, v in profile.keycloak_attributes.items()}
        desired_attrs["employeeId"] = [employee_id]
        desired_attrs["lifecycleState"] = ["active"]

        # Without this the writes below succeed and silently discard every
        # custom attribute. See ensure_profile_attributes.
        self.ensure_profile_attributes(sorted(desired_attrs), result)

        user = self.find_user(username)

        if user is None:
            self._call(
                "POST",
                "/users",
                json_body={
                    "username": username,
                    "enabled": True,
                    "emailVerified": True,
                    "email": f"{username}@lab.example.com",
                    "firstName": username.capitalize(),
                    # Keycloak validates person names and rejects punctuation
                    # such as parentheses with error-person-name-invalid-character,
                    # so this stays plain alphabetic.
                    "lastName": "Lab",
                    "attributes": desired_attrs,
                },
            )
            user = self.require_user(username)
            result.record(CREATED, f"user {username!r} created")
            return user

        # Converge: re-enable a returning identity, fix drifted attributes.
        patch, reasons = {}, []
        if not user.get("enabled"):
            patch["enabled"] = True
            reasons.append("re-enabled")

        current = user.get("attributes") or {}
        merged = dict(current)
        for key, value in desired_attrs.items():
            # employeeId is assigned once at creation; regenerating it on every
            # run would make the identity untraceable across its own history.
            if key == "employeeId" and current.get("employeeId"):
                continue
            if current.get(key) != value:
                merged[key] = value
                reasons.append(f"{key}={value[0]}")
        if merged != current:
            patch["attributes"] = merged

        if patch:
            self._call("PUT", f"/users/{user['id']}", json_body={**user, **patch})
            result.record(UPDATED, f"user {username!r}: {', '.join(reasons)}")
            user = self.require_user(username)
        else:
            result.record(UNCHANGED, f"user {username!r} already correct")

        return user

    def set_password_if_absent(self, user_id: str, username: str, password: str, result: ServiceResult) -> bool:
        """
        Set the initial credential only when the account has none.

        Idempotency requirement: a second `jml-join` must not silently reset a
        password the person has already changed. Returns True if it set one.
        """
        creds = self._call("GET", f"/users/{user_id}/credentials") or []
        if any(c.get("type") == "password" for c in creds):
            result.record(UNCHANGED, "password already set (not reset)")
            return False

        self._call(
            "PUT",
            f"/users/{user_id}/reset-password",
            json_body={"type": "password", "value": password, "temporary": False},
        )
        result.record(CREATED, "initial password set")
        return True

    def ensure_group(self, user_id: str, username: str, group_path: str, result: ServiceResult) -> None:
        if group_path in self.user_groups(user_id):
            result.record(UNCHANGED, f"already in group {group_path}")
            return
        group = self.group_by_path(group_path)
        self._call("PUT", f"/users/{user_id}/groups/{group['id']}")
        result.record(UPDATED, f"added to group {group_path}")

    # -- mover ---------------------------------------------------------------

    def remove_group(self, user_id: str, group_path: str, result: ServiceResult) -> None:
        """
        Remove obsolete membership. Runs BEFORE the new one is added, so a
        mover cannot momentarily hold both entitlements.
        """
        if group_path not in self.user_groups(user_id):
            result.record(UNCHANGED, f"not in group {group_path} (nothing to remove)")
            return
        group = self.group_by_path(group_path)
        self._call("DELETE", f"/users/{user_id}/groups/{group['id']}")
        result.record(UPDATED, f"removed from group {group_path}")

    def strip_all_groups(self, user_id: str, result: ServiceResult) -> list:
        removed = []
        for path in self.user_groups(user_id):
            group = self.group_by_path(path)
            self._call("DELETE", f"/users/{user_id}/groups/{group['id']}")
            removed.append(path)
        if removed:
            result.record(UPDATED, f"removed from all groups: {', '.join(sorted(removed))}")
        else:
            result.record(UNCHANGED, "held no group memberships")
        return removed

    # -- leaver --------------------------------------------------------------

    def disable_user(self, user: dict, result: ServiceResult) -> None:
        if not user.get("enabled"):
            result.record(UNCHANGED, "account already disabled")
            return
        # An identity created outside this engine may predate the declaration,
        # and an offboarding that silently loses its own audit marker is worse
        # than a slow one.
        self.ensure_profile_attributes(["lifecycleState"], result)
        attrs = dict(user.get("attributes") or {})
        attrs["lifecycleState"] = ["offboarded"]
        self._call("PUT", f"/users/{user['id']}", json_body={**user, "enabled": False, "attributes": attrs})
        result.record(UPDATED, "account disabled (retained for audit, not deleted)")

    def revoke_sessions(self, user_id: str, result: ServiceResult) -> int:
        """
        Revoke every session and every refresh/offline token for this user.

        `POST /users/{id}/logout` is the operation that actually matters in an
        offboarding, and it does more than its name suggests:

          - kills all online sessions
          - invalidates the refresh tokens bound to them, so the holder of a
            refresh token cannot mint a fresh access token
          - bumps the user's `notBefore`, which is what makes already-issued
            tokens fail server-side session validation

        What it does NOT do is reach out and un-issue a self-contained JWT that
        is already in someone's hands. A resource server that only checks the
        signature and `exp` locally will keep accepting that token until it
        expires -- at most `accessTokenLifespan` (300s in this realm). A resource
        server that calls the userinfo or introspection endpoint sees the
        revocation immediately.

        Offline sessions survive a plain logout in some Keycloak versions, so
        they are enumerated and revoked per-client as well.
        """
        before = self.active_session_count(user_id)

        # Online sessions + refresh tokens + notBefore bump.
        self._call("POST", f"/users/{user_id}/logout")

        # Offline sessions are held per client and are not always cleared above.
        offline_revoked = 0
        for client in self._call("GET", "/clients?briefRepresentation=true") or []:
            try:
                sessions = self._call(
                    "GET", f"/users/{user_id}/offline-sessions/{client['id']}"
                ) or []
            except HttpError:
                continue
            for session in sessions:
                try:
                    self._call(
                        "DELETE",
                        f"/sessions/{session['id']}?isOffline=true",
                    )
                    offline_revoked += 1
                except HttpError:
                    # Report rather than swallow: an offline token that survives
                    # offboarding is exactly the failure this flow exists to catch.
                    result.record(
                        "FAILED",
                        f"could not revoke offline session {session['id']} for client {client.get('clientId')}",
                    )

        detail = f"revoked {before} active session(s)"
        if offline_revoked:
            detail += f" and {offline_revoked} offline session(s)"
        result.record(UPDATED if (before or offline_revoked) else UNCHANGED, detail)
        return before + offline_revoked

    # -- verification --------------------------------------------------------

    def can_authenticate(self, username: str, password: str, client_id: str = "lab-cli") -> bool:
        """
        Try a real password grant. Used to prove that a leaver cannot log in.

        Returns False on the 401 Keycloak gives a disabled account, and lets any
        other error propagate -- a network failure must not be mistaken for
        successful revocation.
        """
        try:
            data = request(
                "POST",
                f"{self.base}/realms/{self.realm}/protocol/openid-connect/token",
                form_body={
                    "client_id": client_id,
                    "username": username,
                    "password": password,
                    "grant_type": "password",
                    "scope": "openid",
                },
                retries=1,
            )
            return bool((data or {}).get("access_token"))
        except HttpError as exc:
            if exc.status in (400, 401):
                return False
            raise
