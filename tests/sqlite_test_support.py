from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sqlite_lifecycle import ClosingConnection


def sqlite_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
    """Return a test connection with deterministic context-manager closure."""
    kwargs.setdefault("factory", ClosingConnection)
    return sqlite3.connect(*args, **kwargs)
