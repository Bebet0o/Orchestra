from __future__ import annotations

import contextlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sqlite_lifecycle import ClosingConnection


class SQLiteLifecycleTest(unittest.TestCase):
    TARGETS = (
        "orchestra-control.py", "orchestra-db.py", "orchestra-integrator.py",
        "orchestra-notifier.py", "orchestra-objectives.py", "orchestra-orchestrator.py",
        "orchestra-planner.py", "orchestra-recovery.py", "orchestra-reviewer.py",
        "orchestra-supervisor.py", "orchestra-transaction.py", "orchestra-worker.py",
    )

    def test_context_exit_commits_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            connection = sqlite3.connect(database, factory=ClosingConnection)
            with connection:
                connection.execute("CREATE TABLE item(value TEXT NOT NULL)")
                connection.execute("INSERT INTO item(value) VALUES ('ok')")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            with contextlib.closing(sqlite3.connect(database)) as verification, verification:
                self.assertEqual(verification.execute("SELECT value FROM item").fetchone()[0], "ok")

    def test_context_exit_rolls_back_and_closes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.db"
            with contextlib.closing(sqlite3.connect(database)) as setup, setup:
                setup.execute("CREATE TABLE item(value TEXT NOT NULL)")
            connection = sqlite3.connect(database, factory=ClosingConnection)
            with self.assertRaisesRegex(RuntimeError, "rollback"):
                with connection:
                    connection.execute("INSERT INTO item(value) VALUES ('no')")
                    raise RuntimeError("rollback")
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")
            with contextlib.closing(sqlite3.connect(database)) as verification, verification:
                self.assertEqual(verification.execute("SELECT COUNT(*) FROM item").fetchone()[0], 0)

    def test_production_connect_helpers_use_closing_factory(self) -> None:
        for filename in self.TARGETS:
            source = (SCRIPTS / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("from sqlite_lifecycle import ClosingConnection", source)
                self.assertIn("factory=ClosingConnection", source)


if __name__ == "__main__":
    unittest.main()
