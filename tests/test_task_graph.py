from __future__ import annotations

import importlib.util
import concurrent.futures
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
from worker_pool import WorkerAssignment, WorkerPool  # noqa: E402


def load_orchestrator():  # type: ignore[no-untyped-def]
    specification = importlib.util.spec_from_file_location(
        "task_graph_orchestrator", SCRIPTS / "orchestra-orchestrator.py"
    )
    if specification is None or specification.loader is None:
        raise RuntimeError("Unable to load orchestra-orchestrator.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


ORCHESTRATOR = load_orchestrator()
NOW = "2026-09-04T00:00:00.000Z"


class ManualExecutor(concurrent.futures.Executor):
    def __init__(self) -> None:
        self.futures: list[concurrent.futures.Future[object]] = []

    def submit(self, fn, /, *args, **kwargs):  # type: ignore[no-untyped-def]
        future: concurrent.futures.Future[object] = concurrent.futures.Future()
        self.futures.append(future)
        return future


class TaskGraphTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "graph.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
            self._seed(connection)
        ORCHESTRATOR.DATABASE = self.database

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def _seed(self, connection: sqlite3.Connection) -> None:
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
        for key in ("a", "b", "c", "d"):
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
            ) VALUES ('graph-controller', 'test', 1, 'test', '0.2.0',
                      'RUNNING', ?, ?)
            """,
            (NOW, NOW),
        )
        connection.commit()

    def task(self, key: str, dependencies: list[str]) -> dict[str, object]:
        return {
            "key": key,
            "title": f"Task {key.upper()}",
            "kind": "PIPELINE",
            "project_id": f"project-{key}",
            "role_id": "worker-native",
            "instruction": f"Complete {key}",
            "acceptance_criteria": [f"{key} complete"],
            "marker": f"TASK_{key.upper()}_DONE",
            "dependencies": dependencies,
            "priority": 100,
            "max_attempts": 1,
        }

    def plan(self, tasks: list[dict[str, object]], maximum: int = 2) -> dict[str, object]:
        return {
            "schema_version": 1,
            "objective": "execute a task graph",
            "max_parallel_tasks": maximum,
            "tasks": tasks,
        }

    def diamond(self) -> dict[str, object]:
        return self.plan(
            [
                self.task("a", []),
                self.task("b", ["a"]),
                self.task("c", ["a"]),
                self.task("d", ["b", "c"]),
            ]
        )

    def states(self, plan_id: str) -> dict[str, str]:
        with self.connect() as connection:
            return dict(
                connection.execute(
                    "SELECT task_key, status FROM orchestration_tasks "
                    "WHERE plan_id=? ORDER BY graph_position",
                    (plan_id,),
                )
            )

    def wait_for(self, event: threading.Event) -> None:
        self.assertTrue(event.wait(5), "graph execution did not advance")

    def run_diamond(self, *, fail: str | None = None):  # type: ignore[no-untyped-def]
        normalized = ORCHESTRATOR.validate_plan(self.diamond(), allow_test_actions=False)
        plan_id = ORCHESTRATOR.insert_plan(normalized, source="AI", initial_status="READY")
        started = {key: threading.Event() for key in "abcd"}
        released = {key: threading.Event() for key in "abcd"}
        finished = {key: threading.Event() for key in "abcd"}
        branch_completion = threading.Barrier(2)
        scheduling = threading.RLock()
        pool: WorkerPool

        def schedule() -> None:
            with scheduling:
                ORCHESTRATOR.refresh_plan_states(plan_id)
                for task_id in ORCHESTRATOR.runnable_tasks(
                    pool.active_task_ids(), capacity=16
                ):
                    role_id, runtime_kind = ORCHESTRATOR.worker_assignment_snapshot(task_id)
                    assignment_id = pool.submit(task_id, role_id, runtime_kind)
                    ORCHESTRATOR.record_task_dispatch(task_id, assignment_id)

        def dispatch(assignment: WorkerAssignment) -> str:
            attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
                assignment.task_id, instance_id="graph-controller"
            )
            pool.bind_attempt(assignment.assignment_id, attempt_id)
            key = str(task["task_key"])
            started[key].set()
            self.wait_for(released[key])
            if key in {"b", "c"}:
                branch_completion.wait(timeout=5)
            if key == fail:
                ORCHESTRATOR.finish_task_failure(task, attempt_id, "synthetic graph failure")
            else:
                ORCHESTRATOR.finish_task_success(task, attempt_id, {"task": key})
            schedule()
            finished[key].set()
            if key == fail:
                raise RuntimeError("synthetic graph failure")
            return key

        with mock.patch.object(ORCHESTRATOR, "supervisor_is_healthy", return_value=True):
            pool = WorkerPool(
                self.connect,
                dispatch,
                controller_instance_id="graph-controller",
                max_concurrency=2,
            )
            self.addCleanup(pool.shutdown)
            schedule()
            self.wait_for(started["a"])
            self.assertEqual(
                self.states(plan_id),
                {"a": "RUNNING", "b": "PENDING", "c": "PENDING", "d": "PENDING"},
            )
            released["a"].set()
            self.wait_for(started["b"])
            self.wait_for(started["c"])
            self.assertFalse(started["d"].is_set())
            released["b"].set()
            if fail == "b":
                self.wait_for(released["b"])
            self.assertFalse(started["d"].is_set())
            released["c"].set()
            if fail is None:
                self.wait_for(started["d"])
                released["d"].set()
                self.wait_for(finished["d"])
            else:
                self.wait_for(finished["b"])
                self.wait_for(finished["c"])
                self.assertFalse(started["d"].wait(0.2))

        return plan_id, started

    def test_valid_single_and_independent_readiness_persist(self) -> None:
        normalized = ORCHESTRATOR.validate_plan(
            self.plan([self.task("a", []), self.task("b", [])]),
            allow_test_actions=False,
        )
        plan_id = ORCHESTRATOR.insert_plan(normalized, source="AI", initial_status="READY")
        ORCHESTRATOR.refresh_plan_states(plan_id)
        snapshot = ORCHESTRATOR.task_graph_snapshot(plan_id)
        self.assertEqual([task["status"] for task in snapshot["tasks"]], ["READY", "READY"])
        self.assertEqual([task["title"] for task in snapshot["tasks"]], ["Task A", "Task B"])

    def test_dependency_readiness_and_transitive_blocking(self) -> None:
        normalized = ORCHESTRATOR.validate_plan(self.diamond(), allow_test_actions=False)
        plan_id = ORCHESTRATOR.insert_plan(normalized, source="AI", initial_status="READY")
        ORCHESTRATOR.refresh_plan_states(plan_id)
        with self.connect() as connection:
            connection.execute(
                "UPDATE orchestration_tasks SET status='FAILED' "
                "WHERE plan_id=? AND task_key='a'",
                (plan_id,),
            )
            connection.commit()
        ORCHESTRATOR.refresh_plan_states(plan_id)
        self.assertEqual(
            self.states(plan_id),
            {"a": "FAILED", "b": "BLOCKED", "c": "BLOCKED", "d": "BLOCKED"},
        )

    def test_diamond_parallel_execution_and_exactly_once_downstream(self) -> None:
        plan_id, started = self.run_diamond()
        self.wait_for(started["d"])
        ORCHESTRATOR.reconcile_task_graph()
        with self.connect() as connection:
            assignments = connection.execute(
                """
                SELECT task.task_key, COUNT(assignment.assignment_id)
                FROM orchestration_tasks AS task
                LEFT JOIN worker_pool_assignments AS assignment
                  ON assignment.orchestration_task_id=task.orchestration_task_id
                WHERE task.plan_id=? GROUP BY task.task_key
                """,
                (plan_id,),
            ).fetchall()
            plan_status = connection.execute(
                "SELECT status FROM orchestration_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0]
        self.assertEqual(dict(assignments), {"a": 1, "b": 1, "c": 1, "d": 1})
        self.assertEqual(plan_status, "COMPLETED")

    def test_diamond_failure_blocks_join_without_stopping_sibling(self) -> None:
        plan_id, started = self.run_diamond(fail="b")
        self.wait_for(started["c"])
        ORCHESTRATOR.reconcile_task_graph()
        self.assertEqual(
            self.states(plan_id),
            {"a": "COMPLETED", "b": "FAILED", "c": "COMPLETED", "d": "BLOCKED"},
        )
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT status FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()[0],
                "FAILED",
            )

    def test_invalid_graphs_and_bounds_are_rejected_atomically(self) -> None:
        invalid = [
            [self.task("a", ["a"])],
            [self.task("a", ["missing"])],
            [self.task("a", ["b"]), self.task("b", ["a"])],
            [self.task("a", []), self.task("a", [])],
            [self.task("a", ["b", "b"]), self.task("b", [])],
        ]
        for tasks in invalid:
            with self.subTest(tasks=tasks), self.assertRaises(ORCHESTRATOR.OrchestratorError):
                ORCHESTRATOR.validate_plan(self.plan(tasks), allow_test_actions=False)
        too_many = [self.task(f"task_{index}", []) for index in range(33)]
        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.validate_plan(self.plan(too_many), allow_test_actions=False)
        bad_role = self.task("a", [])
        bad_role["role_id"] = "missing"
        with self.assertRaises(ORCHESTRATOR.OrchestratorError):
            ORCHESTRATOR.validate_plan(self.plan([bad_role]), allow_test_actions=False)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM orchestration_plans").fetchone()[0], 0)

    def test_database_rejects_cross_plan_dependency(self) -> None:
        first = ORCHESTRATOR.insert_plan(
            ORCHESTRATOR.validate_plan(
                self.plan([self.task("a", [])]), allow_test_actions=False
            ),
            source="AI",
            initial_status="READY",
        )
        second_task = self.task("b", [])
        second = ORCHESTRATOR.insert_plan(
            ORCHESTRATOR.validate_plan(
                self.plan([second_task]), allow_test_actions=False
            ),
            source="AI",
            initial_status="READY",
        )
        with self.connect() as connection:
            child = connection.execute(
                "SELECT orchestration_task_id FROM orchestration_tasks WHERE plan_id=?",
                (second,),
            ).fetchone()[0]
            parent = connection.execute(
                "SELECT orchestration_task_id FROM orchestration_tasks WHERE plan_id=?",
                (first,),
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO orchestration_dependencies VALUES (?, ?, ?, 'SUCCESS')",
                    (second, child, parent),
                )

    def test_migration_27_preserves_and_backfills_existing_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "upgrade.db"
            with sqlite3.connect(database) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                    if migration.name.startswith("027_"):
                        continue
                    connection.executescript(migration.read_text(encoding="utf-8"))
                self._seed(connection)
                connection.execute(
                    """
                    INSERT INTO orchestration_plans (
                        plan_id, objective, source, planner_role_id, status,
                        max_parallel_tasks, plan_sha256, plan_json, created_at
                    ) VALUES ('legacy-plan', 'legacy', 'AI', 'orchestrator',
                              'READY', 1, ?, '{}', ?)
                    """,
                    ("f" * 64, NOW),
                )
                for index, key in enumerate(("first", "second")):
                    connection.execute(
                        """
                        INSERT INTO orchestration_tasks (
                            orchestration_task_id, plan_id, task_key, kind,
                            status, instruction, created_at
                        ) VALUES (?, 'legacy-plan', ?, 'NOOP', 'PENDING', '', ?)
                        """,
                        (f"legacy-{key}", key, f"{NOW[:-1]}{index}Z"),
                    )
                connection.executescript(
                    (ROOT / "migrations/027_planner_task_graph.sql").read_text(
                        encoding="utf-8"
                    )
                )
                rows = connection.execute(
                    "SELECT task_key, title, graph_position FROM orchestration_tasks "
                    "WHERE plan_id='legacy-plan' ORDER BY graph_position"
                ).fetchall()
                self.assertEqual(
                    rows,
                    [("first", "first", 0), ("second", "second", 1)],
                )
                self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 27)

    def runtime_request(self, key: str) -> RuntimeRequest:
        digest = "sha256:" + "d" * 64
        return RuntimeRequest(
            role=RuntimeRole.WORKER,
            prompt="execute graph task",
            runtime_config_id="ops-worker",
            request_id="graph-runtime-" + key,
            timeout_seconds=30,
            completion_marker="DONE",
            sandbox=RuntimeSandboxContext(
                workspace=Path("/tmp/graph-") / key,
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
                sandbox_handle="graph-sandbox-" + key,
                task_id=key,
                runtime_user="1000:1000",
            ),
        )

    def test_native_and_hermes_graph_workers_share_runtime_boundary(self) -> None:
        provider = FakeModelProvider([FakeModelProviderOutcome.success("native")])
        native = NativeRuntime(provider, "fixed-model")
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            (installed / "repo").symlink_to(ROOT, target_is_directory=True)
            hermes = HermesRuntime(installed, required_role=RuntimeRole.WORKER)
            with mock.patch.object(hermes, "execute", return_value=RuntimeResult("hermes")):
                native_task = self.task("a", [])
                hermes_task = self.task("b", [])
                hermes_task["role_id"] = "worker-hermes"
                normalized = ORCHESTRATOR.validate_plan(
                    self.plan([native_task, hermes_task]), allow_test_actions=False
                )
                plan_id = ORCHESTRATOR.insert_plan(
                    normalized, source="AI", initial_status="READY"
                )
                ORCHESTRATOR.refresh_plan_states(plan_id)
                completed = {key: threading.Event() for key in ("a", "b")}
                pool: WorkerPool

                def dispatch(assignment: WorkerAssignment) -> str:
                    attempt_id, _, task = ORCHESTRATOR.reserve_attempt(
                        assignment.task_id, instance_id="graph-controller"
                    )
                    pool.bind_attempt(assignment.assignment_id, attempt_id)
                    key = str(task["task_key"])
                    runtime = native if assignment.runtime_kind == "native" else hermes
                    output = runtime.execute(self.runtime_request(key)).output
                    ORCHESTRATOR.finish_task_success(task, attempt_id, {"output": output})
                    completed[key].set()
                    return output

                pool = WorkerPool(
                    self.connect,
                    dispatch,
                    controller_instance_id="graph-controller",
                    max_concurrency=2,
                )
                self.addCleanup(pool.shutdown)
                with mock.patch.object(
                    ORCHESTRATOR, "supervisor_is_healthy", return_value=True
                ):
                    for task_id in ORCHESTRATOR.runnable_tasks(set(), capacity=16):
                        role_id, runtime_kind = ORCHESTRATOR.worker_assignment_snapshot(task_id)
                        assignment_id = pool.submit(task_id, role_id, runtime_kind)
                        ORCHESTRATOR.record_task_dispatch(task_id, assignment_id)
                self.wait_for(completed["a"])
                self.wait_for(completed["b"])
                self.assertEqual(self.states(plan_id), {"a": "COMPLETED", "b": "COMPLETED"})
        self.assertEqual(len(provider.requests), 1)

    def test_graph_readback_and_reconciliation_are_idempotent(self) -> None:
        normalized = ORCHESTRATOR.validate_plan(self.diamond(), allow_test_actions=False)
        plan_id = ORCHESTRATOR.insert_plan(normalized, source="AI", initial_status="READY")
        for _ in range(3):
            ORCHESTRATOR.reconcile_task_graph()
        first = ORCHESTRATOR.task_graph_snapshot(plan_id)
        second = ORCHESTRATOR.task_graph_snapshot(plan_id)
        self.assertEqual(first, second)
        self.assertEqual(self.states(plan_id)["a"], "READY")
        self.assertEqual(len(first["dependencies"]), 4)

    def test_pool_reconstruction_keeps_only_one_active_graph_assignment(self) -> None:
        normalized = ORCHESTRATOR.validate_plan(
            self.plan([self.task("a", [])], maximum=1), allow_test_actions=False
        )
        plan_id = ORCHESTRATOR.insert_plan(normalized, source="AI", initial_status="READY")
        ORCHESTRATOR.refresh_plan_states(plan_id)
        with self.connect() as connection:
            task_id = connection.execute(
                "SELECT orchestration_task_id FROM orchestration_tasks WHERE plan_id=?",
                (plan_id,),
            ).fetchone()[0]
        role_id, runtime_kind = ORCHESTRATOR.worker_assignment_snapshot(task_id)
        old_executor = ManualExecutor()
        old = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="old-controller",
            executor=old_executor,
        )
        old_assignment = old.submit(task_id, role_id, runtime_kind)

        replacement_executor = ManualExecutor()
        replacement = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="new-controller",
            executor=replacement_executor,
        )
        self.assertEqual(replacement.reconcile(), 1)
        ORCHESTRATOR.reconcile_task_graph()
        replacement_assignment = replacement.submit(task_id, role_id, runtime_kind)
        self.assertEqual(
            replacement.submit(task_id, role_id, runtime_kind), replacement_assignment
        )
        ORCHESTRATOR.record_task_dispatch(task_id, replacement_assignment)
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT assignment_id, status FROM worker_pool_assignments "
                "WHERE orchestration_task_id=? ORDER BY queue_sequence",
                (task_id,),
            ).fetchall()
        self.assertEqual(
            [(row[0], row[1]) for row in rows],
            [(old_assignment, "INTERRUPTED"), (replacement_assignment, "RUNNING")],
        )
        self.assertEqual(len(old_executor.futures), 1)
        self.assertEqual(len(replacement_executor.futures), 1)


if __name__ == "__main__":
    unittest.main()
