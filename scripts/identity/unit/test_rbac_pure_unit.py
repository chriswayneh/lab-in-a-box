"""
The pure functions inside the RBAC simulator.

These decide what an access review reports. `resource_matches` in particular
has already shipped one defect: it originally matched by prefix in a single
direction, so asking who could reach a Vault path returned "nobody" for a user
holding a wildcard above it. For an access review, a false negative is the
dangerous direction, which is why both directions are pinned here.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rbac import (
    GRAFANA_FALLBACK_ROLE,
    GRAFANA_ROLE_RULES,
    grafana_role,
    parse_vault_policy,
    resource_matches,
)

# -----------------------------------------------------------------------------
# resource_matches
# -----------------------------------------------------------------------------


def test_wildcard_grant_covers_a_deeper_query():
    """A holder of secret/* can reach secret/data/security/. The regression case."""
    assert resource_matches("secret/*", "secret/data/security/")


def test_deeper_grant_covers_a_shallower_query():
    """Asking broadly must still find someone whose grant is narrow."""
    assert resource_matches("secret/data/security/*", "secret/")


def test_exact_match():
    assert resource_matches("secret/data/apps/demo-api", "secret/data/apps/demo-api")


@pytest.mark.parametrize("granted,query", [
    ("secret/data/apps/*", "transit/keys/"),
    ("transit/encrypt/lab-app", "secret/data/"),
    ("secret/data/apps/*", "sys/audit"),
])
def test_unrelated_paths_do_not_match(granted, query):
    assert not resource_matches(granted, query)


def test_empty_query_matches_anything_in_the_service():
    """`vault:` with no path means "any Vault grant", used by who-can."""
    assert resource_matches("anything/at/all", "")


def test_matching_is_symmetric():
    """
    Both directions are the point. If this ever becomes one-directional again,
    who-can starts under-reporting, which is the failure mode that motivated
    the function.
    """
    assert resource_matches("secret/*", "secret/data/x") == resource_matches("secret/data/x", "secret/*")


# -----------------------------------------------------------------------------
# parse_vault_policy
# -----------------------------------------------------------------------------

DEVELOPER_POLICY = """
path "secret/data/apps/*" {
  capabilities = ["create", "read", "update", "delete", "patch"]
}

path "secret/data/infrastructure/*" {
  capabilities = ["read"]
}

path "secret/data/security/*" {
  capabilities = ["deny"]
}
"""


def test_parses_each_path_with_its_capabilities():
    parsed = dict(parse_vault_policy(DEVELOPER_POLICY))
    assert parsed["secret/data/apps/*"] == ["create", "read", "update", "delete", "patch"]
    assert parsed["secret/data/infrastructure/*"] == ["read"]


def test_deny_rules_are_parsed_not_dropped():
    """
    A deny is the strongest statement a policy makes. Losing it while parsing
    would turn "explicitly forbidden" into "not mentioned", which reads as no
    access rather than as a deliberate boundary.
    """
    parsed = dict(parse_vault_policy(DEVELOPER_POLICY))
    assert parsed["secret/data/security/*"] == ["deny"]


def test_empty_and_missing_documents_are_safe():
    assert parse_vault_policy("") == []
    assert parse_vault_policy(None) == []


def test_document_with_no_paths_yields_nothing():
    assert parse_vault_policy("# just a comment\n") == []


def test_capabilities_are_unquoted_and_stripped():
    parsed = dict(parse_vault_policy('path "a/b" {\n  capabilities = [ "read" ,  "list" ]\n}'))
    assert parsed["a/b"] == ["read", "list"]


# -----------------------------------------------------------------------------
# grafana_role
#
# The mapping is duplicated in GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH in
# compose/03-observability.yml. The final test in this section reads that
# expression and keeps the deployment configuration and Python model aligned.
# -----------------------------------------------------------------------------


def test_platform_admin_maps_to_admin():
    role, why = grafana_role(["platform-admin", "developer", "ai-user"])
    assert role == "Admin"
    assert "platform-admin" in why


def test_developer_maps_to_editor():
    role, why = grafana_role(["developer", "ai-user"])
    assert role == "Editor"
    assert "developer" in why


def test_platform_admin_wins_over_developer():
    """
    Order matters: the compose expression checks platform-admin first, so a
    user holding both must resolve to Admin, not Editor.
    """
    assert grafana_role(["developer", "platform-admin"])[0] == "Admin"


@pytest.mark.parametrize("roles", [
    ["contractor"],
    ["security-analyst", "auditor", "ai-user"],
    [],
])
def test_everything_else_falls_through_to_viewer(roles):
    role, why = grafana_role(roles)
    assert role == GRAFANA_FALLBACK_ROLE == "Viewer"
    assert "no matching realm role" in why


def test_python_mapping_matches_grafana_compose_expression():
    repo_root = Path(__file__).resolve().parents[3]
    compose = (repo_root / "compose" / "03-observability.yml").read_text(encoding="utf-8")
    match = re.search(
        r"GF_AUTH_GENERIC_OAUTH_ROLE_ATTRIBUTE_PATH: >-\s*\n"
        r"(?P<expression>(?:\s{8}.+\n)+)",
        compose,
    )
    assert match, "Grafana role_attribute_path expression is missing from Compose"

    expression = match.group("expression")
    configured_rules = re.findall(
        r"contains\(roles\[\*\], '([^']+)'\)\s*&&\s*'([^']+)'",
        expression,
    )
    fallback = re.search(r"\|\|\s*'([^']+)'\s*$", expression)

    assert configured_rules == GRAFANA_ROLE_RULES
    assert fallback and fallback.group(1) == GRAFANA_FALLBACK_ROLE
