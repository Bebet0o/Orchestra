from __future__ import annotations

import concurrent.futures
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from agent_runtime import (  # noqa: E402
    HermesRuntime,
    NativeRuntime,
    RuntimePreparedEnvironmentData,
    RuntimeRequest,
    RuntimeResult,
    RuntimeRole,
    RuntimeSandboxContext,
)
from model_provider import FakeModelProvider, FakeModelProviderOutcome  # noqa: E402
from shared_context import (  # noqa: E402
    ContextProjector,
    SharedContextError,
    SharedContextStore,
    canonical_json,
    content_sha256,
)
from worker_pool import WorkerPool  # noqa: E402


def load_script(name: str, filename: str):  # type: ignore[no-untyped-def]
    specification = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    if specification is None or specification.loader is None:
        raise RuntimeError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


ORCHESTRATOR = load_script("shared_context_orchestrator", "orchestra-orchestrator.py")
WORKER = load_script("shared_context_worker", "orchestra-worker.py")
NOW = "2026-09-04T00:00:00.000Z"


class ManualExecutor(concurrent.futures.Executor):
    def __init__(self) -> None:
        self.futures: list[concurrent.futures.Future[object]] = []

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        future: concurrent.futures.Future[object] = concurrent.futures.Future()
        self.futures.append(future)
        return future


class SharedContextTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "context.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
            self.seed(connection)
        ORCHESTRATOR.DATABASE = self.database
        self.store = SharedContextStore(self.connect)
        self.projector = ContextProjector(self.connect)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def seed(self, connection: sqlite3.Connection) -> None:
        for role_id, profile, kind, runtime, may_commit in (
            ("orchestrator", "ops-orchestrator", "orchestrator", "hermes", 0),
            ("worker-native", "ops-worker-native", "worker", "native", 1),
            ("worker-hermes", "ops-worker-hermes", "worker", "hermes", 1),
        ):
            connection.execute(
                """
                INSERT INTO roles (
                    role_id, profile_name, role_kind, description,
                    reasoning_effort, max_turns, toolsets_json, skills_json,
                    workspace_mode, may_commit, may_push, network_enabled,
                    cpu_limit, memory_mb, enabled, config_source, config_hash,
                    registered_at, updated_at, runtime_kind, model_id
                ) VALUES (?, ?, ?, 'test', 'high', 10, '[]', '[]', ?, ?, 0, 0,
                          1, 512, 1, 'test', ?, ?, ?, ?, 'fixed-model')
                """,
                (
                    role_id,
                    profile,
                    kind,
                    "write" if may_commit else "none",
                    may_commit,
                    role_id[0] * 64,
                    NOW,
                    NOW,
                    runtime,
                ),
            )
        for key in ("a", "b", "c", "d", "x"):
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, repo_path, data_path, policy_id,
                    enabled, config_source, config_hash, registered_at, updated_at
                ) VALUES (?, ?, ?, ?, 'default', 1, 'test', ?, ?, ?)
                """,
                (
                    f"project-{key}",
                    f"Project {key.upper()}",
                    f"/tmp/project-{key}",
                    f"/tmp/data-{key}",
                    key * 64,
                    NOW,
                    NOW,
                ),
            )
        connection.execute(
            """
            INSERT INTO orchestrator_instances (
                instance_id, hostname, pid, owner, version, status,
                started_at, heartbeat_at
            ) VALUES ('context-controller', 'test', 1, 'test', '0.2.0',
                      'RUNNING', ?, ?)
            """,
            (NOW, NOW),
        )
        connection.commit()

    def task(
        self, key: str, dependencies: list[str], project: str | None = None
    ) -> dict[str, object]:
        project = project or key
        return {
            "key": key,
            "title": f"Task {key.upper()}",
            "kind": "PIPELINE",
            "project_id": f"project-{project}",
            "role_id": "worker-native",
            "instruction": f"Execute task {key}",
            "acceptance_criteria": [f"{key} completed"],
            "marker": f"TASK_{key.upper()}_DONE",
            "dependencies": dependencies,
            "priority": 100,
            "max_attempts": 1,
        }

    def create_plan(
        self,
        tasks: list[dict[str, object]],
        *,
        objective_id: str,
        projects: list[str],
        objective: str = "Build the shared-context feature",
    ) -> str:
        plan = ORCHESTRATOR.validate_plan(
            {
                "schema_version": 1,
                "objective": objective,
                "max_parallel_tasks": 2,
                "tasks": tasks,
            },
            allow_test_actions=False,
        )
        plan_id = ORCHESTRATOR.insert_plan(plan, source="AI", initial_status="READY")
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO objective_queue (
                    objective_id, objective, source, status, priority, not_before,
                    project_scope_json, max_parallel_tasks, planning_max_attempts,
                    planning_attempt_count, plan_id, created_at, heartbeat_at
                ) VALUES (?, ?, 'AI', 'RUNNING', 100, ?, ?, 2, 3, 1, ?, ?, ?)
                """,
                (
                    objective_id,
                    objective,
                    NOW,
                    json.dumps([f"project-{item}" for item in projects]),
                    plan_id,
                    NOW,
                    NOW,
                ),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        return plan_id

    def task_row(self, plan_id: str, key: str) -> sqlite3.Row:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM orchestration_tasks WHERE plan_id=? AND task_key=?",
                (plan_id, key),
            ).fetchone()
        self.assertIsNotNone(row)
        return row

    def complete(self, plan_id: str, key: str, result: dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE orchestration_tasks
                SET status='COMPLETED', result_json=?, finished_at=?, heartbeat_at=?
                WHERE plan_id=? AND task_key=?
                """,
                (canonical_json(result), NOW, NOW, plan_id, key),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)

    def start_snapshot(
        self, pool: WorkerPool, plan_id: str, key: str
    ) -> tuple[sqlite3.Row, str, str, dict[str, object]]:
        task = self.task_row(plan_id, key)
        assignment_id = pool.submit(
            task["orchestration_task_id"], task["role_id"], "native"
        )
        attempt_id, _, reserved = ORCHESTRATOR.reserve_attempt(
            task["orchestration_task_id"], instance_id="context-controller"
        )
        pool.bind_attempt(assignment_id, attempt_id)
        snapshot = self.projector.freeze_task(
            task_id=task["orchestration_task_id"],
            assignment_id=assignment_id,
            attempt_id=attempt_id,
        )
        return reserved, attempt_id, assignment_id, snapshot

    def test_project_and_objective_scopes_are_tenant_isolated(self) -> None:
        first = self.create_plan(
            [self.task("a", [])], objective_id="objective-one", projects=["a"]
        )
        constraint = self.store.add(
            project_id="project-a",
            scope="PROJECT",
            kind="CONSTRAINT",
            key="python.version",
            content="Use Python 3.12",
        )
        decision = self.store.add(
            project_id="project-a",
            objective_id="objective-one",
            scope="OBJECTIVE",
            kind="DECISION",
            key="auth.protocol",
            content="Authentication uses OAuth",
        )
        projection = self.projector.for_task(
            self.task_row(first, "a")["orchestration_task_id"]
        )
        self.assertEqual(
            [item["context_id"] for item in projection["shared_context"]],
            [decision, constraint],
        )

        second = self.create_plan(
            [self.task("a", [])], objective_id="objective-two", projects=["a"]
        )
        second_projection = self.projector.for_task(
            self.task_row(second, "a")["orchestration_task_id"]
        )
        self.assertEqual(
            [item["context_id"] for item in second_projection["shared_context"]],
            [constraint],
        )
        other = self.create_plan(
            [self.task("x", [])], objective_id="objective-other", projects=["x"]
        )
        self.assertEqual(
            self.projector.for_task(
                self.task_row(other, "x")["orchestration_task_id"]
            )["shared_context"],
            [],
        )
        with self.assertRaises(SharedContextError):
            self.store.add(
                project_id="project-x",
                objective_id="objective-one",
                scope="OBJECTIVE",
                kind="NOTE",
                key="invalid.scope",
                content="must not leak",
            )

    def test_dependency_projection_is_direct_deterministic_and_history_free(self) -> None:
        plan_id = self.create_plan(
            [self.task("a", []), self.task("b", ["a"]), self.task("c", [])],
            objective_id="objective-dependency",
            projects=["a", "b", "c"],
        )
        self.complete(plan_id, "a", {"artifact": "commit-a", "summary": "A result"})
        self.complete(plan_id, "c", {"summary": "unrelated result"})
        task_id = self.task_row(plan_id, "b")["orchestration_task_id"]
        first = self.projector.for_task(task_id)
        second = self.projector.for_task(task_id)
        self.assertEqual(canonical_json(first), canonical_json(second))
        self.assertEqual(content_sha256(first), content_sha256(second))
        self.assertEqual(first["task"]["instruction"], "Execute task b")
        self.assertEqual(
            [item["task_key"] for item in first["dependency_results"]], ["a"]
        )
        encoded = canonical_json(first)
        self.assertNotIn("unrelated result", encoded)
        self.assertNotIn("runtime_events", encoded)
        self.assertNotIn("worker_pool_events", encoded)
        reviewer = self.projector.for_reviewer(task_id)
        self.assertEqual(reviewer["consumer"], "REVIEWER")
        self.assertEqual(reviewer["dependency_results"], first["dependency_results"])

    def test_diamond_context_flows_through_two_pool_slots(self) -> None:
        plan_id = self.create_plan(
            [
                self.task("a", []),
                self.task("b", ["a"]),
                self.task("c", ["a"]),
                self.task("d", ["b", "c"]),
            ],
            objective_id="objective-diamond",
            projects=["a", "b", "c", "d"],
        )
        self.complete(plan_id, "a", {"summary": "result-a"})
        executor = ManualExecutor()
        pool = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="context-controller",
            max_concurrency=2,
            executor=executor,
        )
        task_b, attempt_b, _, snapshot_b = self.start_snapshot(pool, plan_id, "b")
        task_c, attempt_c, _, snapshot_c = self.start_snapshot(pool, plan_id, "c")
        self.assertEqual(
            [item["task_key"] for item in snapshot_b["projection"]["dependency_results"]],
            ["a"],
        )
        self.assertEqual(
            [item["task_key"] for item in snapshot_c["projection"]["dependency_results"]],
            ["a"],
        )
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM worker_pool_assignments WHERE status='RUNNING'"
                ).fetchone()[0],
                2,
            )
        ORCHESTRATOR.finish_task_success(task_b, attempt_b, {"summary": "result-b"})
        ORCHESTRATOR.finish_task_success(task_c, attempt_c, {"summary": "result-c"})
        executor.futures[0].set_result("b")
        executor.futures[1].set_result("c")
        ORCHESTRATOR.refresh_plan_states(plan_id)
        task_d, attempt_d, _, snapshot_d = self.start_snapshot(pool, plan_id, "d")
        self.assertEqual(
            [item["task_key"] for item in snapshot_d["projection"]["dependency_results"]],
            ["b", "c"],
        )
        self.assertEqual(canonical_json(snapshot_d["projection"]).count("result-b"), 1)
        self.assertEqual(canonical_json(snapshot_d["projection"]).count("result-c"), 1)
        ORCHESTRATOR.finish_task_success(task_d, attempt_d, {"summary": "result-d"})
        executor.futures[2].set_result("d")
        ORCHESTRATOR.refresh_plan_states(plan_id)
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM orchestration_plans WHERE plan_id=?", (plan_id,)
                ).fetchone()[0],
                "COMPLETED",
            )

    def test_projection_bounds_are_explicit_and_deterministic(self) -> None:
        plan_id = self.create_plan(
            [self.task("a", [])], objective_id="objective-bounds", projects=["a"]
        )
        for index in range(12):
            self.store.add(
                project_id="project-a",
                scope="PROJECT",
                kind="NOTE",
                key=f"note.{index:02d}",
                content=(f"entry-{index:02d}-" + "x" * 180),
            )
        task_id = self.task_row(plan_id, "a")["orchestration_task_id"]
        bounded = ContextProjector(
            self.connect, max_projection_bytes=1200, max_entries=8
        )
        first = bounded.for_task(task_id)
        second = bounded.for_task(task_id)
        self.assertEqual(first, second)
        self.assertTrue(first["bounding"]["budget_exhausted"])
        self.assertGreater(first["bounding"]["omitted_count"], 0)
        self.assertLessEqual(len(canonical_json(first).encode("utf-8")), 1200)
        with self.connect() as connection:
            connection.execute(
                "UPDATE orchestration_tasks SET instruction=? WHERE orchestration_task_id=?",
                ("mandatory-" + "z" * 1500, task_id),
            )
            connection.commit()
        with self.assertRaisesRegex(SharedContextError, "Mandatory context"):
            ContextProjector(self.connect, max_projection_bytes=512).for_task(task_id)

    def test_snapshot_is_immutable_and_future_attempt_sees_new_context(self) -> None:
        plan_id = self.create_plan(
            [self.task("a", []), self.task("b", ["a"], project="a")],
            objective_id="objective-immutable",
            projects=["a"],
        )
        executor = ManualExecutor()
        pool = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="context-controller",
            executor=executor,
        )
        task_a, attempt_a, assignment_a, first = self.start_snapshot(pool, plan_id, "a")
        restarted = ContextProjector(self.connect).freeze_task(
            task_id=task_a["orchestration_task_id"],
            assignment_id=assignment_a,
            attempt_id=attempt_a,
        )
        self.assertEqual(restarted["context_snapshot_id"], first["context_snapshot_id"])
        original = canonical_json(first["projection"])
        new_id = self.store.add(
            project_id="project-a",
            scope="PROJECT",
            kind="FINDING",
            key="late.finding",
            content="Visible only to future starts",
        )
        readback = self.store.snapshot_for_attempt(attempt_a)
        self.assertEqual(canonical_json(readback["projection"]), original)
        self.assertEqual(readback["projection_sha256"], content_sha256(first["projection"]))
        with self.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE context_snapshots SET projection_json='{}' "
                    "WHERE context_snapshot_id=?",
                    (first["context_snapshot_id"],),
                )
        ORCHESTRATOR.finish_task_success(task_a, attempt_a, {"summary": "a"})
        executor.futures[0].set_result("a")
        ORCHESTRATOR.refresh_plan_states(plan_id)
        result_context = self.store.add(
            project_id="project-a",
            objective_id="objective-immutable",
            scope="OBJECTIVE",
            kind="FINDING",
            key="task.a.finding",
            content="A completed finding",
            source_type="TASK_RESULT",
            source_task_id=task_a["orchestration_task_id"],
            source_assignment_id=assignment_a,
            source_attempt_id=attempt_a,
        )
        with self.assertRaisesRegex(SharedContextError, "outside project scope"):
            self.store.add(
                project_id="project-x",
                scope="PROJECT",
                kind="FINDING",
                key="injected.finding",
                content="must not cross projects",
                source_type="TASK_RESULT",
                source_task_id=task_a["orchestration_task_id"],
                source_assignment_id=assignment_a,
                source_attempt_id=attempt_a,
            )
        _, _, _, second = self.start_snapshot(pool, plan_id, "b")
        self.assertIn(
            new_id,
            [item["context_id"] for item in second["projection"]["shared_context"]],
        )
        self.assertIn(
            result_context,
            [item["context_id"] for item in second["projection"]["shared_context"]],
        )

    def test_queued_task_freezes_context_only_when_attempt_starts(self) -> None:
        plan_id = self.create_plan(
            [self.task("a", []), self.task("b", [])],
            objective_id="objective-queued",
            projects=["a", "b"],
        )
        executor = ManualExecutor()
        pool = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="context-controller",
            max_concurrency=1,
            executor=executor,
        )
        a = self.task_row(plan_id, "a")
        b = self.task_row(plan_id, "b")
        pool.submit(a["orchestration_task_id"], a["role_id"], "native")
        queued = pool.submit(b["orchestration_task_id"], b["role_id"], "native")
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM worker_pool_assignments WHERE assignment_id=?",
                    (queued,),
                ).fetchone()[0],
                "QUEUED",
            )
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0], 0)
        late = self.store.add(
            project_id="project-b",
            scope="PROJECT",
            kind="NOTE",
            key="queued.update",
            content="Arrived while queued",
        )
        executor.futures[0].set_result("a")
        _, _, _, snapshot = self.start_snapshot(pool, plan_id, "b")
        self.assertIn(
            late,
            [item["context_id"] for item in snapshot["projection"]["shared_context"]],
        )

    def test_planner_snapshot_is_durable_and_idempotent(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO objective_queue (
                    objective_id, objective, source, status, priority, not_before,
                    project_scope_json, max_parallel_tasks, planning_max_attempts,
                    planning_attempt_count, created_at, heartbeat_at
                ) VALUES ('planner-objective', 'Plan work', 'AI', 'PLANNING', 100,
                          ?, '["project-a"]', 1, 3, 1, ?, ?)
                """,
                (NOW, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO objective_attempts (
                    objective_attempt_id, objective_id, attempt_number, status,
                    executor_instance_id, result_json, started_at, heartbeat_at
                ) VALUES ('planner-attempt', 'planner-objective', 1, 'RUNNING',
                          'context-controller', '{}', ?, ?)
                """,
                (NOW, NOW),
            )
            connection.commit()
        context_id = self.store.add(
            project_id="project-a",
            objective_id="planner-objective",
            scope="OBJECTIVE",
            kind="DECISION",
            key="planner.input",
            content="Use the durable graph",
        )
        first = self.projector.freeze_planner(
            objective_id="planner-objective", objective_attempt_id="planner-attempt"
        )
        second = ContextProjector(self.connect).freeze_planner(
            objective_id="planner-objective", objective_attempt_id="planner-attempt"
        )
        self.assertEqual(first["context_snapshot_id"], second["context_snapshot_id"])
        self.assertIn(
            context_id,
            [item["context_id"] for item in first["projection"]["shared_context"]],
        )
        readback = self.store.snapshot_for_objective_attempt("planner-attempt")
        self.assertEqual(readback["context_snapshot_id"], first["context_snapshot_id"])

    def test_projection_failure_fails_dispatch_and_releases_capacity(self) -> None:
        plan_id = self.create_plan(
            [self.task("a", [])], objective_id="objective-projection-failure", projects=["a"]
        )
        task = self.task_row(plan_id, "a")
        with self.connect() as connection:
            connection.execute(
                "UPDATE orchestration_tasks SET instruction=? "
                "WHERE orchestration_task_id=?",
                ("oversized-" + "x" * 70_000, task["orchestration_task_id"]),
            )
            connection.commit()
        pool: WorkerPool

        def dispatch(assignment):  # type: ignore[no-untyped-def]
            return ORCHESTRATOR.execute_task(
                assignment.task_id,
                instance_id="context-controller",
                config={"heartbeat_seconds": 1},
                pool_assignment_id=assignment.assignment_id,
                pool=pool,
            )

        pool = WorkerPool(
            self.connect,
            dispatch,
            controller_instance_id="context-controller",
        )
        self.addCleanup(pool.shutdown)
        assignment_id = pool.submit(
            task["orchestration_task_id"], task["role_id"], "native"
        )
        for _ in range(100):
            with self.connect() as connection:
                assignment = connection.execute(
                    "SELECT status FROM worker_pool_assignments WHERE assignment_id=?",
                    (assignment_id,),
                ).fetchone()[0]
            if assignment == "FAILED":
                break
            threading.Event().wait(0.01)
        self.assertEqual(assignment, "FAILED")
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM orchestration_tasks WHERE orchestration_task_id=?",
                    (task["orchestration_task_id"],),
                ).fetchone()[0],
                "FAILED",
            )
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM worker_pool_assignments WHERE status='RUNNING'"
                ).fetchone()[0],
                0,
            )

    def test_runtime_context_is_equivalent_and_secrets_are_not_auto_persisted(self) -> None:
        context = {
            "context_schema_version": 1,
            "consumer": "TASK",
            "task": {"instruction": "Use context"},
        }
        run = {
            "run_id": "run-context",
            "branch_name": "branch",
            "base_commit": "a" * 40,
        }
        prompt = WORKER.build_prompt(
            run=run,
            instruction="Implement the task",
            marker="DONE",
            context=context,
        )
        provider = FakeModelProvider([FakeModelProviderOutcome.success("DONE")])
        runtime = NativeRuntime(provider, "fixed-model")
        digest = "sha256:" + "d" * 64
        request = RuntimeRequest(
            role=RuntimeRole.WORKER,
            prompt=prompt,
            runtime_config_id="ops-worker-native",
            request_id="context-runtime",
            timeout_seconds=30,
            completion_marker="DONE",
            sandbox=RuntimeSandboxContext(
                workspace=Path("/tmp/context-runtime"),
                prepared_environment=RuntimePreparedEnvironmentData(
                    executable_image_selector="example/worker@" + digest,
                    local_image_config_id="sha256:" + "e" * 64,
                    oci_digest=digest,
                    image_reference="example/worker@" + digest,
                ),
                cpu_limit=1,
                memory_mb=512,
                read_only=False,
                network_enabled=False,
                sandbox_handle="context-sandbox",
                task_id="context-task",
                runtime_user="1000:1000",
            ),
            context=context,
        )
        with mock.patch.dict(os.environ, {"ORCHESTRA_NATIVE_API_KEY": "secret-value"}):
            runtime.execute(request)
        self.assertEqual(request.context, context)
        self.assertEqual(provider.requests[0].messages[0].content, prompt)
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            (installed / "repo").symlink_to(ROOT, target_is_directory=True)
            hermes = HermesRuntime(installed, required_role=RuntimeRole.WORKER)
            with mock.patch.object(
                hermes, "execute", return_value=RuntimeResult("DONE")
            ) as execute:
                hermes.execute(request)
            execute.assert_called_once_with(request)
            self.assertEqual(execute.call_args.args[0].context, context)
        with self.connect() as connection:
            persisted = " ".join(
                str(value)
                for table in ("shared_context_entries", "context_events")
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
                for value in row
            )
        self.assertNotIn("secret-value", persisted)

    def test_migration_28_preserves_historical_rows_without_fabricated_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                migrations = sorted(
                    (ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")
                )
                for migration in migrations[:-1]:
                    connection.executescript(migration.read_text(encoding="utf-8"))
                self.seed(connection)
                connection.execute(
                    """
                    INSERT INTO orchestration_plans (
                        plan_id, objective, source, status, max_parallel_tasks,
                        plan_sha256, plan_json, created_at, graph_schema_version,
                        graph_activated_at
                    ) VALUES ('legacy-plan', 'Legacy objective', 'AI', 'RUNNING', 1,
                              ?, '{}', ?, 1, ?)
                    """,
                    ("a" * 64, NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO orchestration_tasks (
                        orchestration_task_id, plan_id, task_key, kind, project_id,
                        role_id, status, instruction, title, graph_position, created_at
                    ) VALUES ('legacy-task', 'legacy-plan', 'legacy', 'NOOP',
                              'project-a', 'worker-native', 'RUNNING', 'legacy work',
                              'Legacy task', 0, ?)
                    """,
                    (NOW,),
                )
                connection.execute(
                    """
                    INSERT INTO orchestration_attempts (
                        attempt_id, orchestration_task_id, attempt_number, status,
                        executor_instance_id, started_at, heartbeat_at
                    ) VALUES ('legacy-attempt', 'legacy-task', 1, 'RUNNING',
                              'context-controller', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO objective_queue (
                        objective_id, objective, source, status, not_before,
                        project_scope_json, plan_id, created_at, heartbeat_at
                    ) VALUES ('legacy-objective', 'Legacy objective', 'AI', 'RUNNING',
                              ?, '["project-a"]', 'legacy-plan', ?, ?)
                    """,
                    (NOW, NOW, NOW),
                )
                connection.execute(
                    """
                    INSERT INTO objective_attempts (
                        objective_attempt_id, objective_id, attempt_number, status,
                        executor_instance_id, started_at, heartbeat_at
                    ) VALUES ('legacy-objective-attempt', 'legacy-objective', 1,
                              'RUNNING', 'context-controller', ?, ?)
                    """,
                    (NOW, NOW),
                )
                connection.commit()
                connection.executescript(migrations[-1].read_text(encoding="utf-8"))
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 31)
                self.assertEqual(
                    connection.execute(
                        "SELECT context_snapshot_id FROM orchestration_attempts "
                        "WHERE attempt_id='legacy-attempt'"
                    ).fetchone()[0],
                    None,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT context_snapshot_id FROM objective_attempts "
                        "WHERE objective_attempt_id='legacy-objective-attempt'"
                    ).fetchone()[0],
                    None,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM context_snapshots").fetchone()[0],
                    0,
                )


if __name__ == "__main__":
    unittest.main()
