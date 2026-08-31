"""
Deterministic identifiers.

Both of these have golden values pinned. An identifier that changes when the
algorithm is refactored is worse than no identifier: employee IDs written into
Keycloak last, and campaign item IDs are what an audit trail cross-references,
so a silent change breaks correlation with records already on disk rather than
failing loudly.
"""

from __future__ import annotations

import subprocess
import sys

import campaign
import jml


def test_employee_id_is_stable_for_the_same_username():
    assert jml.employee_id("erin") == jml.employee_id("erin")


def test_employee_id_golden_values():
    """
    Regression: this was originally derived from hash(), which Python seeds
    randomly per process, so the same person received a different ID on every
    run. These constants lock the current algorithm; a change to them is a
    change to every ID already written into the realm.
    """
    assert jml.employee_id("erin") == "E-77532"
    assert jml.employee_id("alice") == "E-17801"


def test_employee_id_differs_between_users():
    assert jml.employee_id("erin") != jml.employee_id("erica")


def test_employee_id_shape():
    value = jml.employee_id("erin")
    assert value.startswith("E-")
    assert len(value) == 7
    assert value[2:].isdigit()


def test_employee_id_is_stable_across_a_separate_interpreter():
    """
    The property that actually failed before: within one process a randomised
    hash still looks stable, so this has to cross a process boundary with a
    fresh PYTHONHASHSEED to be a real check.
    """
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '/workdir/scripts/identity'); "
         "import jml; print(jml.employee_id('erin'))"],
        capture_output=True, text=True, env={"PYTHONHASHSEED": "random", "PATH": "/usr/local/bin:/usr/bin:/bin"},
    )
    assert out.stdout.strip() == jml.employee_id("erin"), out.stderr


def test_item_id_is_stable():
    assert (campaign.compute_item_id("erin", "Gitea:team developers")
            == campaign.compute_item_id("erin", "Gitea:team developers"))


def test_item_id_golden_value():
    assert campaign.compute_item_id("erin", "Gitea:team developers") == "7402c9e1ad3e"


def test_item_id_varies_with_both_inputs():
    base = campaign.compute_item_id("erin", "Gitea:team developers")
    assert base != campaign.compute_item_id("erin", "Gitea:team security")
    assert base != campaign.compute_item_id("erica", "Gitea:team developers")


def test_item_id_shape():
    value = campaign.compute_item_id("erin", "Gitea:team developers")
    assert len(value) == 12
    assert all(c in "0123456789abcdef" for c in value)


def test_slugify_id_is_pure_and_formatted():
    from datetime import datetime, timezone

    moment = datetime(2026, 8, 19, 1, 2, 3, tzinfo=timezone.utc)
    assert campaign.slugify_id("quarterly-q3", moment) == "quarterly-q3-20260819T010203Z"
