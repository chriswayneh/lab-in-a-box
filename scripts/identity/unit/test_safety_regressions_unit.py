"""Regression tests for identity-engine safety fixes (no lab needed)."""

from __future__ import annotations

import re
from typing import Any, cast

import pytest

import campaign
import rbac
from keycloak import Keycloak
from labhttp import HttpError, Unavailable as KeycloakUnavailable
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


def test_vault_mover_revokes_existing_tokens(monkeypatch):
    from vault import Vault

    calls = []
    v = Vault.__new__(Vault)
    monkeypatch.setattr(Vault, "ensure_policy", lambda self, name, result: None)
    monkeypatch.setattr(Vault, "user_policies", lambda self, username: ["lab-admin"])
    monkeypatch.setattr(Vault, "_call", lambda self, method, path, **kw: calls.append((method, path)))
    v.reconcile_user("alice", "lab-readonly", "pw", ServiceResult("Vault"))
    assert ("PUT", "/sys/leases/revoke-prefix/auth/userpass/login/alice") in calls


def test_gitea_reactivation_clears_prohibit_login(monkeypatch):
    from gitea import Gitea

    bodies = []
    g = Gitea.__new__(Gitea)
    monkeypatch.setattr(
        Gitea, "find_user", lambda self, u: {"login": u, "active": True, "prohibit_login": True}
    )

    def fake_call(self, method, path, json_body=None, **kw):
        bodies.append((method, json_body))

    monkeypatch.setattr(Gitea, "_call", fake_call)
    g.reconcile_user("bob", "pw", "Bob", ServiceResult("Gitea"))
    assert any(m == "PATCH" and b and b.get("prohibit_login") is False for m, b in bodies)


# --- QE review follow-ups -------------------------------------------------



def _vault_policy_item():
    return campaign.ReviewItem(
        item_id="i1", username="alice", service="Vault", resource="policy lab-admin",
        permission="", source="direct", inheritance=rbac.DIRECT, expectation=rbac.UNEXPECTED,
        decision=campaign.REVOKE,
    )


class _FakeVault:
    def user_policies(self, username):
        return ["lab-admin"]

    def set_policies(self, username, policies, result):
        result.record(UPDATED, "policies replaced")


class _FakeSim:
    def __init__(self, report):
        self.report = report

    def analyse(self, username):
        return self.report


def test_remediation_unknown_fresh_read_is_not_verified():
    report = rbac.Report(user="alice", exists=True)
    report.add(rbac.Grant(service="Vault", resource="(service)", decision=rbac.UNKNOWN,
                          source="Vault unreachable"))
    item = _vault_policy_item()
    campaign._remediate_one(item, cast(Any, _FakeSim(report)), None, _FakeVault(), None)
    assert item.remediation_status == campaign.REM_FAILED
    assert "Vault unreachable" in item.remediation_detail


def test_remediation_authoritative_empty_read_is_verified():
    item = _vault_policy_item()
    campaign._remediate_one(item, cast(Any, _FakeSim(rbac.Report(user="alice", exists=True))), None,
                            _FakeVault(), None)
    assert item.remediation_status == campaign.REM_VERIFIED


@pytest.mark.parametrize("page", [None, {"error": "unexpected"}])
def test_all_users_rejects_non_list_page(monkeypatch, page):
    monkeypatch.setattr(Keycloak, "_call", lambda self, method, path, **kw: page)
    kc = Keycloak.__new__(Keycloak)
    with pytest.raises(KeycloakUnavailable):
        kc.all_users()


def test_all_users_exactly_two_full_pages(monkeypatch):
    realm = [{"username": f"user{i:04d}"} for i in range(400)]
    calls = []

    def fake_call(self, method, path, **kwargs):
        first_m = re.search(r"first=(\d+)", path)
        size_m = re.search(r"max=(\d+)", path)
        assert first_m and size_m
        first, size = int(first_m.group(1)), int(size_m.group(1))
        calls.append(first)
        return realm[first:first + size]

    monkeypatch.setattr(Keycloak, "_call", fake_call)
    kc = Keycloak.__new__(Keycloak)
    assert len(kc.all_users()) == 400
    assert calls == [0, 200, 400]


def _vault_raising(monkeypatch, status):
    from vault import Vault

    def fake_call(self, method, path, **kw):
        raise HttpError(method, "http://vault" + path, status, "boom")

    monkeypatch.setattr(Vault, "_call", fake_call)
    return Vault.__new__(Vault)


def test_revoke_user_tokens_server_error_raises(monkeypatch):
    v = _vault_raising(monkeypatch, 500)
    with pytest.raises(HttpError):
        v.revoke_user_tokens("alice", ServiceResult("Vault"))


def test_revoke_user_tokens_not_found_is_swallowed(monkeypatch):
    v = _vault_raising(monkeypatch, 404)
    result = ServiceResult("Vault")
    v.revoke_user_tokens("alice", result)
    assert not result.failed
