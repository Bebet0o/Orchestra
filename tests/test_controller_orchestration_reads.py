from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from controller_api.core import ControllerError, Settings
from controller_api.orchestration_reads import OrchestrationReadStore

PLAN_ONE = "plan-" + "1" * 32
PLAN_TWO = "plan-" + "2" * 32
OBJECTIVE_ONE = "objective-" + "1" * 32
OBJECTIVE_TWO = "objective-" + "2" * 32
TASK_ONE = "orchestration-task-" + "1" * 32
TASK_TWO = "orchestration-task-" + "2" * 32
TASK_THREE = "orchestration-task-" + "3" * 32
RUN_ONE = "orchestration-attempt-" + "1" * 32
RUN_TWO = "orchestration-attempt-" + "2" * 32
ASSIGNMENT_ONE = "review-assignment-" + "1" * 32
REVIEW_EXECUTION = "review-execution-" + "1" * 32
REVIEW_ID = "review-" + "1" * 32
WORKER_EXECUTION = "execution-" + "1" * 32
INTEGRATION = "integration-" + "1" * 32
T1 = "2026-07-21T20:00:00.000Z"
T2 = "2026-07-21T20:01:00.000Z"
T3 = "2026-07-21T20:02:00.000Z"

SCHEMA = """
PRAGMA foreign_keys=OFF;
CREATE TABLE projects(project_id TEXT PRIMARY KEY);
CREATE TABLE roles(
    role_id TEXT PRIMARY KEY,
    workspace_mode TEXT NOT NULL
);
CREATE TABLE objective_queue(
    objective_id TEXT PRIMARY KEY,
    plan_id TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE orchestration_plans(
    plan_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    source TEXT NOT NULL,
    planner_role_id TEXT NOT NULL,
    status TEXT NOT NULL,
    max_parallel_tasks INTEGER NOT NULL,
    plan_sha256 TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT,
    last_error TEXT
);
CREATE TABLE orchestration_tasks(
    orchestration_task_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    task_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    project_id TEXT,
    role_id TEXT,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    instruction TEXT NOT NULL,
    acceptance_json TEXT NOT NULL,
    marker TEXT,
    max_attempts INTEGER NOT NULL,
    attempt_count INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT
);
CREATE TABLE orchestration_dependencies(
    plan_id TEXT NOT NULL,
    orchestration_task_id TEXT NOT NULL,
    depends_on_task_id TEXT NOT NULL,
    dependency_condition TEXT NOT NULL
);
CREATE TABLE orchestration_attempts(
    attempt_id TEXT PRIMARY KEY,
    orchestration_task_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    executor_instance_id TEXT,
    run_id TEXT,
    worker_execution_id TEXT,
    review_execution_id TEXT,
    integration_id TEXT,
    result_json TEXT NOT NULL,
    failure_reason TEXT,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE TABLE reviewer_assignments(
    assignment_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    orchestration_attempt_id TEXT NOT NULL,
    assignment_number INTEGER NOT NULL,
    role_id TEXT NOT NULL,
    source_profile TEXT NOT NULL,
    status TEXT NOT NULL,
    assigned_by TEXT NOT NULL,
    claim_owner TEXT,
    review_execution_id TEXT,
    review_id TEXT,
    failure_code TEXT,
    assigned_at TEXT NOT NULL,
    claimed_at TEXT,
    heartbeat_at TEXT,
    finished_at TEXT
);
"""


class OrchestrationReadsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "repo").mkdir()
        (self.root / "state/controller").mkdir(parents=True)
        (self.root / "secrets").mkdir()
        (self.root / "secrets/controller-session").write_text("s" * 64, encoding="ascii")
        self.database = self.root / "state/controller/orchestra.db"
        connection = sqlite3.connect(self.database)
        connection.executescript(SCHEMA)
        connection.executemany(
            "INSERT INTO projects VALUES (?)",
            [("alpha",), ("beta",)],
        )
        connection.executemany(
            "INSERT INTO roles VALUES (?,?)",
            [("orchestrator", "controller_only"), ("worker_code", "write")],
        )
        connection.execute(
            """
            INSERT INTO orchestration_plans VALUES(
                ?, 'PRIVATE OBJECTIVE', 'AI', 'orchestrator', 'COMPLETED', 2,
                ?, '{"instruction":"secret"}', ?, ?, ?, ?, NULL
            )
            """,
            (PLAN_ONE, "a" * 64, T1, T1, T3, T3),
        )
        connection.execute(
            """
            INSERT INTO orchestration_plans VALUES(
                ?, 'SECOND PRIVATE OBJECTIVE', 'DECLARATIVE', 'FAILED', 'FAILED', 1,
                ?, '{"prompt":"secret"}', ?, ?, ?, ?, 'raw provider failure'
            )
            """.replace("'FAILED', 'FAILED'", "'orchestrator', 'FAILED'"),
            (PLAN_TWO, "b" * 64, T2, T2, T3, T3),
        )
        connection.executemany(
            "INSERT INTO objective_queue VALUES (?,?,?)",
            [(OBJECTIVE_ONE, PLAN_ONE, T1), (OBJECTIVE_TWO, PLAN_TWO, T2)],
        )
        connection.executemany(
            """
            INSERT INTO orchestration_tasks VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            [
                (
                    TASK_ONE, PLAN_ONE, "build", "PIPELINE", "alpha", "worker_code",
                    "COMPLETED", 10, "private instruction", '["private criterion"]',
                    "PRIVATE_MARKER", 2, 1, '{"private":"result"}', None,
                    T1, T1, T2, T2,
                ),
                (
                    TASK_TWO, PLAN_ONE, "review_gate", "NOOP", "alpha", None,
                    "FAILED", 20, "private instruction 2", '["private criterion 2"]',
                    None, 1, 1, "{}", "raw failure reason",
                    T2, T2, T3, T3,
                ),
                (
                    TASK_THREE, PLAN_TWO, "other", "PIPELINE", "beta", "worker_code",
                    "CANCELLED", 10, "other private", "[]", None, 1, 0, "{}",
                    None, T2, None, T3, T3,
                ),
            ],
        )
        connection.execute(
            "INSERT INTO orchestration_dependencies VALUES (?,?,?,'SUCCESS')",
            (PLAN_ONE, TASK_TWO, TASK_ONE),
        )
        connection.executemany(
            """
            INSERT INTO orchestration_attempts VALUES(
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            """,
            [
                (
                    RUN_ONE, TASK_ONE, 1, "COMPLETED", "private-instance",
                    "internal-run-key-1", WORKER_EXECUTION, REVIEW_EXECUTION,
                    INTEGRATION, '{"private":"result"}', None, T1, T2, T2,
                ),
                (
                    RUN_TWO, TASK_TWO, 2, "FAILED", "private-instance",
                    "internal-run-key-2", None, None, None, "{}", "raw attempt failure",
                    T2, T3, T3,
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO reviewer_assignments VALUES(
                ?, 'internal-run-key-1', ?, 1, 'reviewer', 'ops-reviewer',
                'COMPLETED', 'orchestrator:private', 'reviewer:private',
                ?, ?, NULL, ?, ?, ?, ?
            )
            """,
            (
                ASSIGNMENT_ONE, RUN_ONE, REVIEW_EXECUTION, REVIEW_ID,
                T1, T1, T2, T2,
            ),
        )
        connection.commit()
        connection.close()
        settings = Settings.from_root(
            self.root,
            database=self.database,
            session_file=self.root / "secrets/controller-session",
        )
        self.store = OrchestrationReadStore(settings)
        self.secret = "x" * 64

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def assert_no_private_material(value: object) -> None:
        serialized = json.dumps(value, sort_keys=True)
        for forbidden in (
            "PRIVATE OBJECTIVE",
            "SECOND PRIVATE OBJECTIVE",
            "private instruction",
            "private criterion",
            "PRIVATE_MARKER",
            "raw provider failure",
            "raw failure reason",
            "raw attempt failure",
            "internal-run-key",
            "private-instance",
            "orchestrator:private",
            "reviewer:private",
        ):
            if forbidden in serialized:
                raise AssertionError(f"private material leaked: {forbidden}")

    def test_plan_list_detail_and_redaction(self) -> None:
        items, cursor = self.store.list_plans(
            limit=50, cursor=None, project_id=None, state=None,
            cursor_secret=self.secret,
        )
        self.assertIsNone(cursor)
        self.assertEqual([item["id"] for item in items], [PLAN_TWO, PLAN_ONE])
        first = self.store.get_plan(PLAN_ONE)
        self.assertEqual(first["objective_id"], OBJECTIVE_ONE)
        self.assertEqual(first["project_ids"], ["alpha"])
        self.assertEqual(first["task_counts"]["total"], 2)
        self.assertEqual(first["attempt_count"], 2)
        self.assertEqual(first["reviewer_assignment_count"], 1)
        self.assertTrue(first["definition_redacted"])
        self.assert_no_private_material(items)
        self.assert_no_private_material(first)

    def test_plan_filters_and_signed_cursor(self) -> None:
        first, cursor = self.store.list_plans(
            limit=1, cursor=None, project_id=None, state=None,
            cursor_secret=self.secret,
        )
        self.assertEqual([item["id"] for item in first], [PLAN_TWO])
        self.assertIsNotNone(cursor)
        second, next_cursor = self.store.list_plans(
            limit=1, cursor=cursor, project_id=None, state=None,
            cursor_secret=self.secret,
        )
        self.assertEqual([item["id"] for item in second], [PLAN_ONE])
        self.assertIsNone(next_cursor)
        filtered, _ = self.store.list_plans(
            limit=50, cursor=None, project_id="alpha", state="succeeded",
            cursor_secret=self.secret,
        )
        self.assertEqual([item["id"] for item in filtered], [PLAN_ONE])
        with self.assertRaises(ControllerError) as caught:
            self.store.list_plans(
                limit=1, cursor=cursor, project_id="alpha", state=None,
                cursor_secret=self.secret,
            )
        self.assertEqual(caught.exception.code, "invalid_cursor")
        with self.assertRaises(ControllerError):
            self.store.list_plans(
                limit=1, cursor=cursor, project_id=None, state=None,
                cursor_secret="y" * 64,
            )

    def test_plan_tasks_dependencies_and_attempts_are_redacted(self) -> None:
        tasks, _ = self.store.list_plan_tasks(
            PLAN_ONE, limit=50, cursor=None, cursor_secret=self.secret
        )
        self.assertEqual([item["id"] for item in tasks], [TASK_ONE, TASK_TWO])
        self.assertEqual(tasks[0]["dependency_count"], 0)
        self.assertEqual(tasks[0]["dependent_count"], 1)
        self.assertTrue(tasks[0]["instruction_redacted"])
        self.assertTrue(tasks[0]["acceptance_redacted"])
        dependencies, _ = self.store.list_plan_dependencies(
            PLAN_ONE, limit=50, cursor=None, cursor_secret=self.secret
        )
        self.assertEqual(len(dependencies), 1)
        self.assertEqual(dependencies[0]["task_id"], TASK_TWO)
        self.assertEqual(dependencies[0]["depends_on_task_id"], TASK_ONE)
        attempts, _ = self.store.list_plan_attempts(
            PLAN_ONE, limit=50, cursor=None, cursor_secret=self.secret
        )
        self.assertEqual([item["id"] for item in attempts], [RUN_TWO, RUN_ONE])
        self.assertTrue(attempts[0]["error"]["legacy_payload_redacted"])
        self.assertRegex(
            attempts[1]["transaction_reference"],
            r"^transaction-[a-f0-9]{32}$",
        )
        self.assert_no_private_material(tasks)
        self.assert_no_private_material(dependencies)
        self.assert_no_private_material(attempts)

    def test_assignment_list_detail_nested_run_and_redaction(self) -> None:
        items, cursor = self.store.list_assignments(
            limit=50, cursor=None, project_id=None, state=None, run_id=None,
            cursor_secret=self.secret,
        )
        self.assertIsNone(cursor)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["id"], ASSIGNMENT_ONE)
        self.assertEqual(item["run_id"], RUN_ONE)
        self.assertEqual(item["task_id"], TASK_ONE)
        self.assertEqual(item["plan_id"], PLAN_ONE)
        self.assertEqual(item["state"], "completed")
        self.assertTrue(item["claim_owner_redacted"])
        nested, _ = self.store.list_assignments(
            limit=50, cursor=None, project_id=None, state=None, run_id=RUN_ONE,
            cursor_secret=self.secret,
        )
        self.assertEqual(nested, items)
        detail = self.store.get_assignment(ASSIGNMENT_ONE)
        self.assertEqual(detail, item)
        self.assert_no_private_material(items)
        self.assert_no_private_material(detail)

    def test_invalid_filters_identifiers_and_limits(self) -> None:
        cases = [
            lambda: self.store.list_plans(
                limit=0, cursor=None, project_id=None, state=None,
                cursor_secret=self.secret,
            ),
            lambda: self.store.list_plans(
                limit=1, cursor=None, project_id="../alpha", state=None,
                cursor_secret=self.secret,
            ),
            lambda: self.store.list_plans(
                limit=1, cursor=None, project_id=None, state="unknown",
                cursor_secret=self.secret,
            ),
            lambda: self.store.get_plan("not-a-plan"),
            lambda: self.store.list_assignments(
                limit=1, cursor=None, project_id=None, state="unknown", run_id=None,
                cursor_secret=self.secret,
            ),
            lambda: self.store.get_assignment("not-an-assignment"),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(ControllerError):
                    case()

    def test_plan_projection_rejects_multiple_public_objectives(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            "INSERT INTO objective_queue VALUES (?,?,?)",
            ("objective-" + "3" * 32, PLAN_ONE, T3),
        )
        connection.commit()
        connection.close()
        with self.assertRaises(ControllerError) as caught:
            self.store.get_plan(PLAN_ONE)
        self.assertEqual(caught.exception.code, "plan_projection_invalid")

    def test_tampered_assignment_cursor_is_rejected(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            INSERT INTO reviewer_assignments VALUES(
                ?, 'internal-run-key-2', ?, 2, 'reviewer', 'ops-reviewer',
                'FAILED', 'orchestrator:private', NULL, NULL, NULL,
                'REVIEW_TRANSPORT_FAILED', ?, NULL, NULL, ?
            )
            """,
            ("review-assignment-" + "2" * 32, RUN_TWO, T2, T3),
        )
        connection.commit()
        connection.close()
        _, cursor = self.store.list_assignments(
            limit=1, cursor=None, project_id=None, state=None, run_id=None,
            cursor_secret=self.secret,
        )
        self.assertIsNotNone(cursor)
        with self.assertRaises(ControllerError) as caught:
            self.store.list_assignments(
                limit=1, cursor=cursor + "x", project_id=None, state=None,
                run_id=None, cursor_secret=self.secret,
            )
        self.assertEqual(caught.exception.code, "invalid_cursor")


if __name__ == "__main__":
    unittest.main(verbosity=2)
