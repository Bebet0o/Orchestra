from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any


REPOSITORY = Path(__file__).resolve().parents[1]
SCRIPTS = REPOSITORY / "scripts"
sys.path.insert(0, str(SCRIPTS))


def load_script(name: str, filename: str) -> Any:
    specification = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


ORCHESTRATOR = load_script(
    "lifecycle_stabilization_orchestrator",
    "orchestra-orchestrator.py",
)
OBJECTIVES = load_script(
    "lifecycle_stabilization_objectives",
    "orchestra-objectives.py",
)
INTEGRATOR = load_script(
    "lifecycle_stabilization_integrator",
    "orchestra-integrator.py",
)


NOW = "2026-08-16T10:00:00.000Z"
PROJECT_ID = "lifecycle-project"
OWNER = "orchestrator:test-instance:pipeline"


class LifecycleStabilizationRegressionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "orchestra.db"
        self.runtime = self.root / "runtime"
        self._apply_migrations()
        self._seed_roles_and_project()

        for module in (ORCHESTRATOR, OBJECTIVES, INTEGRATOR):
            module.DATABASE = self.database
        ORCHESTRATOR.RUNTIME = self.runtime / "orchestrator"
        ORCHESTRATOR.OBJECTIVE_RUNTIME = self.runtime / "objectives"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _apply_migrations(self) -> None:
        with sqlite3.connect(self.database) as connection:
            for migration in sorted((REPOSITORY / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))

    def _seed_roles_and_project(self) -> None:
        with contextlib.closing(self.connect()) as connection:
            for role_id, role_kind, workspace_mode in (
                ("orchestrator", "orchestrator", "controller_only"),
                ("worker", "worker", "write"),
                ("reviewer", "reviewer", "read_only"),
            ):
                connection.execute(
                    """
                    INSERT INTO roles (
                        role_id, profile_name, role_kind, description,
                        reasoning_effort, max_turns, toolsets_json, skills_json,
                        workspace_mode, may_commit, may_push, network_enabled,
                        cpu_limit, memory_mb, enabled, config_source, config_hash,
                        registered_at, updated_at
                    ) VALUES (?, ?, ?, ?, 'medium', 10, '[]', '[]', ?, 0, 0, 0,
                              1, 512, 1, ?, ?, ?, ?)
                    """,
                    (
                        role_id,
                        f"test-{role_id}",
                        role_kind,
                        f"Test {role_kind}",
                        workspace_mode,
                        str(self.root / "roles.toml"),
                        role_id * 8,
                        NOW,
                        NOW,
                    ),
                )
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, repo_path, data_path, policy_id,
                    enabled, config_source, config_hash, registered_at, updated_at
                ) VALUES (?, 'Lifecycle project', ?, ?, 'default', 1, ?, ?, ?, ?)
                """,
                (
                    PROJECT_ID,
                    str(self.root / "repository"),
                    str(self.root / "data"),
                    str(self.root / "project.toml"),
                    "a" * 64,
                    NOW,
                    NOW,
                ),
            )
            connection.execute(
                """
                INSERT INTO orchestrator_instances (
                    instance_id, hostname, pid, owner, version, status,
                    started_at, heartbeat_at
                ) VALUES ('test-instance', 'localhost', 1, 'test',
                          'orchestrator-v2', 'RUNNING', ?, ?)
                """,
                (NOW, NOW),
            )
            connection.commit()

    @staticmethod
    def _plan(*tasks: dict[str, Any], objective: str = "Lifecycle regression") -> dict[str, Any]:
        return {
            "schema_version": 1,
            "objective": objective,
            "max_parallel_tasks": 2,
            "tasks": list(tasks),
        }

    @staticmethod
    def _task(
        key: str,
        *,
        kind: str = "NOOP",
        project_id: str | None = None,
        role_id: str | None = None,
        dependencies: list[str] | None = None,
    ) -> dict[str, Any]:
        return {
            "key": key,
            "kind": kind,
            "project_id": project_id,
            "role_id": role_id,
            "priority": 100,
            "instruction": f"Execute {key}",
            "acceptance_criteria": [f"{key} completed"],
            "marker": f"{key.upper()}_DONE" if kind == "PIPELINE" else None,
            "max_attempts": 1,
            "dependencies": dependencies or [],
        }

    def _insert_objective(
        self,
        *,
        source: str,
        plan_id: str | None = None,
    ) -> str:
        return OBJECTIVES.insert_objective(
            objective="Lifecycle stabilization objective",
            source=source,
            priority=10,
            not_before=NOW,
            project_ids=[PROJECT_ID],
            max_parallel_tasks=1,
            planning_max_attempts=3,
            plan_id=plan_id,
        )

    def _command(self, function: Any, objective_id: str) -> None:
        original_payload = OBJECTIVES.objective_payload
        OBJECTIVES.objective_payload = lambda _: {}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                function(argparse.Namespace(objective=objective_id))
        finally:
            OBJECTIVES.objective_payload = original_payload

    def _row(self, sql: str, parameters: tuple[Any, ...] = ()) -> sqlite3.Row:
        with contextlib.closing(self.connect()) as connection:
            row = connection.execute(sql, parameters).fetchone()
        self.assertIsNotNone(row)
        return row

    def _human_gate_active(self, run_id: str) -> bool:
        with contextlib.closing(self.connect()) as connection:
            return INTEGRATOR.run_plan_has_active_human_gate(connection, run_id)

    def _create_running_plan(self) -> tuple[str, str, str, sqlite3.Row]:
        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(
                self._task(
                    "pipeline",
                    kind="PIPELINE",
                    project_id=PROJECT_ID,
                    role_id="worker",
                ),
                self._task("after_cancel"),
            ),
            source="TEST",
            initial_status="READY",
        )
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='pipeline'",
                (plan_id,),
            )[0]
        )
        attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
            task_id,
            instance_id="test-instance",
        )
        return objective_id, plan_id, attempt_id, task

    def _create_idle_runnable_plan(
        self,
        *,
        task_key: str,
        kind: str = "NOOP",
        project_id: str | None = None,
    ) -> tuple[str, str, str]:
        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(
                self._task(
                    task_key,
                    kind=kind,
                    project_id=project_id,
                    role_id="worker" if kind == "PIPELINE" else None,
                )
            ),
            source="TEST",
            initial_status="READY",
        )
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key=?",
                (plan_id, task_key),
            )[0]
        )
        return objective_id, plan_id, task_id

    def _insert_project(self, project_id: str) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, repo_path, data_path, policy_id,
                    enabled, config_source, config_hash, registered_at, updated_at
                ) VALUES (?, ?, ?, ?, 'default', 1, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    f"Lifecycle project {project_id}",
                    str(self.root / f"repository-{project_id}"),
                    str(self.root / f"data-{project_id}"),
                    str(self.root / f"project-{project_id}.toml"),
                    (project_id * 64)[:64],
                    NOW,
                    NOW,
                ),
            )
            connection.commit()

    def _insert_run(
        self,
        run_id: str,
        *,
        status: str = "RUNNING",
        project_id: str = PROJECT_ID,
    ) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO runs (
                    run_id, project_id, status, base_commit, result_commit,
                    worktree_path, metadata_json, created_at, started_at,
                    heartbeat_at, branch_name, transaction_owner
                ) VALUES (?, ?, ?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    project_id,
                    status,
                    "a" * 40,
                    "b" * 40,
                    str(self.root / "worktree"),
                    NOW,
                    NOW,
                    NOW,
                    "orchestra/test-run",
                    OWNER,
                ),
            )
            connection.execute(
                """
                INSERT INTO project_locks (
                    project_id, run_id, holder, acquired_at, heartbeat_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (project_id, run_id, OWNER, NOW, NOW),
            )
            connection.commit()

    def _seed_review(
        self,
        run_id: str,
        suffix: str,
        *,
        decision: str,
        verdict: str,
    ) -> sqlite3.Row:
        task_id = f"review-task-{suffix}"
        review_id = f"review-{suffix}"
        execution_id = f"reviewer-execution-{suffix}"
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, run_id, role, status, description, attempt,
                    metadata_json, created_at, started_at, finished_at, heartbeat_at
                ) VALUES (?, ?, 'reviewer', 'COMPLETED', ?, 1, '{}', ?, ?, ?, ?)
                """,
                (task_id, run_id, f"Review {suffix}", NOW, NOW, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO review_results (
                    review_id, run_id, verdict, summary, details_json, created_at
                ) VALUES (?, ?, ?, ?, '{}', ?)
                """,
                (review_id, run_id, verdict, f"Review {suffix}", NOW),
            )
            connection.execute(
                """
                INSERT INTO reviewer_executions (
                    execution_id, review_id, task_id, run_id, role_id,
                    source_profile, runtime_profile, outer_container_name,
                    prompt_path, output_path, workspace_mode, network_enabled,
                    cpu_limit, memory_mb, mount_verified, isolation_verified,
                    repository_unchanged, decision, verdict, exit_code,
                    result_json, created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, 'reviewer', 'test-reviewer', ?, ?, ?, ?,
                          'read_only', 0, 1, 512, 1, 1, 1, ?, ?, 0, '{}', ?, ?, ?)
                """,
                (
                    execution_id,
                    review_id,
                    task_id,
                    run_id,
                    f"runtime-{suffix}",
                    f"review-container-{suffix}",
                    str(self.root / f"{suffix}.prompt"),
                    str(self.root / f"{suffix}.output"),
                    decision,
                    verdict,
                    NOW,
                    NOW,
                    NOW,
                ),
            )
            connection.commit()
            review = connection.execute(
                """
                SELECT review.*, execution.execution_id AS review_execution_id
                FROM review_results AS review
                JOIN reviewer_executions AS execution
                  ON execution.review_id=review.review_id
                WHERE review.review_id=?
                """,
                (review_id,),
            ).fetchone()
        self.assertIsNotNone(review)
        return review

    def _create_waiting_human_plan(
        self,
        run_id: str,
    ) -> tuple[str, str, str, sqlite3.Row]:
        objective_id, plan_id, attempt_id, task = self._create_running_plan()
        self._insert_run(run_id, status="REVIEWING")
        self._seed_human_review(run_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
            review = connection.execute(
                """
                SELECT review.*, execution.execution_id AS review_execution_id
                FROM review_results AS review
                JOIN reviewer_executions AS execution
                  ON execution.review_id=review.review_id
                WHERE review.run_id=?
                """,
                (run_id,),
            ).fetchone()
        result = INTEGRATOR.record_non_integration(
            run=run,
            review=review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(task, attempt_id, result)
        return objective_id, plan_id, attempt_id, task

    def _create_human_gate_with_running_sibling(
        self,
        suffix: str,
        *,
        sibling_max_attempts: int = 1,
    ) -> tuple[str, str, str, sqlite3.Row]:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        sibling_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_tasks SET max_attempts=? "
                "WHERE orchestration_task_id=?",
                (sibling_max_attempts, sibling_id),
            )
            connection.commit()
        sibling_attempt_id, _, sibling_task = ORCHESTRATOR.reserve_attempt(
            sibling_id,
            instance_id="test-instance",
        )

        gate_run_id = f"run-{suffix}-gate"
        self._insert_run(gate_run_id, status="REVIEWING")
        gate_review = self._seed_review(
            gate_run_id,
            f"{suffix}-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (gate_run_id, gate_attempt_id),
            )
            connection.commit()
            gate_run = INTEGRATOR.get_run(connection, gate_run_id)
        gate_result = INTEGRATOR.record_non_integration(
            run=gate_run,
            review=gate_review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(
            gate_task,
            gate_attempt_id,
            {"kind": "PIPELINE", "integration": gate_result},
        )
        return plan_id, gate_run_id, sibling_attempt_id, sibling_task

    def _resolve_plan_approvals(self, plan_id: str) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE status='PENDING' AND run_id IN ("
                "SELECT attempt.run_id FROM orchestration_attempts AS attempt "
                "JOIN orchestration_tasks AS task "
                "ON task.orchestration_task_id=attempt.orchestration_task_id "
                "WHERE task.plan_id=?"
                ")",
                (NOW, plan_id),
            )
            connection.commit()

    def _complete_all_plan_tasks(self, plan_id: str) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_tasks SET status='COMPLETED', "
                "failure_reason=NULL, finished_at=? WHERE plan_id=?",
                (NOW, plan_id),
            )
            connection.execute(
                "UPDATE orchestration_attempts SET status='COMPLETED', finished_at=? "
                "WHERE orchestration_task_id IN ("
                "SELECT orchestration_task_id FROM orchestration_tasks WHERE plan_id=?"
                ")",
                (NOW, plan_id),
            )
            connection.commit()

    def _finish_task_with_pending_human_gate(
        self,
        task: sqlite3.Row,
        attempt_id: str,
        suffix: str,
    ) -> dict[str, Any]:
        run_id = f"run-{suffix}-human-gate"
        project_id = f"lifecycle-{suffix}-human-gate"
        self._insert_project(project_id)
        self._insert_run(run_id, status="REVIEWING", project_id=project_id)
        review = self._seed_review(
            run_id,
            f"{suffix}-human-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
        result = INTEGRATOR.record_non_integration(
            run=run,
            review=review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(
            task,
            attempt_id,
            {"kind": "PIPELINE", "integration": result},
        )
        return result

    def _assert_execute_pipeline_ignores_resolved_marker(
        self,
        *,
        marker: str,
        suffix: str,
    ) -> None:
        plan_id, _, sibling_attempt_id, sibling_task = (
            self._create_human_gate_with_running_sibling(suffix)
        )
        self._resolve_plan_approvals(plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_plans SET status=?, last_error=? "
                "WHERE plan_id=?",
                (
                    "BLOCKED" if marker == "waiting for human decision" else "RUNNING",
                    marker,
                    plan_id,
                ),
            )
            connection.commit()

        run_id = f"run-{suffix}-pipeline"
        pipeline_project = f"lifecycle-{suffix}-pipeline"
        self._insert_project(pipeline_project)
        self._insert_run(
            run_id,
            status="RUNNING",
            project_id=pipeline_project,
        )
        integration_calls: list[list[str]] = []
        rollback_calls: list[str] = []
        original_run_json = ORCHESTRATOR.run_json
        original_reviewer = ORCHESTRATOR.launch_reviewer_with_transport_retry
        original_rollback = ORCHESTRATOR.rollback_run_best_effort

        def fake_run_json(arguments: list[str], *, timeout: int) -> dict[str, Any]:
            command = Path(arguments[0]).name
            action = arguments[1]
            if command == "orchestra-transaction.py" and action == "begin":
                return {"run_id": run_id}
            if command == "orchestra-worker.py":
                return {"execution_id": f"worker-{suffix}", "exit_code": 0}
            if command == "orchestra-transaction.py" and action == "submit":
                return {"run_id": run_id, "status": "REVIEWING"}
            if command == "orchestra-integrator.py" and action == "apply":
                integration_calls.append(arguments)
                return {
                    "integration_id": None,
                    "run_id": run_id,
                    "action": "INTEGRATE",
                    "status": "COMPLETED",
                    "integrated": True,
                }
            raise AssertionError(arguments)

        def fake_rollback(called_run_id: str, timeout: int) -> bool:
            rollback_calls.append(called_run_id)
            return True

        ORCHESTRATOR.run_json = fake_run_json
        ORCHESTRATOR.launch_reviewer_with_transport_retry = lambda *args, **kwargs: (
            {"execution_id": f"reviewer-{suffix}", "decision": "APPROVE"},
            [],
            {},
        )
        ORCHESTRATOR.rollback_run_best_effort = fake_rollback
        try:
            result = ORCHESTRATOR.execute_pipeline(
                sibling_task,
                sibling_attempt_id,
                "test-instance",
                {
                    "command_timeout_seconds": 5,
                    "worker_timeout_seconds": 5,
                },
            )
        finally:
            ORCHESTRATOR.run_json = original_run_json
            ORCHESTRATOR.launch_reviewer_with_transport_retry = original_reviewer
            ORCHESTRATOR.rollback_run_best_effort = original_rollback

        self.assertEqual(len(integration_calls), 1)
        self.assertEqual(rollback_calls, [])
        self.assertEqual(
            (result["integration"]["action"], result["integration"]["integrated"]),
            ("INTEGRATE", True),
        )

    def test_cli_pause_rejects_cancel_requested_objective(self) -> None:
        objective_id, _, _, _ = self._create_running_plan()
        self._command(OBJECTIVES.command_cancel, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "CANCEL_REQUESTED",
        )

        with self.assertRaisesRegex(
            OBJECTIVES.ObjectiveError,
            "cancellation is pending",
        ):
            self._command(OBJECTIVES.command_pause, objective_id)

        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "CANCEL_REQUESTED",
        )

    def test_cancel_remains_authoritative_after_rejected_cli_pause(self) -> None:
        objective_id, _, attempt_id, _ = self._create_running_plan()
        run_id = "run-rc8-cancel-pause-integration"
        self._insert_run(run_id, status="REVIEWING")
        review = self._seed_review(
            run_id,
            "rc8-cancel-pause-integration",
            decision="APPROVE",
            verdict="PASS",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)

        self._command(OBJECTIVES.command_cancel, objective_id)
        with self.assertRaisesRegex(
            OBJECTIVES.ObjectiveError,
            "cancellation is pending",
        ):
            self._command(OBJECTIVES.command_pause, objective_id)

        original_git = INTEGRATOR.git
        INTEGRATOR.git = lambda *_: self.fail("cancelled work reached Git")
        try:
            result = INTEGRATOR.integrate_approved(
                run=run,
                review=review,
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                evidence={
                    "repository": str(self.root / "repository"),
                    "worktree": str(self.root / "worktree"),
                    "main_before": "a" * 40,
                },
                transaction=None,
            )
        finally:
            INTEGRATOR.git = original_git

        self.assertEqual(
            (result["action"], result["status"], result["integrated"]),
            ("CANCEL", "CANCELLED", False),
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, run.status, "
                    "(SELECT COUNT(*) FROM integration_executions "
                    "WHERE run_id=run.run_id) "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "WHERE objective.objective_id=? AND run.run_id=?",
                    (objective_id, run_id),
                )
            ),
            ("CANCEL_REQUESTED", "REVIEWING", 0),
        )

    def test_cli_resume_rejects_cancellation_and_non_paused_states(self) -> None:
        for status in (
            "CANCEL_REQUESTED",
            "CANCELLED",
            "COMPLETED",
            "FAILED",
            "PAUSE_REQUESTED",
        ):
            with self.subTest(status=status):
                objective_id = self._insert_objective(source="AI")
                with contextlib.closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE objective_queue SET status=? WHERE objective_id=?",
                        (status, objective_id),
                    )
                    connection.commit()
                with self.assertRaisesRegex(
                    OBJECTIVES.ObjectiveError,
                    "only resume from PAUSED",
                ):
                    self._command(OBJECTIVES.command_resume, objective_id)
                self.assertEqual(
                    self._row(
                        "SELECT status FROM objective_queue WHERE objective_id=?",
                        (objective_id,),
                    )[0],
                    status,
                )

    def test_cli_resume_preserves_future_not_before(self) -> None:
        future = "2099-01-01T00:00:00.000Z"
        objective_id = OBJECTIVES.insert_objective(
            objective="Future lifecycle objective",
            source="AI",
            priority=10,
            not_before=future,
            project_ids=[PROJECT_ID],
            max_parallel_tasks=1,
            planning_max_attempts=3,
            plan_id=None,
        )

        for _ in range(2):
            self._command(OBJECTIVES.command_pause, objective_id)
            self._command(OBJECTIVES.command_resume, objective_id)
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT status, not_before FROM objective_queue "
                        "WHERE objective_id=?",
                        (objective_id,),
                    )
                ),
                ("QUEUED", future),
            )

    def test_cli_resume_preserves_not_before_for_existing_ai_plan(self) -> None:
        future = "2099-01-01T00:00:00.000Z"
        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(self._task("future_planned_work")),
            source="AI",
            initial_status="DRAFT",
        )
        objective_id = OBJECTIVES.insert_objective(
            objective="Future planned AI objective",
            source="AI",
            priority=10,
            not_before=future,
            project_ids=[PROJECT_ID],
            max_parallel_tasks=1,
            planning_max_attempts=3,
            plan_id=plan_id,
        )
        self._command(OBJECTIVES.command_pause, objective_id)
        self._command(OBJECTIVES.command_resume, objective_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, not_before, plan_id FROM objective_queue "
                    "WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("QUEUED", future, plan_id),
        )

    def test_stale_selection_after_cancel_cannot_reserve_attempt(self) -> None:
        objective_id, plan_id, _, _ = self._create_running_plan()
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=4))
        attempts_before = int(
            self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]
        )

        self._command(OBJECTIVES.command_cancel, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "CANCEL_REQUESTED",
        )
        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(task_id, instance_id="test-instance")

        self.assertEqual(
            int(self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]),
            attempts_before,
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_tasks "
                "WHERE orchestration_task_id=?",
                (task_id,),
            )[0],
            "READY",
        )
        self.assertEqual(
            int(self._row("SELECT COUNT(*) FROM worker_executions")[0]),
            0,
        )

    def test_stale_selection_after_paused_cannot_reserve_attempt(self) -> None:
        objective_id, _, task_id = self._create_idle_runnable_plan(
            task_key="stale_after_paused"
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=1))
        self._command(OBJECTIVES.command_pause, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "PAUSED",
        )

        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(task_id, instance_id="test-instance")

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, attempt_count FROM orchestration_tasks "
                    "WHERE orchestration_task_id=?",
                    (task_id,),
                )
            ),
            ("READY", 0),
        )

    def test_stale_selection_after_pause_requested_cannot_reserve_attempt(self) -> None:
        objective_id, plan_id, _, _ = self._create_running_plan()
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=4))
        self._command(OBJECTIVES.command_pause, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "PAUSE_REQUESTED",
        )

        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(task_id, instance_id="test-instance")

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, attempt_count FROM orchestration_tasks "
                    "WHERE orchestration_task_id=?",
                    (task_id,),
                )
            ),
            ("READY", 0),
        )

    def test_stale_selection_after_pending_human_gate_cannot_reserve(self) -> None:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=4))

        run_id = "run-rc9-stale-human-gate"
        self._insert_run(run_id, status="REVIEWING")
        review = self._seed_review(
            run_id,
            "rc9-stale-human-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, gate_attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
        INTEGRATOR.record_non_integration(
            run=run,
            review=review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT plan.status, task.status, approval.status "
                    "FROM orchestration_plans AS plan "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN approvals AS approval ON approval.run_id=? "
                    "WHERE plan.plan_id=? AND task.orchestration_task_id=?",
                    (run_id, plan_id, task_id),
                )
            ),
            ("RUNNING", "READY", "PENDING"),
            "the relational gate commits before plan/task projection",
        )
        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(task_id, instance_id="test-instance")
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_tasks "
                "WHERE orchestration_task_id=?",
                (task_id,),
            )[0],
            "READY",
        )
        self.assertEqual(gate_task["plan_id"], plan_id)

    def test_stale_selection_after_plan_recovery_block_cannot_reserve(self) -> None:
        _, plan_id, task_id = self._create_idle_runnable_plan(
            task_key="stale_after_recovery_block"
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=1))
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_plans SET status='BLOCKED', last_error=? "
                "WHERE plan_id=?",
                ("cancellation cleanup requires recovery", plan_id),
            )
            connection.commit()

        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(task_id, instance_id="test-instance")
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT plan.status, task.status, task.attempt_count "
                    "FROM orchestration_plans AS plan "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "WHERE plan.plan_id=? AND task.orchestration_task_id=?",
                    (plan_id, task_id),
                )
            ),
            ("BLOCKED", "READY", 0),
        )

    def test_duplicate_concurrent_stale_reservation_has_one_winner(self) -> None:
        _, _, task_id = self._create_idle_runnable_plan(
            task_key="duplicate_stale_reservation"
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=1))
        start = threading.Event()
        successes: list[str] = []
        failures: list[BaseException] = []

        def reserve() -> None:
            start.wait(timeout=5)
            try:
                attempt_id, _, _ = ORCHESTRATOR.reserve_attempt(
                    task_id,
                    instance_id="test-instance",
                )
                successes.append(attempt_id)
            except BaseException as error:
                failures.append(error)

        workers = [threading.Thread(target=reserve) for _ in range(2)]
        for worker in workers:
            worker.start()
        start.set()
        for worker in workers:
            worker.join(timeout=5)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertIsInstance(failures[0], ORCHESTRATOR.OrchestratorError)
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM orchestration_attempts "
                    "WHERE orchestration_task_id=?",
                    (task_id,),
                )[0]
            ),
            1,
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, attempt_count, max_attempts "
                    "FROM orchestration_tasks WHERE orchestration_task_id=?",
                    (task_id,),
                )
            ),
            ("RUNNING", 1, 1),
        )

    def test_lifecycle_command_commit_wins_reservation_writer_race(self) -> None:
        objective_id, plan_id, _, _ = self._create_running_plan()
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=4))
        command_holds_writer = threading.Event()
        allow_command_commit = threading.Event()
        reservation_finished = threading.Event()
        command_errors: list[BaseException] = []
        reservation_errors: list[BaseException] = []
        reservation_results: list[str] = []
        original_add_event = OBJECTIVES.add_event

        def blocking_add_event(*args: Any, **kwargs: Any) -> None:
            command_holds_writer.set()
            if not allow_command_commit.wait(timeout=5):
                raise AssertionError("reservation race did not release lifecycle command")
            original_add_event(*args, **kwargs)

        def cancel() -> None:
            try:
                self._command(OBJECTIVES.command_cancel, objective_id)
            except BaseException as error:
                command_errors.append(error)

        def reserve() -> None:
            try:
                command_holds_writer.wait(timeout=5)
                reservation_results.append(
                    ORCHESTRATOR.reserve_attempt(
                        task_id,
                        instance_id="test-instance",
                    )[0]
                )
            except BaseException as error:
                reservation_errors.append(error)
            finally:
                reservation_finished.set()

        OBJECTIVES.add_event = blocking_add_event
        command_worker = threading.Thread(target=cancel)
        reservation_worker = threading.Thread(target=reserve)
        try:
            command_worker.start()
            self.assertTrue(command_holds_writer.wait(timeout=5))
            reservation_worker.start()
            self.assertFalse(
                reservation_finished.wait(timeout=0.2),
                "reservation did not wait for the lifecycle writer",
            )
            allow_command_commit.set()
            command_worker.join(timeout=5)
            reservation_worker.join(timeout=5)
        finally:
            allow_command_commit.set()
            OBJECTIVES.add_event = original_add_event

        self.assertFalse(command_worker.is_alive())
        self.assertFalse(reservation_worker.is_alive())
        self.assertEqual(command_errors, [])
        self.assertEqual(reservation_results, [])
        self.assertEqual(len(reservation_errors), 1)
        self.assertIsInstance(
            reservation_errors[0],
            ORCHESTRATOR.OrchestratorError,
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "CANCEL_REQUESTED",
        )

    def test_reservation_commit_wins_lifecycle_writer_race(self) -> None:
        objective_id, _, task_id = self._create_idle_runnable_plan(
            task_key="reservation_wins_writer_race"
        )
        self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=1))
        reservation_holds_writer = threading.Event()
        allow_reservation_commit = threading.Event()
        command_finished = threading.Event()
        reservation_errors: list[BaseException] = []
        command_errors: list[BaseException] = []
        original_add_event = ORCHESTRATOR.add_event

        def blocking_add_event(*args: Any, **kwargs: Any) -> None:
            if kwargs.get("event_type") == "ORCHESTRATION_TASK_STARTED":
                reservation_holds_writer.set()
                if not allow_reservation_commit.wait(timeout=5):
                    raise AssertionError("lifecycle race did not release reservation")
            original_add_event(*args, **kwargs)

        def reserve() -> None:
            try:
                ORCHESTRATOR.reserve_attempt(
                    task_id,
                    instance_id="test-instance",
                )
            except BaseException as error:
                reservation_errors.append(error)

        def pause() -> None:
            try:
                reservation_holds_writer.wait(timeout=5)
                self._command(OBJECTIVES.command_pause, objective_id)
            except BaseException as error:
                command_errors.append(error)
            finally:
                command_finished.set()

        ORCHESTRATOR.add_event = blocking_add_event
        reservation_worker = threading.Thread(target=reserve)
        command_worker = threading.Thread(target=pause)
        try:
            reservation_worker.start()
            self.assertTrue(reservation_holds_writer.wait(timeout=5))
            command_worker.start()
            self.assertFalse(
                command_finished.wait(timeout=0.2),
                "lifecycle command did not wait for the reservation writer",
            )
            allow_reservation_commit.set()
            reservation_worker.join(timeout=5)
            command_worker.join(timeout=5)
        finally:
            allow_reservation_commit.set()
            ORCHESTRATOR.add_event = original_add_event

        self.assertFalse(reservation_worker.is_alive())
        self.assertFalse(command_worker.is_alive())
        self.assertEqual(reservation_errors, [])
        self.assertEqual(command_errors, [])
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, task.status, task.attempt_count "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_tasks AS task ON task.plan_id=objective.plan_id "
                    "WHERE objective.objective_id=? "
                    "AND task.orchestration_task_id=?",
                    (objective_id, task_id),
                )
            ),
            ("PAUSE_REQUESTED", "RUNNING", 1),
        )

    def test_end_to_end_paused_stale_selection_stops_before_pipeline_and_git(self) -> None:
        objective_id, _, task_id = self._create_idle_runnable_plan(
            task_key="paused_stale_pipeline",
            kind="PIPELINE",
            project_id=PROJECT_ID,
        )
        original_health = ORCHESTRATOR.supervisor_is_healthy
        original_pipeline = ORCHESTRATOR.execute_pipeline
        original_git = INTEGRATOR.git
        pipeline_calls: list[str] = []
        git_calls: list[tuple[str, ...]] = []
        ORCHESTRATOR.supervisor_is_healthy = lambda: True
        try:
            self.assertIn(task_id, ORCHESTRATOR.runnable_tasks(set(), capacity=1))
            self._command(OBJECTIVES.command_pause, objective_id)
            self.assertEqual(
                self._row(
                    "SELECT status FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )[0],
                "PAUSED",
            )
            attempts_before = int(
                self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]
            )
            runs_before = int(self._row("SELECT COUNT(*) FROM runs")[0])

            def fake_pipeline(*args: Any, **kwargs: Any) -> dict[str, Any]:
                pipeline_calls.append(task_id)
                return {
                    "kind": "PIPELINE",
                    "integration": {
                        "action": "INTEGRATE",
                        "status": "COMPLETED",
                        "integrated": True,
                    },
                }

            def fake_git(_: Path, *arguments: str) -> str:
                git_calls.append(arguments)
                return ""

            ORCHESTRATOR.execute_pipeline = fake_pipeline
            INTEGRATOR.git = fake_git
            with self.assertRaises(ORCHESTRATOR.OrchestratorError):
                ORCHESTRATOR.execute_task(
                    task_id,
                    instance_id="test-instance",
                    config={"heartbeat_seconds": 1},
                )

            self.assertEqual(pipeline_calls, [])
            self.assertEqual(git_calls, [])
            self.assertEqual(
                int(self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]),
                attempts_before,
            )
            self.assertEqual(
                int(self._row("SELECT COUNT(*) FROM runs")[0]),
                runs_before,
            )
            self.assertEqual(
                int(
                    self._row(
                        "SELECT COUNT(*) FROM integration_executions "
                        "WHERE status='PREPARED'"
                    )[0]
                ),
                0,
            )
            self.assertEqual(
                int(
                    self._row(
                        "SELECT COUNT(*) FROM runs WHERE status='COMMITTING'"
                    )[0]
                ),
                0,
            )
        finally:
            ORCHESTRATOR.supervisor_is_healthy = original_health
            ORCHESTRATOR.execute_pipeline = original_pipeline
            INTEGRATOR.git = original_git

    def test_stale_selection_revalidates_writer_per_project(self) -> None:
        _, _, first_task = self._create_idle_runnable_plan(
            task_key="writer_candidate_one",
            kind="PIPELINE",
            project_id=PROJECT_ID,
        )
        _, _, second_task = self._create_idle_runnable_plan(
            task_key="writer_candidate_two",
            kind="PIPELINE",
            project_id=PROJECT_ID,
        )
        original_health = ORCHESTRATOR.supervisor_is_healthy
        ORCHESTRATOR.supervisor_is_healthy = lambda: True
        try:
            selected = ORCHESTRATOR.runnable_tasks(set(), capacity=4)
        finally:
            ORCHESTRATOR.supervisor_is_healthy = original_health
        self.assertEqual(len(selected), 1)
        candidate = selected[0]
        other = second_task if candidate == first_task else first_task
        ORCHESTRATOR.reserve_attempt(other, instance_id="test-instance")

        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(candidate, instance_id="test-instance")
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_tasks "
                "WHERE orchestration_task_id=?",
                (candidate,),
            )[0],
            "READY",
        )

    def test_stale_selection_revalidates_plan_parallel_limit(self) -> None:
        first_project = "lifecycle-rc9-parallel-one"
        second_project = "lifecycle-rc9-parallel-two"
        self._insert_project(first_project)
        self._insert_project(second_project)
        plan = self._plan(
            self._task(
                "parallel_candidate_one",
                kind="PIPELINE",
                project_id=first_project,
                role_id="worker",
            ),
            self._task(
                "parallel_candidate_two",
                kind="PIPELINE",
                project_id=second_project,
                role_id="worker",
            ),
        )
        plan["max_parallel_tasks"] = 1
        plan_id = ORCHESTRATOR.insert_plan(plan, source="TEST", initial_status="READY")
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        with contextlib.closing(self.connect()) as connection:
            task_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT orchestration_task_id FROM orchestration_tasks "
                    "WHERE plan_id=? ORDER BY task_key",
                    (plan_id,),
                ).fetchall()
            ]
        original_health = ORCHESTRATOR.supervisor_is_healthy
        ORCHESTRATOR.supervisor_is_healthy = lambda: True
        try:
            selected = ORCHESTRATOR.runnable_tasks(set(), capacity=2)
        finally:
            ORCHESTRATOR.supervisor_is_healthy = original_health
        self.assertEqual(len(selected), 1)
        candidate = selected[0]
        other = task_ids[1] if candidate == task_ids[0] else task_ids[0]
        ORCHESTRATOR.reserve_attempt(other, instance_id="test-instance")

        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.reserve_attempt(candidate, instance_id="test-instance")
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM orchestration_tasks "
                    "WHERE plan_id=? AND status='RUNNING'",
                    (plan_id,),
                )[0]
            ),
            1,
        )

    def test_reservation_objective_status_matrix(self) -> None:
        for status in (
            "QUEUED",
            "PLANNING",
            "RUNNING",
            "PAUSE_REQUESTED",
            "PAUSED",
            "CANCEL_REQUESTED",
            "CANCELLED",
            "COMPLETED",
            "FAILED",
        ):
            with self.subTest(status=status):
                objective_id, _, task_id = self._create_idle_runnable_plan(
                    task_key=f"objective_matrix_{status.lower()}"
                )
                self.assertIn(
                    task_id,
                    ORCHESTRATOR.runnable_tasks(set(), capacity=100),
                )
                with contextlib.closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE objective_queue SET status=? WHERE objective_id=?",
                        (status, objective_id),
                    )
                    connection.commit()
                if status == "RUNNING":
                    ORCHESTRATOR.reserve_attempt(
                        task_id,
                        instance_id="test-instance",
                    )
                else:
                    with self.assertRaises(ORCHESTRATOR.OrchestratorError):
                        ORCHESTRATOR.reserve_attempt(
                            task_id,
                            instance_id="test-instance",
                        )

    def test_reservation_plan_status_matrix(self) -> None:
        for status in (
            "DRAFT",
            "READY",
            "RUNNING",
            "BLOCKED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        ):
            with self.subTest(status=status):
                _, plan_id, task_id = self._create_idle_runnable_plan(
                    task_key=f"plan_matrix_{status.lower()}"
                )
                self.assertIn(
                    task_id,
                    ORCHESTRATOR.runnable_tasks(set(), capacity=100),
                )
                with contextlib.closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE orchestration_plans SET status=? WHERE plan_id=?",
                        (status, plan_id),
                    )
                    connection.commit()
                if status in {"READY", "RUNNING"}:
                    ORCHESTRATOR.reserve_attempt(
                        task_id,
                        instance_id="test-instance",
                    )
                else:
                    with self.assertRaises(ORCHESTRATOR.OrchestratorError):
                        ORCHESTRATOR.reserve_attempt(
                            task_id,
                            instance_id="test-instance",
                        )

    def test_ai_objective_resume_continues_with_its_existing_plan(self) -> None:
        objective_id = self._insert_objective(source="AI")
        reserved = ORCHESTRATOR.reserve_ai_objective(
            objective_id,
            instance_id="test-instance",
            config={"global_parallel_objectives": 2},
        )
        self.assertIsNotNone(reserved)
        _, objective_attempt_id = reserved

        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(self._task("planned_work"), objective="Planned AI work"),
            source="AI",
            initial_status="DRAFT",
        )
        execution_id = "orchestrator-execution-planning"
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO orchestrator_executions (
                    execution_id, plan_id, role_id, source_profile,
                    outer_container_name, prompt_path, output_path, marker,
                    exit_code, result_json, created_at, started_at, finished_at
                ) VALUES (?, ?, 'orchestrator', 'test-orchestrator',
                          'planning-container', ?, ?, 'PLANNED', 0, '{}', ?, ?, ?)
                """,
                (
                    execution_id,
                    plan_id,
                    str(self.root / "planner.prompt"),
                    str(self.root / "planner.output"),
                    NOW,
                    NOW,
                    NOW,
                ),
            )
            connection.commit()

        self._command(OBJECTIVES.command_pause, objective_id)
        ORCHESTRATOR.finish_objective_planning_success(
            objective_id,
            objective_attempt_id,
            {"plan_id": plan_id, "execution_id": execution_id},
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, plan_id FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("PAUSED", plan_id),
        )

        self._command(OBJECTIVES.command_resume, objective_id)
        queued = ORCHESTRATOR.next_queued_objective()
        self.assertEqual(queued["objective_id"], objective_id)

        resumed_plan_id = ORCHESTRATOR.promote_planned_objective(objective_id)
        self.assertEqual(resumed_plan_id, plan_id)
        ORCHESTRATOR.refresh_plan_states(resumed_plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, plan_id, planning_attempt_count "
                    "FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("RUNNING", plan_id, 1),
            "resume must reuse the existing plan without a second planning attempt",
        )

        task_count = int(
            self._row(
                "SELECT COUNT(*) FROM orchestration_tasks WHERE plan_id=?",
                (plan_id,),
            )[0]
        )
        self._command(OBJECTIVES.command_pause, objective_id)
        self._command(OBJECTIVES.command_resume, objective_id)
        self.assertEqual(
            ORCHESTRATOR.promote_planned_objective(objective_id),
            plan_id,
        )
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, plan_id, planning_attempt_count "
                    "FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("RUNNING", plan_id, 1),
        )
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM orchestration_tasks WHERE plan_id=?",
                    (plan_id,),
                )[0]
            ),
            task_count,
        )

    def test_non_ai_planned_objective_promotion_is_unchanged(self) -> None:
        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(self._task("declarative_work")),
            source="TEST",
            initial_status="DRAFT",
        )
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)

        self.assertEqual(
            ORCHESTRATOR.promote_planned_objective(objective_id),
            plan_id,
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, plan_id, planning_attempt_count "
                    "FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("RUNNING", plan_id, 0),
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_plans WHERE plan_id=?",
                (plan_id,),
            )[0],
            "READY",
        )

    def test_resume_of_completed_ai_plan_converges_without_starving_queue(self) -> None:
        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(self._task("last_task")),
            source="AI",
            initial_status="READY",
        )
        objective_id = self._insert_objective(source="AI", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks WHERE plan_id=?",
                (plan_id,),
            )[0]
        )
        attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
            task_id,
            instance_id="test-instance",
        )

        self._command(OBJECTIVES.command_pause, objective_id)
        ORCHESTRATOR.finish_task_success(task, attempt_id, {"kind": "NOOP"})
        ORCHESTRATOR.refresh_plan_states(plan_id)
        ORCHESTRATOR.synchronize_objective_states()
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "WHERE objective.objective_id=?",
                    (objective_id,),
                )
            ),
            ("PAUSED", "COMPLETED"),
        )

        following_id = self._insert_objective(source="AI")
        self._command(OBJECTIVES.command_resume, objective_id)

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, plan_id, planning_attempt_count "
                    "FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("COMPLETED", plan_id, 0),
            "a completed plan must converge instead of returning to QUEUED",
        )
        self.assertEqual(
            ORCHESTRATOR.next_queued_objective()["objective_id"],
            following_id,
            "a non-promotable resumed objective must not starve later work",
        )

    def test_resume_has_deterministic_semantics_for_every_plan_status(self) -> None:
        expected_objective_status = {
            "DRAFT": "QUEUED",
            "READY": "QUEUED",
            "RUNNING": "QUEUED",
            "BLOCKED": "RUNNING",
            "COMPLETED": "COMPLETED",
            "FAILED": "FAILED",
            "CANCELLED": "CANCELLED",
        }
        for plan_status, expected in expected_objective_status.items():
            with self.subTest(plan_status=plan_status):
                plan_id = ORCHESTRATOR.insert_plan(
                    self._plan(self._task(f"task_{plan_status.lower()}")),
                    source="TEST",
                    initial_status="DRAFT",
                )
                objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
                with contextlib.closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE orchestration_plans SET status=? WHERE plan_id=?",
                        (plan_status, plan_id),
                    )
                    connection.execute(
                        "UPDATE objective_queue SET status='PAUSED' WHERE objective_id=?",
                        (objective_id,),
                    )
                    connection.commit()

                self._command(OBJECTIVES.command_resume, objective_id)

                self.assertEqual(
                    self._row(
                        "SELECT status FROM objective_queue WHERE objective_id=?",
                        (objective_id,),
                    )[0],
                    expected,
                )

    def test_legacy_queued_terminal_plan_is_healed_before_next_objective(self) -> None:
        plan_id = ORCHESTRATOR.insert_plan(
            self._plan(self._task("legacy_completed")),
            source="AI",
            initial_status="DRAFT",
        )
        objective_id = self._insert_objective(source="AI", plan_id=plan_id)
        following_id = self._insert_objective(source="AI")
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_plans SET status='COMPLETED' WHERE plan_id=?",
                (plan_id,),
            )
            connection.commit()

        self.assertEqual(
            ORCHESTRATOR.promote_planned_objective(objective_id),
            plan_id,
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "COMPLETED",
        )
        self.assertEqual(
            ORCHESTRATOR.next_queued_objective()["objective_id"],
            following_id,
        )

    def test_cancelled_objective_cannot_reserve_new_tasks_or_attempts(self) -> None:
        objective_id, plan_id, _, _ = self._create_running_plan()
        attempts_before = int(
            self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]
        )

        self._command(OBJECTIVES.command_cancel, objective_id)

        self.assertEqual(ORCHESTRATOR.runnable_tasks(set(), capacity=4), [])
        self.assertEqual(
            int(self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]),
            attempts_before,
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0],
            "READY",
            "the queued task remains READY but must be excluded by objective state",
        )

    def test_cancelled_objective_converges_after_active_attempt_finishes(self) -> None:
        objective_id, plan_id, attempt_id, task = self._create_running_plan()
        self._insert_run("run-cancellation")
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                ("run-cancellation", attempt_id),
            )
            connection.commit()

        self._command(OBJECTIVES.command_cancel, objective_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET status='CANCELLED', finished_at=? WHERE run_id=?",
                (NOW, "run-cancellation"),
            )
            connection.commit()
        ORCHESTRATOR.finish_task_success(task, attempt_id, {"kind": "PIPELINE"})
        ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, finished_at IS NOT NULL FROM objective_queue "
                    "WHERE objective_id=?",
                    (objective_id,),
                )
            ),
            ("CANCELLED", 1),
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_plans WHERE plan_id=?",
                (plan_id,),
            )[0],
            "CANCELLED",
        )

    def test_cancel_is_rejected_after_integration_point_of_no_return(self) -> None:
        objective_id, _, attempt_id, _ = self._create_running_plan()
        run_id = "run-integration-committing"
        self._insert_run(run_id, status="COMMITTING")
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()

        with self.assertRaisesRegex(
            OBJECTIVES.ObjectiveError,
            "integration point of no return",
        ):
            self._command(OBJECTIVES.command_cancel, objective_id)

        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "RUNNING",
            "cancel must not claim acceptance after integration is COMMITTING",
        )

    def test_cancel_during_post_commit_pre_git_window_is_rejected(self) -> None:
        objective_id, _, attempt_id, _ = self._create_running_plan()
        run_id = "run-cancel-race"
        self._insert_run(run_id, status="REVIEWING")
        self._seed_human_review(run_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
            review = connection.execute(
                """
                SELECT review.*, execution.execution_id AS review_execution_id
                FROM review_results AS review
                JOIN reviewer_executions AS execution
                  ON execution.review_id=review.review_id
                WHERE review.run_id=?
                """,
                (run_id,),
            ).fetchone()

        git_reached = threading.Event()
        allow_git = threading.Event()
        integration_result: list[dict[str, Any]] = []
        integration_errors: list[BaseException] = []
        original_git = INTEGRATOR.git

        def fake_git(repository: Path, *arguments: str) -> str:
            if arguments[0] == "merge":
                git_reached.set()
                if not allow_git.wait(timeout=5):
                    raise AssertionError("test did not release the Git mutation")
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "b" * 40
            if arguments[0] == "status":
                return ""
            raise AssertionError(arguments)

        class Transaction:
            @staticmethod
            def cleanup_worktree(*_: Any) -> None:
                return None

        def integrate() -> None:
            try:
                integration_result.append(
                    INTEGRATOR.integrate_approved(
                        run=run,
                        review=review,
                        owner=OWNER,
                        decision="APPROVE",
                        verdict="PASS",
                        evidence={
                            "repository": str(self.root / "repository"),
                            "worktree": str(self.root / "worktree"),
                            "main_before": "a" * 40,
                        },
                        transaction=Transaction(),
                    )
                )
            except BaseException as error:
                integration_errors.append(error)

        INTEGRATOR.git = fake_git
        worker = threading.Thread(target=integrate)
        try:
            worker.start()
            self.assertTrue(
                git_reached.wait(timeout=5),
                "integrator did not reach the post-COMMIT, pre-Git window",
            )
            self.assertEqual(
                self._row("SELECT status FROM runs WHERE run_id=?", (run_id,))[0],
                "COMMITTING",
            )
            with self.assertRaisesRegex(
                OBJECTIVES.ObjectiveError,
                "integration point of no return",
            ):
                self._command(OBJECTIVES.command_cancel, objective_id)
        finally:
            allow_git.set()
            worker.join(timeout=5)
            INTEGRATOR.git = original_git

        self.assertFalse(worker.is_alive())
        self.assertEqual(integration_errors, [])
        self.assertEqual(len(integration_result), 1)
        self.assertTrue(integration_result[0]["integrated"])
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "RUNNING",
        )

    def test_cancelled_objective_blocks_post_cancel_integration(self) -> None:
        objective_id, _, attempt_id, task = self._create_running_plan()
        run_id = "run-post-cancel-integration"
        self._insert_run(run_id)
        integration_calls: list[list[str]] = []

        original_run_json = ORCHESTRATOR.run_json
        original_reviewer = ORCHESTRATOR.launch_reviewer_with_transport_retry
        original_rollback = ORCHESTRATOR.rollback_run_best_effort
        rollback_calls: list[str] = []

        def fake_run_json(arguments: list[str], *, timeout: int) -> dict[str, Any]:
            command = Path(arguments[0]).name
            action = arguments[1]
            if command == "orchestra-transaction.py" and action == "begin":
                return {"run_id": run_id}
            if command == "orchestra-worker.py":
                self._command(OBJECTIVES.command_cancel, objective_id)
                return {"execution_id": "worker-after-cancel", "exit_code": 0}
            if command == "orchestra-transaction.py" and action == "submit":
                return {"run_id": run_id, "status": "REVIEWING"}
            if command == "orchestra-integrator.py":
                integration_calls.append(arguments)
                return {
                    "integration_id": "integration-after-cancel",
                    "status": "COMPLETED",
                    "integrated": True,
                }
            raise AssertionError(arguments)

        ORCHESTRATOR.run_json = fake_run_json
        ORCHESTRATOR.launch_reviewer_with_transport_retry = lambda *args, **kwargs: (
            {"execution_id": "review-after-cancel", "decision": "APPROVE"},
            [],
            {},
        )
        def successful_rollback(called_run_id: str, timeout: int) -> bool:
            rollback_calls.append(called_run_id)
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE runs SET status='CANCELLED', finished_at=? WHERE run_id=?",
                    (NOW, called_run_id),
                )
                connection.commit()
            return True

        ORCHESTRATOR.rollback_run_best_effort = successful_rollback
        try:
            result = ORCHESTRATOR.execute_pipeline(
                task,
                attempt_id,
                "test-instance",
                {
                    "command_timeout_seconds": 5,
                    "worker_timeout_seconds": 5,
                },
            )
        finally:
            ORCHESTRATOR.run_json = original_run_json
            ORCHESTRATOR.launch_reviewer_with_transport_retry = original_reviewer
            ORCHESTRATOR.rollback_run_best_effort = original_rollback

        self.assertEqual(
            result["integration"],
            {
                "integration_id": None,
                "run_id": run_id,
                "action": "CANCEL",
                "status": "CANCELLED",
                "integrated": False,
                "reason_code": "objective_cancel_requested",
            },
        )
        self.assertEqual(
            integration_calls,
            [],
            "the active pipeline reached integrator.apply after objective cancellation",
        )

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET status='REVIEWING' WHERE run_id=?",
                (run_id,),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
        authoritative_result = INTEGRATOR.integrate_approved(
            run=run,
            review={},
            owner=OWNER,
            decision="APPROVE",
            verdict="PASS",
            evidence={
                "repository": str(self.root / "repository"),
                "worktree": str(self.root / "worktree"),
            },
            transaction=None,
        )
        self.assertEqual(authoritative_result, result["integration"])
        self.assertEqual(rollback_calls, [run_id])
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM integration_executions WHERE run_id=?",
                    (run_id,),
                )[0]
            ),
            0,
        )
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM orchestration_attempts "
                    "WHERE orchestration_task_id=?",
                    (task["orchestration_task_id"],),
                )[0]
            ),
            1,
            "structured cancellation must not create a retry",
        )
        self.assertIsNone(
            self._row("SELECT recovery_decision FROM runs WHERE run_id=?", (run_id,))[0]
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET status='CANCELLED', finished_at=? WHERE run_id=?",
                (NOW, run_id),
            )
            connection.commit()
        ORCHESTRATOR.finish_task_success(task, attempt_id, result)
        ORCHESTRATOR.synchronize_objective_states()
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan "
                    "ON plan.plan_id=objective.plan_id "
                    "WHERE objective.objective_id=?",
                    (objective_id,),
                )
            ),
            ("CANCELLED", "CANCELLED"),
        )
        self.assertEqual(
            self._row(
                "SELECT status FROM orchestration_attempts WHERE attempt_id=?",
                (attempt_id,),
            )[0],
            "COMPLETED",
        )

    def test_ambiguous_objective_linkage_fails_before_integration(self) -> None:
        _, _, first_attempt_id, _ = self._create_running_plan()
        run_id = "run-ambiguous-objectives"
        self._insert_run(run_id, status="REVIEWING")
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, first_attempt_id),
            )
            connection.commit()

        second_project = "lifecycle-ambiguous-objective-two"
        self._insert_project(second_project)
        _, _, second_task_id = self._create_idle_runnable_plan(
            task_key="ambiguous_objective_two",
            kind="PIPELINE",
            project_id=second_project,
        )
        second_attempt_id, _, _ = ORCHESTRATOR.reserve_attempt(
            second_task_id,
            instance_id="test-instance",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, second_attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)

        with self.assertRaisesRegex(
            INTEGRATOR.IntegrationError,
            "multiple objectives",
        ):
            INTEGRATOR.integrate_approved(
                run=run,
                review={},
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                evidence={
                    "repository": str(self.root / "repository"),
                    "worktree": str(self.root / "worktree"),
                },
                transaction=None,
            )
        self.assertEqual(
            int(self._row("SELECT COUNT(*) FROM integration_executions")[0]),
            0,
        )

    def test_block_human_freezes_already_running_sibling_outcomes(self) -> None:
        for sibling_outcome in ("success", "failure"):
            with self.subTest(sibling_outcome=sibling_outcome):
                _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
                sibling_id = str(
                    self._row(
                        "SELECT orchestration_task_id FROM orchestration_tasks "
                        "WHERE plan_id=? AND task_key='after_cancel'",
                        (plan_id,),
                    )[0]
                )
                with contextlib.closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE orchestration_tasks SET max_attempts=2 "
                        "WHERE orchestration_task_id=?",
                        (sibling_id,),
                    )
                    connection.commit()
                sibling_attempt_id, _, sibling_task = ORCHESTRATOR.reserve_attempt(
                    sibling_id,
                    instance_id="test-instance",
                )

                self._finish_task_with_pending_human_gate(
                    gate_task,
                    gate_attempt_id,
                    f"parallel-outcome-{sibling_outcome}",
                )
                if sibling_outcome == "success":
                    ORCHESTRATOR.finish_task_success(
                        sibling_task,
                        sibling_attempt_id,
                        {"kind": "NOOP"},
                    )
                else:
                    ORCHESTRATOR.finish_task_failure(
                        sibling_task,
                        sibling_attempt_id,
                        "parallel task failed after human gate",
                    )

                self.assertEqual(
                    tuple(
                        self._row(
                            "SELECT plan.status, task.status "
                            "FROM orchestration_plans AS plan "
                            "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                            "WHERE plan.plan_id=? AND task.orchestration_task_id=?",
                            (plan_id, sibling_id),
                        )
                    ),
                    ("BLOCKED", "BLOCKED"),
                    "an already-running sibling must not finish or retry past the gate",
                )

    def test_block_human_plan_rejects_direct_parallel_integration(self) -> None:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        sibling_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        sibling_attempt_id, _, _ = ORCHESTRATOR.reserve_attempt(
            sibling_id,
            instance_id="test-instance",
        )
        sibling_project = "lifecycle-direct-parallel-sibling"
        self._insert_project(sibling_project)
        run_id = "run-parallel-after-human-gate"
        self._insert_run(
            run_id,
            status="REVIEWING",
            project_id=sibling_project,
        )
        review = self._seed_review(
            run_id,
            "parallel-after-human-gate",
            decision="APPROVE",
            verdict="PASS",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, sibling_attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)

        gate_run_id = "run-direct-parallel-human-gate"
        self._insert_run(gate_run_id, status="REVIEWING")
        gate_review = self._seed_review(
            gate_run_id,
            "direct-parallel-human-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (gate_run_id, gate_attempt_id),
            )
            connection.commit()
            gate_run = INTEGRATOR.get_run(connection, gate_run_id)
        gate_result = INTEGRATOR.record_non_integration(
            run=gate_run,
            review=gate_review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(
            gate_task,
            gate_attempt_id,
            {"kind": "PIPELINE", "integration": gate_result},
        )
        result = INTEGRATOR.integrate_approved(
            run=run,
            review=review,
            owner=OWNER,
            decision="APPROVE",
            verdict="PASS",
            evidence={
                "repository": str(self.root / "repository"),
                "worktree": str(self.root / "worktree"),
                "main_before": "a" * 40,
            },
            transaction=None,
        )

        self.assertEqual(result["action"], "BLOCK_HUMAN")
        self.assertEqual(result["status"], "BLOCKED")
        self.assertFalse(result["integrated"])
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM integration_executions "
                    "WHERE run_id=? AND status='PREPARED'",
                    (run_id,),
                )[0]
            ),
            0,
        )
        review_result = INTEGRATOR.record_non_integration(
            run=run,
            review=review,
            owner=OWNER,
            decision="REJECT",
            verdict="FIX",
            action="REJECT",
            evidence={"main_before": "a" * 40},
        )
        self.assertEqual(review_result["action"], "BLOCK_HUMAN")
        self.assertEqual(review_result["reason_code"], "plan_waiting_human")
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM integration_executions WHERE run_id=?",
                    (run_id,),
                )[0]
            ),
            0,
            "a parallel review outcome must not create a new integration record",
        )

    def test_block_human_stops_parallel_pipeline_after_review(self) -> None:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        sibling_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        sibling_attempt_id, _, sibling_task = ORCHESTRATOR.reserve_attempt(
            sibling_id,
            instance_id="test-instance",
        )
        run_id = "run-parallel-review-gate"
        self._insert_run(run_id, status="REVIEWING")
        integration_calls: list[list[str]] = []
        original_run_json = ORCHESTRATOR.run_json
        original_reviewer = ORCHESTRATOR.launch_reviewer_with_transport_retry
        original_rollback = ORCHESTRATOR.rollback_run_best_effort

        def fake_run_json(arguments: list[str], *, timeout: int) -> dict[str, Any]:
            command = Path(arguments[0]).name
            action = arguments[1]
            if command == "orchestra-transaction.py" and action == "begin":
                return {"run_id": run_id}
            if command == "orchestra-worker.py":
                return {"execution_id": "worker-parallel-review", "exit_code": 0}
            if command == "orchestra-transaction.py" and action == "submit":
                return {"run_id": run_id, "status": "REVIEWING"}
            if command == "orchestra-integrator.py":
                integration_calls.append(arguments)
                return {"status": "COMPLETED", "integrated": True}
            raise AssertionError(arguments)

        def reviewer_then_gate(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
            self._finish_task_with_pending_human_gate(
                gate_task,
                gate_attempt_id,
                "parallel-review",
            )
            return (
                {"execution_id": "review-parallel-gate", "decision": "APPROVE"},
                [],
                {},
            )

        def successful_rollback(called_run_id: str, timeout: int) -> bool:
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "DELETE FROM project_locks WHERE run_id=?",
                    (called_run_id,),
                )
                connection.execute(
                    "UPDATE runs SET status='CANCELLED', finished_at=? WHERE run_id=?",
                    (NOW, called_run_id),
                )
                connection.commit()
            return True

        ORCHESTRATOR.run_json = fake_run_json
        ORCHESTRATOR.launch_reviewer_with_transport_retry = reviewer_then_gate
        ORCHESTRATOR.rollback_run_best_effort = successful_rollback
        try:
            result = ORCHESTRATOR.execute_pipeline(
                sibling_task,
                sibling_attempt_id,
                "test-instance",
                {
                    "command_timeout_seconds": 5,
                    "worker_timeout_seconds": 5,
                },
            )
        finally:
            ORCHESTRATOR.run_json = original_run_json
            ORCHESTRATOR.launch_reviewer_with_transport_retry = original_reviewer
            ORCHESTRATOR.rollback_run_best_effort = original_rollback

        self.assertEqual(integration_calls, [])
        self.assertEqual(result["integration"]["action"], "BLOCK_HUMAN")
        self.assertEqual(
            result["integration"]["reason_code"],
            "plan_waiting_human",
        )
        self.assertTrue(result["integration"]["cleanup_completed"])

    def test_cancellation_rollback_failures_enter_durable_recovery(self) -> None:
        original_run_command = ORCHESTRATOR.run_command
        try:
            for failure_kind in ("nonzero", "exception"):
                with self.subTest(failure_kind=failure_kind):
                    with contextlib.closing(self.connect()) as connection:
                        connection.execute(
                            "DELETE FROM project_locks WHERE project_id=?",
                            (PROJECT_ID,),
                        )
                        connection.commit()
                    run_id = f"run-cancel-cleanup-{failure_kind}"
                    self._insert_run(run_id, status="REVIEWING")

                    if failure_kind == "nonzero":
                        ORCHESTRATOR.run_command = lambda *args, **kwargs: (
                            subprocess.CompletedProcess(args[0], 17, "", "rollback failed")
                        )
                    else:
                        def raise_rollback(*args: Any, **kwargs: Any) -> Any:
                            raise OSError("rollback transport failed")

                        ORCHESTRATOR.run_command = raise_rollback

                    cleanup_complete = ORCHESTRATOR.rollback_run_best_effort(
                        run_id,
                        timeout=5,
                    )
                    self.assertIs(cleanup_complete, False)
                    self.assertEqual(
                        tuple(
                            self._row(
                                "SELECT status, recovery_decision FROM runs WHERE run_id=?",
                                (run_id,),
                            )
                        ),
                        ("RECOVERING", "ROLLBACK_SAFE"),
                    )
                    self.assertEqual(
                        int(
                            self._row(
                                "SELECT COUNT(*) FROM project_locks WHERE run_id=?",
                                (run_id,),
                            )[0]
                        ),
                        1,
                        "Recovery must retain visibility and ownership of active cleanup",
                    )
        finally:
            ORCHESTRATOR.run_command = original_run_command

    def test_cancelled_pipeline_waits_for_recovery_when_rollback_fails(self) -> None:
        objective_id, plan_id, attempt_id, task = self._create_running_plan()
        run_id = "run-cancel-cleanup-pipeline"
        self._insert_run(run_id, status="REVIEWING")
        original_run_json = ORCHESTRATOR.run_json
        original_reviewer = ORCHESTRATOR.launch_reviewer_with_transport_retry
        original_run_command = ORCHESTRATOR.run_command

        def fake_run_json(arguments: list[str], *, timeout: int) -> dict[str, Any]:
            command = Path(arguments[0]).name
            action = arguments[1]
            if command == "orchestra-transaction.py" and action == "begin":
                return {"run_id": run_id}
            if command == "orchestra-worker.py":
                self._command(OBJECTIVES.command_cancel, objective_id)
                return {"execution_id": "worker-cancel-cleanup", "exit_code": 0}
            if command == "orchestra-transaction.py" and action == "submit":
                return {"run_id": run_id, "status": "REVIEWING"}
            raise AssertionError(arguments)

        ORCHESTRATOR.run_json = fake_run_json
        ORCHESTRATOR.launch_reviewer_with_transport_retry = lambda *args, **kwargs: (
            {"execution_id": "review-cancel-cleanup", "decision": "APPROVE"},
            [],
            {},
        )
        ORCHESTRATOR.run_command = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            17,
            "",
            "rollback failed",
        )
        try:
            result = ORCHESTRATOR.execute_pipeline(
                task,
                attempt_id,
                "test-instance",
                {
                    "command_timeout_seconds": 5,
                    "worker_timeout_seconds": 5,
                },
            )
        finally:
            ORCHESTRATOR.run_json = original_run_json
            ORCHESTRATOR.launch_reviewer_with_transport_retry = original_reviewer
            ORCHESTRATOR.run_command = original_run_command

        self.assertEqual(result["integration"]["status"], "RECOVERING")
        self.assertFalse(result["integration"]["cleanup_completed"])
        ORCHESTRATOR.finish_task_cancellation_recovery(task, attempt_id, result)
        ORCHESTRATOR.synchronize_objective_states()
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, task.status, run.status, "
                    "run.recovery_decision "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "WHERE objective.objective_id=? AND task.orchestration_task_id=?",
                    (objective_id, task["orchestration_task_id"]),
                )
            ),
            (
                "CANCEL_REQUESTED",
                "BLOCKED",
                "BLOCKED",
                "RECOVERING",
                "ROLLBACK_SAFE",
            ),
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "DELETE FROM project_locks WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE runs SET status='CANCELLED', finished_at=? WHERE run_id=?",
                (NOW, run_id),
            )
            connection.commit()
        ORCHESTRATOR.synchronize_objective_states()
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "WHERE objective.objective_id=?",
                    (objective_id,),
                )
            ),
            ("CANCELLED", "CANCELLED"),
            "Recovery completion must release the pending business cancellation",
        )

    def test_block_human_gate_is_not_automatically_rolled_back(self) -> None:
        _, _, attempt_id, task = self._create_running_plan()
        run_id = "run-block-human"
        self._insert_run(run_id, status="REVIEWING")
        self._seed_human_review(run_id)
        rollback_calls: list[list[str]] = []

        original_run_json = ORCHESTRATOR.run_json
        original_reviewer = ORCHESTRATOR.launch_reviewer_with_transport_retry
        original_run_command = ORCHESTRATOR.run_command

        def fake_run_json(arguments: list[str], *, timeout: int) -> dict[str, Any]:
            command = Path(arguments[0]).name
            action = arguments[1]
            if command == "orchestra-transaction.py" and action == "begin":
                return {"run_id": run_id}
            if command == "orchestra-worker.py":
                return {"execution_id": "worker-block-human", "exit_code": 0}
            if command == "orchestra-transaction.py" and action == "submit":
                return {"run_id": run_id, "status": "REVIEWING"}
            if command == "orchestra-integrator.py":
                with contextlib.closing(self.connect()) as connection:
                    run = INTEGRATOR.get_run(connection, run_id)
                    review = connection.execute(
                        """
                        SELECT review.*, execution.execution_id AS review_execution_id
                        FROM review_results AS review
                        JOIN reviewer_executions AS execution
                          ON execution.review_id=review.review_id
                        WHERE review.run_id=?
                        """,
                        (run_id,),
                    ).fetchone()
                return INTEGRATOR.record_non_integration(
                    run=run,
                    review=review,
                    owner=OWNER,
                    decision="BLOCK_HUMAN",
                    verdict="HUMAN",
                    action="BLOCK_HUMAN",
                    evidence={"main_before": "a" * 40},
                )
            raise AssertionError(arguments)

        def fake_run_command(
            arguments: list[str],
            *,
            timeout: int,
            check: bool = True,
        ) -> subprocess.CompletedProcess[str]:
            rollback_calls.append(arguments)
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                    "WHERE run_id=? AND status='PENDING'",
                    (NOW, run_id),
                )
                connection.execute(
                    "UPDATE runs SET status='CANCELLED', "
                    "recovery_decision='ROLLBACK_SAFE', finished_at=? WHERE run_id=?",
                    (NOW, run_id),
                )
                connection.commit()
            return subprocess.CompletedProcess(arguments, 0, "", "")

        ORCHESTRATOR.run_json = fake_run_json
        ORCHESTRATOR.launch_reviewer_with_transport_retry = lambda *args, **kwargs: (
            {"execution_id": "reviewer-execution-human", "decision": "BLOCK_HUMAN"},
            [],
            {},
        )
        ORCHESTRATOR.run_command = fake_run_command
        try:
            result = ORCHESTRATOR.execute_pipeline(
                task,
                attempt_id,
                "test-instance",
                {
                    "command_timeout_seconds": 5,
                    "worker_timeout_seconds": 5,
                },
            )
        finally:
            ORCHESTRATOR.run_json = original_run_json
            ORCHESTRATOR.launch_reviewer_with_transport_retry = original_reviewer
            ORCHESTRATOR.run_command = original_run_command

        self.assertEqual(result["integration"]["action"], "BLOCK_HUMAN")
        self.assertFalse(result["integration"]["integrated"])

        state = self._row(
            """
            SELECT run.status, run.recovery_decision, approval.status
            FROM runs AS run
            JOIN approvals AS approval ON approval.run_id=run.run_id
            WHERE run.run_id=?
            """,
            (run_id,),
        )
        self.assertEqual(
            (rollback_calls, state["status"], state["recovery_decision"], state[2]),
            ([], "WAITING_HUMAN", "BLOCK_HUMAN", "PENDING"),
            "BLOCK_HUMAN was treated as pipeline failure and rolled back, losing the pending gate",
        )

        attempts_before = int(
            self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]
        )
        ORCHESTRATOR.finish_task_waiting_human(task, attempt_id, result)
        for _ in range(3):
            ORCHESTRATOR.refresh_plan_states(task["plan_id"])
            ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            ORCHESTRATOR.runnable_tasks(set(), capacity=4),
            [],
            "a stable human gate must freeze every remaining task in the plan",
        )
        self.assertEqual(
            int(self._row("SELECT COUNT(*) FROM orchestration_attempts")[0]),
            attempts_before,
        )
        stable_state = self._row(
            """
            SELECT run.status, run.recovery_decision, approval.status,
                   integration.status
            FROM runs AS run
            JOIN approvals AS approval ON approval.run_id=run.run_id
            JOIN integration_executions AS integration
              ON integration.run_id=run.run_id
            WHERE run.run_id=?
            """,
            (run_id,),
        )
        self.assertEqual(
            tuple(stable_state),
            ("WAITING_HUMAN", "BLOCK_HUMAN", "PENDING", "BLOCKED"),
        )
        self.assertEqual(rollback_calls, [])

    def test_block_human_defers_gate_behind_prepared_sibling(self) -> None:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        sibling_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        sibling_attempt_id, _, sibling_task = ORCHESTRATOR.reserve_attempt(
            sibling_id,
            instance_id="test-instance",
        )
        run_id = "run-human-gate-prepared-race"
        self._insert_run(run_id, status="REVIEWING")
        self._seed_human_review(run_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, sibling_attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
            review = connection.execute(
                """
                SELECT review.*, execution.execution_id AS review_execution_id
                FROM review_results AS review
                JOIN reviewer_executions AS execution
                  ON execution.review_id=review.review_id
                WHERE review.run_id=?
                """,
                (run_id,),
            ).fetchone()

        git_reached = threading.Event()
        allow_git = threading.Event()
        integration_result: list[dict[str, Any]] = []
        integration_errors: list[BaseException] = []
        original_git = INTEGRATOR.git

        def fake_git(repository: Path, *arguments: str) -> str:
            if arguments[0] == "merge":
                git_reached.set()
                if not allow_git.wait(timeout=5):
                    raise AssertionError("test did not release the Git mutation")
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "b" * 40
            if arguments[0] == "status":
                return ""
            raise AssertionError(arguments)

        class Transaction:
            @staticmethod
            def cleanup_worktree(*_: Any) -> None:
                return None

        def integrate() -> None:
            try:
                integration_result.append(
                    INTEGRATOR.integrate_approved(
                        run=run,
                        review=review,
                        owner=OWNER,
                        decision="APPROVE",
                        verdict="PASS",
                        evidence={
                            "repository": str(self.root / "repository"),
                            "worktree": str(self.root / "worktree"),
                            "main_before": "a" * 40,
                        },
                        transaction=Transaction(),
                    )
                )
            except BaseException as error:
                integration_errors.append(error)

        INTEGRATOR.git = fake_git
        worker = threading.Thread(target=integrate)
        try:
            worker.start()
            self.assertTrue(git_reached.wait(timeout=5))
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT run.status, integration.status "
                        "FROM runs AS run JOIN integration_executions AS integration "
                        "ON integration.run_id=run.run_id WHERE run.run_id=?",
                        (run_id,),
                    )
                ),
                ("COMMITTING", "PREPARED"),
            )
            self._finish_task_with_pending_human_gate(
                gate_task,
                gate_attempt_id,
                "prepared-race",
            )
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT status, last_error FROM orchestration_plans "
                        "WHERE plan_id=?",
                        (plan_id,),
                    )
                ),
                (
                    "RUNNING",
                    "waiting for in-flight integration before human decision",
                ),
                "the human gate must not become authoritative after sibling PONR",
            )
        finally:
            allow_git.set()
            worker.join(timeout=5)
            INTEGRATOR.git = original_git

        self.assertFalse(worker.is_alive())
        self.assertEqual(integration_errors, [])
        self.assertEqual(len(integration_result), 1)
        self.assertTrue(integration_result[0]["integrated"])
        ORCHESTRATOR.finish_task_success(
            sibling_task,
            sibling_attempt_id,
            {"kind": "PIPELINE", "integration": integration_result[0]},
        )
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT plan.status, plan.last_error, task.status "
                    "FROM orchestration_plans AS plan "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "WHERE plan.plan_id=? AND task.orchestration_task_id=?",
                    (plan_id, sibling_id),
                )
            ),
            ("BLOCKED", "waiting for human decision", "COMPLETED"),
        )

    def test_deferred_human_gate_blocks_third_sibling_before_ponr(self) -> None:
        project_b = "lifecycle-project-b"
        project_c = "lifecycle-project-c"
        self._insert_project(project_b)
        self._insert_project(project_c)
        plan = self._plan(
            self._task(
                "gate_a",
                kind="PIPELINE",
                project_id=PROJECT_ID,
                role_id="worker",
            ),
            self._task(
                "grandfathered_b",
                kind="PIPELINE",
                project_id=project_b,
                role_id="worker",
            ),
            self._task(
                "pre_ponr_c",
                kind="PIPELINE",
                project_id=project_c,
                role_id="worker",
            ),
        )
        plan["max_parallel_tasks"] = 3
        plan_id = ORCHESTRATOR.insert_plan(
            plan,
            source="TEST",
            initial_status="READY",
        )
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)

        attempts: dict[str, tuple[str, sqlite3.Row]] = {}
        with contextlib.closing(self.connect()) as connection:
            task_rows = connection.execute(
                """
                SELECT task_key, orchestration_task_id
                FROM orchestration_tasks
                WHERE plan_id=?
                """,
                (plan_id,),
            ).fetchall()
        for row in task_rows:
            attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
                str(row["orchestration_task_id"]),
                instance_id="test-instance",
            )
            attempts[str(row["task_key"])] = (attempt_id, task)

        run_a = "run-deferred-gate-a"
        run_b = "run-deferred-grandfathered-b"
        run_c = "run-deferred-pre-ponr-c"
        self._insert_run(run_a, status="REVIEWING")
        self._insert_run(run_b, status="REVIEWING", project_id=project_b)
        self._insert_run(run_c, status="REVIEWING", project_id=project_c)
        review_a = self._seed_review(
            run_a,
            "deferred-a",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        review_b = self._seed_review(
            run_b,
            "deferred-b",
            decision="APPROVE",
            verdict="PASS",
        )
        review_c = self._seed_review(
            run_c,
            "deferred-c",
            decision="APPROVE",
            verdict="PASS",
        )
        with contextlib.closing(self.connect()) as connection:
            for key, run_id in (
                ("gate_a", run_a),
                ("grandfathered_b", run_b),
                ("pre_ponr_c", run_c),
            ):
                connection.execute(
                    "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                    (run_id, attempts[key][0]),
                )
            connection.commit()
            row_a = INTEGRATOR.get_run(connection, run_a)
            row_b = INTEGRATOR.get_run(connection, run_b)
            row_c = INTEGRATOR.get_run(connection, run_c)

        git_reached_b = threading.Event()
        allow_git_b = threading.Event()
        git_merges: list[str] = []
        integration_b: list[dict[str, Any]] = []
        integration_errors: list[BaseException] = []
        original_git = INTEGRATOR.git

        def fake_git(repository: Path, *arguments: str) -> str:
            if arguments[0] == "merge":
                project = Path(repository).name
                git_merges.append(project)
                if project == f"repository-{project_b}":
                    git_reached_b.set()
                    if not allow_git_b.wait(timeout=5):
                        raise AssertionError("test did not release grandfathered Git")
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "b" * 40
            if arguments[0] == "status":
                return ""
            raise AssertionError(arguments)

        class Transaction:
            @staticmethod
            def cleanup_worktree(*_: Any) -> None:
                return None

        def integrate_b() -> None:
            try:
                integration_b.append(
                    INTEGRATOR.integrate_approved(
                        run=row_b,
                        review=review_b,
                        owner=OWNER,
                        decision="APPROVE",
                        verdict="PASS",
                        evidence={
                            "repository": str(self.root / f"repository-{project_b}"),
                            "worktree": str(self.root / "worktree-b"),
                            "main_before": "a" * 40,
                        },
                        transaction=Transaction(),
                    )
                )
            except BaseException as error:
                integration_errors.append(error)

        INTEGRATOR.git = fake_git
        worker_b = threading.Thread(target=integrate_b)
        try:
            worker_b.start()
            self.assertTrue(git_reached_b.wait(timeout=5))
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT run.status, integration.status "
                        "FROM runs AS run JOIN integration_executions AS integration "
                        "ON integration.run_id=run.run_id WHERE run.run_id=?",
                        (run_b,),
                    )
                ),
                ("COMMITTING", "PREPARED"),
            )

            result_a = INTEGRATOR.record_non_integration(
                run=row_a,
                review=review_a,
                owner=OWNER,
                decision="BLOCK_HUMAN",
                verdict="HUMAN",
                action="BLOCK_HUMAN",
                evidence={"main_before": "a" * 40},
            )
            ORCHESTRATOR.finish_task_waiting_human(
                attempts["gate_a"][1],
                attempts["gate_a"][0],
                {"kind": "PIPELINE", "integration": result_a},
            )
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT status, last_error FROM orchestration_plans "
                        "WHERE plan_id=?",
                        (plan_id,),
                    )
                ),
                (
                    "RUNNING",
                    "waiting for in-flight integration before human decision",
                ),
            )

            result_c = INTEGRATOR.integrate_approved(
                run=row_c,
                review=review_c,
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                evidence={
                    "repository": str(self.root / f"repository-{project_c}"),
                    "worktree": str(self.root / "worktree-c"),
                    "main_before": "a" * 40,
                },
                transaction=Transaction(),
            )
            self.assertEqual(
                (
                    result_c["action"],
                    result_c["status"],
                    result_c["integrated"],
                ),
                ("BLOCK_HUMAN", "BLOCKED", False),
            )
            self.assertNotIn(f"repository-{project_c}", git_merges)
            self.assertEqual(
                int(
                    self._row(
                        "SELECT COUNT(*) FROM integration_executions "
                        "WHERE run_id=? AND status='PREPARED'",
                        (run_c,),
                    )[0]
                ),
                0,
            )
            ORCHESTRATOR.finish_task_waiting_human(
                attempts["pre_ponr_c"][1],
                attempts["pre_ponr_c"][0],
                {"kind": "PIPELINE", "integration": result_c},
            )
        finally:
            allow_git_b.set()
            worker_b.join(timeout=5)
            INTEGRATOR.git = original_git

        self.assertFalse(worker_b.is_alive())
        self.assertEqual(integration_errors, [])
        self.assertEqual(len(integration_b), 1)
        self.assertTrue(integration_b[0]["integrated"])
        ORCHESTRATOR.finish_task_success(
            attempts["grandfathered_b"][1],
            attempts["grandfathered_b"][0],
            {"kind": "PIPELINE", "integration": integration_b[0]},
        )
        ORCHESTRATOR.refresh_plan_states(plan_id)

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM approvals WHERE status='PENDING'"
                )[0]
            ),
            1,
        )
        self.assertEqual(ORCHESTRATOR.runnable_tasks(set(), capacity=4), [])

    def test_deferred_human_gate_survives_pre_ponr_cleanup_recovery(self) -> None:
        projects = {
            "grandfathered_b": "lifecycle-rc5-project-b",
            "pre_ponr_c": "lifecycle-rc5-project-c",
            "pre_ponr_d": "lifecycle-rc5-project-d",
        }
        for project_id in projects.values():
            self._insert_project(project_id)
        plan = self._plan(
            self._task("gate_a", kind="PIPELINE", project_id=PROJECT_ID, role_id="worker"),
            *(
                self._task(
                    task_key,
                    kind="PIPELINE",
                    project_id=project_id,
                    role_id="worker",
                )
                for task_key, project_id in projects.items()
            ),
        )
        plan["max_parallel_tasks"] = 4
        plan_id = ORCHESTRATOR.insert_plan(plan, source="TEST", initial_status="READY")
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)

        attempts: dict[str, tuple[str, sqlite3.Row]] = {}
        with contextlib.closing(self.connect()) as connection:
            task_rows = connection.execute(
                "SELECT task_key, orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=?",
                (plan_id,),
            ).fetchall()
        for task_row in task_rows:
            attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
                str(task_row["orchestration_task_id"]),
                instance_id="test-instance",
            )
            attempts[str(task_row["task_key"])] = (attempt_id, task)

        run_ids = {
            "gate_a": "run-rc5-gate-a",
            "grandfathered_b": "run-rc5-grandfathered-b",
            "pre_ponr_c": "run-rc5-pre-ponr-c",
            "pre_ponr_d": "run-rc5-pre-ponr-d",
        }
        reviews: dict[str, sqlite3.Row] = {}
        runs: dict[str, sqlite3.Row] = {}
        for task_key, run_id in run_ids.items():
            project_id = PROJECT_ID if task_key == "gate_a" else projects[task_key]
            self._insert_run(run_id, status="REVIEWING", project_id=project_id)
            reviews[task_key] = self._seed_review(
                run_id,
                f"rc5-{task_key}",
                decision="BLOCK_HUMAN" if task_key == "gate_a" else "APPROVE",
                verdict="HUMAN" if task_key == "gate_a" else "PASS",
            )
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                    (run_id, attempts[task_key][0]),
                )
                connection.commit()
                runs[task_key] = INTEGRATOR.get_run(connection, run_id)

        git_reached_b = threading.Event()
        allow_git_b = threading.Event()
        git_merges: list[str] = []
        integration_b: list[dict[str, Any]] = []
        integration_errors: list[BaseException] = []
        original_git = INTEGRATOR.git
        original_run_command = ORCHESTRATOR.run_command

        def fake_git(repository: Path, *arguments: str) -> str:
            if arguments[0] == "merge":
                project = Path(repository).name
                git_merges.append(project)
                if project == f"repository-{projects['grandfathered_b']}":
                    git_reached_b.set()
                    if not allow_git_b.wait(timeout=5):
                        raise AssertionError("test did not release grandfathered Git")
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "b" * 40
            if arguments[0] == "status":
                return ""
            raise AssertionError(arguments)

        class Transaction:
            @staticmethod
            def cleanup_worktree(*_: Any) -> None:
                return None

        def integrate_b() -> None:
            try:
                integration_b.append(
                    INTEGRATOR.integrate_approved(
                        run=runs["grandfathered_b"],
                        review=reviews["grandfathered_b"],
                        owner=OWNER,
                        decision="APPROVE",
                        verdict="PASS",
                        evidence={
                            "repository": str(
                                self.root / f"repository-{projects['grandfathered_b']}"
                            ),
                            "worktree": str(self.root / "worktree-rc5-b"),
                            "main_before": "a" * 40,
                        },
                        transaction=Transaction(),
                    )
                )
            except BaseException as error:
                integration_errors.append(error)

        INTEGRATOR.git = fake_git
        ORCHESTRATOR.run_command = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 23, "", "rollback failed"
        )
        worker_b = threading.Thread(target=integrate_b)
        try:
            worker_b.start()
            self.assertTrue(git_reached_b.wait(timeout=5))

            result_a = INTEGRATOR.record_non_integration(
                run=runs["gate_a"],
                review=reviews["gate_a"],
                owner=OWNER,
                decision="BLOCK_HUMAN",
                verdict="HUMAN",
                action="BLOCK_HUMAN",
                evidence={"main_before": "a" * 40},
            )
            ORCHESTRATOR.finish_task_waiting_human(
                attempts["gate_a"][1],
                attempts["gate_a"][0],
                {"kind": "PIPELINE", "integration": result_a},
            )
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT status, last_error FROM orchestration_plans "
                        "WHERE plan_id=?",
                        (plan_id,),
                    )
                ),
                ("RUNNING", "waiting for in-flight integration before human decision"),
            )

            result_c = INTEGRATOR.integrate_approved(
                run=runs["pre_ponr_c"],
                review=reviews["pre_ponr_c"],
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                evidence={
                    "repository": str(self.root / f"repository-{projects['pre_ponr_c']}"),
                    "worktree": str(self.root / "worktree-rc5-c"),
                    "main_before": "a" * 40,
                },
                transaction=Transaction(),
            )
            self.assertEqual(
                (result_c["action"], result_c["status"], result_c["integrated"]),
                ("BLOCK_HUMAN", "BLOCKED", False),
            )
            self.assertFalse(
                ORCHESTRATOR.rollback_run_best_effort(run_ids["pre_ponr_c"], 5)
            )
            ORCHESTRATOR.finish_task_waiting_human(
                attempts["pre_ponr_c"][1],
                attempts["pre_ponr_c"][0],
                {"kind": "PIPELINE", "integration": result_c},
            )
            self.assertEqual(
                tuple(
                    self._row(
                        "SELECT plan.status, plan.last_error, run.status, "
                        "run.recovery_decision "
                        "FROM orchestration_plans AS plan "
                        "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                        "JOIN orchestration_attempts AS attempt "
                        "ON attempt.orchestration_task_id=task.orchestration_task_id "
                        "JOIN runs AS run ON run.run_id=attempt.run_id "
                        "WHERE plan.plan_id=? AND run.run_id=?",
                        (plan_id, run_ids["pre_ponr_c"]),
                    )
                ),
                (
                    "BLOCKED",
                    "cancellation cleanup requires recovery",
                    "RECOVERING",
                    "ROLLBACK_SAFE",
                ),
            )

            result_d = INTEGRATOR.integrate_approved(
                run=runs["pre_ponr_d"],
                review=reviews["pre_ponr_d"],
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                evidence={
                    "repository": str(self.root / f"repository-{projects['pre_ponr_d']}"),
                    "worktree": str(self.root / "worktree-rc5-d"),
                    "main_before": "a" * 40,
                },
                transaction=Transaction(),
            )
            self.assertEqual(
                (result_d["action"], result_d["status"], result_d["integrated"]),
                ("BLOCK_HUMAN", "BLOCKED", False),
            )
            self.assertNotIn(f"repository-{projects['pre_ponr_d']}", git_merges)
            self.assertEqual(
                int(
                    self._row(
                        "SELECT COUNT(*) FROM integration_executions "
                        "WHERE run_id=? AND status='PREPARED'",
                        (run_ids["pre_ponr_d"],),
                    )[0]
                ),
                0,
            )
        finally:
            allow_git_b.set()
            worker_b.join(timeout=5)
            INTEGRATOR.git = original_git
            ORCHESTRATOR.run_command = original_run_command

        self.assertFalse(worker_b.is_alive())
        self.assertEqual(integration_errors, [])
        self.assertEqual(len(integration_b), 1)
        self.assertTrue(integration_b[0]["integrated"])

    def test_pending_approval_gate_survives_restart_and_recovery_state(self) -> None:
        _, plan_id, _, _ = self._create_waiting_human_plan(
            "run-rc5-durable-gate"
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET status='RECOVERING', "
                "recovery_decision='ROLLBACK_SAFE' WHERE run_id=?",
                ("run-rc5-durable-gate",),
            )
            connection.execute(
                "UPDATE orchestration_plans SET status='BLOCKED', last_error=? "
                "WHERE plan_id=?",
                ("cancellation cleanup requires recovery", plan_id),
            )
            connection.commit()

        self.assertTrue(self._human_gate_active("run-rc5-durable-gate"))
        self.assertTrue(
            self._human_gate_active("run-rc5-durable-gate"),
            "a fresh SQLite connection must reconstruct the gate",
        )

    def test_final_human_gate_survives_sibling_cleanup_failure(self) -> None:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        sibling_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        sibling_attempt_id, _, sibling_task = ORCHESTRATOR.reserve_attempt(
            sibling_id,
            instance_id="test-instance",
        )
        gate_run_id = "run-rc5-final-gate"
        self._insert_run(gate_run_id, status="REVIEWING")
        gate_review = self._seed_review(
            gate_run_id,
            "rc5-final-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (gate_run_id, gate_attempt_id),
            )
            connection.commit()
            gate_run = INTEGRATOR.get_run(connection, gate_run_id)
        gate_result = INTEGRATOR.record_non_integration(
            run=gate_run,
            review=gate_review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(
            gate_task,
            gate_attempt_id,
            {"kind": "PIPELINE", "integration": gate_result},
        )
        project_id = "lifecycle-rc5-final-sibling"
        run_id = "run-rc5-final-sibling"
        self._insert_project(project_id)
        self._insert_run(run_id, status="REVIEWING", project_id=project_id)
        review = self._seed_review(
            run_id,
            "rc5-final-sibling",
            decision="APPROVE",
            verdict="PASS",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, sibling_attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)

        result = INTEGRATOR.integrate_approved(
            run=run,
            review=review,
            owner=OWNER,
            decision="APPROVE",
            verdict="PASS",
            evidence={
                "repository": str(self.root / f"repository-{project_id}"),
                "worktree": str(self.root / "worktree-rc5-final-sibling"),
                "main_before": "a" * 40,
            },
            transaction=None,
        )
        self.assertEqual(
            (result["action"], result["status"], result["integrated"]),
            ("BLOCK_HUMAN", "BLOCKED", False),
        )

        original_run_command = ORCHESTRATOR.run_command
        ORCHESTRATOR.run_command = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 31, "", "rollback failed"
        )
        try:
            self.assertFalse(ORCHESTRATOR.rollback_run_best_effort(run_id, 5))
        finally:
            ORCHESTRATOR.run_command = original_run_command
        ORCHESTRATOR.finish_task_waiting_human(
            sibling_task,
            sibling_attempt_id,
            {"kind": "PIPELINE", "integration": result},
        )

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )
        self.assertTrue(self._human_gate_active(run_id))

    def test_multiple_approvals_gate_until_last_pending_is_resolved(self) -> None:
        _, plan_id, _, _ = self._create_waiting_human_plan(
            "run-rc5-multiple-approvals"
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans "
                    "WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )
        with contextlib.closing(self.connect()) as connection:
            first_id = str(
                connection.execute(
                    "SELECT approval_id FROM approvals WHERE run_id=?",
                    ("run-rc5-multiple-approvals",),
                ).fetchone()[0]
            )
            connection.execute(
                "INSERT INTO approvals (approval_id, run_id, status, question, "
                "options_json, decision, created_at, resolved_at) "
                "VALUES ('approval-rc5-second', ?, 'PENDING', 'Second gate', "
                "'[]', NULL, ?, NULL)",
                ("run-rc5-multiple-approvals", NOW),
            )
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE approval_id=?",
                (NOW, first_id),
            )
            connection.commit()
        self.assertTrue(self._human_gate_active("run-rc5-multiple-approvals"))

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='APPROVED', decision='RESUME_SAFE' "
                "WHERE approval_id=?",
                (first_id,),
            )
            connection.commit()
        self.assertTrue(self._human_gate_active("run-rc5-multiple-approvals"))

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', decision=NULL "
                "WHERE approval_id=?",
                (first_id,),
            )
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE approval_id='approval-rc5-second'",
                (NOW,),
            )
            connection.commit()
        self.assertFalse(self._human_gate_active("run-rc5-multiple-approvals"))

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "DELETE FROM approvals WHERE run_id=?",
                ("run-rc5-multiple-approvals",),
            )
            connection.commit()
        self.assertFalse(self._human_gate_active("run-rc5-multiple-approvals"))

    def test_resolving_last_approval_clears_historical_gate_markers(self) -> None:
        run_id = "run-rc6-resolved-gate"
        _, plan_id, _, _ = self._create_waiting_human_plan(run_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT plan.status, plan.last_error, approval.status "
                    "FROM orchestration_plans AS plan "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN approvals AS approval ON approval.run_id=attempt.run_id "
                    "WHERE plan.plan_id=? AND approval.run_id=?",
                    (plan_id, run_id),
                )
            ),
            ("BLOCKED", "waiting for human decision", "PENDING"),
        )

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE run_id=? AND status='PENDING'",
                (NOW, run_id),
            )
            connection.commit()
        self.assertFalse(
            self._human_gate_active(run_id),
            "a resolved final approval must override no historical marker",
        )

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='PENDING', resolved_at=NULL "
                "WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE orchestration_plans SET status='RUNNING', last_error=? "
                "WHERE plan_id=?",
                ("waiting for in-flight integration before human decision", plan_id),
            )
            connection.commit()
        self.assertTrue(self._human_gate_active(run_id))

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE run_id=? AND status='PENDING'",
                (NOW, run_id),
            )
            connection.commit()
        self.assertFalse(
            self._human_gate_active(run_id),
            "a resolved deferred approval must override no historical marker",
        )

    def test_resolved_gate_no_longer_blocks_authoritative_integrator(self) -> None:
        _, plan_id, gate_attempt_id, gate_task = self._create_running_plan()
        sibling_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=? AND task_key='after_cancel'",
                (plan_id,),
            )[0]
        )
        sibling_attempt_id, _, _ = ORCHESTRATOR.reserve_attempt(
            sibling_id,
            instance_id="test-instance",
        )

        gate_run_id = "run-rc6-authoritative-gate"
        self._insert_run(gate_run_id, status="REVIEWING")
        gate_review = self._seed_review(
            gate_run_id,
            "rc6-authoritative-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (gate_run_id, gate_attempt_id),
            )
            connection.commit()
            gate_run = INTEGRATOR.get_run(connection, gate_run_id)
        gate_result = INTEGRATOR.record_non_integration(
            run=gate_run,
            review=gate_review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(
            gate_task,
            gate_attempt_id,
            {"kind": "PIPELINE", "integration": gate_result},
        )

        sibling_project = "lifecycle-rc6-authoritative-sibling"
        sibling_run_id = "run-rc6-authoritative-sibling"
        self._insert_project(sibling_project)
        self._insert_run(
            sibling_run_id,
            status="REVIEWING",
            project_id=sibling_project,
        )
        sibling_review = self._seed_review(
            sibling_run_id,
            "rc6-authoritative-sibling",
            decision="APPROVE",
            verdict="PASS",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (sibling_run_id, sibling_attempt_id),
            )
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE run_id=? AND status='PENDING'",
                (NOW, gate_run_id),
            )
            connection.commit()
            sibling_run = INTEGRATOR.get_run(connection, sibling_run_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )

        original_git = INTEGRATOR.git

        def fake_git(_: Path, *arguments: str) -> str:
            if arguments[0] == "merge":
                return ""
            if arguments[:2] == ("rev-parse", "HEAD"):
                return "b" * 40
            if arguments[0] == "status":
                return ""
            raise AssertionError(arguments)

        class Transaction:
            @staticmethod
            def cleanup_worktree(*_: Any) -> None:
                return None

        INTEGRATOR.git = fake_git
        try:
            result = INTEGRATOR.integrate_approved(
                run=sibling_run,
                review=sibling_review,
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                evidence={
                    "repository": str(self.root / f"repository-{sibling_project}"),
                    "worktree": str(self.root / "worktree-rc6-authoritative"),
                    "main_before": "a" * 40,
                },
                transaction=Transaction(),
            )
        finally:
            INTEGRATOR.git = original_git

        self.assertEqual(
            (result["action"], result["status"], result["integrated"]),
            ("INTEGRATE", "COMPLETED", True),
        )
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM integration_executions "
                    "WHERE run_id=? AND status='COMPLETED'",
                    (sibling_run_id,),
                )[0]
            ),
            1,
        )

    def test_absent_or_resolved_approval_does_not_invent_human_gate(self) -> None:
        _, plan_id, attempt_id, _ = self._create_running_plan()
        project_id = "lifecycle-rc5-no-gate"
        run_id = "run-rc5-no-gate"
        self._insert_project(project_id)
        self._insert_run(run_id, status="REVIEWING", project_id=project_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.execute(
                "UPDATE orchestration_plans SET status='BLOCKED', last_error=? "
                "WHERE plan_id=?",
                ("waiting for human decision", plan_id),
            )
            connection.commit()
        self.assertFalse(self._human_gate_active(run_id))

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "INSERT INTO approvals (approval_id, run_id, status, question, "
                "options_json, decision, created_at, resolved_at) "
                "VALUES ('approval-rc5-resolved', ?, 'CANCELLED', 'Resolved', "
                "'[]', NULL, ?, ?)",
                (run_id, NOW, NOW),
            )
            connection.commit()
        self.assertFalse(self._human_gate_active(run_id))

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "DELETE FROM approvals WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE orchestration_plans SET status='RUNNING', last_error=? "
                "WHERE plan_id=?",
                ("waiting for in-flight integration before human decision", plan_id),
            )
            connection.commit()
        self.assertFalse(self._human_gate_active(run_id))

    def test_pending_approval_does_not_gate_an_unrelated_plan(self) -> None:
        _, current_plan_id, attempt_id, _ = self._create_running_plan()
        unrelated_plan_id = ORCHESTRATOR.insert_plan(
            self._plan(self._task("unrelated_gate")),
            source="TEST",
            initial_status="READY",
        )
        unrelated_objective_id = self._insert_objective(
            source="TEST",
            plan_id=unrelated_plan_id,
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (unrelated_objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(unrelated_plan_id)
        unrelated_task_id = str(
            self._row(
                "SELECT orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=?",
                (unrelated_plan_id,),
            )[0]
        )
        unrelated_attempt_id, _, _ = ORCHESTRATOR.reserve_attempt(
            unrelated_task_id,
            instance_id="test-instance",
        )
        current_project = "lifecycle-rc5-current-plan"
        current_run = "run-rc5-current-plan"
        unrelated_project = "lifecycle-rc5-unrelated-plan"
        unrelated_run = "run-rc5-unrelated-plan"
        self._insert_project(current_project)
        self._insert_project(unrelated_project)
        self._insert_run(current_run, status="REVIEWING", project_id=current_project)
        self._insert_run(unrelated_run, status="WAITING_HUMAN", project_id=unrelated_project)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (current_run, attempt_id),
            )
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (unrelated_run, unrelated_attempt_id),
            )
            connection.execute(
                "INSERT INTO approvals (approval_id, run_id, status, question, "
                "options_json, decision, created_at, resolved_at) "
                "VALUES ('approval-rc5-unrelated', ?, 'PENDING', 'Unrelated', "
                "'[]', NULL, ?, NULL)",
                (unrelated_run, NOW),
            )
            connection.commit()
        self.assertNotEqual(current_plan_id, unrelated_plan_id)
        self.assertFalse(self._human_gate_active(current_run))
        self.assertTrue(self._human_gate_active(unrelated_run))

    def test_resolved_final_gate_does_not_block_finish_task_success(self) -> None:
        plan_id, _, sibling_attempt_id, sibling_task = (
            self._create_human_gate_with_running_sibling("rc7-success-resolved")
        )
        self._resolve_plan_approvals(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )

        ORCHESTRATOR.finish_task_success(
            sibling_task,
            sibling_attempt_id,
            {"kind": "NOOP", "completed": True},
        )

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT task.status, attempt.status, task.failure_reason "
                    "FROM orchestration_tasks AS task "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "WHERE task.orchestration_task_id=? AND attempt.attempt_id=?",
                    (sibling_task["orchestration_task_id"], sibling_attempt_id),
                )
            ),
            ("COMPLETED", "COMPLETED", None),
        )

    def test_pending_gate_still_blocks_finish_task_success(self) -> None:
        _, _, sibling_attempt_id, sibling_task = (
            self._create_human_gate_with_running_sibling("rc7-success-pending")
        )
        ORCHESTRATOR.finish_task_success(
            sibling_task,
            sibling_attempt_id,
            {"kind": "NOOP", "completed": True},
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT task.status, attempt.status, task.failure_reason "
                    "FROM orchestration_tasks AS task "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "WHERE task.orchestration_task_id=? AND attempt.attempt_id=?",
                    (sibling_task["orchestration_task_id"], sibling_attempt_id),
                )
            ),
            ("BLOCKED", "COMPLETED", "waiting for human decision"),
        )

    def test_pending_gate_with_recovery_marker_still_blocks_success(self) -> None:
        plan_id, _, sibling_attempt_id, sibling_task = (
            self._create_human_gate_with_running_sibling("rc7-success-recovery")
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_plans SET status='BLOCKED', last_error=? "
                "WHERE plan_id=?",
                ("cancellation cleanup requires recovery", plan_id),
            )
            connection.commit()
        ORCHESTRATOR.finish_task_success(
            sibling_task,
            sibling_attempt_id,
            {"kind": "NOOP", "completed": True},
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT task.status, attempt.status FROM orchestration_tasks AS task "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "WHERE task.orchestration_task_id=? AND attempt.attempt_id=?",
                    (sibling_task["orchestration_task_id"], sibling_attempt_id),
                )
            ),
            ("BLOCKED", "COMPLETED"),
        )

    def test_resolved_final_gate_does_not_suppress_failure_retry(self) -> None:
        plan_id, _, sibling_attempt_id, sibling_task = (
            self._create_human_gate_with_running_sibling(
                "rc7-failure-resolved",
                sibling_max_attempts=2,
            )
        )
        self._resolve_plan_approvals(plan_id)
        ORCHESTRATOR.finish_task_failure(
            sibling_task,
            sibling_attempt_id,
            "retryable sibling failure",
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT task.status, attempt.status, task.failure_reason "
                    "FROM orchestration_tasks AS task "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "WHERE task.orchestration_task_id=? AND attempt.attempt_id=?",
                    (sibling_task["orchestration_task_id"], sibling_attempt_id),
                )
            ),
            ("READY", "FAILED", "retryable sibling failure"),
        )

    def test_pending_gate_still_suppresses_failure_retry(self) -> None:
        _, _, sibling_attempt_id, sibling_task = (
            self._create_human_gate_with_running_sibling(
                "rc7-failure-pending",
                sibling_max_attempts=2,
            )
        )
        ORCHESTRATOR.finish_task_failure(
            sibling_task,
            sibling_attempt_id,
            "retryable sibling failure",
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT task.status, attempt.status FROM orchestration_tasks AS task "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "WHERE task.orchestration_task_id=? AND attempt.attempt_id=?",
                    (sibling_task["orchestration_task_id"], sibling_attempt_id),
                )
            ),
            ("BLOCKED", "FAILED"),
        )

    def test_resolved_final_marker_does_not_short_circuit_execute_pipeline(self) -> None:
        self._assert_execute_pipeline_ignores_resolved_marker(
            marker="waiting for human decision",
            suffix="rc7-pipeline-final",
        )

    def test_resolved_deferred_marker_does_not_short_circuit_execute_pipeline(self) -> None:
        self._assert_execute_pipeline_ignores_resolved_marker(
            marker="waiting for in-flight integration before human decision",
            suffix="rc7-pipeline-deferred",
        )

    def test_resolved_deferred_marker_is_not_promoted_by_refresh(self) -> None:
        plan_id, _, _, _ = self._create_human_gate_with_running_sibling(
            "rc7-refresh-resolved"
        )
        self._resolve_plan_approvals(plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_plans SET status='RUNNING', last_error=? "
                "WHERE plan_id=?",
                ("waiting for in-flight integration before human decision", plan_id),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("RUNNING", "waiting for in-flight integration before human decision"),
        )

    def test_pending_deferred_marker_is_finalized_by_refresh(self) -> None:
        plan_id, _, _, _ = self._create_human_gate_with_running_sibling(
            "rc7-refresh-pending"
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_plans SET status='RUNNING', last_error=? "
                "WHERE plan_id=?",
                ("waiting for in-flight integration before human decision", plan_id),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )

    def test_orchestrator_gate_ignores_markers_without_pending_approval(self) -> None:
        _, plan_id, _, _ = self._create_running_plan()
        for status, marker in (
            ("BLOCKED", "waiting for human decision"),
            ("RUNNING", "waiting for in-flight integration before human decision"),
        ):
            with self.subTest(marker=marker):
                with contextlib.closing(self.connect()) as connection:
                    connection.execute(
                        "UPDATE orchestration_plans SET status=?, last_error=? "
                        "WHERE plan_id=?",
                        (status, marker, plan_id),
                    )
                    connection.commit()
                    self.assertFalse(
                        ORCHESTRATOR.plan_is_waiting_human(connection, plan_id)
                    )
                    self.assertFalse(
                        ORCHESTRATOR.plan_has_pending_human_gate(connection, plan_id)
                    )
                self.assertFalse(ORCHESTRATOR.plan_waiting_human_requested(plan_id))

    def test_refresh_does_not_complete_plan_with_pending_human_approval(self) -> None:
        objective_id, plan_id, _, _ = self._create_waiting_human_plan(
            "run-rc8-pending-terminal-tasks"
        )
        self._complete_all_plan_tasks(plan_id)

        ORCHESTRATOR.refresh_plan_states(plan_id)
        ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, plan.last_error, "
                    "approval.status, run.status "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN approvals AS approval ON approval.run_id=attempt.run_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "WHERE objective.objective_id=?",
                    (objective_id,),
                )
            ),
            (
                "RUNNING",
                "BLOCKED",
                "waiting for human decision",
                "PENDING",
                "WAITING_HUMAN",
            ),
        )
        self.assertTrue(self._human_gate_active("run-rc8-pending-terminal-tasks"))

    def test_refresh_completes_after_last_human_approval_is_resolved(self) -> None:
        run_id = "run-rc8-resolved-terminal-tasks"
        objective_id, plan_id, _, _ = self._create_waiting_human_plan(run_id)
        self._complete_all_plan_tasks(plan_id)
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                "WHERE run_id=? AND status='PENDING'",
                (NOW, run_id),
            )
            connection.execute(
                "UPDATE runs SET status='COMPLETED', finished_at=?, heartbeat_at=? "
                "WHERE run_id=?",
                (NOW, NOW, run_id),
            )
            connection.execute("DELETE FROM project_locks WHERE run_id=?", (run_id,))
            connection.commit()

        ORCHESTRATOR.refresh_plan_states(plan_id)
        ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, approval.status, run.status, "
                    "EXISTS(SELECT 1 FROM project_locks AS lock "
                    "WHERE lock.run_id=run.run_id) "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN approvals AS approval ON approval.run_id=attempt.run_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "WHERE objective.objective_id=?",
                    (objective_id,),
                )
            ),
            ("COMPLETED", "COMPLETED", "CANCELLED", "COMPLETED", 0),
        )
        self.assertFalse(self._human_gate_active(run_id))

    def test_refresh_preserves_recovery_with_pending_human_approval(self) -> None:
        run_id = "run-rc8-recovery-terminal-tasks"
        objective_id, plan_id, _, _ = self._create_waiting_human_plan(run_id)
        self._complete_all_plan_tasks(plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET status='RECOVERING', "
                "recovery_decision='BLOCK_HUMAN' WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE orchestration_plans SET status='BLOCKED', last_error=? "
                "WHERE plan_id=?",
                ("cancellation cleanup requires recovery", plan_id),
            )
            connection.commit()

        ORCHESTRATOR.refresh_plan_states(plan_id)
        ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, plan.last_error, "
                    "approval.status, run.status, run.recovery_decision "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN approvals AS approval ON approval.run_id=attempt.run_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "WHERE objective.objective_id=?",
                    (objective_id,),
                )
            ),
            (
                "RUNNING",
                "BLOCKED",
                "cancellation cleanup requires recovery",
                "PENDING",
                "RECOVERING",
                "BLOCK_HUMAN",
            ),
        )
        self.assertTrue(self._human_gate_active(run_id))

    def test_deferred_gate_waits_for_all_grandfathered_integrations(self) -> None:
        project_one = "lifecycle-grandfathered-one"
        project_two = "lifecycle-grandfathered-two"
        self._insert_project(project_one)
        self._insert_project(project_two)
        plan = self._plan(
            self._task("gate"),
            self._task("grandfathered_one"),
            self._task("grandfathered_two"),
        )
        plan["max_parallel_tasks"] = 3
        plan_id = ORCHESTRATOR.insert_plan(
            plan,
            source="TEST",
            initial_status="READY",
        )
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)

        attempts: dict[str, tuple[str, sqlite3.Row]] = {}
        with contextlib.closing(self.connect()) as connection:
            task_rows = connection.execute(
                "SELECT task_key, orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=?",
                (plan_id,),
            ).fetchall()
        for row in task_rows:
            attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
                str(row["orchestration_task_id"]),
                instance_id="test-instance",
            )
            attempts[str(row["task_key"])] = (attempt_id, task)

        for suffix, task_key, project_id in (
            ("grandfathered-one", "grandfathered_one", project_one),
            ("grandfathered-two", "grandfathered_two", project_two),
        ):
            run_id = f"run-{suffix}"
            self._insert_run(
                run_id,
                status="COMMITTING",
                project_id=project_id,
            )
            review = self._seed_review(
                run_id,
                suffix,
                decision="APPROVE",
                verdict="PASS",
            )
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                    (run_id, attempts[task_key][0]),
                )
                run = INTEGRATOR.get_run(connection, run_id)
                INTEGRATOR.insert_integration(
                    connection,
                    integration_id=f"integration-{suffix}",
                    run=run,
                    review=review,
                    owner=OWNER,
                    decision="APPROVE",
                    verdict="PASS",
                    status="PREPARED",
                    evidence={"main_before": "a" * 40},
                    snapshot_verified=True,
                    review_current=True,
                )
                connection.commit()

        self._finish_task_with_pending_human_gate(
            attempts["gate"][1],
            attempts["gate"][0],
            "all-grandfathered",
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            (
                "RUNNING",
                "waiting for in-flight integration before human decision",
            ),
        )

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE integration_executions SET status='COMPLETED' "
                "WHERE integration_id='integration-grandfathered-one'"
            )
            connection.execute(
                "UPDATE runs SET status='COMPLETED' "
                "WHERE run_id='run-grandfathered-one'"
            )
            connection.execute(
                "UPDATE orchestration_attempts SET status='COMPLETED' "
                "WHERE attempt_id=?",
                (attempts["grandfathered_one"][0],),
            )
            connection.execute(
                "UPDATE orchestration_tasks SET status='COMPLETED' "
                "WHERE orchestration_task_id=?",
                (attempts["grandfathered_one"][1]["orchestration_task_id"],),
            )
            connection.execute(
                "UPDATE integration_executions SET status='FAILED' "
                "WHERE integration_id='integration-grandfathered-two'"
            )
            connection.execute(
                "UPDATE runs SET status='RECOVERING', "
                "recovery_decision='BLOCK_HUMAN' "
                "WHERE run_id='run-grandfathered-two'"
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            (
                "RUNNING",
                "waiting for in-flight integration before human decision",
            ),
            "Recovery of one grandfathered run must retain the deferred gate",
        )

        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE integration_executions SET status='COMPLETED' "
                "WHERE integration_id='integration-grandfathered-two'"
            )
            connection.execute(
                "UPDATE runs SET status='COMPLETED', recovery_decision='RESUME_SAFE' "
                "WHERE run_id='run-grandfathered-two'"
            )
            connection.execute(
                "UPDATE orchestration_attempts SET status='COMPLETED' "
                "WHERE attempt_id=?",
                (attempts["grandfathered_two"][0],),
            )
            connection.execute(
                "UPDATE orchestration_tasks SET status='COMPLETED' "
                "WHERE orchestration_task_id=?",
                (attempts["grandfathered_two"][1]["orchestration_task_id"],),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )

    def test_human_gate_blocks_two_pre_ponr_siblings(self) -> None:
        project_one = "lifecycle-pre-ponr-one"
        project_two = "lifecycle-pre-ponr-two"
        self._insert_project(project_one)
        self._insert_project(project_two)
        plan = self._plan(
            self._task("gate"),
            self._task("pre_ponr_one"),
            self._task("pre_ponr_two"),
        )
        plan["max_parallel_tasks"] = 3
        plan_id = ORCHESTRATOR.insert_plan(
            plan,
            source="TEST",
            initial_status="READY",
        )
        objective_id = self._insert_objective(source="TEST", plan_id=plan_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE objective_queue SET status='RUNNING' WHERE objective_id=?",
                (objective_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)

        attempts: dict[str, tuple[str, sqlite3.Row]] = {}
        with contextlib.closing(self.connect()) as connection:
            task_rows = connection.execute(
                "SELECT task_key, orchestration_task_id FROM orchestration_tasks "
                "WHERE plan_id=?",
                (plan_id,),
            ).fetchall()
        for row in task_rows:
            attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
                str(row["orchestration_task_id"]),
                instance_id="test-instance",
            )
            attempts[str(row["task_key"])] = (attempt_id, task)

        runs: list[tuple[sqlite3.Row, sqlite3.Row]] = []
        for suffix, task_key, project_id in (
            ("pre-ponr-one", "pre_ponr_one", project_one),
            ("pre-ponr-two", "pre_ponr_two", project_two),
        ):
            run_id = f"run-{suffix}"
            self._insert_run(
                run_id,
                status="REVIEWING",
                project_id=project_id,
            )
            review = self._seed_review(
                run_id,
                suffix,
                decision="APPROVE",
                verdict="PASS",
            )
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                    (run_id, attempts[task_key][0]),
                )
                connection.commit()
                run = INTEGRATOR.get_run(connection, run_id)
            runs.append((run, review))

        gate_run_id = "run-two-pre-ponr-human-gate"
        self._insert_run(gate_run_id, status="REVIEWING")
        gate_review = self._seed_review(
            gate_run_id,
            "two-pre-ponr-human-gate",
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
        )
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (gate_run_id, attempts["gate"][0]),
            )
            connection.commit()
            gate_run = INTEGRATOR.get_run(connection, gate_run_id)
        gate_result = INTEGRATOR.record_non_integration(
            run=gate_run,
            review=gate_review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )
        ORCHESTRATOR.finish_task_waiting_human(
            attempts["gate"][1],
            attempts["gate"][0],
            {"kind": "PIPELINE", "integration": gate_result},
        )
        self.assertEqual(
            tuple(
                self._row(
                    "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                )
            ),
            ("BLOCKED", "waiting for human decision"),
        )

        original_git = INTEGRATOR.git
        INTEGRATOR.git = lambda *_: self.fail("pre-PONR sibling reached Git")
        try:
            for run, review in runs:
                result = INTEGRATOR.integrate_approved(
                    run=run,
                    review=review,
                    owner=OWNER,
                    decision="APPROVE",
                    verdict="PASS",
                    evidence={
                        "repository": str(self.root / "repository-unused"),
                        "worktree": str(self.root / "worktree-unused"),
                        "main_before": "a" * 40,
                    },
                    transaction=None,
                )
                self.assertEqual(
                    (result["action"], result["status"], result["integrated"]),
                    ("BLOCK_HUMAN", "BLOCKED", False),
                )
        finally:
            INTEGRATOR.git = original_git
        self.assertEqual(
            int(
                self._row(
                    "SELECT COUNT(*) FROM integration_executions "
                    "WHERE run_id IN ('run-pre-ponr-one', 'run-pre-ponr-two')"
                )[0]
            ),
            0,
        )

    def test_cancel_waiting_human_cleanup_success_converges(self) -> None:
        objective_id, plan_id, _, task = self._create_waiting_human_plan(
            "run-waiting-human-cancel-success"
        )
        run_id = "run-waiting-human-cancel-success"
        original_run_command = ORCHESTRATOR.run_command

        def successful_rollback(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            with contextlib.closing(self.connect()) as connection:
                connection.execute(
                    "UPDATE approvals SET status='CANCELLED', resolved_at=? "
                    "WHERE run_id=? AND status='PENDING'",
                    (NOW, run_id),
                )
                connection.execute(
                    "DELETE FROM project_locks WHERE run_id=?",
                    (run_id,),
                )
                connection.execute(
                    "UPDATE runs SET status='CANCELLED', "
                    "recovery_decision='ROLLBACK_SAFE', finished_at=? WHERE run_id=?",
                    (NOW, run_id),
                )
                connection.commit()
            return subprocess.CompletedProcess(args[0], 0, "", "")

        self._command(OBJECTIVES.command_cancel, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "CANCEL_REQUESTED",
        )
        ORCHESTRATOR.run_command = successful_rollback
        try:
            ORCHESTRATOR.cleanup_cancel_requested_human_runs(timeout=5)
        finally:
            ORCHESTRATOR.run_command = original_run_command
        ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, task.status, "
                    "run.status, approval.status, "
                    "EXISTS(SELECT 1 FROM project_locks AS lock "
                    "WHERE lock.run_id=run.run_id) "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "JOIN approvals AS approval ON approval.run_id=run.run_id "
                    "WHERE objective.objective_id=? "
                    "AND task.orchestration_task_id=?",
                    (objective_id, task["orchestration_task_id"]),
                )
            ),
            ("CANCELLED", "CANCELLED", "CANCELLED", "CANCELLED", "CANCELLED", 0),
        )
        self.assertFalse(
            self._human_gate_active(run_id),
            "successful cancellation must resolve the pending human gate",
        )

    def test_cancel_waiting_human_cleanup_nonzero_enters_recovery(self) -> None:
        self._assert_waiting_human_cleanup_failure("nonzero")

    def test_cancel_waiting_human_cleanup_exception_enters_recovery(self) -> None:
        self._assert_waiting_human_cleanup_failure("exception")

    def test_cancel_before_human_task_finish_preserves_cleanup_recovery(self) -> None:
        objective_id, _, attempt_id, task = self._create_running_plan()
        run_id = "run-cancel-before-human-finish"
        self._insert_run(run_id, status="REVIEWING")
        self._seed_human_review(run_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()
            run = INTEGRATOR.get_run(connection, run_id)
            review = connection.execute(
                """
                SELECT review.*, execution.execution_id AS review_execution_id
                FROM review_results AS review
                JOIN reviewer_executions AS execution
                  ON execution.review_id=review.review_id
                WHERE review.run_id=?
                """,
                (run_id,),
            ).fetchone()
        result = INTEGRATOR.record_non_integration(
            run=run,
            review=review,
            owner=OWNER,
            decision="BLOCK_HUMAN",
            verdict="HUMAN",
            action="BLOCK_HUMAN",
            evidence={"main_before": "a" * 40},
        )

        self._command(OBJECTIVES.command_cancel, objective_id)
        original_run_command = ORCHESTRATOR.run_command
        ORCHESTRATOR.run_command = lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 23, "", "rollback failed"
        )
        try:
            ORCHESTRATOR.cleanup_cancel_requested_human_runs(timeout=5)
        finally:
            ORCHESTRATOR.run_command = original_run_command
        ORCHESTRATOR.finish_task_waiting_human(task, attempt_id, result)
        ORCHESTRATOR.refresh_plan_states(task["plan_id"])

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, plan.last_error, "
                    "task.status, task.failure_reason, run.status, "
                    "run.recovery_decision, approval.status "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "JOIN approvals AS approval ON approval.run_id=run.run_id "
                    "WHERE objective.objective_id=? "
                    "AND task.orchestration_task_id=?",
                    (objective_id, task["orchestration_task_id"]),
                )
            ),
            (
                "CANCEL_REQUESTED",
                "BLOCKED",
                "cancellation cleanup requires recovery",
                "BLOCKED",
                "cancellation cleanup requires recovery",
                "RECOVERING",
                "ROLLBACK_SAFE",
                "CANCELLED",
            ),
        )

    def _assert_waiting_human_cleanup_failure(self, failure_kind: str) -> None:
        run_id = f"run-waiting-human-cancel-{failure_kind}"
        objective_id, _, _, task = self._create_waiting_human_plan(run_id)
        original_run_command = ORCHESTRATOR.run_command
        if failure_kind == "nonzero":
            ORCHESTRATOR.run_command = lambda *args, **kwargs: subprocess.CompletedProcess(
                args[0], 19, "", "rollback failed"
            )
        else:
            def raise_rollback(*args: Any, **kwargs: Any) -> Any:
                raise OSError("rollback transport failed")

            ORCHESTRATOR.run_command = raise_rollback
        try:
            self._command(OBJECTIVES.command_cancel, objective_id)
            self.assertEqual(
                self._row(
                    "SELECT status FROM objective_queue WHERE objective_id=?",
                    (objective_id,),
                )[0],
                "CANCEL_REQUESTED",
            )
            ORCHESTRATOR.cleanup_cancel_requested_human_runs(timeout=5)
        finally:
            ORCHESTRATOR.run_command = original_run_command
        ORCHESTRATOR.synchronize_objective_states()

        self.assertEqual(
            tuple(
                self._row(
                    "SELECT objective.status, plan.status, task.status, "
                    "run.status, run.recovery_decision, approval.status, "
                    "EXISTS(SELECT 1 FROM project_locks AS lock "
                    "WHERE lock.run_id=run.run_id) "
                    "FROM objective_queue AS objective "
                    "JOIN orchestration_plans AS plan ON plan.plan_id=objective.plan_id "
                    "JOIN orchestration_tasks AS task ON task.plan_id=plan.plan_id "
                    "JOIN orchestration_attempts AS attempt "
                    "ON attempt.orchestration_task_id=task.orchestration_task_id "
                    "JOIN runs AS run ON run.run_id=attempt.run_id "
                    "JOIN approvals AS approval ON approval.run_id=run.run_id "
                    "WHERE objective.objective_id=? "
                    "AND task.orchestration_task_id=?",
                    (objective_id, task["orchestration_task_id"]),
                )
            ),
            (
                "CANCEL_REQUESTED",
                "BLOCKED",
                "BLOCKED",
                "RECOVERING",
                "ROLLBACK_SAFE",
                "CANCELLED",
                1,
            ),
        )
        self.assertFalse(
            self._human_gate_active(run_id),
            "the explicit cancel transition resolves the approval while Recovery "
            "and CANCEL_REQUESTED remain authoritative",
        )

    def test_cancel_rejects_recovering_run_that_crossed_ponr(self) -> None:
        objective_id, _, attempt_id, _ = self._create_running_plan()
        run_id = "run-recovering-post-ponr"
        self._insert_run(run_id, status="RECOVERING")
        self._seed_human_review(run_id)
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET recovery_decision='BLOCK_HUMAN' WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            run = INTEGRATOR.get_run(connection, run_id)
            review = connection.execute(
                """
                SELECT review.*, execution.execution_id AS review_execution_id
                FROM review_results AS review
                JOIN reviewer_executions AS execution
                  ON execution.review_id=review.review_id
                WHERE review.run_id=?
                """,
                (run_id,),
            ).fetchone()
            INTEGRATOR.insert_integration(
                connection,
                integration_id="integration-recovering-post-ponr",
                run=run,
                review=review,
                owner=OWNER,
                decision="APPROVE",
                verdict="PASS",
                status="FAILED",
                evidence={"main_before": "a" * 40},
                snapshot_verified=True,
                review_current=True,
                finished=True,
            )
            connection.commit()

        with self.assertRaisesRegex(
            OBJECTIVES.ObjectiveError,
            "integration point of no return",
        ):
            self._command(OBJECTIVES.command_cancel, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "RUNNING",
        )

    def test_cancel_accepts_recovering_run_before_ponr(self) -> None:
        objective_id, _, attempt_id, _ = self._create_running_plan()
        run_id = "run-recovering-before-ponr"
        self._insert_run(run_id, status="RECOVERING")
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                "UPDATE runs SET recovery_decision='ROLLBACK_SAFE' WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                (run_id, attempt_id),
            )
            connection.commit()

        self._command(OBJECTIVES.command_cancel, objective_id)
        self.assertEqual(
            self._row(
                "SELECT status FROM objective_queue WHERE objective_id=?",
                (objective_id,),
            )[0],
            "CANCEL_REQUESTED",
        )

    def _seed_human_review(self, run_id: str) -> None:
        with contextlib.closing(self.connect()) as connection:
            connection.execute(
                """
                INSERT INTO tasks (
                    task_id, run_id, role, status, description, attempt,
                    metadata_json, created_at, started_at, finished_at, heartbeat_at
                ) VALUES ('review-task-human', ?, 'reviewer', 'COMPLETED',
                          'Human gate review', 1, '{}', ?, ?, ?, ?)
                """,
                (run_id, NOW, NOW, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO review_results (
                    review_id, run_id, verdict, summary, details_json, created_at
                ) VALUES ('review-human', ?, 'HUMAN', 'Human decision required', '{}', ?)
                """,
                (run_id, NOW),
            )
            connection.execute(
                """
                INSERT INTO reviewer_executions (
                    execution_id, review_id, task_id, run_id, role_id,
                    source_profile, runtime_profile, outer_container_name,
                    prompt_path, output_path, workspace_mode, network_enabled,
                    cpu_limit, memory_mb, mount_verified, isolation_verified,
                    repository_unchanged, decision, verdict, exit_code,
                    result_json, created_at, started_at, finished_at
                ) VALUES (
                    'reviewer-execution-human', 'review-human',
                    'review-task-human', ?, 'reviewer', 'test-reviewer',
                    'runtime-review-human', 'review-container-human', ?, ?,
                    'read_only', 0, 1, 512, 1, 1, 1,
                    'BLOCK_HUMAN', 'HUMAN', 0, '{}', ?, ?, ?
                )
                """,
                (
                    run_id,
                    str(self.root / "review.prompt"),
                    str(self.root / "review.output"),
                    NOW,
                    NOW,
                    NOW,
                ),
            )
            connection.commit()


if __name__ == "__main__":
    unittest.main()
