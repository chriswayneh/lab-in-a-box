"""
Role profile loading.

profiles.json is the declarative source the whole lifecycle reads from. A
malformed one should fail loudly at load time with a message naming the
problem, rather than half-loading and producing a campaign that silently
reviews the wrong thing.
"""

from __future__ import annotations

import json

import pytest

from model import ValidationError, load_catalogue

# Annotated as a bare dict so the nested literals stay indexable in the tests
# below. Without it the inferred value type is the join of the two branches,
# which is `object`, and every nested lookup becomes a type error.
MINIMAL: dict = {
    "profiles": {
        "developer": {
            "summary": "Builds things.",
            "keycloak": {
                "group": "/Application Engineering",
                "attributes": {"title": "Software Engineer"},
                "effective_roles": ["developer", "ai-user"],
            },
            "vault": {"policy": "developer", "grants": ["read secret/data/apps/*"]},
            "gitea": {"team": "developers", "permission": "write", "grants": []},
        },
    },
    "gitea": {
        "organization": "lab-engineering",
        "organization_full_name": "Lab Engineering",
        "organization_description": "",
        "repository_transfer_target": "labadmin",
    },
}


def write(tmp_path, payload):
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_loads_a_wellformed_catalogue(tmp_path):
    catalogue = load_catalogue(write(tmp_path, MINIMAL))
    profile = catalogue.get("developer")

    assert profile.keycloak_group == "/Application Engineering"
    assert profile.vault_policy == "developer"
    assert profile.gitea_team == "developers"
    assert profile.gitea_permission == "write"
    assert profile.effective_roles == ["developer", "ai-user"]


def test_gitea_organisation_settings_are_read(tmp_path):
    catalogue = load_catalogue(write(tmp_path, MINIMAL))
    assert catalogue.gitea_org == "lab-engineering"
    assert catalogue.gitea_transfer_target == "labadmin"


def test_profile_lookup_is_case_insensitive_and_trimmed(tmp_path):
    catalogue = load_catalogue(write(tmp_path, MINIMAL))
    assert catalogue.get("  DEVELOPER  ").name == "developer"


def test_unknown_profile_names_the_known_ones(tmp_path):
    """
    The error is what an operator sees after a typo, so it has to list the
    valid options rather than only rejecting the invalid one.
    """
    catalogue = load_catalogue(write(tmp_path, MINIMAL))
    with pytest.raises(ValidationError) as exc:
        catalogue.get("developerr")
    assert "developer" in str(exc.value)


def test_missing_file_is_a_validation_error(tmp_path):
    with pytest.raises(ValidationError):
        load_catalogue(tmp_path / "does-not-exist.json")


def test_empty_profile_set_is_rejected(tmp_path):
    with pytest.raises(ValidationError):
        load_catalogue(write(tmp_path, {"profiles": {}, "gitea": MINIMAL["gitea"]}))


@pytest.mark.parametrize("drop,expected", [
    ("keycloak", "keycloak.group"),
    ("vault", "vault.policy"),
    ("gitea", "gitea.team"),
])
def test_missing_required_field_names_that_field(tmp_path, drop, expected):
    """
    A profile missing its group, policy or team cannot provision anything. The
    message has to say which one is absent, because all three failures
    otherwise look identical from the command line.
    """
    payload: dict = json.loads(json.dumps(MINIMAL))
    payload["profiles"]["developer"][drop] = {}

    with pytest.raises(ValidationError) as exc:
        load_catalogue(write(tmp_path, payload))
    assert expected in str(exc.value)


def test_optional_fields_have_sensible_defaults(tmp_path):
    payload: dict = json.loads(json.dumps(MINIMAL))
    del payload["profiles"]["developer"]["gitea"]["permission"]
    del payload["profiles"]["developer"]["summary"]

    profile = load_catalogue(write(tmp_path, payload)).get("developer")
    assert profile.gitea_permission == "read"  # least privilege by default
    assert profile.summary == ""


def test_names_are_sorted(tmp_path):
    payload: dict = json.loads(json.dumps(MINIMAL))
    payload["profiles"]["admin"] = json.loads(json.dumps(MINIMAL["profiles"]["developer"]))
    assert load_catalogue(write(tmp_path, payload)).names == ["admin", "developer"]
