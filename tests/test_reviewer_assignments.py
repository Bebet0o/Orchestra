from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "orchestra_review_assignment.py"
MIGRATION = ROOT / "migrations" / "019_reviewer_assignments.sql"

spec = importlib.util.spec_from_file_location("reviewer_assignments", MODULE_PATH)
assert spec is not None and spec.loader is not None
ASSIGNMENTS = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ASSIGNMENTS)


BASE_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE projects(project_id TEXT PRIMARY KEY);
CREATE TABLE runs(run_id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(project_id));
CREATE TABLE roles(
 role_id TEXT PRIMARY KEY, profile_name TEXT NOT NULL, role_kind TEXT NOT NULL,
 workspace_mode TEXT NOT NULL, may_commit INTEGER NOT NULL, may_push INTEGER NOT NULL,
 network_enabled INTEGER NOT NULL, enabled INTEGER NOT NULL
);
CREATE TABLE tasks(task_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id));
CREATE TABLE reviewer_executions(
 execution_id TEXT PRIMARY KEY,
 task_id TEXT NOT NULL REFERENCES tasks(task_id),
 run_id TEXT NOT NULL REFERENCES runs(run_id),
 role_id TEXT NOT NULL REFERENCES roles(role_id),
 source_profile TEXT NOT NULL,
 review_id TEXT,
 finished_at TEXT
);
CREATE TABLE review_results(review_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id));
CREATE TABLE orchestration_attempts(
 attempt_id TEXT PRIMARY KEY, run_id TEXT REFERENCES runs(run_id)
);
CREATE TABLE events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT, run_id TEXT, task_id TEXT,
 event_type TEXT NOT NULL, severity TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE controller_event_journal (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT,
 event_id TEXT NOT NULL UNIQUE,
 schema_version INTEGER NOT NULL,
 event_type TEXT NOT NULL,
 occurred_at TEXT NOT NULL,
 actor_type TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 aggregate_type TEXT NOT NULL,
 aggregate_id TEXT NOT NULL,
 aggregate_revision INTEGER NOT NULL,
 project_id TEXT,
 objective_id TEXT,
 correlation_id TEXT NOT NULL,
 causation_id TEXT,
 redacted_data_json TEXT NOT NULL,
 UNIQUE(aggregate_type, aggregate_id, aggregate_revision)
);
"""


class ReviewerAssignmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.db"
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(BASE_SCHEMA)
        self.connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.connection.execute("INSERT INTO projects VALUES ('project')")
        self.connection.execute("INSERT INTO runs VALUES ('run-1','project')")
        self.connection.execute(
            "INSERT INTO roles VALUES ('reviewer','ops-reviewer','reviewer','read_only',0,0,0,1)"
        )
        self.connection.execute(
            "INSERT INTO orchestration_attempts VALUES ('attempt-1','run-1')"
        )
        self.connection.commit()

    def tearDown(self) -> None:
        self.connection.close()
        self.temp.cleanup()

    def create(self, number: int = 1) -> dict[str, object]:
        self.connection.execute("BEGIN IMMEDIATE")
        assignment = ASSIGNMENTS.create_assignment(
            self.connection,
            run_id="run-1",
            orchestration_attempt_id="attempt-1",
            assignment_number=number,
            role_id="reviewer",
            assigned_by="orchestrator:attempt-1",
        )
        self.connection.commit()
        return assignment

    def claim(self, assignment_id: str, execution: str = "review-execution-1") -> None:
        task = "task-" + execution[-1]
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("INSERT INTO tasks VALUES (?, 'run-1')", (task,))
        self.connection.execute(
            "INSERT INTO reviewer_executions VALUES (?, ?, 'run-1', 'reviewer', 'ops-reviewer', NULL, NULL)",
            (execution, task),
        )
        ASSIGNMENTS.claim_assignment(
            self.connection,
            assignment_id=assignment_id,
            run_id="run-1",
            role_id="reviewer",
            source_profile="ops-reviewer",
            review_execution_id=execution,
            task_id=task,
        )
        self.connection.commit()

    def test_create_claim_complete_and_events(self) -> None:
        assignment = self.create()
        assignment_id = str(assignment["assignment_id"])
        self.claim(assignment_id)
        self.connection.execute("BEGIN IMMEDIATE")
        self.connection.execute("INSERT INTO review_results VALUES ('review-1','run-1')")
        self.connection.execute(
            "UPDATE reviewer_executions SET review_id='review-1', finished_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')"
        )
        ASSIGNMENTS.finish_assignment(
            self.connection,
            assignment_id=assignment_id,
            run_id="run-1",
            review_execution_id="review-execution-1",
            task_id="task-1",
            success=True,
            review_id="review-1",
        )
        self.connection.commit()
        row = self.connection.execute(
            "SELECT status, review_id, failure_code FROM reviewer_assignments"
        ).fetchone()
        self.assertEqual(tuple(row), ("COMPLETED", "review-1", None))
        self.assertEqual(
            [r[0] for r in self.connection.execute(
                "SELECT event_type FROM controller_event_journal ORDER BY sequence"
            )],
            [
                "review.assignment_created",
                "review.assignment_claimed",
                "review.assignment_completed",
            ],
        )

    def test_only_one_active_assignment_per_run(self) -> None:
        self.create()
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(sqlite3.IntegrityError):
            ASSIGNMENTS.create_assignment(
                self.connection,
                run_id="run-1",
                orchestration_attempt_id="attempt-1",
                assignment_number=2,
                role_id="reviewer",
                assigned_by="orchestrator:attempt-1",
            )
        self.connection.rollback()

    def test_failed_assignment_allows_retry(self) -> None:
        first = self.create()
        self.connection.execute("BEGIN IMMEDIATE")
        changed = ASSIGNMENTS.fail_active_assignment(
            self.connection,
            assignment_id=str(first["assignment_id"]),
            actor_id="orchestrator:attempt-1",
            failure_code="REVIEW_TRANSPORT_FAILED",
        )
        self.connection.commit()
        self.assertTrue(changed)
        second = self.create(number=2)
        self.assertNotEqual(first["assignment_id"], second["assignment_id"])

    def test_role_policy_is_enforced(self) -> None:
        self.connection.execute("UPDATE roles SET network_enabled=1")
        self.connection.commit()
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ASSIGNMENTS.ReviewerAssignmentError):
            ASSIGNMENTS.create_assignment(
                self.connection,
                run_id="run-1",
                orchestration_attempt_id="attempt-1",
                assignment_number=1,
                role_id="reviewer",
                assigned_by="orchestrator:attempt-1",
            )
        self.connection.rollback()

    def test_claim_is_atomic_and_single_use(self) -> None:
        assignment = self.create()
        assignment_id = str(assignment["assignment_id"])
        self.claim(assignment_id)
        self.connection.execute("BEGIN IMMEDIATE")
        with self.assertRaises(ASSIGNMENTS.ReviewerAssignmentError):
            ASSIGNMENTS.claim_assignment(
                self.connection,
                assignment_id=assignment_id,
                run_id="run-1",
                role_id="reviewer",
                source_profile="ops-reviewer",
                review_execution_id="review-execution-1",
                task_id="task-1",
            )
        self.connection.rollback()

    def test_recovery_closes_active_assignment_without_raw_reason(self) -> None:
        assignment = self.create()
        self.connection.execute("BEGIN IMMEDIATE")
        count = ASSIGNMENTS.recover_active_assignments(
            self.connection,
            run_id="run-1",
            actor_id="recovery:run-1",
        )
        self.connection.commit()
        self.assertEqual(count, 1)
        row = self.connection.execute(
            "SELECT status, failure_code, review_id FROM reviewer_assignments"
        ).fetchone()
        self.assertEqual(tuple(row), ("FAILED", "RECOVERY_ABANDONED", None))
        self.assertNotIn("reason", MIGRATION.read_text(encoding="utf-8").lower())

    def test_identity_terminal_and_delete_are_immutable(self) -> None:
        assignment = self.create()
        assignment_id = str(assignment["assignment_id"])
        self.connection.execute("BEGIN IMMEDIATE")
        ASSIGNMENTS.fail_active_assignment(
            self.connection,
            assignment_id=assignment_id,
            actor_id="orchestrator:attempt-1",
            failure_code="REVIEW_LAUNCH_FAILED",
        )
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "UPDATE reviewer_assignments SET status='CANCELLED' WHERE assignment_id=?",
                (assignment_id,),
            )
        self.connection.rollback()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "DELETE FROM reviewer_assignments WHERE assignment_id=?",
                (assignment_id,),
            )
        self.connection.rollback()

    def test_migration_version_and_indexes(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA user_version").fetchone()[0], 19)
        names = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index','trigger')"
            )
        }
        self.assertIn("reviewer_assignments", names)
        self.assertIn("idx_reviewer_assignments_one_active_run", names)
        self.assertIn("reviewer_assignment_transition_guard", names)
        self.assertIn("reviewer_assignment_insert_policy_guard", names)
        self.assertIn("reviewer_assignment_claim_link_guard", names)
        self.assertIn("reviewer_assignment_completion_link_guard", names)

    def test_direct_non_reviewer_assignment_is_rejected(self) -> None:
        self.connection.execute(
            "INSERT INTO roles VALUES ('worker','ops-worker','worker','write',1,0,0,1)"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO reviewer_assignments (
                    assignment_id, run_id, orchestration_attempt_id,
                    assignment_number, role_id, source_profile, status,
                    assigned_by, assigned_at
                ) VALUES (
                    'review-assignment-00000000000000000000000000000001',
                    'run-1', 'attempt-1', 1, 'worker', 'ops-worker',
                    'ASSIGNED', 'orchestrator:attempt-1',
                    strftime('%Y-%m-%dT%H:%M:%fZ','now')
                )
                """
            )
        self.connection.rollback()

    def test_noncanonical_timestamp_is_rejected(self) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                """
                INSERT INTO reviewer_assignments (
                    assignment_id, run_id, orchestration_attempt_id,
                    assignment_number, role_id, source_profile, status,
                    assigned_by, assigned_at
                ) VALUES (
                    'review-assignment-00000000000000000000000000000002',
                    'run-1', 'attempt-1', 1, 'reviewer', 'ops-reviewer',
                    'ASSIGNED', 'orchestrator:attempt-1',
                    '2026-07-21 12:00:00'
                )
                """
            )
        self.connection.rollback()


if __name__ == "__main__":
    unittest.main(verbosity=2)
