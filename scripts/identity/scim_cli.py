#!/usr/bin/env python3
"""Small operator CLI for Keycloak's native SCIM endpoint."""

from __future__ import annotations

import argparse
import os

from labhttp import HttpError, Unavailable, request


def token() -> int:
    base = os.environ.get("KEYCLOAK_URL", "http://keycloak:8080").rstrip("/")
    realm = os.environ.get("KEYCLOAK_REALM", "lab")
    data = request(
        "POST",
        f"{base}/realms/{realm}/protocol/openid-connect/token",
        form_body={
            "grant_type": "client_credentials",
            "client_id": "lab-scim",
            "client_secret": os.environ.get(
                "KEYCLOAK_SCIM_CLIENT_SECRET", "scim-insecure-dev-only"
            ),
        },
        retries=1,
    ) or {}
    access_token = data.get("access_token")
    if not access_token:
        raise RuntimeError("Keycloak returned no SCIM access token")
    print(access_token)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Lab SCIM operator commands")
    parser.add_argument("command", choices=("token",))
    args = parser.parse_args()
    try:
        return {"token": token}[args.command]()
    except (HttpError, Unavailable, RuntimeError) as exc:
        print(f"SCIM command failed: {exc}", file=__import__("sys").stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
