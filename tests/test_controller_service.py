from __future__ import annotations

import hashlib
import http.client
import os
import sqlite3
import tempfile
import threading
import unittest
from unittest import mock
from pathlib import Path

from controller_api.core import Settings
from controller_api.server import build_server
from controller_api.service_support import (
    ServiceSupportError,
    _write_new_session,
    ensure_session,
    probe_controller,
    read_session,
    rotate_session,
)


class ControllerServiceSupportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        self.secrets = self.root / "secrets"
        self.secrets.mkdir(parents=True)
        os.chmod(self.secrets, 0o700)
        self.session = self.secrets / "controller-session"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_ensure_is_private_and_idempotent(self) -> None:
        output = ensure_session(self.session)
        self.assertEqual(output, "created")
        before = hashlib.sha256(self.session.read_bytes()).hexdigest()
        self.assertEqual(oct(self.session.stat().st_mode & 0o777), "0o600")
        self.assertEqual(ensure_session(self.session), "valid")
        after = hashlib.sha256(self.session.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertGreaterEqual(len(read_session(self.session)), 32)

    def test_exclusive_create_never_deletes_existing(self) -> None:
        ensure_session(self.session)
        before = self.session.read_bytes()
        with self.assertRaises(FileExistsError):
            _write_new_session(self.session, "b" * 64)
        self.assertEqual(self.session.read_bytes(), before)
        self.assertEqual(read_session(self.session), before.decode("ascii").strip())

    def test_rotate_changes_value_atomically(self) -> None:
        ensure_session(self.session)
        before = read_session(self.session)
        self.assertEqual(rotate_session(self.session), "rotated")
        after = read_session(self.session)
        self.assertNotEqual(before, after)
        self.assertEqual(oct(self.session.stat().st_mode & 0o777), "0o600")

    def test_new_session_syncs_file_and_parent_directory(self) -> None:
        with mock.patch(
            "controller_api.service_support.os.fsync",
            wraps=os.fsync,
        ) as synchronized:
            self.assertEqual(ensure_session(self.session), "created")
        self.assertGreaterEqual(synchronized.call_count, 2)

    def test_rotate_syncs_parent_directory(self) -> None:
        ensure_session(self.session)
        with mock.patch(
            "controller_api.service_support.os.fsync",
            wraps=os.fsync,
        ) as synchronized:
            self.assertEqual(rotate_session(self.session), "rotated")
        self.assertGreaterEqual(synchronized.call_count, 3)

    def test_symlink_is_rejected(self) -> None:
        target = self.secrets / "real-session"
        target.write_text("a" * 64 + "\n", encoding="ascii")
        os.chmod(target, 0o600)
        self.session.symlink_to(target)
        with self.assertRaises(ServiceSupportError):
            read_session(self.session)

    def test_insecure_parent_is_rejected(self) -> None:
        os.chmod(self.secrets, 0o750)
        with self.assertRaises(ServiceSupportError):
            ensure_session(self.session)


class ControllerProbeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "root"
        (self.root / "repo").mkdir(parents=True)
        projects = self.root / "repo" / "config" / "projects.d"
        projects.mkdir(parents=True)
        project_config = projects / "alpha.toml"
        project_config.write_text(
            'schema_version = 1\n[git]\ndefault_branch = "main"\n',
            encoding="utf-8",
        )

        secrets_dir = self.root / "secrets"
        secrets_dir.mkdir()
        os.chmod(secrets_dir, 0o700)
        self.session = secrets_dir / "controller-session"
        ensure_session(self.session)

        database = self.root / "state" / "controller" / "orchestra.db"
        database.parent.mkdir(parents=True)
        with sqlite3.connect(database) as connection:
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
                    action_id TEXT PRIMARY KEY, review_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    command TEXT NOT NULL, reason_present INTEGER NOT NULL,
                    status TEXT NOT NULL, created_at TEXT NOT NULL,
                    UNIQUE(review_id, command)
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
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE orchestration_plans (
                    plan_id TEXT PRIMARY KEY,
                    status TEXT NOT NULL
                );
                CREATE TABLE objective_queue (
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
                -- Orchestra milestone 2I: durable Controller event journal.
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

                PRAGMA user_version = 15;
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
                INSERT INTO schema_migrations VALUES (
                    10, '2026-07-19T00:00:00.000Z'
                );
                """
            )

        migration_connection = sqlite3.connect(database)
        try:
            for migration_name in (
                "020_sandbox_profile_persistence.sql",
                "021_project_lifecycle.sql",
                "022_hermesfile_lifecycle.sql",
                "023_blueprint_migration.sql",
            ):
                migration_connection.executescript(
                    (
                        Path(__file__).resolve().parents[1]
                        / "migrations"
                        / migration_name
                    ).read_text(encoding="utf-8")
                )
            migration_connection.commit()
        finally:
            migration_connection.close()

        settings = Settings.from_root(
            self.root,
            host="127.0.0.1",
            port=0,
        )
        self.server = build_server(settings)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            daemon=True,
        )
        self.thread.start()
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def test_authenticated_probe(self) -> None:
        result = probe_controller(
            f"http://127.0.0.1:{self.port}",
            self.session,
            wait_seconds=2,
        )
        self.assertEqual(result.health_status, 200)
        self.assertEqual(result.ready_status, 200)
        self.assertEqual(result.capabilities_status, 200)

    def test_probe_fixture_satisfies_objective_readiness_schema(self) -> None:
        result = probe_controller(
            f"http://127.0.0.1:{self.port}",
            self.session,
            wait_seconds=2,
        )
        self.assertEqual(result.ready_status, 200)

        database = self.root / "state" / "controller" / "orchestra.db"
        with sqlite3.connect(database) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        self.assertTrue(
            {
                "projects",
                "schema_migrations",
                "orchestration_plans",
                "objective_queue",
                "objective_attempts",
                "objective_events",
                "controller_event_journal",
                "roles",
                "runs",
                "events",
                "worker_executions",
                "orchestration_tasks",
                "orchestration_attempts",
                "orchestration_dependencies",
                "sandbox_profiles",
                "sandbox_profile_revisions",
            }.issubset(tables)
        )

    def test_probe_rejects_non_loopback(self) -> None:
        with self.assertRaises(ServiceSupportError):
            probe_controller(
                "http://192.0.2.1:8765",
                self.session,
                wait_seconds=0,
            )

    def test_probe_rejects_localhost_hostname(self) -> None:
        with self.assertRaises(ServiceSupportError):
            probe_controller(
                "http://localhost:8765",
                self.session,
                wait_seconds=0,
            )

    def test_live_rotation_invalidates_old_cookie_without_restart(self) -> None:
        old_token = read_session(self.session)
        self.assertEqual(rotate_session(self.session), "rotated")
        new_token = read_session(self.session)
        self.assertNotEqual(old_token, new_token)

        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=2,
        )
        try:
            connection.request(
                "GET",
                "/api/v1/system/capabilities",
                headers={
                    "Cookie": f"orchestra_session={old_token}",
                },
            )
            response = connection.getresponse()
            response.read()
            self.assertEqual(response.status, 401)
        finally:
            connection.close()

        result = probe_controller(
            f"http://127.0.0.1:{self.port}",
            self.session,
            wait_seconds=2,
        )
        self.assertEqual(result.capabilities_status, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
