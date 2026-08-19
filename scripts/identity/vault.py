"""
Vault adapter — userpass identities and their policies.

Integrates with the model scripts/init-vault.sh already establishes: humans
authenticate with userpass, and their entitlements come from a named ACL policy
that matches the realm role the Keycloak group grants.

The root token is read from the environment, used as a header, and never
logged or written to an artifact.
"""

from __future__ import annotations

from pathlib import Path

from labhttp import HttpError, Unavailable, request
from model import CREATED, FAILED, UNCHANGED, UPDATED, ServiceResult

POLICY_DIR = Path("/vault-policies")


class Vault:
    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/")
        self._token = token

    def _call(self, method: str, path: str, **kwargs):
        return request(
            method,
            f"{self.base}/v1{path}",
            headers={"X-Vault-Token": self._token},
            **kwargs,
        )

    def ping(self) -> None:
        health = request("GET", f"{self.base}/v1/sys/health", retries=3)
        if not (health or {}).get("initialized", True):
            raise Unavailable("Vault is not initialised")

    # -- policies ------------------------------------------------------------

    def ensure_policy(self, name: str, result: ServiceResult) -> None:
        """
        Make sure the profile's policy exists.

        init-vault.sh loads configs/vault/policies/*.hcl at provisioning time,
        but the lab's Vault runs in dev mode and holds its state in memory, so a
        policy added to the repo after the last `make up` will not be present.
        Rather than fail with "policy not found" and make the operator restart
        the stack, reconcile it from the same file init-vault.sh would have used.
        """
        try:
            self._call("GET", f"/sys/policies/acl/{name}")
            return
        except HttpError as exc:
            if exc.status != 404:
                raise

        source = POLICY_DIR / f"{name}.hcl"
        if not source.exists():
            result.record(FAILED, f"Vault policy {name!r} is missing and {source} does not exist")
            raise LookupError(f"no Vault policy {name!r} and no {source} to create it from")

        self._call(
            "PUT",
            f"/sys/policies/acl/{name}",
            json_body={"policy": source.read_text(encoding="utf-8")},
        )
        result.record(CREATED, f"Vault policy {name!r} loaded from {source.name}")

    # -- reads ---------------------------------------------------------------

    def user_policies(self, username: str) -> list | None:
        """Policies bound to a userpass identity, or None when it does not exist."""
        try:
            data = self._call("GET", f"/auth/userpass/users/{username}")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise
        return sorted((data or {}).get("data", {}).get("token_policies", []) or [])

    def snapshot(self, username: str) -> dict:
        policies = self.user_policies(username)
        return {"exists": policies is not None, "policies": policies or []}

    # -- joiner / mover ------------------------------------------------------

    def reconcile_user(self, username: str, policy: str, password: str, result: ServiceResult) -> None:
        """
        Converge the userpass identity onto exactly one policy.

        The policy list is REPLACED rather than appended to. That is the whole
        point of the mover flow: a person who moves from contractor to developer
        must not end up holding both.
        """
        self.ensure_policy(policy, result)
        current = self.user_policies(username)

        if current is None:
            self._call(
                "POST",
                f"/auth/userpass/users/{username}",
                json_body={"password": password, "token_policies": policy, "token_ttl": "1h"},
            )
            result.record(CREATED, f"userpass identity created with policy {policy!r}")
            return

        if current == [policy]:
            result.record(UNCHANGED, f"userpass identity already holds exactly {policy!r}")
            return

        # Update policies without touching the password: POST to the user path
        # with only token_policies leaves the credential intact.
        self._call(
            "POST",
            f"/auth/userpass/users/{username}/policies",
            json_body={"token_policies": policy},
        )
        result.record(
            UPDATED,
            f"policies replaced: {', '.join(current) or '(none)'} -> {policy}",
        )

    # -- leaver --------------------------------------------------------------

    def revoke_identity(self, username: str, result: ServiceResult) -> None:
        """
        Remove the login, then kill anything it already issued.

        Order matters. Deleting the userpass entry stops new logins but does
        nothing to a token already in circulation, and Vault tokens outlive the
        auth entry that produced them. `revoke-prefix` on the accessor path is
        what actually severs live access.
        """
        existed = self.user_policies(username) is not None

        if existed:
            self._call("DELETE", f"/auth/userpass/users/{username}")
            result.record(UPDATED, "userpass identity deleted (no further logins)")
        else:
            result.record(UNCHANGED, "no userpass identity to remove")

        # Revoke outstanding leases/tokens issued through userpass for this user.
        #
        # Always attempted, even when the identity was already gone: tokens
        # outlive the auth entry that minted them, so skipping this on a re-run
        # would be the one case where a live token survives offboarding.
        #
        # Vault answers 204 whether or not it revoked anything, so the status
        # reported here is based on whether an identity existed to have issued
        # leases in the first place. Claiming UPDATED on a re-run would make an
        # idempotent command look like it kept changing things.
        try:
            self._call("PUT", f"/sys/leases/revoke-prefix/auth/userpass/login/{username}")
            if existed:
                result.record(UPDATED, "revoked outstanding Vault tokens/leases for this identity")
            else:
                result.record(UNCHANGED, "no identity remained; lease revocation re-attempted as a precaution")
        except HttpError as exc:
            # A user who never logged in has no lease prefix; that is success,
            # not failure.
            if exc.status in (404, 400):
                result.record(UNCHANGED, "no outstanding Vault leases to revoke")
            else:
                result.record(FAILED, f"lease revocation failed: HTTP {exc.status}")
                raise

    # -- verification --------------------------------------------------------

    def can_authenticate(self, username: str, password: str) -> bool:
        try:
            data = request(
                "POST",
                f"{self.base}/v1/auth/userpass/login/{username}",
                json_body={"password": password},
                retries=1,
            )
            return bool((data or {}).get("auth", {}).get("client_token"))
        except HttpError as exc:
            if exc.status in (400, 401, 403, 404):
                return False
            raise
