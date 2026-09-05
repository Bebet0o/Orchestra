"""Deterministic SQLite connection lifecycle for Orchestra-owned connections."""

from __future__ import annotations

import sqlite3
from types import TracebackType


class ClosingConnection(sqlite3.Connection):
    """Preserve sqlite3 transaction semantics and close after context exit."""

    def __enter__(self) -> ClosingConnection:
        super().__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()
