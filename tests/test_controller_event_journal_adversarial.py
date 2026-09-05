from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller_api.core import ControllerError  # noqa: E402
from controller_api.event_journal import EventJournal  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_015 = ROOT / "migrations" / "015_controller_event_journal.sql"
MIGRATION_016 = ROOT / "migrations" / "016_controller_event_journal_hardening.sql"


class EventJournalAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.connection.executescript(MIGRATION_015.read_text(encoding="utf-8"))
        self.connection.executescript(MIGRATION_016.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.connection.close()

    def emit(
        self,
        *,
        aggregate_id: str = "objective-" + "a" * 32,
        event_id: str | None = None,
        data: dict[str, object] | None = None,
        occurred_at: str = "2026-07-21T12:00:00.000Z",
    ) -> dict[str, object]:
        return EventJournal.emit(
            self.connection,
            event_type="objective.created",
            actor_type="operator",
            actor_id="operator:local-controller-session",
            aggregate_type="objective",
            aggregate_id=aggregate_id,
            correlation_id="corr_" + "b" * 32,
            causation_id="operation-" + "c" * 32,
            project_id="alpha",
            objective_id=aggregate_id,
            event_id=event_id,
            data=data or {"state": "queued"},
            occurred_at=occurred_at,
        )

    def test_replace_cannot_bypass_immutability(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        original = self.emit(event_id="evt_" + "d" * 32)
        self.connection.commit()
        row = self.connection.execute(
            "SELECT * FROM controller_event_journal"
        ).fetchone()
        values = tuple(row[key] for key in row.keys() if key != "sequence")
        columns = ",".join(key for key in row.keys() if key != "sequence")
        placeholders = ",".join("?" for _ in values)

        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                f"INSERT OR REPLACE INTO controller_event_journal ({columns}) "
                f"VALUES ({placeholders})",
                values,
            )

        replacement = list(values)
        event_id_index = [key for key in row.keys() if key != "sequence"].index("event_id")
        replacement[event_id_index] = "evt_" + "e" * 32
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                f"REPLACE INTO controller_event_journal ({columns}) "
                f"VALUES ({placeholders})",
                tuple(replacement),
            )

        current = self.connection.execute(
            "SELECT sequence,event_id,event_type,redacted_data_json "
            "FROM controller_event_journal"
        ).fetchone()
        self.assertEqual(int(current["sequence"]), int(original["sequence"]))
        self.assertEqual(str(current["event_id"]), str(original["event_id"]))
        self.assertEqual(str(current["event_type"]), "objective.created")
        self.assertEqual(str(current["redacted_data_json"]), '{"state":"queued"}')

    def test_explicit_sequence_replace_is_blocked(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit()
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT OR REPLACE INTO controller_event_journal (
                    sequence,event_id,schema_version,event_type,occurred_at,
                    actor_type,actor_id,aggregate_type,aggregate_id,
                    aggregate_revision,project_id,objective_id,correlation_id,
                    causation_id,redacted_data_json
                ) VALUES (1,?,1,'audit.recorded',?,'system',?,'audit',?,1,
                          NULL,NULL,?,NULL,'{}')
                """,
                (
                    "evt_" + "f" * 32,
                    "2026-07-21T12:00:01Z",
                    "system:test",
                    "audit-" + "a" * 32,
                    "corr_" + "f" * 32,
                ),
            )

    def test_sensitive_key_normalization_blocks_bypasses(self) -> None:
        cases = (
            {"accessToken": "hidden"},
            {"refresh-token": "hidden"},
            {"session.token": "hidden"},
            {"clientSecret": "hidden"},
            {"apiKey": "hidden"},
            {"private-key": "hidden"},
            {"credentials": {"user": "x"}},
            {"nested": {"AUTH_JSON": "hidden"}},
            {"environment": {"HOME": "/private"}},
        )
        for data in cases:
            with self.subTest(data=data):
                self.connection.execute("BEGIN IMMEDIATE")
                with self.assertRaises(ControllerError) as caught:
                    self.emit(data=data)
                self.connection.rollback()
                self.assertEqual(caught.exception.code, "event_journal_redaction_failed")

    def test_common_secret_value_formats_are_rejected(self) -> None:
        cases = (
            "Basic dXNlcjpwYXNzd29yZA==",
            "AKIAABCDEFGHIJKLMNOP",
            "AIza" + "A" * 35,
            "123456789:" + "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
            "eyJabcde.eyJfghij.signature12345",
            "https://admin:supersecret@example.invalid/path",
            "glpat-abcdefghijklmnop1234",
            "sk-" + "live-abcdefghijklmnop",
        )
        for value in cases:
            with self.subTest(value=value):
                self.connection.execute("BEGIN IMMEDIATE")
                with self.assertRaises(ControllerError) as caught:
                    self.emit(data={"value": value})
                self.connection.rollback()
                self.assertEqual(caught.exception.code, "event_journal_redaction_failed")

    def test_safe_display_metadata_still_passes(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        event = self.emit(
            data={
                "authentication_state": "not_configured",
                "message": "Worker completed offline validation.",
                "resource_usage": {"cpu_percent": 42.0},
            }
        )
        self.connection.commit()
        self.assertEqual(event["aggregate"]["revision"], 1)

    def test_timestamp_is_canonical_rfc3339_utc(self) -> None:
        valid = (
            "2026-07-21T12:00:00Z",
            "2026-07-21T12:00:00.1Z",
            "2026-07-21T12:00:00.123456Z",
        )
        for value in valid:
            with self.subTest(valid=value):
                aggregate_id = "objective-" + format(len(value), "032x")
                self.connection.execute("BEGIN IMMEDIATE")
                self.emit(aggregate_id=aggregate_id, occurred_at=value)
                self.connection.commit()

        invalid = (
            "2026-07-21 12:00:00Z",
            "2026-07-21t12:00:00Z",
            "2026-07-21T12:00Z",
            "2026-07-21T12:00:00.1234567Z",
            "2026-02-30T12:00:00Z",
            "2026-07-21T25:00:00Z",
        )
        for value in invalid:
            with self.subTest(invalid=value):
                self.connection.execute("BEGIN IMMEDIATE")
                with self.assertRaises(ControllerError) as caught:
                    self.emit(occurred_at=value)
                self.connection.rollback()
                self.assertEqual(caught.exception.code, "event_journal_input_invalid")

    def test_sql_timestamp_guard_rejects_noncanonical_direct_insert(self) -> None:
        statement = """
            INSERT INTO controller_event_journal (
                event_id,schema_version,event_type,occurred_at,actor_type,
                actor_id,aggregate_type,aggregate_id,aggregate_revision,
                project_id,objective_id,correlation_id,causation_id,
                redacted_data_json
            ) VALUES (?,1,'audit.recorded',?,'system','system:test','audit',?,1,
                      NULL,NULL,?,NULL,'{}')
        """
        invalid = (
            "2026-07-21 12:00:00Z",
            "2026-02-30T12:00:00Z",
            "2025-02-29T12:00:00Z",
            "2026-04-31T12:00:00Z",
            "0000-01-01T12:00:00Z",
            "2026-00-10T12:00:00Z",
            "2026-13-10T12:00:00Z",
            "2026-07-00T12:00:00Z",
            "2026-07-21T24:00:00Z",
            "2026-07-21T12:60:00Z",
            "2026-07-21T12:00:60Z",
        )
        for index, value in enumerate(invalid, start=1):
            with self.subTest(value=value):
                with self.assertRaises(sqlite3.IntegrityError):
                    self.connection.execute(
                        statement,
                        (
                            "evt_" + format(index, "032x"),
                            value,
                            "audit-" + format(index, "032x"),
                            "corr_" + format(index, "032x"),
                        ),
                    )

        self.connection.execute(
            statement,
            (
                "evt_" + "f" * 32,
                "2024-02-29T23:59:59.123456Z",
                "audit-" + "f" * 32,
                "corr_" + "f" * 32,
            ),
        )

    def test_concurrent_writers_produce_unique_ordered_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            setup = sqlite3.connect(database, isolation_level=None)
            setup.execute(
                "CREATE TABLE schema_migrations ("
                "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            setup.executescript(MIGRATION_015.read_text(encoding="utf-8"))
            setup.executescript(MIGRATION_016.read_text(encoding="utf-8"))
            setup.close()

            workers = 8
            barrier = threading.Barrier(workers)
            errors: list[BaseException] = []

            def write(index: int) -> None:
                try:
                    connection = sqlite3.connect(
                        database, isolation_level=None, timeout=20.0
                    )
                    connection.row_factory = sqlite3.Row
                    connection.execute("PRAGMA busy_timeout=20000")
                    barrier.wait(timeout=10)
                    connection.execute("BEGIN IMMEDIATE")
                    EventJournal.emit(
                        connection,
                        event_type="objective.state_changed",
                        actor_type="system",
                        actor_id="system:concurrency-test",
                        aggregate_type="objective",
                        aggregate_id="objective-" + "9" * 32,
                        correlation_id="corr_" + format(index + 1, "032x"),
                        causation_id="operation-" + format(index + 1, "032x"),
                        project_id="alpha",
                        objective_id="objective-" + "9" * 32,
                        data={"state": "queued", "writer": index},
                        occurred_at="2026-07-21T12:00:00.000Z",
                    )
                    connection.commit()
                    connection.close()
                except BaseException as error:  # capture thread failures for assertion
                    errors.append(error)

            threads = [threading.Thread(target=write, args=(i,)) for i in range(workers)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=30)
            self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])

            verify = sqlite3.connect(database)
            rows = list(
                verify.execute(
                    "SELECT sequence,aggregate_revision,event_id "
                    "FROM controller_event_journal ORDER BY sequence"
                )
            )
            verify.close()
            self.assertEqual([row[0] for row in rows], list(range(1, workers + 1)))
            self.assertEqual(
                [row[1] for row in rows], list(range(1, workers + 1))
            )
            self.assertEqual(len({row[2] for row in rows}), workers)

    def test_human_review_event_is_in_public_catalog(self) -> None:
        schema = json.loads((ROOT / "specs" / "events-v1.schema.json").read_text())
        known = schema["properties"]["type"]["x-orchestra-known-event-types"]
        self.assertIn("review.human_review_requested", known)
        contract = (ROOT / "docs" / "api" / "EVENTS_V1.md").read_text()
        self.assertIn("review.human_review_requested", contract)

    def test_migration_ledger_and_rerun_are_complete(self) -> None:
        versions = [
            int(row[0])
            for row in self.connection.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            )
        ]
        self.assertEqual(versions, [15, 16])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "repo").symlink_to(ROOT, target_is_directory=True)
            environment = os.environ.copy()
            environment["ORCHESTRA_ROOT"] = str(root)
            command = [
                sys.executable,
                str(ROOT / "scripts" / "orchestra-db.py"),
                "migrate",
            ]
            first = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            second = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            for version in range(15, 29):
                self.assertIn(
                    f"Migration {version:03d}: applied",
                    first.stdout,
                )
                self.assertIn(
                    f"Migration {version:03d}: already applied",
                    second.stdout,
                )
            database = root / "state" / "controller" / "orchestra.db"
            verify = sqlite3.connect(database)
            try:
                all_versions = [
                    int(row[0])
                    for row in verify.execute(
                        "SELECT version FROM schema_migrations ORDER BY version"
                    )
                ]
                self.assertEqual(all_versions, list(range(1, 31)))
                self.assertEqual(
                    verify.execute("PRAGMA user_version").fetchone()[0],
                    29,
                )
            finally:
                verify.close()

    def test_migration_016_is_installed(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 16)
        triggers = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'"
            )
        }
        self.assertIn("controller_event_journal_no_replace_insert", triggers)
        self.assertIn("controller_event_journal_timestamp_insert_guard", triggers)


if __name__ == "__main__":
    unittest.main(verbosity=2)
