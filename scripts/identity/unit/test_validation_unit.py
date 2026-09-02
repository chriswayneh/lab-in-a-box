"""
Input validation, the single gate in front of four services and the filesystem.

These are the functions that decide whether operator-supplied text reaches
Keycloak, Vault and Gitea as a URL path segment, and reaches disk as a
directory name. A regression here is not a cosmetic bug, so this is the one
area covered exhaustively rather than representatively.
"""

from __future__ import annotations

import pytest

import campaign
from model import ValidationError, validate_username

# Each entry is (input, why it must be rejected). The reason is carried into
# the assertion message so a future failure says what protection lapsed rather
# than just printing a rejected string.
HOSTILE_USERNAMES = [
    ("../../etc/passwd", "path traversal"),
    ("..", "path traversal, bare"),
    ("erin/../admin", "embedded traversal"),
    ("erin;rm -rf /", "shell command separator"),
    ("erin$(whoami)", "shell command substitution"),
    ("erin`id`", "backtick command substitution"),
    ("erin&&curl evil.test", "shell conjunction"),
    ("erin|tee", "shell pipe"),
    ("erin with space", "whitespace"),
    ("erin\nadmin", "newline injection"),
    ("erin%2f..%2fadmin", "URL-encoded traversal"),
    ("Erin", "uppercase, services differ on case folding"),
    ("1erin", "must start with a letter"),
    ("-erin", "leading dash could read as a CLI flag"),
    ("e", "too short"),
    ("e" * 40, "too long"),
    ("", "empty"),
    ("   ", "whitespace only"),
]


@pytest.mark.parametrize("value,reason", HOSTILE_USERNAMES)
def test_hostile_usernames_rejected(value, reason):
    with pytest.raises(ValidationError):
        validate_username(value)


@pytest.mark.parametrize("value", ["erin", "alice", "a1", "erin.smith", "erin-smith", "erin_smith"])
def test_ordinary_usernames_accepted(value):
    assert validate_username(value) == value


def test_username_is_trimmed_not_transformed():
    """Surrounding whitespace is forgiven; the value itself is never rewritten."""
    assert validate_username("  erin  ") == "erin"


# -----------------------------------------------------------------------------
# Campaign identifiers
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("value", [
    "../../etc/passwd",
    "name;rm -rf /",
    "name$(whoami)",
    "UPPER",
    "1starts-with-digit",
    "has spaces",
    "",
    "a" * 100,
])
def test_hostile_campaign_names_rejected(value):
    with pytest.raises(ValidationError):
        campaign.validate_campaign_name(value)


def test_ordinary_campaign_name_accepted():
    assert campaign.validate_campaign_name("quarterly-q3") == "quarterly-q3"


def test_campaign_name_is_not_case_folded():
    """
    Mirrors validate_username: uppercase is rejected as typed rather than
    silently lowercased. Folding here would accept input the username gate
    rejects, for no reason beyond the two rules having drifted apart.
    """
    with pytest.raises(ValidationError):
        campaign.validate_campaign_name("Quarterly-Q3")


@pytest.mark.parametrize("value", [
    "../etc/passwd",
    "name-with-no-timestamp",
    "'; DROP TABLE x;--",
    "",
    "quarterly-q3-20260101t000000z",  # lowercase t/z is not what slugify_id writes
])
def test_malformed_campaign_ids_rejected(value):
    with pytest.raises(ValidationError):
        campaign.validate_campaign_id(value)


def test_generated_campaign_id_round_trips():
    """
    Regression: validate_campaign_id used to lowercase its input, which
    destroyed the uppercase T/Z markers slugify_id writes and made every real
    campaign id fail its own validator. Caught only by using the CLI end to
    end, so it is pinned here where it costs nothing to check.
    """
    from datetime import datetime, timezone

    generated = campaign.slugify_id("quarterly-q3", datetime(2026, 8, 19, 1, 2, 3, tzinfo=timezone.utc))
    assert generated == "quarterly-q3-20260819T010203Z"
    assert campaign.validate_campaign_id(generated) == generated


@pytest.mark.parametrize("value", [
    "no-colon-here",
    "Keycloak:" + "x" * 300,
    "Keycloak:bad\nnewline",
    "",
])
def test_malformed_entitlements_rejected(value):
    with pytest.raises(ValidationError):
        campaign.validate_entitlement(value)


@pytest.mark.parametrize("value", [
    "Gitea:team developers",
    "Keycloak:group /Application Engineering",
    "Vault:secret/data/apps/*",
    "Keycloak:role ai-user",
])
def test_real_entitlements_accepted(value):
    assert campaign.validate_entitlement(value) == value


def test_decisions_map_to_canonical_values():
    assert campaign.validate_decision("approve") == campaign.APPROVE
    assert campaign.validate_decision("revoke") == campaign.REVOKE
    assert campaign.validate_decision("not-applicable") == campaign.NOT_APPLICABLE


def test_decision_accepts_surrounding_whitespace_and_case():
    assert campaign.validate_decision("  APPROVE  ") == campaign.APPROVE


@pytest.mark.parametrize("value", ["delete-everything", "", "approved", "yes"])
def test_unknown_decisions_rejected(value):
    with pytest.raises(ValidationError):
        campaign.validate_decision(value)


def test_undecided_is_not_reachable_as_a_decision():
    """
    UNDECIDED is a state an item starts in, never something a reviewer can
    select. If it were selectable, "nobody looked at this" and "somebody
    actively chose to defer" would become indistinguishable in the record.
    """
    with pytest.raises(ValidationError):
        campaign.validate_decision("undecided")
