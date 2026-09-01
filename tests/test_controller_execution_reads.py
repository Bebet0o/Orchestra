from __future__ import annotations

import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

from controller_api.core import Settings
from controller_api.server import build_server

TOKEN = "e" * 64
OBJECTIVE_ID = "objective-" + "1" * 32
PLAN_ID = "plan-" + "1" * 32
TASK_ONE = "orchestration-task-" + "1" * 32
TASK_TWO = "orchestration-task-" + "2" * 32
TASK_THREE = "orchestration-task-" + "3" * 32
RUN_ONE = "orchestration-attempt-" + "a" * 32
RUN_TWO = "orchestration-attempt-" + "b" * 32
RUN_THREE = "orchestration-attempt-" + "c" * 32
LEGACY_ONE = "11111111-1111-4111-8111-111111111111"
LEGACY_TWO = "22222222-2222-4222-8222-222222222222"
WORKER_ONE = "execution-" + "1" * 32
IMAGE_DIGEST = "sha256:" + "a" * 64
IMAGE_REFERENCE = "registry.example.com/team/worker@" + IMAGE_DIGEST


SCHEMA = """
CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
CREATE TABLE projects(
    project_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    data_path TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    config_source TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE roles(
    role_id TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL,
    workspace_mode TEXT NOT NULL
);
CREATE TABLE runs(
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT
);
CREATE TABLE tasks(
    task_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    description TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    heartbeat_at TEXT
);
CREATE TABLE events(
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id TEXT,
    run_id TEXT,
    task_id TEXT,
    event_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE worker_executions(
    execution_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    source_profile TEXT NOT NULL,
    runtime_profile TEXT NOT NULL,
    outer_container_name TEXT NOT NULL,
    sandbox_container_id TEXT,
    prompt_path TEXT NOT NULL,
    output_path TEXT NOT NULL,
    workspace_mode TEXT NOT NULL,
    network_enabled INTEGER NOT NULL,
    cpu_limit INTEGER NOT NULL,
    memory_mb INTEGER NOT NULL,
    mount_verified INTEGER NOT NULL,
    isolation_verified INTEGER NOT NULL,
    exit_code INTEGER,
    result_json TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE orchestration_plans(plan_id TEXT PRIMARY KEY, status TEXT NOT NULL);
CREATE TABLE objective_queue(
    objective_id TEXT PRIMARY KEY,
    objective TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    priority INTEGER NOT NULL,
    not_before TEXT NOT NULL,
    project_scope_json TEXT NOT NULL,
    max_parallel_tasks INTEGER NOT NULL,
    planning_max_attempts INTEGER NOT NULL,
    planning_attempt_count INTEGER NOT NULL,
    plan_id TEXT,
    planner_execution_id TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    paused_at TEXT,
    last_error TEXT
);
CREATE TABLE objective_attempts(
    objective_attempt_id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    attempt_number INTEGER NOT NULL,
    status TEXT NOT NULL,
    executor_instance_id TEXT,
    planner_execution_id TEXT,
    plan_id TEXT,
    result_json TEXT NOT NULL,
    failure_reason TEXT,
    started_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    finished_at TEXT,
    next_attempt_at TEXT
);
CREATE TABLE objective_events(
    objective_event_id TEXT PRIMARY KEY,
    objective_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    old_status TEXT,
    new_status TEXT,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE review_results (review_id TEXT PRIMARY KEY);
CREATE TABLE reviewer_executions (execution_id TEXT PRIMARY KEY);
CREATE TABLE integration_executions (integration_id TEXT PRIMARY KEY);
CREATE TABLE recovery_executions (recovery_id TEXT PRIMARY KEY);
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
"""


class Fixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"
        (self.root / "repo/config/projects.d").mkdir(parents=True)
        config = self.root / "repo/config/projects.d/alpha.toml"
        config.write_text('[git]\ndefault_branch="main"\n', encoding="utf-8")
        secrets = self.root / "secrets"
        secrets.mkdir()
        os.chmod(secrets, 0o700)
        self.session = secrets / "controller-session"
        self.session.write_text(TOKEN + "\n", encoding="ascii")
        os.chmod(self.session, 0o600)
        self.database = self.root / "state/controller/orchestra.db"
        self.database.parent.mkdir(parents=True)
        self.private_log = self.root / "private-worker-output.log"
        self.private_log.write_text("ULTRA_PRIVATE_WORKER_OUTPUT\n", encoding="utf-8")
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(SCHEMA)
            connection.execute(
                "INSERT INTO schema_migrations VALUES(10, ?)",
                ("2026-07-19T00:00:00.000Z",),
            )
            connection.execute(
                "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "alpha", "Alpha", "/host/repository/alpha", "/host/data/alpha",
                    "default", 1, str(config), "0" * 64,
                    "2026-07-19T00:00:00.000Z", "2026-07-19T00:00:00.000Z",
                ),
            )
            connection.execute(
                "INSERT INTO roles VALUES(?,?,?)",
                ("worker_docs", "ops-worker-docs", "write"),
            )
            connection.execute(
                "INSERT INTO orchestration_plans VALUES(?,?)",
                (PLAN_ID, "COMPLETED"),
            )
            connection.execute(
                """INSERT INTO objective_queue VALUES(
                    ?,?,'AI','COMPLETED',100,?,'["alpha"]',2,3,1,?,NULL,?,?,?, ?,NULL,NULL
                )""",
                (
                    OBJECTIVE_ID,
                    "Build the read model",
                    "2026-07-19T00:00:00.000Z",
                    PLAN_ID,
                    "2026-07-19T00:00:00.000Z",
                    "2026-07-19T00:00:00.000Z",
                    "2026-07-19T00:20:00.000Z",
                    "2026-07-19T00:20:00.000Z",
                ),
            )
            rows = [
                (
                    TASK_ONE, PLAN_ID, "implement_read_model", "PIPELINE", "alpha",
                    "worker_docs", "COMPLETED", 10,
                    "Private instruction one", '["criterion"]', "MARKER", 3, 2,
                    '{"private":"task result"}', None,
                    "2026-07-19T00:01:00.000Z", "2026-07-19T00:02:00.000Z",
                    "2026-07-19T00:10:00.000Z", "2026-07-19T00:10:00.000Z",
                ),
                (
                    TASK_TWO, PLAN_ID, "review_runtime", "PIPELINE", "alpha",
                    "worker_docs", "RUNNING", 20,
                    "Private instruction two", '["criterion"]', "MARKER", 2, 1,
                    '{}', None,
                    "2026-07-19T00:02:00.000Z", "2026-07-19T00:11:00.000Z",
                    "2026-07-19T00:12:00.000Z", None,
                ),
                (
                    TASK_THREE, PLAN_ID, "final_noop", "NOOP", None,
                    None, "PENDING", 30,
                    "Private instruction three", '[]', None, 1, 0,
                    '{}', None,
                    "2026-07-19T00:03:00.000Z", None, None, None,
                ),
            ]
            connection.executemany(
                "INSERT INTO orchestration_tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
            connection.execute(
                "INSERT INTO orchestration_dependencies VALUES(?,?,?,'SUCCESS')",
                (PLAN_ID, TASK_TWO, TASK_ONE),
            )
            connection.executemany(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        LEGACY_ONE, "alpha", "FAILED",
                        "2026-07-19T00:02:00.000Z", "2026-07-19T00:02:00.000Z",
                        "2026-07-19T00:04:00.000Z", "2026-07-19T00:04:00.000Z",
                    ),
                    (
                        LEGACY_TWO, "alpha", "COMPLETED",
                        "2026-07-19T00:05:00.000Z", "2026-07-19T00:05:00.000Z",
                        "2026-07-19T00:10:00.000Z", "2026-07-19T00:10:00.000Z",
                    ),
                ],
            )
            connection.execute(
                """INSERT INTO worker_executions VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )""",
                (
                    WORKER_ONE, "task-" + "9" * 32, LEGACY_TWO,
                    "worker_docs", "ops-worker-docs", "runtime-worker-private",
                    "private-container-name", "abcdef123456", "/host/private/prompt",
                    str(self.private_log), "write", 0, 2, 4096, 1, 1, 0,
                    json.dumps(
                        {
                            "sandbox_image_reference": IMAGE_REFERENCE,
                            "sandbox_image_digest": IMAGE_DIGEST,
                            "secret": "hidden",
                        }
                    ),
                    None, "2026-07-19T00:05:00.000Z",
                    "2026-07-19T00:05:00.000Z", "2026-07-19T00:10:00.000Z",
                ),
            )
            connection.executemany(
                "INSERT INTO orchestration_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        RUN_ONE, TASK_ONE, 1, "FAILED", "orchestrator-instance-private",
                        LEGACY_ONE, None, None, None, '{}', "PRIVATE ATTEMPT FAILURE",
                        "2026-07-19T00:02:00.000Z", "2026-07-19T00:04:00.000Z",
                        "2026-07-19T00:04:00.000Z",
                    ),
                    (
                        RUN_TWO, TASK_ONE, 2, "COMPLETED", "orchestrator-instance-private",
                        LEGACY_TWO, WORKER_ONE, "review-private", "integration-private",
                        '{"private":"attempt result"}', None,
                        "2026-07-19T00:05:00.000Z", "2026-07-19T00:10:00.000Z",
                        "2026-07-19T00:10:00.000Z",
                    ),
                    (
                        RUN_THREE, TASK_TWO, 1, "RUNNING", "orchestrator-instance-private",
                        None, None, None, None, '{}', None,
                        "2026-07-19T00:11:00.000Z", "2026-07-19T00:12:00.000Z", None,
                    ),
                ],
            )
            connection.executemany(
                "INSERT INTO events(project_id,run_id,task_id,event_type,severity,payload_json,created_at) VALUES(?,?,?,?,?,?,?)",
                [
                    (
                        "alpha", LEGACY_TWO, "task-" + "9" * 32,
                        "WORKER_RESERVED", "INFO", '{"secret":"one"}',
                        "2026-07-19T00:05:00.000Z",
                    ),
                    (
                        "alpha", LEGACY_TWO, "task-" + "9" * 32,
                        "WORKER_PROGRESS", "DEBUG", '{"secret":"two"}',
                        "2026-07-19T00:06:00.000Z",
                    ),
                    (
                        "alpha", LEGACY_TWO, "task-" + "9" * 32,
                        "WORKER_COMPLETED", "INFO", '{"secret":"three"}',
                        "2026-07-19T00:10:00.000Z",
                    ),
                ],
            )
            connection.commit()
        settings = Settings.from_root(self.root, host="127.0.0.1", port=0)
        self.server = build_server(settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(
        self,
        path: str,
        *,
        authenticated: bool = True,
        token: str = TOKEN,
    ) -> tuple[int, dict[str, str], dict[str, object] | None]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Cookie": f"orchestra_session={token}"} if authenticated else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        return response.status, response_headers, json.loads(raw) if raw else None

    def execute(self, sql: str, parameters: tuple[object, ...] = ()) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(sql, parameters)
            connection.commit()

    def scalar(self, sql: str) -> object:
        with closing(sqlite3.connect(self.database)) as connection:
            return connection.execute(sql).fetchone()[0]


class ExecutionReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def assert_no_sensitive_values(self, payload: object) -> None:
        serialized = json.dumps(payload, sort_keys=True)
        for forbidden in (
            "/host/", "ULTRA_PRIVATE_WORKER_OUTPUT", "PRIVATE ATTEMPT FAILURE",
            "private-container-name", "orchestrator-instance-private",
            '"secret"', "Private instruction",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_capabilities_advertise_only_safe_execution_reads(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/system/capabilities")
        self.assertEqual(status, 200)
        features = payload["data"]["features"]
        self.assertTrue(features["task_reads"])
        self.assertTrue(features["run_reads"])
        self.assertTrue(features["worker_execution_reads"])
        self.assertTrue(features["persisted_event_log_reads"])
        self.assertFalse(features["raw_worker_log_reads"])
        self.assertFalse(features["run_artifact_reads"])

    def test_registered_underscore_role_alias_is_projected(self) -> None:
        status, _, task = self.fixture.request(f"/api/v1/tasks/{TASK_ONE}")
        self.assertEqual(status, 200)
        self.assertEqual(task["data"]["role_id"], "worker_docs")
        self.assertTrue(task["data"]["writer"])

        status, _, run = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 200)
        self.assertEqual(run["data"]["role_id"], "worker_docs")
        self.assertEqual(run["data"]["sandbox_profile_id"], "ops-worker-docs")

    def test_unregistered_role_alias_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE orchestration_tasks SET role_id = 'ghost_role' "
            "WHERE orchestration_task_id = ?",
            (TASK_ONE,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/tasks/{TASK_ONE}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "task_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_task_list_requires_authentication(self) -> None:
        status, _, payload = self.fixture.request(
            f"/api/v1/objectives/{OBJECTIVE_ID}/tasks",
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_task_list_matches_contract_and_state_projection(self) -> None:
        status, _, payload = self.fixture.request(
            f"/api/v1/objectives/{OBJECTIVE_ID}/tasks?limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["data"]], [TASK_ONE, TASK_TWO, TASK_THREE])
        states = {item["id"]: item["state"] for item in payload["data"]}
        self.assertEqual(states[TASK_ONE], "succeeded")
        self.assertEqual(states[TASK_TWO], "running")
        self.assertEqual(states[TASK_THREE], "pending")
        for item in payload["data"]:
            self.assertTrue({
                "id", "created_at", "updated_at", "resource_revision", "state",
                "objective_id", "title", "role_id", "writer",
            }.issubset(item))
        self.assert_no_sensitive_values(payload)

    def test_task_detail_has_complete_etag_and_safe_projection(self) -> None:
        path = f"/api/v1/tasks/{TASK_ONE}"
        status, headers, payload = self.fixture.request(path)
        self.assertEqual(status, 200)
        self.assertEqual(headers["etag"], f'"{payload["data"]["resource_revision"]}"')
        previous = headers["etag"]
        self.fixture.execute(
            "UPDATE orchestration_tasks SET priority = priority + 1 WHERE orchestration_task_id = ?",
            (TASK_ONE,),
        )
        status, headers, payload = self.fixture.request(path)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["etag"], previous)
        self.assert_no_sensitive_values(payload)

    def test_task_cursor_is_signed_bound_and_session_rotation_invalidates_it(self) -> None:
        path = f"/api/v1/objectives/{OBJECTIVE_ID}/tasks?limit=1"
        status, _, payload = self.fixture.request(path)
        self.assertEqual(status, 200)
        cursor = payload["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        status, _, second = self.fixture.request(path + "&cursor=" + quote(cursor, safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(second["data"][0]["id"], TASK_TWO)
        tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
        status, _, problem = self.fixture.request(path + "&cursor=" + quote(tampered, safe=""))
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "invalid_cursor")
        new_token = "f" * 64
        self.fixture.session.write_text(new_token + "\n", encoding="ascii")
        status, _, problem = self.fixture.request(
            path + "&cursor=" + quote(cursor, safe=""),
            token=new_token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "invalid_cursor")

    def test_unknown_and_malformed_task_resources_fail_closed(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/tasks/not-a-task")
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_task_id")
        unknown = "orchestration-task-" + "f" * 32
        status, _, payload = self.fixture.request(f"/api/v1/tasks/{unknown}")
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "task_not_found")

    def test_run_list_orders_attempts_and_projects_worker_metadata_safely(self) -> None:
        status, _, payload = self.fixture.request(f"/api/v1/tasks/{TASK_ONE}/runs?limit=10")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in payload["data"]], [RUN_TWO, RUN_ONE])
        latest = payload["data"][0]
        self.assertEqual(latest["state"], "succeeded")
        self.assertEqual(latest["sandbox_image_digest"], IMAGE_DIGEST)
        self.assertRegex(
            latest["transaction_run_id"],
            r"^transaction-[a-f0-9]{32}$",
        )
        self.assertNotEqual(latest["transaction_run_id"], LEGACY_TWO)
        worker = latest["worker_execution"]
        self.assertEqual(worker["id"], WORKER_ONE)
        self.assertFalse(worker["network_enabled"])
        self.assertTrue(worker["mount_verified"])
        self.assertTrue(worker["isolation_verified"])
        self.assert_no_sensitive_values(payload)

    def test_opaque_internal_transaction_key_resolves_logs(self) -> None:
        status, _, run = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 200)
        reference = run["data"]["transaction_run_id"]
        self.assertRegex(reference, r"^transaction-[a-f0-9]{32}$")
        self.assertNotEqual(reference, LEGACY_TWO)

        status, _, logs = self.fixture.request(
            f"/api/v1/runs/{RUN_TWO}/logs?limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual(len(logs["data"]["entries"]), 3)
        self.assert_no_sensitive_values(logs)

    def test_missing_linked_transaction_fails_closed(self) -> None:
        self.fixture.execute(
            "DELETE FROM runs WHERE run_id = ?",
            (LEGACY_TWO,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_worker_transaction_mismatch_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE worker_executions SET run_id = ? WHERE execution_id = ?",
            (LEGACY_ONE, WORKER_ONE),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_control_character_transaction_key_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE orchestration_attempts SET run_id = ? WHERE attempt_id = ?",
            ("bad\ntransaction", RUN_TWO),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_running_attempt_without_transaction_is_projected(self) -> None:
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_THREE}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["state"], "running")
        self.assertIsNone(payload["data"]["transaction_run_id"])
        self.assertIsNone(payload["data"]["worker_execution"])

    def test_run_detail_etag_covers_worker_fields(self) -> None:
        path = f"/api/v1/runs/{RUN_TWO}"
        status, headers, payload = self.fixture.request(path)
        self.assertEqual(status, 200)
        first = headers["etag"]
        self.fixture.execute(
            "UPDATE worker_executions SET cpu_limit = 3 WHERE execution_id = ?",
            (WORKER_ONE,),
        )
        status, headers, payload = self.fixture.request(path)
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["etag"], first)
        self.assertEqual(payload["data"]["worker_execution"]["cpu_limit"], 3)

    def test_worker_role_mismatch_fails_closed(self) -> None:
        self.fixture.execute(
            "INSERT INTO roles VALUES('worker_other','ops-worker-other','read')"
        )
        self.fixture.execute(
            "UPDATE worker_executions SET role_id = 'worker_other' "
            "WHERE execution_id = ?",
            (WORKER_ONE,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_worker_profile_mismatch_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE worker_executions SET source_profile = 'ops-worker-other' "
            "WHERE execution_id = ?",
            (WORKER_ONE,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_worker_workspace_mismatch_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE worker_executions SET workspace_mode = 'read' "
            "WHERE execution_id = ?",
            (WORKER_ONE,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_transaction_project_mismatch_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE runs SET project_id = 'other' WHERE run_id = ?",
            (LEGACY_TWO,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_malformed_worker_numeric_value_maps_to_projection_error(self) -> None:
        self.fixture.execute(
            "UPDATE worker_executions SET cpu_limit = 'PRIVATE_NUMERIC' "
            "WHERE execution_id = ?",
            (WORKER_ONE,),
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_event_timestamp_path_fails_closed_without_exposure(self) -> None:
        self.fixture.execute(
            "UPDATE events SET created_at = '/host/private/timestamp' "
            "WHERE event_id = 1"
        )
        status, _, problem = self.fixture.request(f"/api/v1/runs/{RUN_TWO}/logs")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "log_projection_invalid")
        self.assert_no_sensitive_values(problem)

    def test_run_cursor_is_signed_and_bound_to_task(self) -> None:
        path = f"/api/v1/tasks/{TASK_ONE}/runs?limit=1"
        status, _, payload = self.fixture.request(path)
        self.assertEqual(status, 200)
        cursor = payload["meta"]["next_cursor"]
        status, _, page = self.fixture.request(path + "&cursor=" + quote(cursor, safe=""))
        self.assertEqual(status, 200)
        self.assertEqual(page["data"][0]["id"], RUN_ONE)
        status, _, problem = self.fixture.request(
            f"/api/v1/tasks/{TASK_TWO}/runs?limit=1&cursor=" + quote(cursor, safe="")
        )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "invalid_cursor")

    def test_logs_are_bounded_structured_and_payloads_are_redacted(self) -> None:
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}/logs?limit=2")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]["entries"]), 2)
        self.assertTrue(payload["data"]["truncated"])
        self.assertEqual(payload["data"]["next_sequence"], 2)
        self.assertEqual(payload["meta"]["snapshot_sequence"], 3)
        for entry in payload["data"]["entries"]:
            self.assertTrue(entry["payload_available"])
            self.assertTrue(entry["payload_redacted"])
            self.assertTrue(entry["payload_valid"])
            self.assertNotIn("payload", entry)
        self.assert_no_sensitive_values(payload)

    def test_log_continuation_uses_after_sequence(self) -> None:
        status, _, payload = self.fixture.request(
            f"/api/v1/runs/{RUN_TWO}/logs?after_sequence=2&limit=10"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["sequence"] for item in payload["data"]["entries"]], [3])
        self.assertFalse(payload["data"]["truncated"])
        self.assertIsNone(payload["data"]["next_sequence"])

    def test_run_without_legacy_transaction_has_empty_logs(self) -> None:
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_THREE}/logs")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["entries"], [])
        self.assertIsNone(payload["meta"]["snapshot_sequence"])

    def test_log_query_rejects_invalid_bounds_and_unknown_fields(self) -> None:
        for suffix, code in (
            ("?after_sequence=-1", "invalid_after_sequence"),
            ("?limit=501", "invalid_limit"),
            ("?raw=true", "unknown_query_parameter"),
        ):
            status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}/logs{suffix}")
            self.assertEqual(status, 400)
            self.assertEqual(payload["code"], code)

    def test_malformed_persisted_event_fails_closed(self) -> None:
        self.fixture.execute(
            "UPDATE events SET severity = 'PRIVATE' WHERE event_id = 1"
        )
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}/logs")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "log_projection_invalid")
        self.assert_no_sensitive_values(payload)

    def test_invalid_legacy_identifiers_fail_closed(self) -> None:
        self.fixture.execute(
            "UPDATE orchestration_attempts SET worker_execution_id = 'bad-worker' WHERE attempt_id = ?",
            (RUN_TWO,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(payload)

    def test_missing_execution_table_maps_to_database_unavailable(self) -> None:
        self.fixture.execute("DROP TABLE events")
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}/logs")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "database_unavailable")


    def test_task_runtime_states_have_total_projection(self) -> None:
        expected = {
            "PENDING": "pending",
            "READY": "ready",
            "RUNNING": "running",
            "BLOCKED": "blocked",
            "COMPLETED": "succeeded",
            "FAILED": "failed",
            "CANCELLED": "cancelled",
        }
        for raw, public in expected.items():
            with self.subTest(raw=raw):
                self.fixture.execute(
                    "UPDATE orchestration_tasks SET status = ? WHERE orchestration_task_id = ?",
                    (raw, TASK_ONE),
                )
                status, _, payload = self.fixture.request(f"/api/v1/tasks/{TASK_ONE}")
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["state"], public)

    def test_running_transaction_states_have_total_projection(self) -> None:
        expected = {
            "QUEUED": "created",
            "SNAPSHOTTING": "preparing",
            "RUNNING": "running",
            "REVIEWING": "waiting_review",
            "WAITING_HUMAN": "waiting_integration",
            "COMMITTING": "waiting_integration",
            "COMPLETED": "succeeded",
            "FAILED": "interrupted",
            "CANCELLED": "cancelled",
            "RECOVERING": "recovery_required",
        }
        self.fixture.execute(
            "UPDATE orchestration_attempts SET status = 'RUNNING' WHERE attempt_id = ?",
            (RUN_TWO,),
        )
        for raw, public in expected.items():
            with self.subTest(raw=raw):
                self.fixture.execute(
                    "UPDATE runs SET status = ? WHERE run_id = ?",
                    (raw, LEGACY_TWO),
                )
                status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["state"], public)

    def test_ambiguous_objective_plan_link_fails_closed(self) -> None:
        other = "objective-" + "2" * 32
        self.fixture.execute(
            """INSERT INTO objective_queue VALUES(
                ?,?,'AI','COMPLETED',100,?,'["alpha"]',2,3,1,?,NULL,?,?,?, ?,NULL,NULL
            )""",
            (
                other,
                "Ambiguous objective",
                "2026-07-19T00:00:00.000Z",
                PLAN_ID,
                "2026-07-19T00:00:01.000Z",
                "2026-07-19T00:00:01.000Z",
                "2026-07-19T00:20:01.000Z",
                "2026-07-19T00:20:01.000Z",
            ),
        )
        status, _, payload = self.fixture.request(f"/api/v1/tasks/{TASK_ONE}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "task_projection_invalid")
        self.assert_no_sensitive_values(payload)

    def test_malformed_event_payload_is_flagged_without_exposure(self) -> None:
        self.fixture.execute(
            "UPDATE events SET payload_json = 'not-json-PRIVATE' WHERE event_id = 1"
        )
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}/logs")
        self.assertEqual(status, 200)
        first = payload["data"]["entries"][0]
        self.assertTrue(first["payload_available"])
        self.assertTrue(first["payload_redacted"])
        self.assertFalse(first["payload_valid"])
        self.assertNotIn("not-json-PRIVATE", json.dumps(payload))

    def test_profile_and_project_path_values_fail_closed(self) -> None:
        self.fixture.execute(
            "UPDATE worker_executions SET source_profile = '/host/private/profile' WHERE execution_id = ?",
            (WORKER_ONE,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/runs/{RUN_TWO}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "run_projection_invalid")
        self.assert_no_sensitive_values(payload)

    def test_reads_do_not_modify_execution_tables(self) -> None:
        tables = (
            "orchestration_tasks", "orchestration_attempts", "worker_executions",
            "runs", "events",
        )
        before = {table: self.fixture.scalar(f"SELECT COUNT(*) FROM {table}") for table in tables}
        for path in (
            f"/api/v1/objectives/{OBJECTIVE_ID}/tasks",
            f"/api/v1/tasks/{TASK_ONE}",
            f"/api/v1/tasks/{TASK_ONE}/runs",
            f"/api/v1/runs/{RUN_TWO}",
            f"/api/v1/runs/{RUN_TWO}/logs",
        ):
            status, _, _ = self.fixture.request(path)
            self.assertEqual(status, 200)
        after = {table: self.fixture.scalar(f"SELECT COUNT(*) FROM {table}") for table in tables}
        self.assertEqual(after, before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
