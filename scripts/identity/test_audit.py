#!/usr/bin/env python3
"""Live, failure-aware verification of the identity audit pipeline."""

from __future__ import annotations

import os
import sys
import time
import urllib.parse
from pathlib import Path

from keycloak import Keycloak
from labhttp import HttpError, request

KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
LOKI_URL = os.environ.get("LOKI_URL", "http://loki:3100").rstrip("/")
VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault:8200").rstrip("/")
REALM = os.environ.get("KEYCLOAK_REALM", "lab")
VAULT_TOKEN = os.environ.get("VAULT_TOKEN", "")

MARKER = f"auditprobe-{int(time.time())}"
PROBE_SECRET = f"sensitive-{MARKER}"
PASSED: list[str] = []
FAILED: list[str] = []


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


def expect_rejection(method: str, url: str, **kwargs) -> bool:
    try:
        request(method, url, retries=1, **kwargs)
    except HttpError as exc:
        return 400 <= exc.status < 500
    return False


def keycloak_events() -> tuple[Keycloak, str]:
    section("Generate Keycloak evidence")

    token_url = f"{KEYCLOAK_URL}/realms/{REALM}/protocol/openid-connect/token"
    rejected = 0
    for _ in range(5):
        if expect_rejection(
            "POST",
            token_url,
            form_body={
                "grant_type": "password",
                "client_id": "lab-cli",
                "username": MARKER,
                "password": "deliberately-wrong",
            },
        ):
            rejected += 1
    check("five invalid credential grants are rejected", rejected == 5, f"observed {rejected} rejected grants")

    keycloak = Keycloak(
        KEYCLOAK_URL,
        os.environ.get("KEYCLOAK_ADMIN", "admin"),
        os.environ.get("KEYCLOAK_ADMIN_PASSWORD", "admin-insecure-dev-only"),
        REALM,
    )
    keycloak._call(
        "POST",
        "/users",
        json_body={
            "username": MARKER,
            "enabled": True,
            "email": f"{MARKER}@lab.example.com",
            "firstName": "Audit",
            "lastName": "Probe",
        },
    )
    user = keycloak.find_user(MARKER)
    check("admin API creates a disposable user", bool(user))
    return keycloak, (user or {}).get("id", "")


def vault_events() -> None:
    section("Generate Vault evidence")
    headers = {"X-Vault-Token": VAULT_TOKEN}

    secret_path = f"secret/data/{MARKER}"
    request(
        "POST",
        f"{VAULT_ADDR}/v1/{secret_path}",
        headers=headers,
        json_body={"data": {"probe_secret": PROBE_SECRET}},
    )
    secret = request("GET", f"{VAULT_ADDR}/v1/{secret_path}", headers=headers) or {}
    check(
        "a disposable secret can be written and read",
        (((secret.get("data") or {}).get("data") or {}).get("probe_secret") == PROBE_SECRET),
    )

    policy_path = f"sys/policies/acl/{MARKER}"
    request(
        "PUT",
        f"{VAULT_ADDR}/v1/{policy_path}",
        headers=headers,
        json_body={"policy": f'path "secret/data/{MARKER}" {{ capabilities = ["read"] }}'},
    )
    check("a privileged policy change is accepted", True)


def loki_query(expression: str) -> list[dict]:
    now = time.time_ns()
    query = urllib.parse.urlencode(
        {
            "query": expression,
            "start": str(now - 10 * 60 * 1_000_000_000),
            "end": str(now),
            "limit": "500",
            "direction": "backward",
        }
    )
    payload = request("GET", f"{LOKI_URL}/loki/api/v1/query_range?{query}", retries=1) or {}
    return ((payload.get("data") or {}).get("result") or [])


def wait_for_stream(expression: str, predicate, timeout: int = 60) -> list[dict]:
    deadline = time.monotonic() + timeout
    latest: list[dict] = []
    while time.monotonic() < deadline:
        latest = loki_query(expression)
        if predicate(latest):
            return latest
        time.sleep(2)
    return latest


def lines(results: list[dict]) -> list[str]:
    return [line for stream in results for _, line in stream.get("values", [])]


def verify_loki(user_id: str) -> None:
    section("Verify parsed Loki streams")

    login = wait_for_stream(
        f'{{audit_source="keycloak", event_type="LOGIN_ERROR", outcome="failure"}} |= "{MARKER}"',
        lambda result: len(lines(result)) >= 5,
    )
    check("Keycloak login failures arrive with parsed labels", len(lines(login)) >= 5)

    admin = wait_for_stream(
        f'{{audit_source="keycloak", operation_type="CREATE", outcome="success"}} |= "users/{user_id}"',
        lambda result: bool(lines(result)),
    )
    check("Keycloak admin changes arrive with parsed labels", bool(lines(admin)))

    vault = wait_for_stream(
        f'{{audit_source="vault", event_type="request", outcome="success"}} |~ "{MARKER}"',
        lambda result: bool(lines(result)),
    )
    check("Vault requests arrive with parsed labels", bool(lines(vault)))


def verify_hashing() -> None:
    section("Verify Vault redaction")
    audit_path = Path("/vault-audit/audit.log")
    content = audit_path.read_text(encoding="utf-8", errors="replace") if audit_path.exists() else ""
    marker_records = [line for line in content.splitlines() if MARKER in line]
    check("Vault audit file contains the probe path", bool(marker_records))
    check(
        "Vault never writes the probe secret in plaintext",
        PROBE_SECRET not in content,
        "the raw audit file contained the generated secret value",
    )
    check(
        "Vault never writes the root token in plaintext",
        bool(VAULT_TOKEN) and VAULT_TOKEN not in content,
        "the raw audit file contained the active root token",
    )
    hmac_present = any("hmac-sha256:" in line for line in marker_records)
    check("sensitive audit values are represented by HMACs", hmac_present)


def verify_rules() -> None:
    section("Verify audit alert rules")
    expected = {"KeycloakBruteForcePattern", "VaultPrivilegedPolicyChange"}
    deadline = time.monotonic() + 75
    names: set[str] = set()
    firing: set[str] = set()

    while time.monotonic() < deadline:
        payload = request("GET", f"{LOKI_URL}/prometheus/api/v1/rules", retries=1) or {}
        groups = ((payload.get("data") or {}).get("groups") or [])
        names = {rule.get("name", "") for group in groups for rule in group.get("rules", [])}
        firing = {
            rule.get("name", "")
            for group in groups
            for rule in group.get("rules", [])
            if rule.get("state") == "firing"
        }
        if expected <= firing:
            break
        time.sleep(3)

    check("both identity audit rules are loaded", expected <= names, f"loaded: {sorted(names)}")
    check("brute-force rule reaches firing state", "KeycloakBruteForcePattern" in firing)
    check("policy-change rule reaches firing state", "VaultPrivilegedPolicyChange" in firing)


def cleanup(keycloak: Keycloak | None, user_id: str) -> None:
    headers = {"X-Vault-Token": VAULT_TOKEN}
    for path in (f"sys/policies/acl/{MARKER}", f"secret/metadata/{MARKER}"):
        try:
            request("DELETE", f"{VAULT_ADDR}/v1/{path}", headers=headers, retries=1)
        except Exception:
            pass
    if keycloak and user_id:
        try:
            keycloak._call("DELETE", f"/users/{user_id}")
        except Exception:
            pass


def main() -> int:
    keycloak = None
    user_id = ""
    try:
        keycloak, user_id = keycloak_events()
        vault_events()
        verify_loki(user_id)
        verify_hashing()
        verify_rules()
    except Exception as exc:
        check("test completed without an unexpected exception", False, str(exc))
    finally:
        cleanup(keycloak, user_id)

    print(f"\n\033[1mResult\033[0m\n  {len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        for failure in FAILED:
            print(f"  - {failure}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
