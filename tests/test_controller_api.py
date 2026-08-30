from __future__ import annotations

import http.client
import json
import os
import sqlite3
import tempfile
import socket
import threading
import unittest
from contextlib import closing
from pathlib import Path

from controller_api.core import (
    ControllerError,
    ReadOnlyDatabase,
    Settings,
)
from controller_api.server import build_server

TOKEN = "a" * 64


class APIFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.database = (
            self.root / "state" / "controller" / "hermesops.db"
        )
        self.session_file = (
            self.root / "secrets" / "controller-session"
        )
        self.project_config = (
            self.root
            / "repo"
            / "config"
            / "projects.d"
            / "alpha.toml"
        )
        (self.root / "repo").mkdir(parents=True)
        (self.root / "repo" / "VERSION").write_text(
            "0.1.0-alpha\n",
            encoding="utf-8",
        )
        self.project_config.parent.mkdir(parents=True)
        self.project_config.write_text(
            """
schema_version = 1
[git]
default_branch = "main"
""".lstrip(),
            encoding="utf-8",
        )
        self.session_file.parent.mkdir(parents=True)
        self.session_file.write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.session_file, 0o600)
        self.database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(self.database)) as connection:
            connection.executescript(
                """
                CREATE TABLE schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                );
                CREATE TABLE controller_operations (
                    operation_id TEXT PRIMARY KEY, command_kind TEXT NOT NULL,
                    state TEXT NOT NULL, target_type TEXT NOT NULL,
                    target_id TEXT NOT NULL, result_json TEXT NOT NULL,
                    error_code TEXT, created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE controller_idempotency (
                    session_fingerprint TEXT NOT NULL, key_hash TEXT NOT NULL,
                    method TEXT NOT NULL, route TEXT NOT NULL, request_hash TEXT NOT NULL,
                    response_status INTEGER, response_json TEXT, operation_id TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY(session_fingerprint, key_hash)
                );
                CREATE TABLE controller_command_audit (
                    audit_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                    actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                    session_fingerprint TEXT NOT NULL, idempotency_key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL, outcome TEXT NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE projects (
                    project_id TEXT PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    repo_path TEXT NOT NULL UNIQUE,
                    data_path TEXT NOT NULL UNIQUE,
                    policy_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL,
                    config_source TEXT NOT NULL,
                    config_hash TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    default_branch TEXT NOT NULL,
                    sandbox_profile_id TEXT,
                    archived INTEGER NOT NULL,
                    repository_mode TEXT NOT NULL,
                    resource_revision INTEGER NOT NULL
                );
                CREATE TABLE project_locks (
                    project_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    holder TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL
                );
                CREATE TABLE controller_project_operations (
                    operation_id TEXT PRIMARY KEY, command_kind TEXT NOT NULL,
                    state TEXT NOT NULL, target_id TEXT NOT NULL,
                    result_json TEXT NOT NULL, error_code TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE controller_project_idempotency (
                    session_fingerprint TEXT NOT NULL, key_hash TEXT NOT NULL,
                    method TEXT NOT NULL, route TEXT NOT NULL, request_hash TEXT NOT NULL,
                    response_status INTEGER, response_json TEXT, operation_id TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY(session_fingerprint, key_hash)
                );
                CREATE TABLE controller_project_command_audit (
                    audit_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                    actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                    session_fingerprint TEXT NOT NULL, idempotency_key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL, outcome TEXT NOT NULL,
                    reason_present INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE orchestration_plans (
                    plan_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE objective_queue (
                    objective_id TEXT PRIMARY KEY,
                    objective TEXT NOT NULL,
                    source TEXT NOT NULL CHECK (source IN ('AI','DECLARATIVE','TEST')),
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
                CREATE TABLE objective_attempts (
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
                CREATE TABLE objective_events (
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
                -- HermesOps milestone 2I: durable Controller event journal.
                -- The legacy events and objective_events tables remain untouched.

                CREATE TABLE controller_event_journal (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL UNIQUE CHECK (
                        length(event_id) = 36
                        AND substr(event_id, 1, 4) = 'evt_'
                        AND substr(event_id, 5) NOT GLOB '*[^0-9a-f]*'
                    ),
                    schema_version INTEGER NOT NULL CHECK (schema_version = 1),
                    event_type TEXT NOT NULL CHECK (
                        length(event_type) BETWEEN 3 AND 128
                        AND event_type = lower(event_type)
                        AND substr(event_type, 1, 1) GLOB '[a-z]'
                        AND substr(event_type, -1, 1) GLOB '[a-z0-9_]'
                        AND instr(event_type, '.') > 1
                        AND instr(event_type, '..') = 0
                        AND event_type NOT GLOB '*[^a-z0-9_.]*'
                    ),
                    occurred_at TEXT NOT NULL CHECK (
                        length(occurred_at) BETWEEN 20 AND 40
                        AND substr(occurred_at, -1, 1) = 'Z'
                    ),
                    actor_type TEXT NOT NULL CHECK (
                        actor_type IN ('operator', 'system', 'agent', 'worker')
                    ),
                    actor_id TEXT NOT NULL CHECK (
                        length(actor_id) BETWEEN 1 AND 200
                    ),
                    aggregate_type TEXT NOT NULL CHECK (
                        aggregate_type IN (
                            'system', 'project', 'objective', 'task', 'run', 'review',
                            'recovery', 'sandbox', 'sandbox_build', 'backup',
                            'notification', 'confirmation', 'audit'
                        )
                    ),
                    aggregate_id TEXT NOT NULL CHECK (
                        length(aggregate_id) BETWEEN 1 AND 200
                    ),
                    aggregate_revision INTEGER NOT NULL CHECK (aggregate_revision >= 1),
                    project_id TEXT CHECK (
                        project_id IS NULL OR (
                            length(project_id) BETWEEN 1 AND 200
                        )
                    ),
                    objective_id TEXT CHECK (
                        objective_id IS NULL OR (
                            length(objective_id) BETWEEN 1 AND 200
                        )
                    ),
                    correlation_id TEXT NOT NULL CHECK (
                        length(correlation_id) = 37
                        AND substr(correlation_id, 1, 5) = 'corr_'
                        AND substr(correlation_id, 6) NOT GLOB '*[^0-9a-f]*'
                    ),
                    causation_id TEXT CHECK (
                        causation_id IS NULL OR (
                            length(causation_id) BETWEEN 1 AND 200
                        )
                    ),
                    redacted_data_json TEXT NOT NULL CHECK (
                        length(redacted_data_json) <= 16384
                        AND json_valid(redacted_data_json)
                        AND json_type(redacted_data_json) = 'object'
                    ),
                    UNIQUE (aggregate_type, aggregate_id, aggregate_revision)
                );

                CREATE TRIGGER controller_event_journal_immutable_update
                BEFORE UPDATE ON controller_event_journal
                BEGIN
                    SELECT RAISE(ABORT, 'controller event journal is immutable');
                END;

                CREATE TRIGGER controller_event_journal_immutable_delete
                BEFORE DELETE ON controller_event_journal
                BEGIN
                    SELECT RAISE(ABORT, 'controller event journal is immutable');
                END;

                CREATE INDEX idx_controller_event_journal_project_sequence
                    ON controller_event_journal(project_id, sequence);

                CREATE INDEX idx_controller_event_journal_objective_sequence
                    ON controller_event_journal(objective_id, sequence);

                CREATE INDEX idx_controller_event_journal_aggregate_revision
                    ON controller_event_journal(
                        aggregate_type, aggregate_id, aggregate_revision
                    );

                CREATE INDEX idx_controller_event_journal_correlation
                    ON controller_event_journal(correlation_id, sequence);

                PRAGMA user_version = 21;
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
                    review_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE controller_review_operations (
                    operation_id TEXT PRIMARY KEY, command_kind TEXT NOT NULL,
                    state TEXT NOT NULL, target_id TEXT NOT NULL,
                    result_json TEXT NOT NULL, error_code TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, finished_at TEXT
                );
                CREATE TABLE controller_review_idempotency (
                    session_fingerprint TEXT NOT NULL, key_hash TEXT NOT NULL,
                    method TEXT NOT NULL, route TEXT NOT NULL, request_hash TEXT NOT NULL,
                    response_status INTEGER, response_json TEXT, operation_id TEXT,
                    created_at TEXT NOT NULL, completed_at TEXT,
                    PRIMARY KEY(session_fingerprint, key_hash)
                );
                CREATE TABLE controller_review_command_audit (
                    audit_id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
                    actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, action TEXT NOT NULL,
                    resource_type TEXT NOT NULL, resource_id TEXT NOT NULL,
                    session_fingerprint TEXT NOT NULL, idempotency_key_hash TEXT NOT NULL,
                    request_hash TEXT NOT NULL, outcome TEXT NOT NULL,
                    reason_present INTEGER NOT NULL, created_at TEXT NOT NULL
                );
                CREATE TABLE controller_review_actions (
                    action_id TEXT PRIMARY KEY, review_id TEXT NOT NULL,
                    run_id TEXT NOT NULL, command TEXT NOT NULL,
                    reason_present INTEGER NOT NULL, status TEXT NOT NULL,
                    created_at TEXT NOT NULL, UNIQUE(review_id)
                );
                CREATE TABLE reviewer_executions (
                    execution_id TEXT PRIMARY KEY
                );
                CREATE TABLE integration_executions (
                    integration_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    decision TEXT,
                    status TEXT
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
                INSERT INTO schema_migrations VALUES (
                    1, '2026-07-18T00:00:00.000Z'
                );
                """
            )
            connection.execute(
                """
                INSERT INTO projects VALUES (
                    'alpha',
                    'Alpha Project',
                    '/workspace/alpha',
                    '/data/alpha',
                    'default',
                    1,
                    ?,
                    '0123456789abcdef0123456789abcdef',
                    '2026-07-18T00:00:00.000Z',
                    '2026-07-18T01:00:00.000Z',
                    'main',
                    NULL,
                    0,
                    'existing',
                    1
                )
                """,
                (str(self.project_config),),
            )
            connection.execute(
                """
                INSERT INTO runs VALUES (
                    'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'alpha', 'COMPLETED',
                    '2026-07-18T00:00:00.000Z',
                    '2026-07-18T00:01:00.000Z',
                    '2026-07-18T00:02:00.000Z',
                    '2026-07-18T00:02:00.000Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO review_results VALUES (
                    'review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'PASS_WITH_DEBT', 'Debt remains', '{}',
                    '2026-07-18T00:02:00.000Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO review_results VALUES (
                    'review-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
                    'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'FIX', 'Fix required', '{}',
                    '2026-07-18T00:03:00.000Z'
                )
                """
            )
            connection.commit()
        with closing(sqlite3.connect(self.database)) as migration_connection:
            migration_connection.executescript(
                (
                    Path(__file__).resolve().parents[1]
                    / "migrations/020_sandbox_profile_persistence.sql"
                ).read_text(encoding="utf-8")
            )
            migration_connection.execute(
                "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(21, ?)",
                ("2026-07-27T00:00:00.000Z",),
            )
            migration_connection.execute("PRAGMA user_version = 21")
            migration_connection.executescript(
                (
                    Path(__file__).resolve().parents[1]
                    / "migrations/022_hermesfile_lifecycle.sql"
                ).read_text(encoding="utf-8")
            )
            migration_connection.executescript(
                (
                    Path(__file__).resolve().parents[1]
                    / "migrations/023_blueprint_migration.sql"
                ).read_text(encoding="utf-8")
            )
            migration_connection.commit()

        self.settings = Settings.from_root(
            self.root,
            host="127.0.0.1",
            port=0,
        )
        self.server = build_server(self.settings)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = False,
        request_id: str | None = None,
        headers_override: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object] | None]:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=5,
        )
        headers: dict[str, str] = {}
        if authenticated:
            headers["Cookie"] = f"hermesops_session={TOKEN}"
        if request_id:
            headers["X-Request-ID"] = request_id
        if headers_override:
            headers.update(headers_override)
        connection.request(
            method,
            path,
            body=body,
            headers=headers,
        )
        response = connection.getresponse()
        raw = response.read()
        response_headers = {
            key.lower(): value
            for key, value in response.getheaders()
        }
        connection.close()
        payload = json.loads(raw) if raw else None
        return response.status, response_headers, payload


class ControllerAPITest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = APIFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_non_loopback_bind_is_rejected(self) -> None:
        settings = Settings.from_root(
            self.fixture.root,
            host="0.0.0.0",
            port=8765,
        )
        with self.assertRaises(ControllerError) as context:
            settings.validate_bind()
        self.assertEqual(
            context.exception.code,
            "non_loopback_bind_forbidden",
        )

    def test_database_connection_is_read_only(self) -> None:
        database = ReadOnlyDatabase(self.fixture.settings)
        with closing(database.connect()) as connection:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute(
                    "INSERT INTO projects VALUES "
                    "('x','x','x','y','z',1,'s','h','a','b')"
                )

    def test_health_is_public_and_has_request_id(self) -> None:
        status, headers, payload = self.fixture.request(
            "GET",
            "/health",
            request_id="request-12345678",
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-request-id"], "request-12345678")
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["version"], "0.1.0-alpha")

    def test_ready_checks_database_and_auth_configuration(self) -> None:
        status, _, payload = self.fixture.request("GET", "/ready")
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "ready")

    def test_capabilities_advertise_objective_reads(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/system/capabilities",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        features = payload["data"]["features"]
        self.assertEqual(payload["data"]["blueprint_versions"], ["v1"])
        self.assertNotIn("hermesfile_versions", payload["data"])
        self.assertTrue(features["objective_reads"])
        self.assertTrue(features["operation_reads"])
        self.assertTrue(features["legacy_operation_projection"])
        self.assertTrue(features["durable_controller_operations"])
        self.assertTrue(features["objective_writes"])
        self.assertTrue(features["review_writes"])
        self.assertEqual(
            features["review_write_commands"],
            ["acknowledge-debt", "request-human-review"],
        )
        self.assertFalse(features["review_rerun"])

    def test_protected_endpoint_requires_cookie(self) -> None:
        status, headers, payload = self.fixture.request(
            "GET",
            "/api/v1/projects",
        )
        self.assertEqual(status, 401)
        self.assertTrue(
            headers["content-type"].startswith(
                "application/problem+json"
            )
        )
        self.assertEqual(payload["code"], "authentication_required")

    def test_project_list_matches_contract_shape(self) -> None:
        status, headers, payload = self.fixture.request(
            "GET",
            "/api/v1/projects?limit=50",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertEqual(len(payload["data"]), 1)
        project = payload["data"][0]
        self.assertEqual(project["id"], "alpha")
        self.assertEqual(project["slug"], "alpha")
        self.assertEqual(project["default_branch"], "main")
        self.assertEqual(project["state"], "enabled")
        self.assertIsInstance(project["resource_revision"], int)
        self.assertIn("request_id", payload["meta"])

    def test_project_detail_has_etag(self) -> None:
        status, headers, payload = self.fixture.request(
            "GET",
            "/api/v1/projects/alpha",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        self.assertIn("etag", headers)
        self.assertEqual(payload["data"]["name"], "Alpha Project")

    def test_unknown_project_returns_problem_json(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects/missing",
            authenticated=True,
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "project_not_found")
        self.assertEqual(
            payload["resource"],
            {"type": "project", "id": "missing"},
        )

    def test_invalid_limit_is_rejected(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects?limit=1000",
            authenticated=True,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_limit")

    def test_destructive_write_methods_are_disabled(self) -> None:
        before = self.fixture.database.read_bytes()
        status, headers, payload = self.fixture.request(
            "DELETE",
            "/api/v1/projects/alpha",
            authenticated=True,
        )
        after = self.fixture.database.read_bytes()
        self.assertEqual(status, 405)
        self.assertEqual(headers["allow"], "GET, HEAD")
        self.assertEqual(payload["code"], "method_not_allowed")
        self.assertEqual(before, after)

    def test_capability_flags_do_not_claim_unimplemented_features(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/system/capabilities",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        features = payload["data"]["features"]
        self.assertFalse(features["read_only_controller_api"])
        self.assertTrue(features["project_writes"])
        self.assertEqual(
            features["project_write_commands"],
            ["create", "update", "enable", "disable", "rescan", "archive"],
        )
        self.assertFalse(features["project_delete"])
        self.assertTrue(features["websocket_events"])
        self.assertFalse(features["blueprint_builds"])
        self.assertNotIn("hermesfile_builds", features)
        self.assertNotIn("hermesfile_revision_history", features)
        self.assertNotIn("hermesfile_revision_comparison", features)
        self.assertNotIn("hermesfile_runtime_projection", features)


    def test_project_payload_does_not_leak_host_paths(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects/alpha",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        project = payload["data"]
        self.assertNotIn("repo_path", project)
        self.assertNotIn("data_path", project)

    def test_duplicate_session_cookie_is_rejected(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects",
            headers_override={
                "Cookie": (
                    f"hermesops_session={TOKEN}; "
                    f"hermesops_session={TOKEN}"
                )
            },
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_percent_encoded_cookie_is_rejected(self) -> None:
        encoded = "%61" + TOKEN[1:]
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects",
            headers_override={
                "Cookie": f"hermesops_session={encoded}"
            },
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_session_symlink_is_rejected(self) -> None:
        real_file = self.fixture.session_file.with_name("real-session")
        self.fixture.session_file.rename(real_file)
        self.fixture.session_file.symlink_to(real_file)
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects",
            headers_override={
                "Cookie": f"hermesops_session={TOKEN}"
            },
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "controller_auth_unavailable")

    def test_unknown_query_parameter_is_rejected(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects?unexpected=1",
            authenticated=True,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_query_parameter")

    def test_excessive_query_fields_are_rejected(self) -> None:
        query = "&".join(f"x{i}=1" for i in range(9))
        status, _, payload = self.fixture.request(
            "GET",
            f"/api/v1/projects?{query}",
            authenticated=True,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_query")

    def test_invalid_host_is_rejected(self) -> None:
        status, _, payload = self.fixture.request(
            "GET",
            "/health",
            headers_override={"Host": "attacker.example"},
        )
        self.assertEqual(status, 421)
        self.assertEqual(payload["code"], "misdirected_request")

    def test_security_headers_are_present(self) -> None:
        status, headers, _ = self.fixture.request("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(
            headers["cross-origin-resource-policy"],
            "same-origin",
        )
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", headers["content-security-policy"])

    def test_get_body_is_rejected_and_connection_closed(self) -> None:
        status, headers, payload = self.fixture.request(
            "GET",
            "/health",
            body=b"unexpected",
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "request_body_not_allowed")
        self.assertEqual(headers["connection"], "close")

    def test_write_method_with_body_closes_connection(self) -> None:
        status, headers, payload = self.fixture.request(
            "PUT",
            "/api/v1/projects",
            body=b'{"ignored":true}',
        )
        self.assertEqual(status, 405)
        self.assertEqual(payload["code"], "method_not_allowed")
        self.assertEqual(headers["connection"], "close")

    def test_trace_is_json_405_and_closes_connection(self) -> None:
        status, headers, payload = self.fixture.request(
            "TRACE",
            "/api/v1/projects",
        )
        self.assertEqual(status, 405)
        self.assertEqual(payload["code"], "method_not_allowed")
        self.assertEqual(headers["connection"], "close")

    def test_readiness_does_not_run_full_integrity_scan(self) -> None:
        statements: list[str] = []
        database = ReadOnlyDatabase(self.fixture.settings)
        original_connect = database.connect

        def traced_connect() -> sqlite3.Connection:
            connection = original_connect()
            connection.set_trace_callback(statements.append)
            return connection

        database.connect = traced_connect  # type: ignore[method-assign]
        ready, reason = database.readiness()
        self.assertTrue(ready, reason)
        self.assertFalse(
            any("quick_check" in sql.lower() for sql in statements)
        )

    def test_database_failure_is_reported_as_503(self) -> None:
        def broken_connect() -> sqlite3.Connection:
            raise sqlite3.OperationalError("synthetic failure")

        self.fixture.server.service.database.connect = broken_connect  # type: ignore[method-assign]
        status, _, payload = self.fixture.request(
            "GET",
            "/api/v1/projects",
            authenticated=True,
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "database_unavailable")

    def test_server_applies_connection_timeout(self) -> None:
        self.assertEqual(
            self.fixture.server.service.settings.socket_timeout_seconds,
            10.0,
        )
        self.assertEqual(
            self.fixture.server.service.settings.max_concurrent_requests,
            32,
        )


class MissingAuthenticationConfigurationTest(unittest.TestCase):
    def test_missing_session_file_is_not_ready(self) -> None:
        fixture = APIFixture()
        try:
            fixture.session_file.unlink()
            status, _, payload = fixture.request("GET", "/ready")
            self.assertEqual(status, 503)
            self.assertEqual(payload["status"], "not_ready")
            status, _, payload = fixture.request(
                "GET",
                "/api/v1/projects",
            )
            self.assertEqual(status, 503)
            self.assertEqual(
                payload["code"],
                "controller_auth_not_configured",
            )
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
