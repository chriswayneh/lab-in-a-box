"""
Secret redaction, the single chokepoint for "no credential reaches an artifact".

Every lifecycle record and every campaign evidence file passes through
record.redact before it is written. The live suites assert that no credential
from the environment appears in the files they produce, which proves the
chokepoint is wired up; these prove the chokepoint itself is correct, including
for shapes the live suites happen not to generate.
"""

from __future__ import annotations

import pytest

from record import REDACTED, redact


@pytest.mark.parametrize("key", [
    "password",
    "passwd",
    "secret",
    "token",
    "credential",
    "cookie",
    "private_key",
    "private-key",
    "client_secret",
    "client-secret",
])
def test_forbidden_keys_are_redacted(key):
    assert redact({key: "hunter2"})[key] == REDACTED


@pytest.mark.parametrize("key", [
    "PASSWORD",
    "Password",
    "accessToken",
    "refresh_token",
    "X-Vault-Token",
    "adminPassword",
    "db_password",
])
def test_matching_is_case_insensitive_and_substring(key):
    """
    The rule is "any key containing a forbidden word", not "any key exactly
    equal to one". A field named adminPassword is exactly as dangerous as one
    named password, and camelCase is what the services actually return.
    """
    assert redact({key: "hunter2"})[key] == REDACTED


def test_nested_dicts_are_redacted():
    out = redact({"outer": {"inner": {"password": "hunter2", "safe": "keep"}}})
    assert out["outer"]["inner"]["password"] == REDACTED
    assert out["outer"]["inner"]["safe"] == "keep"


def test_lists_of_dicts_are_redacted():
    out = redact({"items": [{"token": "abc"}, {"name": "erin"}]})
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["name"] == "erin"


def test_deeply_nested_mixed_structures():
    """The shape a campaign record actually has: dicts inside lists inside dicts."""
    payload = {
        "results": [
            {"service": "Vault", "changes": [{"detail": "ok", "secret": "leaked"}]},
        ],
    }
    out = redact(payload)
    assert out["results"][0]["changes"][0]["secret"] == REDACTED
    assert out["results"][0]["changes"][0]["detail"] == "ok"
    assert out["results"][0]["service"] == "Vault"


def test_non_secret_keys_are_untouched():
    payload = {"user": "erin", "service": "Gitea", "count": 3, "enabled": True, "items": None}
    assert redact(payload) == payload


def test_redaction_replaces_the_whole_value_not_part_of_it():
    """
    A partial mask would still leak length and prefix. The value is replaced
    wholesale, so nothing about the original survives.
    """
    out = redact({"password": "a-very-long-password-with-structure"})
    assert out["password"] == REDACTED
    assert "very-long" not in str(out)


def test_redaction_does_not_mutate_its_input():
    """
    The caller keeps using the live object after writing the record. If redact
    mutated in place, writing an audit artifact would strip values out from
    under the code that is still running.
    """
    nested = {"token": "abc"}
    original = {"password": "hunter2", "nested": nested}
    redact(original)
    assert original["password"] == "hunter2"
    assert nested["token"] == "abc"


def test_scalars_and_empty_structures_pass_through():
    assert redact("plain string") == "plain string"
    assert redact(42) == 42
    assert redact(None) is None
    assert redact({}) == {}
    assert redact([]) == []


def test_tuples_become_lists():
    """
    Redaction normalises tuples to lists, which is what json.dumps would do
    anyway. Pinned so the behaviour is intentional rather than incidental.
    """
    assert redact(({"token": "abc"},)) == [{"token": REDACTED}]
