from __future__ import annotations

import hashlib
import hmac
import json
import socket
import sqlite3
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_controller_api import APIFixture, TOKEN  # noqa: E402
from controller_api.objective_command_probe import probe_objective_commands  # noqa: E402


class ObjectiveCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = APIFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        key: str | None = "idem-key-0001",
        csrf: str | None = None,
        authenticated: bool = True,
        origin: str | None = None,
    ):
        headers = {"Content-Type": "application/json"}
        if key is not None:
            headers["Idempotency-Key"] = key
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if origin is not None:
            headers["Origin"] = origin
        return self.fixture.request(
            "POST",
            path,
            authenticated=authenticated,
            headers_override=headers,
            body=json.dumps(body, separators=(",", ":")).encode(),
        )

    def raw_post(
        self,
        path: str,
        raw_body: bytes,
        headers: list[tuple[str, str]],
    ) -> tuple[int, dict[str, object]]:
        lines = [
            f"POST {path} HTTP/1.1",
            f"Host: 127.0.0.1:{self.fixture.port}",
            "Connection: close",
        ]
        lines.extend(f"{name}: {value}" for name, value in headers)
        request = ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + raw_body
        with socket.create_connection(("127.0.0.1", self.fixture.port), timeout=5) as stream:
            stream.sendall(request)
            stream.shutdown(socket.SHUT_WR)
            response = bytearray()
            while True:
                chunk = stream.recv(65536)
                if not chunk:
                    break
                response.extend(chunk)
        head, _, body = bytes(response).partition(b"\r\n\r\n")
        status = int(head.split(b"\r\n", 1)[0].split()[1])
        return status, json.loads(body.decode("utf-8"))

    def csrf(self, key: str = "csrf-key-0001") -> str:
        status, _, payload = self.post(
            "/api/v1/auth/csrf",
            {},
            key=key,
        )
        self.assertEqual(status, 200)
        return str(payload["data"]["token"])

    def create_body(self) -> dict[str, object]:
        return {
            "project_ids": ["alpha"],
            "title": "Controller objective",
            "description": "Exercise secure objective mutations.",
            "priority": 90,
            "not_before": "2099-01-01T00:00:00Z",
            "max_parallel_tasks": 1,
            "planning_max_attempts": 3,
            "constraints": ["Do not start before the scheduled date"],
        }

    def create(self, *, key: str = "create-key-0001"):
        token = self.csrf(key="csrf-" + key)
        status, headers, payload = self.post(
            "/api/v1/objectives",
            self.create_body(),
            key=key,
            csrf=token,
        )
        self.assertEqual(status, 202)
        return token, headers, payload

    def seed_linked_run(
        self,
        objective_id: str,
        *,
        suffix: str,
        plan_status: str,
        task_status: str,
        run_status: str,
    ) -> tuple[str, str, str]:
        plan_id = f"plan-{suffix}"
        task_id = f"task-{suffix}"
        run_id = f"run-{suffix}"
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute(
                "INSERT INTO orchestration_plans(plan_id,status) VALUES (?,?)",
                (plan_id, plan_status),
            )
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING', plan_id=? "
                "WHERE objective_id=?",
                (plan_id, objective_id),
            )
            connection.execute(
                """
                INSERT INTO orchestration_tasks (
                    orchestration_task_id, plan_id, task_key, kind, project_id,
                    role_id, status, priority, instruction, acceptance_json,
                    marker, max_attempts, attempt_count, result_json,
                    failure_reason, created_at, started_at, heartbeat_at, finished_at
                ) VALUES (?, ?, 'pipeline', 'PIPELINE', 'alpha', NULL, ?, 100,
                          'test', '[]', 'DONE', 1, 1, '{}', NULL,
                          '2026-08-16T00:00:00.000Z',
                          '2026-08-16T00:00:00.000Z',
                          '2026-08-16T00:00:00.000Z', NULL)
                """,
                (task_id, plan_id, task_status),
            )
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, status, created_at, started_at,
                    finished_at, heartbeat_at
                ) VALUES (?, 'alpha', ?, '2026-08-16T00:00:00.000Z',
                          '2026-08-16T00:00:00.000Z', NULL,
                          '2026-08-16T00:00:00.000Z')
                """,
                (run_id, run_status),
            )
            connection.execute(
                """
                INSERT INTO orchestration_attempts (
                    attempt_id, orchestration_task_id, attempt_number, status,
                    executor_instance_id, run_id, worker_execution_id,
                    review_execution_id, integration_id, result_json,
                    failure_reason, started_at, heartbeat_at, finished_at
                ) VALUES (?, ?, 1, 'RUNNING', NULL, ?, NULL, NULL, NULL, '{}',
                          NULL, '2026-08-16T00:00:00.000Z',
                          '2026-08-16T00:00:00.000Z', NULL)
                """,
                (f"attempt-{suffix}", task_id, run_id),
            )
            connection.execute(
                "INSERT INTO project_locks(project_id,run_id,holder,acquired_at,heartbeat_at) "
                "VALUES ('alpha',?,'controller-test','2026-08-16T00:00:00.000Z',"
                "'2026-08-16T00:00:00.000Z')",
                (run_id,),
            )
            connection.commit()
        return plan_id, task_id, run_id

    def test_installed_service_probe_is_safe_and_self_cancels(self) -> None:
        self.fixture.session_file.parent.chmod(0o700)
        result = probe_objective_commands(
            f"http://127.0.0.1:{self.fixture.port}",
            self.fixture.session_file,
            wait_seconds=5,
        )
        self.assertEqual(result.csrf_status, 200)
        self.assertEqual(result.create_status, 202)
        self.assertEqual(result.pause_status, 202)
        self.assertEqual(result.resume_status, 202)
        self.assertEqual(result.cancel_status, 202)
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            row = connection.execute(
                "SELECT status, not_before FROM objective_queue "
                "WHERE objective LIKE 'Orchestra Controller command probe%'"
            ).fetchone()
            self.assertEqual(row[0], "CANCELLED")
            self.assertEqual(row[1], "2099-01-01T00:00:00.000Z")

    def test_csrf_requires_authentication(self) -> None:
        status, _, payload = self.post(
            "/api/v1/auth/csrf",
            {},
            authenticated=False,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_csrf_requires_valid_idempotency_key(self) -> None:
        status, _, payload = self.post(
            "/api/v1/auth/csrf",
            {},
            key=None,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_idempotency_key")

    def test_csrf_issue_and_idempotent_replay(self) -> None:
        first = self.post("/api/v1/auth/csrf", {}, key="csrf-replay-01")
        second = self.post("/api/v1/auth/csrf", {}, key="csrf-replay-01")
        self.assertEqual(first[0], 200)
        self.assertEqual(first[2], second[2])
        self.assertRegex(first[2]["data"]["token"], r"^csrf1\.")

    def test_idempotency_key_conflict_is_rejected(self) -> None:
        self.post("/api/v1/auth/csrf", {}, key="same-key-0001")
        status, _, payload = self.post(
            "/api/v1/auth/csrf",
            {"unexpected": True},
            key="same-key-0001",
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "idempotency_conflict")

    def test_create_requires_csrf(self) -> None:
        status, _, payload = self.post(
            "/api/v1/objectives",
            self.create_body(),
            key="create-no-csrf",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "csrf_required")

    def test_create_rejects_invalid_csrf(self) -> None:
        status, _, payload = self.post(
            "/api/v1/objectives",
            self.create_body(),
            key="create-bad-csrf",
            csrf="csrf1.invalid",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "csrf_invalid")

    def test_create_objective_is_atomic_and_audited(self) -> None:
        _, _, payload = self.create(key="create-atomic-01")
        operation = payload["data"]
        objective_id = operation["target"]["id"]
        self.assertEqual(operation["kind"], "objective.create")
        self.assertEqual(operation["state"], "succeeded")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            objective = connection.execute(
                "SELECT status, source, objective FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            ).fetchone()
            self.assertEqual(objective[0], "QUEUED")
            self.assertEqual(objective[1], "AI")
            self.assertIn("Controller objective", objective[2])
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_operations WHERE operation_id=?",
                    (operation["id"],),
                ).fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_command_audit WHERE operation_id=?",
                    (operation["id"],),
                ).fetchone()[0],
                1,
            )

    def test_create_replay_is_identical_and_does_not_duplicate(self) -> None:
        token = self.csrf("csrf-create-replay")
        body = self.create_body()
        first = self.post(
            "/api/v1/objectives",
            body,
            key="create-replay-01",
            csrf=token,
        )
        second = self.post(
            "/api/v1/objectives",
            body,
            key="create-replay-01",
            csrf=token,
        )
        self.assertEqual(first[2], second[2])
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM objective_queue"
                ).fetchone()[0],
                1,
            )

    def test_create_reuse_with_different_body_conflicts(self) -> None:
        token = self.csrf("csrf-create-conflict")
        body = self.create_body()
        self.post(
            "/api/v1/objectives",
            body,
            key="create-conflict-01",
            csrf=token,
        )
        changed = dict(body)
        changed["priority"] = 91
        status, _, payload = self.post(
            "/api/v1/objectives",
            changed,
            key="create-conflict-01",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "idempotency_conflict")

    def test_unknown_and_disabled_projects_fail_closed(self) -> None:
        token = self.csrf("csrf-project-errors")
        body = self.create_body()
        body["project_ids"] = ["missing"]
        status, _, payload = self.post(
            "/api/v1/objectives",
            body,
            key="unknown-project-1",
            csrf=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_project")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute("UPDATE projects SET enabled=0 WHERE project_id='alpha'")
            connection.commit()
        body["project_ids"] = ["alpha"]
        status, _, payload = self.post(
            "/api/v1/objectives",
            body,
            key="disabled-project-1",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "project_disabled")

    def test_unknown_fields_and_oversized_objective_are_rejected(self) -> None:
        token = self.csrf("csrf-invalid-body")
        body = self.create_body()
        body["secret"] = "not allowed"
        status, _, payload = self.post(
            "/api/v1/objectives",
            body,
            key="invalid-field-1",
            csrf=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_request_field")
        body = self.create_body()
        body["description"] = "x" * 16_384
        status, _, payload = self.post(
            "/api/v1/objectives",
            body,
            key="too-large-1",
            csrf=token,
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "objective_too_large")

    def test_pause_resume_cancel_lifecycle(self) -> None:
        token, _, payload = self.create(key="create-lifecycle")
        objective_id = payload["data"]["target"]["id"]
        for command, key, expected in (
            ("pause", "pause-lifecycle", "PAUSED"),
            ("resume", "resume-lifecycle", "QUEUED"),
            ("cancel", "cancel-lifecycle", "CANCELLED"),
        ):
            status, _, command_payload = self.post(
                f"/api/v1/objectives/{objective_id}/commands/{command}",
                {"reason": "safe test"},
                key=key,
                csrf=token,
            )
            self.assertEqual(status, 202)
            self.assertEqual(command_payload["data"]["kind"], f"objective.{command}")
            with closing(sqlite3.connect(self.fixture.database)) as connection:
                state = connection.execute(
                    "SELECT status FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                ).fetchone()[0]
            self.assertEqual(state, expected)

    def test_resume_from_non_paused_is_conflict(self) -> None:
        token, _, payload = self.create(key="create-resume-conflict")
        objective_id = payload["data"]["target"]["id"]
        status, _, problem = self.post(
            f"/api/v1/objectives/{objective_id}/commands/resume",
            {},
            key="resume-conflict-01",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "objective_not_paused")

    def test_unsupported_command_is_explicitly_unavailable(self) -> None:
        token, _, payload = self.create(key="create-command-unavailable")
        objective_id = payload["data"]["target"]["id"]
        status, _, problem = self.post(
            f"/api/v1/objectives/{objective_id}/commands/archive",
            {},
            key="archive-unavailable",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "objective_command_unavailable")

    def test_controller_operation_is_readable(self) -> None:
        _, _, payload = self.create(key="create-operation-read")
        operation_id = payload["data"]["id"]
        status, headers, operation = self.fixture.request(
            "GET",
            f"/api/v1/operations/{operation_id}",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(operation["data"]["id"], operation_id)
        self.assertIn("etag", headers)

    def test_session_rotation_invalidates_csrf_and_idempotency_namespace(self) -> None:
        token = self.csrf("csrf-before-rotation")
        new_token = "b" * 64
        self.fixture.session_file.write_text(new_token + "\n", encoding="utf-8")
        status, _, payload = self.post(
            "/api/v1/objectives",
            self.create_body(),
            key="after-rotation-01",
            csrf=token,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["code"], "authentication_required")

    def test_cross_origin_request_is_rejected(self) -> None:
        status, _, payload = self.post(
            "/api/v1/auth/csrf",
            {},
            key="origin-reject-01",
            origin="https://evil.invalid",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "origin_forbidden")

    def test_audit_and_idempotency_do_not_store_raw_key_or_reason(self) -> None:
        token, _, payload = self.create(key="raw-secret-key-01")
        objective_id = payload["data"]["target"]["id"]
        self.post(
            f"/api/v1/objectives/{objective_id}/commands/pause",
            {"reason": "private operator explanation"},
            key="private-command-key-01",
            csrf=token,
        )
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            values = "\n".join(
                str(value)
                for table in (
                    "controller_idempotency",
                    "controller_command_audit",
                )
                for row in connection.execute(f"SELECT * FROM {table}")
                for value in row
                if value is not None
            )
        self.assertNotIn("private-command-key-01", values)
        self.assertNotIn("private operator explanation", values)

    def test_duplicate_security_headers_are_rejected(self) -> None:
        base = [
            ("Cookie", f"orchestra_session={TOKEN}"),
            ("Content-Type", "application/json"),
            ("Content-Length", "2"),
            ("Idempotency-Key", "duplicate-header-01"),
        ]
        cases = (
            base + [("Content-Length", "2")],
            base + [("Idempotency-Key", "duplicate-header-02")],
            base + [("Cookie", f"orchestra_session={'b' * 64}")],
            base + [("Content-Type", "application/json")],
            base + [("Origin", f"http://127.0.0.1:{self.fixture.port}"), ("Origin", "https://evil.invalid")],
        )
        for headers in cases:
            with self.subTest(headers=headers[-1][0]):
                status, payload = self.raw_post(
                    "/api/v1/auth/csrf",
                    b"{}",
                    list(headers),
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "ambiguous_header")

    def test_duplicate_json_members_are_rejected(self) -> None:
        token = self.csrf("csrf-duplicate-json")
        raw = (
            b'{"project_ids":["alpha"],"title":"first",'
            b'"title":"second","description":"body"}'
        )
        status, payload = self.raw_post(
            "/api/v1/objectives",
            raw,
            [
                ("Cookie", f"orchestra_session={TOKEN}"),
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(raw))),
                ("Idempotency-Key", "duplicate-json-01"),
                ("X-CSRF-Token", token),
            ],
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "invalid_json")

    def test_non_standard_and_pathological_json_fail_cleanly(self) -> None:
        token = self.csrf("csrf-pathological-json")
        cases = [
            b'{"project_ids":["alpha"],"title":"x","description":"x","priority":NaN}',
            b'{"project_ids":["alpha"],"title":"\\ud800","description":"x"}',
            (b'{"unknown":' + b'[' * 1100 + b'0' + b']' * 1100 + b'}'),
        ]
        for index, raw in enumerate(cases):
            with self.subTest(index=index):
                status, payload = self.raw_post(
                    "/api/v1/objectives",
                    raw,
                    [
                        ("Cookie", f"orchestra_session={TOKEN}"),
                        ("Content-Type", "application/json"),
                        ("Content-Length", str(len(raw))),
                        ("Idempotency-Key", f"pathological-{index:02d}"),
                        ("X-CSRF-Token", token),
                    ],
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["code"], "invalid_json")

    def test_reused_key_conflicts_before_semantic_validation(self) -> None:
        token = self.csrf("csrf-validation-order")
        body = self.create_body()
        first = self.post(
            "/api/v1/objectives",
            body,
            key="validation-order-01",
            csrf=token,
        )
        self.assertEqual(first[0], 202)
        changed = dict(body)
        changed["secret"] = "must not bypass conflict detection"
        status, _, payload = self.post(
            "/api/v1/objectives",
            changed,
            key="validation-order-01",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "idempotency_conflict")

    def test_resume_preserves_original_not_before(self) -> None:
        token, _, payload = self.create(key="create-future-resume")
        objective_id = payload["data"]["target"]["id"]
        self.post(
            f"/api/v1/objectives/{objective_id}/commands/pause",
            {},
            key="pause-future-resume",
            csrf=token,
        )
        self.post(
            f"/api/v1/objectives/{objective_id}/commands/resume",
            {},
            key="resume-future-resume",
            csrf=token,
        )
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            state, not_before = connection.execute(
                "SELECT status, not_before FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            ).fetchone()
        self.assertEqual(state, "QUEUED")
        self.assertEqual(not_before, "2099-01-01T00:00:00.000Z")

    def test_resume_completed_plan_converges_objective_to_completed(self) -> None:
        token, _, payload = self.create(key="create-completed-resume")
        objective_id = payload["data"]["target"]["id"]
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute(
                "INSERT INTO orchestration_plans(plan_id,status) "
                "VALUES ('plan-completed-resume','COMPLETED')"
            )
            connection.execute(
                "UPDATE objective_queue SET status='PAUSED', "
                "plan_id='plan-completed-resume' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()

        status, _, operation = self.post(
            f"/api/v1/objectives/{objective_id}/commands/resume",
            {},
            key="resume-completed-plan",
            csrf=token,
        )

        self.assertEqual(status, 202)
        self.assertEqual(operation["data"]["result"]["raw_state"], "COMPLETED")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                ).fetchone()[0],
                "COMPLETED",
            )

    def test_pause_cannot_override_pending_cancellation(self) -> None:
        token, _, payload = self.create(key="create-cancel-pending")
        objective_id = payload["data"]["target"]["id"]
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='CANCEL_REQUESTED' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        status, _, problem = self.post(
            f"/api/v1/objectives/{objective_id}/commands/pause",
            {},
            key="pause-cancel-pending",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "objective_cancel_pending")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            state = connection.execute(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            ).fetchone()[0]
        self.assertEqual(state, "CANCEL_REQUESTED")

    def test_cancel_rejects_committing_integration(self) -> None:
        token, _, payload = self.create(key="create-committing-cancel")
        objective_id = payload["data"]["target"]["id"]
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute(
                "INSERT INTO orchestration_plans(plan_id,status) "
                "VALUES ('plan-committing','RUNNING')"
            )
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING', plan_id='plan-committing' "
                "WHERE objective_id=?",
                (objective_id,),
            )
            connection.execute(
                """
                INSERT INTO orchestration_tasks (
                    orchestration_task_id, plan_id, task_key, kind, project_id,
                    role_id, status, priority, instruction, acceptance_json,
                    marker, max_attempts, attempt_count, result_json,
                    failure_reason, created_at, started_at, heartbeat_at, finished_at
                ) VALUES (
                    'task-committing', 'plan-committing', 'pipeline', 'PIPELINE',
                    'alpha', NULL, 'RUNNING', 100, 'test', '[]', 'DONE',
                    1, 1, '{}', NULL, '2026-08-16T00:00:00.000Z',
                    '2026-08-16T00:00:00.000Z',
                    '2026-08-16T00:00:00.000Z', NULL
                )
                """
            )
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, status, created_at, started_at,
                    finished_at, heartbeat_at
                ) VALUES (
                    'run-committing', 'alpha', 'COMMITTING',
                    '2026-08-16T00:00:00.000Z',
                    '2026-08-16T00:00:00.000Z', NULL,
                    '2026-08-16T00:00:00.000Z'
                )
                """
            )
            connection.execute(
                """
                INSERT INTO orchestration_attempts (
                    attempt_id, orchestration_task_id, attempt_number, status,
                    executor_instance_id, run_id, worker_execution_id,
                    review_execution_id, integration_id, result_json,
                    failure_reason, started_at, heartbeat_at, finished_at
                ) VALUES (
                    'attempt-committing', 'task-committing', 1, 'RUNNING',
                    NULL, 'run-committing', NULL, NULL, NULL, '{}', NULL,
                    '2026-08-16T00:00:00.000Z',
                    '2026-08-16T00:00:00.000Z', NULL
                )
                """
            )
            connection.commit()

        status, _, problem = self.post(
            f"/api/v1/objectives/{objective_id}/commands/cancel",
            {},
            key="cancel-committing-integration",
            csrf=token,
        )
        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "objective_integration_committed")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                ).fetchone()[0],
                "RUNNING",
            )

    def test_cancel_waiting_human_requests_cleanup_without_false_terminal(self) -> None:
        token, _, payload = self.create(key="create-api-waiting-human-cancel")
        objective_id = payload["data"]["target"]["id"]
        plan_id, task_id, run_id = self.seed_linked_run(
            objective_id,
            suffix="api-waiting-human-cancel",
            plan_status="BLOCKED",
            task_status="BLOCKED",
            run_status="WAITING_HUMAN",
        )

        status, _, operation = self.post(
            f"/api/v1/objectives/{objective_id}/commands/cancel",
            {},
            key="cancel-api-waiting-human",
            csrf=token,
        )

        self.assertEqual(status, 202)
        self.assertEqual(operation["data"]["result"]["raw_state"], "CANCEL_REQUESTED")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT objective.status, plan.status, task.status, run.status, "
                    "EXISTS(SELECT 1 FROM project_locks AS lock "
                    "WHERE lock.run_id=run.run_id) "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "WHERE objective.objective_id=? AND plan.plan_id=? "
                    "AND task.orchestration_task_id=? AND run.run_id=?",
                    (objective_id, plan_id, task_id, run_id),
                ).fetchone(),
                ("CANCEL_REQUESTED", "BLOCKED", "BLOCKED", "WAITING_HUMAN", 1),
            )

    def test_cancel_rejects_recovering_integration_after_ponr(self) -> None:
        token, _, payload = self.create(key="create-api-recovering-post-ponr")
        objective_id = payload["data"]["target"]["id"]
        _, _, run_id = self.seed_linked_run(
            objective_id,
            suffix="api-recovering-post-ponr",
            plan_status="RUNNING",
            task_status="RUNNING",
            run_status="RECOVERING",
        )
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute(
                "INSERT INTO integration_executions(integration_id,run_id,decision,status) "
                "VALUES ('integration-api-recovering-post-ponr',?,'APPROVE','FAILED')",
                (run_id,),
            )
            connection.commit()

        status, _, problem = self.post(
            f"/api/v1/objectives/{objective_id}/commands/cancel",
            {},
            key="cancel-api-recovering-post-ponr",
            csrf=token,
        )

        self.assertEqual(status, 409)
        self.assertEqual(problem["code"], "objective_integration_committed")

    def test_cancel_accepts_recovering_run_before_ponr(self) -> None:
        token, _, payload = self.create(key="create-api-recovering-pre-ponr")
        objective_id = payload["data"]["target"]["id"]
        plan_id, _, _ = self.seed_linked_run(
            objective_id,
            suffix="api-recovering-pre-ponr",
            plan_status="RUNNING",
            task_status="BLOCKED",
            run_status="RECOVERING",
        )

        status, _, operation = self.post(
            f"/api/v1/objectives/{objective_id}/commands/cancel",
            {},
            key="cancel-api-recovering-pre-ponr",
            csrf=token,
        )

        self.assertEqual(status, 202)
        self.assertEqual(operation["data"]["result"]["raw_state"], "CANCEL_REQUESTED")
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()[0],
                "RUNNING",
                "an active pre-PONR Recovery run must not be terminalized before cleanup",
            )

    def test_unknown_persisted_state_fails_closed(self) -> None:
        token, _, payload = self.create(key="create-invalid-state")
        objective_id = payload["data"]["target"]["id"]
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='CORRUPT' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        status, _, problem = self.post(
            f"/api/v1/objectives/{objective_id}/commands/cancel",
            {},
            key="cancel-invalid-state",
            csrf=token,
        )
        self.assertEqual(status, 503)
        self.assertEqual(problem["code"], "objective_state_invalid")

    def test_request_hash_is_keyed_not_plaintext_verifier(self) -> None:
        token = self.csrf("csrf-keyed-request-hash")
        body = self.create_body()
        status, _, _ = self.post(
            "/api/v1/objectives",
            body,
            key="keyed-request-hash-01",
            csrf=token,
        )
        self.assertEqual(status, 202)
        canonical = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        material = b"POST\0/api/v1/objectives\0" + canonical
        plain = hashlib.sha256(material).hexdigest()
        expected = hmac.new(
            TOKEN.encode("ascii"),
            b"hermesops-request-v1\0" + material,
            hashlib.sha256,
        ).hexdigest()
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            stored = connection.execute(
                "SELECT request_hash FROM controller_idempotency "
                "WHERE route='/api/v1/objectives'"
            ).fetchone()[0]
        self.assertEqual(stored, expected)
        self.assertNotEqual(stored, plain)

    def test_concurrent_identical_retry_creates_one_objective(self) -> None:
        token = self.csrf("csrf-concurrent")
        body = self.create_body()
        results: list[tuple[int, object]] = []
        lock = threading.Lock()

        def invoke() -> None:
            status, _, payload = self.post(
                "/api/v1/objectives",
                body,
                key="concurrent-create-01",
                csrf=token,
            )
            with lock:
                results.append((status, payload))

        threads = [threading.Thread(target=invoke) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual([item[0] for item in results], [202, 202])
        self.assertEqual(results[0][1], results[1][1])
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM objective_queue").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
