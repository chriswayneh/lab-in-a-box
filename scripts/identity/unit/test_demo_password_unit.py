"""The shared development credential must pass the real imported realm policy."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import jml

LAB_ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("empty", [False, True])
def test_default_demo_password_matches_realm_policy(monkeypatch, empty):
    monkeypatch.delenv("DEMO_USER_PASSWORD", raising=False)
    if empty:
        monkeypatch.setenv("DEMO_USER_PASSWORD", "")
    password = jml.initial_password()
    realm = json.loads((LAB_ROOT / "configs/keycloak/realm-export.json").read_text())
    counts = {
        "length": len(password),
        "upperCase": sum(c.isupper() for c in password),
        "lowerCase": sum(c.islower() for c in password),
        "digits": sum(c.isdigit() for c in password),
    }
    for name, minimum in re.findall(r"(length|upperCase|lowerCase|digits)\((\d+)\)",
                                    realm["passwordPolicy"]):
        assert counts[name] >= int(minimum), name
    for relative in ["compose/02-iam.yml", "scripts/init-vault.sh"]:
        text = (LAB_ROOT / relative).read_text()
        defaults = re.findall(r"\$\{DEMO_USER_PASSWORD:-([^}]+)\}", text)
        assert defaults and all(value == password for value in defaults), relative
    assert f"env_value DEMO_USER_PASSWORD {password}" in (
        LAB_ROOT / "scripts/show-credentials.sh"
    ).read_text()


def test_explicit_demo_password_is_preserved(monkeypatch):
    configured = "ConfiguredDemoPass9!"
    monkeypatch.setenv("DEMO_USER_PASSWORD", configured)
    assert jml.initial_password() == configured
