from __future__ import annotations

import base64
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

from controller_api.core import Settings
from controller_api.server import build_server

TOKEN = "b" * 64


class Fixture:
    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "root"
        self.db = self.root / "state/controller/orchestra.db"
        self.session = self.root / "secrets/controller-session"
        (self.root / "repo/config/projects.d").mkdir(parents=True)
        self.session.parent.mkdir(parents=True)
        self.session.write_text(TOKEN + "\n", encoding="ascii")
        os.chmod(self.session, 0o600)
        self.db.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.db)) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
                CREATE TABLE projects(
                    project_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    repo_path TEXT NOT NULL UNIQUE,
                    data_path TEXT NOT NULL UNIQUE,
                    policy_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    config_source TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
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
                CREATE TABLE roles (
                    role_id TEXT PRIMARY KEY,
                    profile_name TEXT NOT NULL,
                    workspace_mode TEXT NOT NULL
                );
                CREATE TABLE runs (
                    run_id TEXT PRIMARY KEY,
                    project_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    heartbeat_at TEXT
                );
                CREATE TABLE events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT,
                    run_id TEXT,
                    task_id TEXT,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE worker_executions (
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
                CREATE TABLE review_results (
                    review_id TEXT PRIMARY KEY
                );
                CREATE TABLE reviewer_executions (
                    execution_id TEXT PRIMARY KEY
                );
                CREATE TABLE integration_executions (
                    integration_id TEXT PRIMARY KEY
                );
                CREATE TABLE recovery_executions (
                    recovery_id TEXT PRIMARY KEY
                );
                CREATE TABLE orchestration_tasks (
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
                CREATE TABLE orchestration_attempts (
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
                CREATE TABLE orchestration_dependencies (
                    plan_id TEXT NOT NULL,
                    orchestration_task_id TEXT NOT NULL,
                    depends_on_task_id TEXT NOT NULL,
                    dependency_condition TEXT NOT NULL
                );
                INSERT INTO schema_migrations VALUES(10, '2026-07-19T00:00:00.000Z');
                """
            )
            for project in ("alpha", "beta"):
                config = self.root / f"repo/config/projects.d/{project}.toml"
                config.write_text('[git]\ndefault_branch="main"\n', encoding="utf-8")
                connection.execute(
                    "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        project,
                        project.title(),
                        f"/repo/{project}",
                        f"/data/{project}",
                        "default",
                        1,
                        str(config),
                        "0" * 64,
                        "2026-07-19T00:00:00.000Z",
                        "2026-07-19T00:00:00.000Z",
                    ),
                )
            connection.executemany(
                "INSERT INTO orchestration_plans VALUES(?,?)",
                [("plan-ready", "READY"), ("plan-blocked", "BLOCKED")],
            )
            statuses = [
                ("0", "QUEUED", None, '["alpha"]'),
                ("1", "QUEUED", "plan-ready", '["alpha","beta"]'),
                ("2", "PLANNING", None, '["beta"]'),
                ("3", "RUNNING", None, '["alpha"]'),
                ("4", "RUNNING", "plan-blocked", '["alpha"]'),
                ("5", "PAUSE_REQUESTED", None, '["alpha"]'),
                ("6", "PAUSED", None, '["alpha"]'),
                ("7", "CANCEL_REQUESTED", None, '["alpha"]'),
                ("8", "COMPLETED", None, '["alpha"]'),
                ("9", "FAILED", None, '["alpha"]'),
                ("a", "CANCELLED", None, '["alpha"]'),
            ]
            for index, (suffix, status, plan_id, scope) in enumerate(statuses):
                identifier = "objective-" + suffix * 32
                created = f"2026-07-19T00:{index:02d}:00.000Z"
                connection.execute(
                    """
                    INSERT INTO objective_queue VALUES(
                        ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                    )
                    """,
                    (
                        identifier,
                        f"Title {suffix}\nDetailed objective {suffix}",
                        "AI",
                        status,
                        index,
                        created,
                        scope,
                        2,
                        3,
                        1,
                        plan_id,
                        None,
                        created,
                        created if status not in {"QUEUED", "PLANNING"} else None,
                        created,
                        created if status in {"COMPLETED", "FAILED", "CANCELLED"} else None,
                        created if status == "PAUSED" else None,
                        "private failure detail" if status == "FAILED" else None,
                    ),
                )
                connection.execute(
                    "INSERT INTO objective_events VALUES(?,?,?,?,?,?,?)",
                    (
                        "objective-event-" + suffix * 32,
                        identifier,
                        "STATE",
                        None,
                        status,
                        '{"secret":"not projected"}',
                        created,
                    ),
                )
            objective = "objective-" + "2" * 32
            operation = "objective-attempt-" + "c" * 32
            connection.execute(
                """
                INSERT INTO objective_attempts VALUES(
                    ?,?,1,'FAILED',NULL,NULL,NULL,?, ?, ?, ?, ?, NULL
                )
                """,
                (
                    operation,
                    objective,
                    '{"private":"result"}',
                    "private failure reason",
                    "2026-07-19T01:00:00.000Z",
                    "2026-07-19T01:01:00.000Z",
                    "2026-07-19T01:02:00.000Z",
                ),
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

    def request(self, path: str, *, authenticated: bool = True):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Cookie": f"orchestra_session={TOKEN}"} if authenticated else {}
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read()
        headers_out = {k.lower(): v for k, v in response.getheaders()}
        connection.close()
        return response.status, headers_out, json.loads(raw) if raw else None


class ObjectiveReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_objective_list_requires_authentication(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/objectives", authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_all_runtime_states_have_total_projection(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/objectives?limit=50")
        self.assertEqual(status, 200)
        pairs = [(item["raw_state"], item["state"]) for item in payload["data"]]
        self.assertEqual(
            {state for raw, state in pairs if raw == "QUEUED"},
            {"draft", "planned"},
        )
        expected = {
            "PLANNING": "planning",
            "PAUSE_REQUESTED": "running",
            "PAUSED": "paused",
            "CANCEL_REQUESTED": "running",
            "COMPLETED": "succeeded",
            "FAILED": "failed",
            "CANCELLED": "cancelled",
        }
        for raw, projected in expected.items():
            self.assertIn((raw, projected), pairs)
        self.assertIn(("RUNNING", "running"), pairs)
        self.assertIn(("RUNNING", "blocked"), pairs)

    def test_payload_matches_contract_and_redacts_errors(self) -> None:
        objective = "objective-" + "9" * 32
        status, headers, payload = self.fixture.request(f"/api/v1/objectives/{objective}")
        self.assertEqual(status, 200)
        self.assertIn("etag", headers)
        item = payload["data"]
        for key in (
            "id", "created_at", "updated_at", "resource_revision", "state",
            "title", "description", "priority", "project_ids", "not_before",
            "max_parallel_tasks", "planning_max_attempts",
        ):
            self.assertIn(key, item)
        self.assertTrue(item["has_error"])
        serialized = json.dumps(item)
        self.assertNotIn("private failure detail", serialized)
        self.assertLess(item["resource_revision"], 2**53)

    def test_project_filter_and_nested_route_are_equivalent(self) -> None:
        _, _, global_payload = self.fixture.request(
            "/api/v1/objectives?project_id=beta&limit=50"
        )
        _, _, nested_payload = self.fixture.request(
            "/api/v1/projects/beta/objectives?limit=50"
        )
        self.assertEqual(
            [item["id"] for item in global_payload["data"]],
            [item["id"] for item in nested_payload["data"]],
        )
        self.assertTrue(global_payload["data"])
        self.assertTrue(all("beta" in item["project_ids"] for item in global_payload["data"]))

    def test_state_filter_and_opaque_cursor(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/objectives?limit=2")
        self.assertEqual(status, 200)
        cursor = payload["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        self.assertNotIn("objective-", cursor)
        status, _, second = self.fixture.request(
            f"/api/v1/objectives?limit=2&cursor={cursor}"
        )
        self.assertEqual(status, 200)
        self.assertFalse(
            {item["id"] for item in payload["data"]}
            & {item["id"] for item in second["data"]}
        )
        status, _, mismatch = self.fixture.request(
            f"/api/v1/objectives?limit=2&state=failed&cursor={cursor}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(mismatch["code"], "invalid_cursor")

    def test_unknown_project_and_invalid_state_are_rejected(self) -> None:
        status, _, payload = self.fixture.request(
            "/api/v1/projects/missing/objectives"
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "project_not_found")
        status, _, payload = self.fixture.request(
            "/api/v1/objectives?state=unknown"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_objective_state")

    def test_operation_is_safe_legacy_projection(self) -> None:
        operation = "objective-attempt-" + "c" * 32
        status, headers, payload = self.fixture.request(
            f"/api/v1/operations/{operation}"
        )
        self.assertEqual(status, 200)
        self.assertIn("etag", headers)
        item = payload["data"]
        self.assertEqual(item["kind"], "objective.planning_attempt")
        self.assertEqual(item["state"], "failed")
        self.assertTrue(item["legacy_projection"])
        self.assertTrue(item["result"]["legacy_payload_redacted"])
        self.assertTrue(item["error"]["legacy_payload_redacted"])
        serialized = json.dumps(item)
        self.assertNotIn("private failure reason", serialized)
        self.assertNotIn("private", serialized)

    def test_objective_detail_discovers_operations(self) -> None:
        objective = "objective-" + "2" * 32
        operation = "objective-attempt-" + "c" * 32
        status, _, payload = self.fixture.request(f"/api/v1/objectives/{objective}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation_ids"], [operation])
        self.assertEqual(payload["data"]["latest_operation_id"], operation)

    def test_malformed_scope_fails_closed_without_leaking_raw_value(self) -> None:
        objective = "objective-" + "0" * 32
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "UPDATE objective_queue SET project_scope_json = ? WHERE objective_id = ?",
                ('{"secret":"value"}', objective),
            )
            connection.commit()
        status, _, payload = self.fixture.request(f"/api/v1/objectives/{objective}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "objective_projection_invalid")
        self.assertNotIn("secret", json.dumps(payload))


    def test_latest_operation_is_selected_by_attempt_number(self) -> None:
        objective = "objective-" + "2" * 32
        first = "objective-attempt-" + "c" * 32
        second = "objective-attempt-" + "a" * 32
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                """
                INSERT INTO objective_attempts VALUES(
                    ?,?,2,'RUNNING','executor-new',NULL,NULL,'{}',
                    NULL,?,?,NULL,NULL
                )
                """,
                (
                    second,
                    objective,
                    "2026-07-19T02:00:00.000Z",
                    "2026-07-19T02:01:00.000Z",
                ),
            )
            connection.commit()

        status, _, listing = self.fixture.request(
            "/api/v1/objectives?limit=50"
        )
        self.assertEqual(status, 200)
        listed = next(
            item for item in listing["data"] if item["id"] == objective
        )
        self.assertEqual(listed["attempt_count"], 2)
        self.assertEqual(listed["latest_operation_id"], second)

        status, _, detail = self.fixture.request(
            f"/api/v1/objectives/{objective}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(
            detail["data"]["operation_ids"],
            [first, second],
        )
        self.assertEqual(detail["data"]["latest_operation_id"], second)

    def test_state_filters_match_projected_transition_and_plan_state(self) -> None:
        pause_requested = "objective-" + "5" * 32
        queued = "objective-" + "0" * 32
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                "INSERT INTO orchestration_plans VALUES(?,?)",
                ("plan-blocked-transition", "BLOCKED"),
            )
            connection.execute(
                "UPDATE objective_queue SET plan_id = ? WHERE objective_id = ?",
                ("plan-blocked-transition", pause_requested),
            )
            # Simulate a damaged legacy reference. Projection follows the
            # joined plan, not merely a non-null foreign-key value.
            connection.execute(
                "UPDATE objective_queue SET plan_id = ? WHERE objective_id = ?",
                ("missing-plan", queued),
            )
            connection.commit()

        status, _, detail = self.fixture.request(
            f"/api/v1/objectives/{pause_requested}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(detail["data"]["state"], "running")
        self.assertEqual(detail["data"]["requested_transition"], "pause")

        status, _, running = self.fixture.request(
            "/api/v1/objectives?state=running&limit=50"
        )
        self.assertEqual(status, 200)
        self.assertIn(
            pause_requested,
            {item["id"] for item in running["data"]},
        )

        status, _, queued_detail = self.fixture.request(
            f"/api/v1/objectives/{queued}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(queued_detail["data"]["state"], "draft")

        _, _, drafts = self.fixture.request(
            "/api/v1/objectives?state=draft&limit=50"
        )
        _, _, planned = self.fixture.request(
            "/api/v1/objectives?state=planned&limit=50"
        )
        self.assertIn(queued, {item["id"] for item in drafts["data"]})
        self.assertNotIn(queued, {item["id"] for item in planned["data"]})

    def test_unsigned_and_tampered_cursors_are_rejected(self) -> None:
        status, _, first = self.fixture.request(
            "/api/v1/objectives?limit=2"
        )
        self.assertEqual(status, 200)
        cursor = first["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)

        last = first["data"][-1]
        forged_payload = json.dumps(
            {
                "v": 1,
                "c": last["created_at"],
                "i": last["id"],
                "p": None,
                "s": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        forged = base64.urlsafe_b64encode(
            forged_payload
        ).rstrip(b"=").decode("ascii")
        status, _, payload = self.fixture.request(
            f"/api/v1/objectives?limit=2&cursor={forged}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_cursor")

        decoded = bytearray(
            base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        )
        decoded[-1] ^= 1
        tampered = base64.urlsafe_b64encode(
            decoded
        ).rstrip(b"=").decode("ascii")
        status, _, payload = self.fixture.request(
            f"/api/v1/objectives?limit=2&cursor={tampered}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_cursor")

    def test_etags_cover_all_projected_mutable_fields(self) -> None:
        objective = "objective-" + "3" * 32
        status, headers, before = self.fixture.request(
            f"/api/v1/objectives/{objective}"
        )
        self.assertEqual(status, 200)
        before_etag = headers["etag"]
        before_heartbeat = before["data"]["updated_at"]

        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                """
                UPDATE objective_queue
                SET objective = ?, priority = ?
                WHERE objective_id = ?
                """,
                ("Changed title\nChanged details", 999, objective),
            )
            connection.commit()

        status, headers, after = self.fixture.request(
            f"/api/v1/objectives/{objective}"
        )
        self.assertEqual(status, 200)
        self.assertEqual(after["data"]["updated_at"], before_heartbeat)
        self.assertNotEqual(headers["etag"], before_etag)
        self.assertNotEqual(
            after["data"]["resource_revision"],
            before["data"]["resource_revision"],
        )

        operation = "objective-attempt-" + "c" * 32
        status, headers, before_operation = self.fixture.request(
            f"/api/v1/operations/{operation}"
        )
        self.assertEqual(status, 200)
        operation_etag = headers["etag"]
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                """
                UPDATE objective_attempts
                SET executor_instance_id = ?
                WHERE objective_attempt_id = ?
                """,
                ("executor-updated", operation),
            )
            connection.commit()
        status, headers, after_operation = self.fixture.request(
            f"/api/v1/operations/{operation}"
        )
        self.assertEqual(status, 200)
        self.assertNotEqual(headers["etag"], operation_etag)
        self.assertNotEqual(
            after_operation["data"]["resource_revision"],
            before_operation["data"]["resource_revision"],
        )

    def test_project_filter_survives_unrelated_malformed_scope(self) -> None:
        malformed = "objective-" + "0" * 32
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                """
                UPDATE objective_queue
                SET project_scope_json = ?
                WHERE objective_id = ?
                """,
                ('{"private":"malformed"}', malformed),
            )
            connection.commit()

        status, _, payload = self.fixture.request(
            "/api/v1/projects/beta/objectives?limit=50"
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"])
        self.assertNotIn("private", json.dumps(payload))
        self.assertTrue(
            all("beta" in item["project_ids"] for item in payload["data"])
        )

    def test_invalid_legacy_identifiers_fail_closed(self) -> None:
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                """
                INSERT INTO objective_queue VALUES(
                    ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
                )
                """,
                (
                    "legacy/private-objective",
                    "Private objective",
                    "AI",
                    "QUEUED",
                    1,
                    "2026-07-19T03:00:00.000Z",
                    '["alpha"]',
                    1,
                    3,
                    0,
                    None,
                    None,
                    "2026-07-19T03:00:00.000Z",
                    None,
                    "2026-07-19T03:00:00.000Z",
                    None,
                    None,
                    None,
                ),
            )
            connection.commit()

        status, _, payload = self.fixture.request(
            "/api/v1/objectives?limit=50"
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "objective_projection_invalid")
        self.assertNotIn("legacy/private-objective", json.dumps(payload))

        operation = "objective-attempt-" + "c" * 32
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute(
                """
                UPDATE objective_attempts
                SET objective_id = ?
                WHERE objective_attempt_id = ?
                """,
                ("private-objective-target", operation),
            )
            connection.commit()
        status, _, payload = self.fixture.request(
            f"/api/v1/operations/{operation}"
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "operation_projection_invalid")
        self.assertNotIn("private-objective-target", json.dumps(payload))

    def test_missing_objective_table_maps_to_database_unavailable(self) -> None:
        with closing(sqlite3.connect(self.fixture.db)) as connection:
            connection.execute("DROP TABLE objective_events")
            connection.commit()
        status, _, payload = self.fixture.request(
            "/api/v1/objectives?limit=1"
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "database_unavailable")

    def test_reads_do_not_modify_objective_tables(self) -> None:
        def counts():
            with closing(sqlite3.connect(self.fixture.db)) as connection:
                return tuple(
                    connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                    for table in ("objective_queue", "objective_attempts", "objective_events")
                )
        before = counts()
        self.fixture.request("/api/v1/objectives?limit=5")
        self.fixture.request("/api/v1/objectives/" + "2" * 32)
        self.fixture.request("/api/v1/operations/objective-attempt-" + "c" * 32)
        self.assertEqual(counts(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
