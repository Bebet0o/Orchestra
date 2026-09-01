from __future__ import annotations

import hashlib
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

TOKEN = "r" * 64
REVIEW_ID = "review-" + "1" * 32
REVIEW_ID_2 = "review-" + "2" * 32
REVIEW_EXECUTION_ID = "review-execution-" + "3" * 32
REVIEW_EXECUTION_ID_2 = "review-execution-" + "4" * 32
INTEGRATION_ID = "integration-" + "5" * 32
RECOVERY_ID = "recovery-" + "6" * 32
RECOVERY_ID_2 = "recovery-" + "7" * 32
RUN_ID = "run-20260718T103447Z-72aa741491"
RUN_ID_2 = "run-20260718T103500Z-82aa741492"


SCHEMA = """
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
CREATE TABLE roles(
    role_id TEXT PRIMARY KEY,
    profile_name TEXT NOT NULL UNIQUE,
    role_kind TEXT NOT NULL,
    description TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    max_turns INTEGER NOT NULL,
    toolsets_json TEXT NOT NULL,
    skills_json TEXT NOT NULL,
    workspace_mode TEXT NOT NULL,
    may_commit INTEGER NOT NULL,
    may_push INTEGER NOT NULL,
    network_enabled INTEGER NOT NULL,
    cpu_limit INTEGER NOT NULL,
    memory_mb INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    config_source TEXT NOT NULL,
    config_hash TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE runs(
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    recovery_decision TEXT,
    base_commit TEXT,
    result_commit TEXT,
    worktree_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
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
CREATE TABLE worker_executions(execution_id TEXT PRIMARY KEY);
CREATE TABLE objective_queue(objective_id TEXT PRIMARY KEY);
CREATE TABLE objective_attempts(objective_attempt_id TEXT PRIMARY KEY);
CREATE TABLE objective_events(objective_event_id TEXT PRIMARY KEY);
CREATE TABLE orchestration_plans(plan_id TEXT PRIMARY KEY);
CREATE TABLE orchestration_tasks(orchestration_task_id TEXT PRIMARY KEY);
CREATE TABLE orchestration_attempts(attempt_id TEXT PRIMARY KEY);
CREATE TABLE orchestration_dependencies(orchestration_task_id TEXT PRIMARY KEY);
CREATE TABLE review_results(
    review_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT NOT NULL,
    details_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE reviewer_executions(
    execution_id TEXT PRIMARY KEY,
    review_id TEXT UNIQUE,
    task_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    source_profile TEXT NOT NULL,
    runtime_profile TEXT NOT NULL UNIQUE,
    outer_container_name TEXT NOT NULL UNIQUE,
    sandbox_container_id TEXT,
    prompt_path TEXT NOT NULL UNIQUE,
    output_path TEXT NOT NULL UNIQUE,
    workspace_mode TEXT NOT NULL,
    network_enabled INTEGER NOT NULL,
    cpu_limit INTEGER NOT NULL,
    memory_mb INTEGER NOT NULL,
    mount_verified INTEGER NOT NULL,
    isolation_verified INTEGER NOT NULL,
    repository_unchanged INTEGER NOT NULL,
    decision TEXT,
    verdict TEXT,
    exit_code INTEGER,
    result_json TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE integration_executions(
    integration_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    review_id TEXT NOT NULL,
    review_execution_id TEXT NOT NULL,
    controller_owner TEXT NOT NULL,
    decision TEXT NOT NULL,
    verdict TEXT NOT NULL,
    status TEXT NOT NULL,
    base_commit TEXT NOT NULL,
    reviewed_commit TEXT NOT NULL,
    main_before TEXT NOT NULL,
    main_after TEXT NOT NULL,
    snapshot_verified INTEGER NOT NULL,
    review_current INTEGER NOT NULL,
    approval_id TEXT,
    details_json TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE recovery_executions(
    recovery_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    role_id TEXT NOT NULL,
    source_profile TEXT NOT NULL,
    controller_owner TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    observed_status TEXT NOT NULL,
    decision TEXT NOT NULL,
    outcome TEXT NOT NULL,
    evidence_sha256 TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    actions_json TEXT NOT NULL,
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);
"""


class Fixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.database = self.root / "state/controller/orchestra.db"
        self.session = self.root / "secrets/controller-session"
        (self.root / "repo/config/projects.d").mkdir(parents=True)
        config = self.root / "repo/config/projects.d/alpha.toml"
        config.write_text('[git]\ndefault_branch="main"\n', encoding="utf-8")
        self.session.parent.mkdir(parents=True)
        self.session.write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.session, 0o600)
        self.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(SCHEMA)
            now = "2026-07-18T10:40:53.548Z"
            older = "2026-07-18T10:30:53.548Z"
            for project in ("alpha", "beta"):
                connection.execute(
                    "INSERT INTO projects VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        project,
                        project.title(),
                        f"/workspace/{project}",
                        f"/data/{project}",
                        "default",
                        1,
                        str(config),
                        "a" * 32,
                        older,
                        now,
                    ),
                )
            role_values = (
                "reviewer", "ops-reviewer", "reviewer", "Independent reviewer",
                "xhigh", 80, "[]", "[]", "read_only", 0, 0, 0, 2, 4096, 1,
                "/private/roles.toml", "b" * 32, older, now,
            )
            connection.execute(
                "INSERT INTO roles VALUES(" + ",".join("?" for _ in role_values) + ")",
                role_values,
            )
            recovery_role = (
                "recovery", "ops-recovery", "recovery", "Recovery manager",
                "high", 40, "[]", "[]", "controller_only", 0, 0, 0, 2, 4096, 1,
                "/private/roles.toml", "c" * 32, older, now,
            )
            connection.execute(
                "INSERT INTO roles VALUES(" + ",".join("?" for _ in recovery_role) + ")",
                recovery_role,
            )
            for run_id, project, created in (
                (RUN_ID, "alpha", now),
                (RUN_ID_2, "beta", older),
            ):
                connection.execute(
                    "INSERT INTO runs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        run_id, project, "COMPLETED", None, "a" * 40, "b" * 40,
                        f"/private/{run_id}", "{}", created, created, created, created,
                    ),
                )
            connection.execute(
                "INSERT INTO review_results VALUES(?,?,?,?,?,?)",
                (
                    REVIEW_ID, RUN_ID, "PASS",
                    "Independent review passed without exposing /opt/private data",
                    json.dumps({"checks": [], "result_commit": "b" * 40}), now,
                ),
            )
            connection.execute(
                "INSERT INTO review_results VALUES(?,?,?,?,?,?)",
                (
                    REVIEW_ID_2, RUN_ID_2, "FIX", "Changes requested",
                    json.dumps({"checks": []}), older,
                ),
            )
            connection.execute(
                "INSERT INTO reviewer_executions VALUES(" + ",".join("?" for _ in range(26)) + ")",
                (
                    REVIEW_EXECUTION_ID, REVIEW_ID, "task-" + "8" * 32, RUN_ID,
                    "reviewer", "ops-reviewer", "runtime-reviewer-" + "9" * 12,
                    "private-container", "sandbox-secret", "/private/prompt.json",
                    "/private/output.json", "read_only", 0, 2, 4096, 1, 1, 1,
                    "APPROVE", "PASS", 0,
                    json.dumps({"checks": [{"evidence": "/private/evidence"}]}),
                    None, now, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO reviewer_executions VALUES(" + ",".join("?" for _ in range(26)) + ")",
                (
                    REVIEW_EXECUTION_ID_2, REVIEW_ID_2, "task-" + "a" * 32, RUN_ID_2,
                    "reviewer", "ops-reviewer", "runtime-reviewer-" + "a" * 12,
                    "private-container-2", None, "/private/prompt2.json",
                    "/private/output2.json", "read_only", 0, 2, 4096, 1, 1, 1,
                    "REJECT", "FIX", 0, "{}", None, older, older, older,
                ),
            )
            connection.execute(
                "INSERT INTO integration_executions VALUES(" + ",".join("?" for _ in range(20)) + ")",
                (
                    INTEGRATION_ID, RUN_ID, REVIEW_ID, REVIEW_EXECUTION_ID,
                    "controller-private-owner", "APPROVE", "PASS", "COMPLETED",
                    "a" * 40, "b" * 40, "a" * 40, "b" * 40, 1, 1, None,
                    json.dumps({"private_path": "/opt/private"}), None, now, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO recovery_executions VALUES(" + ",".join("?" for _ in range(16)) + ")",
                (
                    RECOVERY_ID, RUN_ID, "recovery", "ops-recovery",
                    "controller-private-owner", "v1", "COMMITTING", "RESUME_SAFE",
                    "RESUMED", "d" * 64,
                    json.dumps({"run": {"path": "/opt/private"}, "snapshot": {}}),
                    json.dumps([{"action": "integration-finalized", "path": "/private"}]),
                    None, now, now, now,
                ),
            )
            connection.execute(
                "INSERT INTO recovery_executions VALUES(" + ",".join("?" for _ in range(16)) + ")",
                (
                    RECOVERY_ID_2, RUN_ID_2, "recovery", "ops-recovery",
                    "controller-private-owner", "v1", "FAILED", "BLOCK_HUMAN",
                    "BLOCKED", "e" * 64, "{}",
                    json.dumps([{"action": "human-approval-created"}]),
                    "private failure /opt/secret", older, older, older,
                ),
            )
            connection.execute("INSERT INTO schema_migrations VALUES(11, ?)", (now,))
            connection.commit()
        self.settings = Settings.from_root(self.root, host="127.0.0.1", port=0)
        self.server = build_server(self.settings)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        path: str,
        *,
        authenticated: bool = True,
        token: str = TOKEN,
    ) -> tuple[int, dict[str, str], dict[str, object]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if authenticated:
            headers["Cookie"] = f"orchestra_session={token}"
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        headers_out = {name: value for name, value in response.getheaders()}
        status = response.status
        connection.close()
        return status, headers_out, payload

    def update(self, sql: str, parameters: tuple[object, ...]) -> None:
        with closing(sqlite3.connect(self.database)) as connection:
            connection.execute(sql, parameters)
            connection.commit()


class ReviewRecoveryReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = Fixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_review_list_requires_authentication(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/reviews", authenticated=False)
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_capabilities_advertise_only_safe_review_recovery_reads(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/system/capabilities")
        self.assertEqual(status, 200)
        features = payload["data"]["features"]
        self.assertTrue(features["review_reads"])
        self.assertTrue(features["review_evidence_reads"])
        self.assertTrue(features["integration_summary_reads"])
        self.assertTrue(features["recovery_reads"])
        self.assertFalse(features["raw_review_artifact_reads"])

    def test_review_list_is_redacted_and_uses_opaque_run_reference(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/reviews?project_id=alpha")
        self.assertEqual(status, 200)
        review = payload["data"][0]
        self.assertEqual(review["id"], REVIEW_ID)
        self.assertRegex(review["run_id"], r"^transaction-[a-f0-9]{32}$")
        encoded = json.dumps(review)
        for forbidden in (RUN_ID, "/private", "container", "prompt", "output", "result_json"):
            self.assertNotIn(forbidden, encoded)
        self.assertEqual(review["summary"], "Review summary redacted.")

    def test_review_detail_has_etag_and_complete_revision(self) -> None:
        status, headers, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["ETag"], f'"{payload["data"]["resource_revision"]}"')
        before = payload["data"]["resource_revision"]
        self.fixture.update(
            "UPDATE integration_executions SET review_current=0 WHERE integration_id=?",
            (INTEGRATION_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertNotEqual(before, payload["data"]["resource_revision"])

    def test_review_evidence_is_metadata_only(self) -> None:
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}/evidence")
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["data"]), 3)
        encoded = json.dumps(payload)
        self.assertNotIn("/private", encoded)
        self.assertNotIn("evidence\"", encoded)
        for item in payload["data"]:
            self.assertFalse(item["available"])
            self.assertFalse(item["raw_content_available"])
            self.assertRegex(item["sha256"], r"^[a-f0-9]{64}$")

    def test_review_state_and_project_filters(self) -> None:
        status, _, approved = self.fixture.request("/api/v1/reviews?state=approved")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in approved["data"]], [REVIEW_ID])
        status, _, rejected = self.fixture.request("/api/v1/reviews?state=rejected")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in rejected["data"]], [REVIEW_ID_2])
        status, _, beta = self.fixture.request("/api/v1/reviews?project_id=beta")
        self.assertEqual(status, 200)
        self.assertEqual(beta["data"][0]["project_id"], "beta")

    def test_review_cursor_is_signed_and_bound_to_filters(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/reviews?limit=1")
        self.assertEqual(status, 200)
        cursor = payload["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        status, _, page = self.fixture.request(f"/api/v1/reviews?limit=1&cursor={cursor}")
        self.assertEqual(status, 200)
        self.assertEqual(page["data"][0]["id"], REVIEW_ID_2)
        status, _, problem = self.fixture.request(
            f"/api/v1/reviews?limit=1&project_id=alpha&cursor={cursor}"
        )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "invalid_cursor")
        status, _, problem = self.fixture.request(f"/api/v1/reviews?cursor={cursor}x")
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "invalid_cursor")

    def test_session_rotation_invalidates_review_cursor(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/reviews?limit=1")
        self.assertEqual(status, 200)
        cursor = payload["meta"]["next_cursor"]
        rotated = "s" * 64
        self.fixture.session.write_text(rotated + "\n", encoding="utf-8")
        status, _, problem = self.fixture.request(
            f"/api/v1/reviews?cursor={cursor}", token=rotated
        )
        self.assertEqual(status, 400)
        self.assertEqual(problem["code"], "invalid_cursor")

    def test_review_query_validation(self) -> None:
        for path, code in (
            ("/api/v1/reviews?limit=0", "invalid_limit"),
            ("/api/v1/reviews?state=unknown", "invalid_state"),
            ("/api/v1/reviews?unexpected=1", "unknown_query_parameter"),
            ("/api/v1/reviews?project_id=../alpha", "invalid_project_id"),
        ):
            status, _, payload = self.fixture.request(path)
            self.assertEqual(status, 400, path)
            self.assertEqual(payload["code"], code, path)

    def test_unknown_and_malformed_review_resources_fail_closed(self) -> None:
        for path in (
            "/api/v1/reviews/not-a-review",
            "/api/v1/reviews/review-" + "f" * 32,
            "/api/v1/reviews/not-a-review/evidence",
        ):
            status, _, payload = self.fixture.request(path)
            self.assertEqual(status, 404)
            self.assertEqual(payload["code"], "review_not_found")

    def test_reviewer_role_mismatch_fails_closed(self) -> None:
        self.fixture.update(
            "UPDATE reviewer_executions SET role_id='recovery' WHERE review_id=?",
            (REVIEW_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")

    def test_reviewer_profile_mismatch_fails_closed(self) -> None:
        self.fixture.update(
            "UPDATE reviewer_executions SET source_profile='ops-recovery' WHERE review_id=?",
            (REVIEW_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")

    def test_reviewer_network_policy_mismatch_fails_closed(self) -> None:
        self.fixture.update(
            "UPDATE reviewer_executions SET network_enabled=1 WHERE review_id=?",
            (REVIEW_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")

    def test_integration_link_mismatch_fails_closed(self) -> None:
        self.fixture.update(
            "UPDATE integration_executions SET run_id=? WHERE integration_id=?",
            (RUN_ID_2, INTEGRATION_ID),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")

    def test_malformed_review_json_fails_closed_without_exposure(self) -> None:
        self.fixture.update(
            "UPDATE review_results SET details_json=? WHERE review_id=?",
            ('{"secret":"/opt/private"', REVIEW_ID),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")
        self.assertNotIn("/opt/private", json.dumps(payload))

    def test_recovery_list_detail_and_redaction(self) -> None:
        status, _, payload = self.fixture.request("/api/v1/recoveries?project_id=alpha")
        self.assertEqual(status, 200)
        recovery = payload["data"][0]
        self.assertEqual(recovery["id"], RECOVERY_ID)
        self.assertEqual(recovery["state"], "resumed")
        self.assertRegex(recovery["run_id"], r"^transaction-[a-f0-9]{32}$")
        encoded = json.dumps(recovery)
        self.assertNotIn(RUN_ID, encoded)
        self.assertNotIn("/opt/private", encoded)
        self.assertEqual(recovery["actions"]["types"], ["integration-finalized"])
        status, headers, detail = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 200)
        self.assertIn("ETag", headers)
        self.assertEqual(detail["data"]["id"], RECOVERY_ID)

    def test_recovery_state_filter_and_cursor(self) -> None:
        status, _, blocked = self.fixture.request("/api/v1/recoveries?state=blocked")
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in blocked["data"]], [RECOVERY_ID_2])
        status, _, first = self.fixture.request("/api/v1/recoveries?limit=1")
        self.assertEqual(status, 200)
        cursor = first["meta"]["next_cursor"]
        status, _, second = self.fixture.request(f"/api/v1/recoveries?limit=1&cursor={cursor}")
        self.assertEqual(status, 200)
        self.assertEqual(second["data"][0]["id"], RECOVERY_ID_2)

    def test_recovery_query_validation(self) -> None:
        for path, code in (
            ("/api/v1/recoveries?limit=201", "invalid_limit"),
            ("/api/v1/recoveries?state=unknown", "invalid_state"),
            ("/api/v1/recoveries?unexpected=1", "unknown_query_parameter"),
        ):
            status, _, payload = self.fixture.request(path)
            self.assertEqual(status, 400, path)
            self.assertEqual(payload["code"], code, path)

    def test_unknown_and_malformed_recovery_resources_fail_closed(self) -> None:
        for path in (
            "/api/v1/recoveries/not-a-recovery",
            "/api/v1/recoveries/recovery-" + "f" * 32,
        ):
            status, _, payload = self.fixture.request(path)
            self.assertEqual(status, 404)
            self.assertEqual(payload["code"], "recovery_not_found")

    def test_recovery_role_mismatch_fails_closed(self) -> None:
        self.fixture.update(
            "UPDATE recovery_executions SET role_id='reviewer' WHERE recovery_id=?",
            (RECOVERY_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "recovery_projection_invalid")

    def test_recovery_evidence_digest_and_json_fail_closed(self) -> None:
        self.fixture.update(
            "UPDATE recovery_executions SET evidence_sha256='bad' WHERE recovery_id=?",
            (RECOVERY_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "recovery_projection_invalid")
        self.fixture.update(
            "UPDATE recovery_executions SET evidence_sha256=?, evidence_json=? WHERE recovery_id=?",
            ("d" * 64, "[1,2,3]", RECOVERY_ID),
        )
        status, _, payload = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "recovery_projection_invalid")

    def test_missing_review_table_maps_to_database_unavailable(self) -> None:
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute("DROP TABLE integration_executions")
            connection.commit()
        status, _, payload = self.fixture.request("/api/v1/reviews")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "database_unavailable")

    def test_reads_do_not_modify_review_recovery_tables(self) -> None:
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            before = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "review_results", "reviewer_executions",
                    "integration_executions", "recovery_executions",
                )
            }
        for path in (
            "/api/v1/reviews",
            f"/api/v1/reviews/{REVIEW_ID}",
            f"/api/v1/reviews/{REVIEW_ID}/evidence",
            "/api/v1/recoveries",
            f"/api/v1/recoveries/{RECOVERY_ID}",
        ):
            self.assertEqual(self.fixture.request(path)[0], 200)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            after = {
                table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in before
            }
        self.assertEqual(before, after)


    def test_public_transaction_reference_matches_execution_projection(self) -> None:
        expected = "transaction-" + hashlib.sha256(
            b"hermesops-transaction-reference-v1\0" + RUN_ID.encode("utf-8")
        ).hexdigest()[:32]
        status, _, review = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(review["data"]["run_id"], expected)
        status, _, recovery = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(recovery["data"]["run_id"], expected)

    def test_review_summary_redacts_internal_run_reference(self) -> None:
        self.fixture.update(
            "UPDATE review_results SET summary=? WHERE review_id=?",
            (f"Reviewed internal transaction {RUN_ID}", REVIEW_ID),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["summary"], "Review summary redacted.")
        self.assertNotIn(RUN_ID, json.dumps(payload))

    def test_sensitive_field_names_are_not_projected(self) -> None:
        self.fixture.update(
            "UPDATE review_results SET details_json=? WHERE review_id=?",
            (json.dumps({"api_key": "redacted", "prompt_path": "/private", "safe_field": 1}), REVIEW_ID),
        )
        status, _, review = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(review["data"]["details"]["fields"], ["safe_field"])
        self.fixture.update(
            "UPDATE recovery_executions SET evidence_json=? WHERE recovery_id=?",
            (json.dumps({"private_key": "redacted", "container_id": "private", "safe_field": 1}), RECOVERY_ID),
        )
        status, _, recovery = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(recovery["data"]["evidence"]["fields"], ["safe_field"])

    def test_nested_json_payload_remains_metadata_only(self) -> None:
        payload: object = "private-leaf"
        for _ in range(20):
            payload = {"level": payload}
        self.fixture.update(
            "UPDATE review_results SET details_json=? WHERE review_id=?",
            (json.dumps(payload), REVIEW_ID),
        )
        status, _, response = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(response["data"]["details"]["fields"], ["level"])
        self.assertNotIn("private-leaf", json.dumps(response))

    def test_oversized_json_fails_closed(self) -> None:
        oversized = json.dumps({"safe_field": "x" * (70 * 1024)})
        self.fixture.update(
            "UPDATE review_results SET details_json=? WHERE review_id=?",
            (oversized, REVIEW_ID),
        )
        status, _, problem = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "review_projection_invalid")
        self.assertNotIn("x" * 100, json.dumps(problem))

    def test_historical_reviewer_resources_are_not_reinterpreted(self) -> None:
        self.fixture.update(
            "UPDATE reviewer_executions SET cpu_limit=3, memory_mb=6144 WHERE review_id=?",
            (REVIEW_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["reviewer"]["cpu_limit"], 3)
        self.assertEqual(payload["data"]["reviewer"]["memory_mb"], 6144)

    def test_current_reviewer_mutation_policy_drift_does_not_hide_history(self) -> None:
        self.fixture.update(
            "UPDATE roles SET may_push=1, may_commit=1 WHERE role_id='reviewer'",
            (),
        )
        status, _, payload = self.fixture.request(f"/api/v1/reviews/{REVIEW_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["id"], REVIEW_ID)

    def test_current_recovery_network_policy_drift_does_not_hide_history(self) -> None:
        self.fixture.update(
            "UPDATE roles SET network_enabled=1 WHERE role_id='recovery'",
            (),
        )
        status, _, payload = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["id"], RECOVERY_ID)

    def test_recovery_decision_outcome_mismatch_fails_closed(self) -> None:
        self.fixture.update(
            "UPDATE recovery_executions SET decision='ROLLBACK_SAFE', outcome='RESUMED' WHERE recovery_id=?",
            (RECOVERY_ID,),
        )
        status, _, payload = self.fixture.request(f"/api/v1/recoveries/{RECOVERY_ID}")
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "recovery_projection_invalid")

    def test_filtered_review_pagination_preserves_continuation(self) -> None:
        created = "2026-07-19T12:00:00.000Z"
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.executemany(
                "INSERT INTO review_results VALUES(?,?,?,?,?,?)",
                [
                    (
                        f"review-{value:032x}", RUN_ID, "PASS", "Safe summary", "{}", created
                    )
                    for value in range(0x1000, 0x1000 + 1001)
                ],
            )
            connection.commit()
        status, _, first = self.fixture.request("/api/v1/reviews?state=rejected&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(first["data"], [])
        cursor = first["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        status, _, second = self.fixture.request(
            f"/api/v1/reviews?state=rejected&limit=1&cursor={cursor}"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in second["data"]], [REVIEW_ID_2])

    def test_filtered_recovery_pagination_preserves_continuation(self) -> None:
        created = "2026-07-19T12:00:00.000Z"
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.executemany(
                "INSERT INTO recovery_executions VALUES(" + ",".join("?" for _ in range(16)) + ")",
                [
                    (
                        f"recovery-{value:032x}", RUN_ID, "recovery", "ops-recovery",
                        "controller-owner", "v1", "RUNNING", "RESUME_SAFE",
                        "ASSESSED", "a" * 64, "{}", "[]", None, created, created, created,
                    )
                    for value in range(0x2000, 0x2000 + 1001)
                ],
            )
            connection.commit()
        status, _, first = self.fixture.request("/api/v1/recoveries?state=blocked&limit=1")
        self.assertEqual(status, 200)
        self.assertEqual(first["data"], [])
        cursor = first["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)
        status, _, second = self.fixture.request(
            f"/api/v1/recoveries?state=blocked&limit=1&cursor={cursor}"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in second["data"]], [RECOVERY_ID_2])



if __name__ == "__main__":
    unittest.main(verbosity=2)
