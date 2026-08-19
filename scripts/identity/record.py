"""
Lifecycle records — the audit artifact.

One JSON document per operation, under artifacts/identity/<user>/. This is the
evidence half of the feature: the terminal output is for the operator running
the command, the artifact is for whoever asks six months later what happened.

Everything written here passes through `redact` first. That function is the
single chokepoint for "no secrets in artifacts", and the test suite asserts
against it directly.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

ARTIFACT_ROOT = Path(os.environ.get("LAB_ARTIFACT_DIR", "/artifacts")) / "identity"

# Keys whose values must never be persisted, matched case-insensitively against
# the whole key name.
FORBIDDEN_KEYS = re.compile(
    r"(password|passwd|secret|token|credential|cookie|private[_-]?key|client[_-]?secret)",
    re.IGNORECASE,
)

REDACTED = "<redacted>"


def redact(value):
    """
    Recursively strip anything that looks like a credential.

    Key-based rather than value-based: guessing whether a string 'looks like' a
    secret is unreliable, but the engine controls its own key names, and any key
    matching FORBIDDEN_KEYS is replaced wholesale.
    """
    if isinstance(value, dict):
        return {
            k: (REDACTED if FORBIDDEN_KEYS.search(str(k)) else redact(v))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def write(operation: str, username: str, payload: dict) -> Path:
    """Persist one lifecycle record and return its path."""
    now = datetime.now(timezone.utc)
    document = redact(
        {
            "schemaVersion": 1,
            "operation": operation,
            "user": username,
            "timestamp": now.isoformat(timespec="seconds"),
            **payload,
        }
    )

    directory = ARTIFACT_ROOT / username
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{now.strftime('%Y%m%dT%H%M%SZ')}-{operation}.json"
    path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return path
