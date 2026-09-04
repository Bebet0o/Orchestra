from __future__ import annotations

import concurrent.futures
import importlib.util
import sqlite3
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
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
        "worker_pool_orchestrator", SCRIPTS / "orchestra-orchestrator.py"
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


class WorkerPoolTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "pool.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                """
                INSERT INTO roles (
                    role_id, profile_name, role_kind, description,
                    reasoning_effort, max_turns, toolsets_json, skills_json,
                    workspace_mode, may_commit, may_push, network_enabled,
                    cpu_limit, memory_mb, enabled, config_source, config_hash,
                    registered_at, updated_at, runtime_kind, model_id
                ) VALUES (
                    'orchestrator', 'ops-orchestrator', 'orchestrator', 'planner',
                    'high', 10, '[]', '[]', 'none', 0, 0, 0, 1, 512, 1,
                    'test', ?, ?, ?, 'hermes', 'fixed-model'
                )
                """,
                ("a" * 64, NOW, NOW),
            )
            for role_id, profile, runtime_kind in (
                ("worker-hermes", "ops-worker-hermes", "hermes"),
                ("worker-native", "ops-worker-native", "native"),
            ):
                connection.execute(
                    """
                    INSERT INTO roles (
                        role_id, profile_name, role_kind, description,
                        reasoning_effort, max_turns, toolsets_json, skills_json,
                        workspace_mode, may_commit, may_push, network_enabled,
                        cpu_limit, memory_mb, enabled, config_source, config_hash,
                        registered_at, updated_at, runtime_kind, model_id
                    ) VALUES (?, ?, 'worker', 'worker', 'high', 10, '[]', '[]',
                              'write', 1, 0, 0, 1, 512, 1, 'test', ?, ?, ?, ?,
                              'fixed-model')
                    """,
                    (role_id, profile, "b" * 64, NOW, NOW, runtime_kind),
                )
            connection.execute(
                """
                INSERT INTO orchestration_plans (
                    plan_id, objective, source, planner_role_id, status,
                    max_parallel_tasks, plan_sha256, plan_json, created_at
                ) VALUES ('plan', 'test', 'TEST', 'orchestrator', 'READY', 16,
                          ?, '{}', ?)
                """,
                ("c" * 64, NOW),
            )
            for key in ("a", "b", "c", "d"):
                connection.execute(
                    """
                    INSERT INTO orchestration_tasks (
                        orchestration_task_id, plan_id, task_key, kind,
                        project_id, role_id, status, instruction, marker,
                        created_at
                    ) VALUES (?, 'plan', ?, 'PIPELINE', NULL,
                              'worker-native', 'READY', 'work', 'DONE', ?)
                    """,
                    (f"task-{key}", key, f"{NOW[:-1]}{key}Z"),
                )
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def statuses(self) -> dict[str, str]:
        with self.connect() as connection:
            return {
                str(row[0]): str(row[1])
                for row in connection.execute(
                    """
                    SELECT orchestration_task_id, status
                    FROM worker_pool_assignments
                    ORDER BY queue_sequence
                    """
                )
            }

    def test_default_max_concurrency_preserves_serial_execution(self) -> None:
        started = {key: threading.Event() for key in ("task-a", "task-b")}
        releases = {key: threading.Event() for key in started}

        def dispatch(assignment: WorkerAssignment) -> None:
            started[assignment.task_id].set()
            self.assertTrue(releases[assignment.task_id].wait(5))

        pool = WorkerPool(self.connect, dispatch, controller_instance_id="one")
        self.addCleanup(pool.shutdown)
        pool.submit("task-a", "worker-native", "native")
        self.assertTrue(started["task-a"].wait(5))
        pool.submit("task-b", "worker-native", "native")
        self.assertFalse(started["task-b"].is_set())
        self.assertEqual(self.statuses(), {"task-a": "RUNNING", "task-b": "QUEUED"})
        releases["task-a"].set()
        self.assertTrue(started["task-b"].wait(5))
        releases["task-b"].set()

    def test_durable_limit_is_shared_by_multiple_pool_instances(self) -> None:
        first_executor = ManualExecutor()
        second_executor = ManualExecutor()
        first = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="first",
            executor=first_executor,
        )
        second = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="second",
            executor=second_executor,
        )
        first.submit("task-a", "worker-native", "native")
        second.submit("task-b", "worker-native", "native")

        self.assertEqual(len(first_executor.futures), 1)
        self.assertEqual(len(second_executor.futures), 0)
        self.assertEqual(self.statuses(), {"task-a": "RUNNING", "task-b": "QUEUED"})
        with self.connect() as connection:
            sequence = connection.execute(
                "SELECT orchestration_task_id FROM worker_pool_assignments "
                "ORDER BY queue_sequence"
            ).fetchall()
        self.assertEqual(sequence, [("task-a",), ("task-b",)])

    def test_two_overlap_third_queues_then_auto_dispatches(self) -> None:
        started = {key: threading.Event() for key in ("task-a", "task-b", "task-c")}
        releases = {key: threading.Event() for key in started}
        sandboxes: dict[str, Path] = {}

        def dispatch(assignment: WorkerAssignment) -> str:
            sandboxes[assignment.task_id] = self.sandbox(assignment.task_id).workspace
            started[assignment.task_id].set()
            self.assertTrue(releases[assignment.task_id].wait(5))
            return assignment.task_id

        pool = WorkerPool(
            self.connect,
            dispatch,
            controller_instance_id="parallel",
            max_concurrency=2,
        )
        self.addCleanup(pool.shutdown)
        for task in ("task-a", "task-b", "task-c"):
            pool.submit(task, "worker-native", "native")
        self.assertTrue(started["task-a"].wait(5))
        self.assertTrue(started["task-b"].wait(5))
        self.assertNotEqual(sandboxes["task-a"], sandboxes["task-b"])
        self.assertFalse(started["task-c"].is_set())
        self.assertEqual(
            self.statuses(),
            {"task-a": "RUNNING", "task-b": "RUNNING", "task-c": "QUEUED"},
        )
        releases["task-a"].set()
        self.assertTrue(started["task-c"].wait(5))
        self.assertFalse(releases["task-b"].is_set())
        releases["task-b"].set()
        releases["task-c"].set()

    def test_configuration_defaults_to_one_and_rejects_invalid_limit(self) -> None:
        original = ORCHESTRATOR.CONFIG_PATH
        self.addCleanup(setattr, ORCHESTRATOR, "CONFIG_PATH", original)
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "orchestrator.toml"
            config.write_text("[worker_pool]\nmax_concurrency = 1\n", encoding="utf-8")
            ORCHESTRATOR.CONFIG_PATH = config
            with mock.patch.dict("os.environ", {}, clear=True):
                self.assertEqual(ORCHESTRATOR.load_config()["worker_pool_max_concurrency"], 1)
            config.write_text("[worker_pool]\nmax_concurrency = 0\n", encoding="utf-8")
            with mock.patch.dict("os.environ", {}, clear=True), self.assertRaises(
                ORCHESTRATOR.OrchestratorError
            ):
                ORCHESTRATOR.load_config()

    def test_failure_isolated_and_releases_capacity(self) -> None:
        started = {key: threading.Event() for key in ("task-a", "task-b", "task-c")}
        release_a = threading.Event()
        release_b = threading.Event()
        release_c = threading.Event()

        def dispatch(assignment: WorkerAssignment) -> None:
            started[assignment.task_id].set()
            if assignment.task_id == "task-a":
                self.assertTrue(release_a.wait(5))
                raise RuntimeError("synthetic worker failure")
            release = release_b if assignment.task_id == "task-b" else release_c
            self.assertTrue(release.wait(5))

        pool = WorkerPool(
            self.connect,
            dispatch,
            controller_instance_id="failure",
            max_concurrency=2,
        )
        self.addCleanup(pool.shutdown)
        for task in ("task-a", "task-b", "task-c"):
            pool.submit(task, "worker-native", "native")
        self.assertTrue(started["task-a"].wait(5))
        self.assertTrue(started["task-b"].wait(5))
        release_a.set()
        self.assertTrue(started["task-c"].wait(5))
        self.assertFalse(release_b.is_set())
        release_b.set()
        release_c.set()

    def sandbox(self, task_id: str) -> RuntimeSandboxContext:
        digest = "sha256:" + "d" * 64
        return RuntimeSandboxContext(
            workspace=Path("/tmp") / task_id,
            prepared_environment=RuntimePreparedEnvironmentData(
                executable_image_selector="registry.example/worker@" + digest,
                local_image_config_id="sha256:" + "e" * 64,
                oci_digest=digest,
                image_reference="registry.example/worker@" + digest,
            ),
            cpu_limit=1,
            memory_mb=512,
            read_only=False,
            network_enabled=False,
            sandbox_handle="sandbox-" + task_id,
            task_id=task_id,
            runtime_user="1000:1000",
        )

    def request(self, task_id: str) -> RuntimeRequest:
        return RuntimeRequest(
            role=RuntimeRole.WORKER,
            prompt="perform worker assignment",
            runtime_config_id="ops-worker-native",
            request_id="request-" + task_id,
            timeout_seconds=30,
            completion_marker="DONE",
            sandbox=self.sandbox(task_id),
        )

    def test_native_runtime_pool_dispatch_reaches_fake_model_provider(self) -> None:
        provider = FakeModelProvider([FakeModelProviderOutcome.success("native")])
        runtime = NativeRuntime(provider, "fixed-model")
        completed = threading.Event()

        def dispatch(assignment: WorkerAssignment) -> str:
            result = runtime.execute(self.request(assignment.task_id))
            completed.set()
            return result.output

        pool = WorkerPool(self.connect, dispatch, controller_instance_id="native")
        self.addCleanup(pool.shutdown)
        pool.submit("task-a", "worker-native", "native")
        self.assertTrue(completed.wait(5))
        self.assertEqual(len(provider.requests), 1)

    def test_hermes_runtime_dispatch_uses_same_pool_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            (installed / "repo").symlink_to(ROOT, target_is_directory=True)
            runtime = HermesRuntime(installed, required_role=RuntimeRole.WORKER)
            completed = threading.Event()

            def dispatch(assignment: WorkerAssignment) -> str:
                result = runtime.execute(self.request(assignment.task_id))
                completed.set()
                return result.output

            with mock.patch.object(
                runtime,
                "execute",
                return_value=RuntimeResult("hermes"),
            ) as execute:
                pool = WorkerPool(
                    self.connect,
                    dispatch,
                    controller_instance_id="hermes",
                )
                self.addCleanup(pool.shutdown)
                pool.submit("task-a", "worker-hermes", "hermes")
                self.assertTrue(completed.wait(5))
                execute.assert_called_once()

    def test_reconstruction_preserves_queue_and_interrupts_without_duplicate(self) -> None:
        manual = ManualExecutor()
        old = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="old",
            max_concurrency=1,
            executor=manual,
        )
        first = old.submit("task-a", "worker-native", "native")
        second = old.submit("task-b", "worker-native", "native")
        self.assertEqual(self.statuses(), {"task-a": "RUNNING", "task-b": "QUEUED"})

        started_b = threading.Event()
        release_b = threading.Event()

        def dispatch(assignment: WorkerAssignment) -> None:
            self.assertEqual(assignment.task_id, "task-b")
            started_b.set()
            self.assertTrue(release_b.wait(5))

        restarted = WorkerPool(
            self.connect,
            dispatch,
            controller_instance_id="new",
            max_concurrency=1,
        )
        self.addCleanup(restarted.shutdown)
        self.assertEqual(restarted.reconcile(), 1)
        restarted.pump()
        self.assertTrue(started_b.wait(5))
        with self.connect() as connection:
            states = dict(
                connection.execute(
                    "SELECT assignment_id, status FROM worker_pool_assignments"
                )
            )
        self.assertEqual(states[first], "INTERRUPTED")
        self.assertEqual(states[second], "RUNNING")
        self.assertEqual(len(manual.futures), 1)
        release_b.set()

    def test_invalid_limits_cancellation_and_runtime_neutral_events(self) -> None:
        for value in (0, 17, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                WorkerPool(
                    self.connect,
                    lambda _: None,
                    controller_instance_id="invalid",
                    max_concurrency=value,
                )
        manual = ManualExecutor()
        pool = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="cancel",
            executor=manual,
        )
        pool.submit("task-a", "worker-native", "native")
        queued = pool.submit("task-b", "worker-native", "native")
        self.assertTrue(pool.cancel_queued(queued))
        with self.connect() as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(worker_pool_events)")
            }
            state = connection.execute(
                "SELECT status FROM worker_pool_assignments WHERE assignment_id = ?",
                (queued,),
            ).fetchone()[0]
        self.assertEqual(state, "CANCELLED")
        self.assertEqual(columns, {"pool_event_id", "assignment_id", "event_kind", "created_at"})
        self.assertFalse(any("provider" in column or "prompt" in column for column in columns))


if __name__ == "__main__":
    unittest.main()
