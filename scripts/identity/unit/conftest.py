"""
Make the engine importable from these tests.

The engine modules import each other flat (`import rbac`, `from model import
...`), because that is how they run inside their container with /engine on
sys.path. Nothing here is a package, so pytest collecting from a subdirectory
would not otherwise find them.
"""

from __future__ import annotations

import sys
from pathlib import Path

ENGINE_ROOT = Path(__file__).resolve().parent.parent
if str(ENGINE_ROOT) not in sys.path:
    sys.path.insert(0, str(ENGINE_ROOT))
