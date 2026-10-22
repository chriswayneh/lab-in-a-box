"""The demo-user password has no published default and must come from .env."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import jml
from model import ValidationError

LAB_ROOT = Path(__file__).resolve().parents[3]
OLD_DEFAULT = "LabDemo" + "Pass1!"  # split so a repo-wide grep finds no literal


@pytest.mark.parametrize("empty", [False, True])
def test_missing_demo_password_is_refused(monkeypatch, empty):
    monkeypatch.delenv("DEMO_USER_PASSWORD", raising=False)
    if empty:
        monkeypatch.setenv("DEMO_USER_PASSWORD", "")
    with pytest.raises(ValidationError):
        jml.initial_password()


def test_explicit_demo_password_is_preserved(monkeypatch):
    configured = "ConfiguredDemoPass9!"
    monkeypatch.setenv("DEMO_USER_PASSWORD", configured)
    assert jml.initial_password() == configured


def test_no_published_demo_password_anywhere():
    for relative in [
        "compose/02-iam.yml",
        "scripts/init-vault.sh",
        "scripts/show-credentials.sh",
        "scripts/_capture-screens.mjs",
        "scripts/identity/jml.py",
        "scripts/identity/test_lifecycle.py",
    ]:
        text = (LAB_ROOT / relative).read_text()
        assert OLD_DEFAULT not in text, relative
        assert not re.findall(r"\$\{DEMO_USER_PASSWORD:-[^}]+\}", text), relative
