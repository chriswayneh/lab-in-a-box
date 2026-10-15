"""Regression tests for identity-engine safety fixes (no lab needed)."""

from __future__ import annotations

import re

from keycloak import Keycloak
from model import ServiceResult, UPDATED


def test_failed_verification_marks_result_failed():
    result = ServiceResult("Gitea")
    result.record(UPDATED, "changed")
    result.verify(False, "post-check did not hold")
    assert result.failed


def test_passing_verification_does_not_fail_result():
    result = ServiceResult("Gitea")
    result.record(UPDATED, "changed")
    result.verify(True, "post-check held")
    assert not result.failed


def test_all_users_pages_past_first_page(monkeypatch):
    realm = [{"username": f"user{i:04d}"} for i in range(450)]
    calls = []

    def fake_call(self, method, path, **kwargs):
        first_match = re.search(r"first=(\d+)", path)
        size_match = re.search(r"max=(\d+)", path)
        assert first_match and size_match
        first, size = int(first_match.group(1)), int(size_match.group(1))
        calls.append(first)
        return realm[first:first + size]

    monkeypatch.setattr(Keycloak, "_call", fake_call)
    kc = Keycloak.__new__(Keycloak)
    users = kc.all_users(brief=True)
    assert len(users) == 450
    assert len(calls) == 3
