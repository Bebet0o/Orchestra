from __future__ import annotations

import concurrent.futures
from dataclasses import replace
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_task_graph as graph
from agent_runtime import (
    FakeRuntime,
    FakeRuntimeOutcome,
    HermesRuntime,
    NativeRuntime,
    RuntimeResult,
    RuntimeRole,
)
from model_provider import FakeModelProvider, FakeModelProviderOutcome
from recovery_coordinator import RecoveryCoordinator, RecoveryError, policy_authority
from reviewer_judge import ReviewStore
from shared_context import ContextProjector, canonical_json, content_sha256
from worker_pool import WorkerPool

import test_reviewer_judge as reviewer_tests


ORCH = graph.ORCHESTRATOR


class RecoveryLoopTest(unittest.TestCase):
    connect = graph.TaskGraphTest.connect
    _seed = graph.TaskGraphTest._seed
    task = graph.TaskGraphTest.task
    plan = graph.TaskGraphTest.plan
    states = graph.TaskGraphTest.states
    runtime_request = graph.TaskGraphTest.runtime_request

    def setUp(self) -> None:
        graph.TaskGraphTest.setUp(self)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO roles (
                    role_id,profile_name,role_kind,description,reasoning_effort,
                    max_turns,toolsets_json,skills_json,workspace_mode,may_commit,
                    may_push,network_enabled,cpu_limit,memory_mb,enabled,
                    config_source,config_hash,registered_at,updated_at,runtime_kind,
                    model_id
                ) SELECT 'reviewer','ops-reviewer','reviewer',description,
                    reasoning_effort,max_turns,toolsets_json,skills_json,
                    'read_only',0,0,0,cpu_limit,memory_mb,enabled,config_source,
                    config_hash,registered_at,updated_at,'native',model_id
                  FROM roles WHERE role_id='orchestrator'
                """
            )
        supervisor = mock.patch.object(ORCH, "supervisor_is_healthy", return_value=True)
        supervisor.start()
        self.addCleanup(supervisor.stop)
        self.executor = graph.ManualExecutor()
        self.pool = WorkerPool(
            self.connect,
            lambda _: None,
            controller_instance_id="graph-controller",
            max_concurrency=2,
            executor=self.executor,
            recovery_eligible=ORCH.recovery_dispatch_eligible,
        )
        self.addCleanup(self.pool.shutdown)
        self.store = ReviewStore(self.connect)
        self.projector = ContextProjector(self.connect)
        self.recovery = RecoveryCoordinator(self.connect)
        self.sandbox = replace(
            graph.TaskGraphTest.runtime_request(self, "review").sandbox,
            read_only=True,
        )

    def create(self, *, retries: int = 1, diamond: bool = False) -> str:
        shape = (
            [("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"])]
            if diamond
            else [("b", []), ("d", ["b"])]
        )
        tasks = [self.task(key, dependencies) for key, dependencies in shape]
        for task in tasks:
            if task["key"] in {"b", "c"}:
                task["review"] = {"required": True}
        plan = ORCH.validate_plan(self.plan(tasks), allow_test_actions=False)
        plan_id = ORCH.insert_plan(
            plan,
            source="AI",
            initial_status="READY",
            recovery_max_retries=retries,
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO objective_queue(
                    objective_id,objective,source,status,priority,not_before,
                    project_scope_json,max_parallel_tasks,planning_max_attempts,
                    planning_attempt_count,plan_id,created_at,heartbeat_at
                ) VALUES (?,'recovery objective','AI','RUNNING',100,?,?,2,3,1,?,?,?)
                """,
                (
                    "objective-" + plan_id,
                    graph.NOW,
                    json.dumps(["project-" + task["key"] for task in tasks]),
                    plan_id,
                    graph.NOW,
                    graph.NOW,
                ),
            )
        ORCH.refresh_plan_states(plan_id)
        return plan_id

    def start(self, plan_id: str, key: str) -> tuple[sqlite3.Row, str, str, dict]:
        with self.connect() as connection:
            task = connection.execute(
                "SELECT * FROM orchestration_tasks WHERE plan_id=? AND task_key=?",
                (plan_id, key),
            ).fetchone()
        _, runtime_kind = ORCH.worker_assignment_snapshot(
            task["orchestration_task_id"]
        )
        assignment = self.pool.submit(
            task["orchestration_task_id"], task["role_id"], runtime_kind
        )
        attempt, _, task = ORCH.reserve_attempt(
            task["orchestration_task_id"], instance_id="graph-controller"
        )
        self.pool.bind_attempt(assignment, attempt)
        snapshot = self.projector.freeze_task(
            task_id=task["orchestration_task_id"],
            assignment_id=assignment,
            attempt_id=attempt,
        )
        return task, attempt, assignment, snapshot

    def finish(self, started: tuple, result: str) -> str | None:
        task, attempt, assignment, _ = started
        ORCH.finish_task_success(task, attempt, {"output": result})
        with self.connect() as connection:
            assignments = [
                row[0]
                for row in connection.execute(
                    "SELECT assignment_id FROM worker_pool_assignments ORDER BY queue_sequence"
                )
            ]
        future = self.executor.futures[assignments.index(assignment)]
        if not future.done():
            future.set_result({"output": result})
        return next(
            (
                row["review_id"]
                for row in self.store.list(plan_id=task["plan_id"])
                if row["attempt_id"] == attempt
            ),
            None,
        )

    def judge(self, review_id: str, assessment: str) -> None:
        value = reviewer_tests.payload(
            assessment,
            summary="Fix the rejected implementation",
            findings=[
                {
                    "code": "FIX_X",
                    "severity": "error",
                    "message": "X remains incorrect",
                    "evidence": [],
                }
            ] if assessment == "needs_fix" else [],
            required_changes=["fix X"] if assessment == "needs_fix" else [],
        )
        runtime = FakeRuntime(
            [FakeRuntimeOutcome.success(output=reviewer_tests.output(value))]
        )
        self.assertTrue(
            self.store.execute(
                review_id, runtime, owner="judge", sandbox=self.sandbox
            )
        )

    def rejected(self, *, retries: int = 1) -> tuple[str, tuple, str]:
        plan_id = self.create(retries=retries)
        first = self.start(plan_id, "b")
        review_id = self.finish(first, "R1")
        assert review_id is not None
        self.judge(review_id, "needs_fix")
        return plan_id, first, review_id

    def corrective(self, first: tuple) -> tuple:
        self.assertEqual(self.recovery.request_eligible(), 1)
        self.assertEqual(self.recovery.dispatch_pending(self.pool), 1)
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[-1]
        attempt, number, task = ORCH.reserve_attempt(
            first[0]["orchestration_task_id"],
            instance_id="graph-controller",
            pool_assignment_id=action["target_assignment_id"],
        )
        self.assertEqual(number, 2)
        self.pool.bind_attempt(action["target_assignment_id"], attempt)
        self.recovery.link_attempt(
            assignment_id=action["target_assignment_id"], attempt_id=attempt
        )
        snapshot = self.projector.freeze_task(
            task_id=task["orchestration_task_id"],
            assignment_id=action["target_assignment_id"],
            attempt_id=attempt,
        )
        return task, attempt, action["target_assignment_id"], snapshot

    def test_default_zero_preserves_needs_fix_without_retry(self) -> None:
        plan_id, first, _ = self.rejected(retries=0)
        for _ in range(3):
            self.recovery.reconcile(self.pool)
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])
        self.assertEqual([item["status"] for item in action], ["EXHAUSTED"])
        self.assertEqual(self.states(plan_id), {"b": "BLOCKED", "d": "PENDING"})
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM orchestration_attempts").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT recovery_retry_count FROM orchestration_tasks WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 0)

    def test_corrective_attempt_gets_review_overlay_and_separate_snapshot(self) -> None:
        _, first, review_id = self.rejected()
        first_bytes = canonical_json(first[3]["projection"])
        second = self.corrective(first)
        recovery = second[3]["projection"]["recovery"]
        self.assertEqual(recovery["source_attempt_id"], first[1])
        self.assertEqual(recovery["source_review"]["review_id"], review_id)
        self.assertEqual(recovery["required_changes"], ["fix X"])
        self.assertEqual(recovery["judge"]["disposition"], "NEEDS_FIX")
        self.assertNotEqual(first[3]["context_snapshot_id"], second[3]["context_snapshot_id"])
        self.assertEqual(first_bytes, canonical_json(first[3]["projection"]))
        with self.connect() as connection:
            row = connection.execute("SELECT recovery_action_id FROM context_snapshots WHERE attempt_id=?", (second[1],)).fetchone()
            self.assertIsNotNone(row[0])

    def test_recovery_pass_accepts_task_and_releases_downstream(self) -> None:
        plan_id, first, _ = self.rejected()
        second = self.corrective(first)
        review_id = self.finish(second, "R2")
        assert review_id is not None
        self.judge(review_id, "pass")
        self.assertTrue(self.store.accept(review_id))
        self.recovery.finish_terminal_actions()
        ORCH.refresh_plan_states(plan_id)
        self.assertEqual(self.states(plan_id), {"b": "COMPLETED", "d": "READY"})
        lineage = self.recovery.lineage(first[0]["orchestration_task_id"])
        self.assertEqual(len(lineage["attempts"]), 2)
        self.assertEqual([item["disposition"] for item in lineage["attempts"]], ["NEEDS_FIX", "PASS"])
        self.assertEqual(lineage["recovery_actions"][0]["status"], "COMPLETED")

    def test_second_needs_fix_exhausts_budget_stably(self) -> None:
        plan_id, first, _ = self.rejected()
        second = self.corrective(first)
        review_id = self.finish(second, "R2 rejected")
        assert review_id is not None
        self.judge(review_id, "needs_fix")
        for _ in range(3):
            self.recovery.reconcile(self.pool)
        actions = self.recovery.list(task_id=first[0]["orchestration_task_id"])
        self.assertEqual([item["status"] for item in actions], ["COMPLETED", "EXHAUSTED"])
        self.assertEqual(self.states(plan_id)["d"], "PENDING")
        with self.connect() as connection:
            task = connection.execute("SELECT recovery_retry_count FROM orchestration_tasks WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()
            self.assertEqual(task[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM orchestration_attempts WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 2)

    def test_non_needs_fix_dispositions_never_create_recovery(self) -> None:
        for assessment in ("pass", "blocked", "human_review"):
            with self.subTest(assessment=assessment):
                plan_id = self.create()
                started = self.start(plan_id, "b")
                if assessment == "human_review":
                    with self.connect() as connection:
                        connection.execute("INSERT INTO runs(run_id,project_id,status,created_at) VALUES (?,?, 'COMPLETED',?)", ("run-" + started[1], started[0]["project_id"], graph.NOW))
                        connection.execute("UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?", ("run-" + started[1], started[1]))
                review_id = self.finish(started, assessment)
                assert review_id is not None
                self.judge(review_id, assessment)
                self.assertEqual(self.recovery.request_eligible(), 0)
                self.assertEqual(self.recovery.list(task_id=started[0]["orchestration_task_id"]), [])

    def test_reviewer_failure_never_creates_recovery(self) -> None:
        plan_id = self.create()
        started = self.start(plan_id, "b")
        review_id = self.finish(started, "R1")
        assert review_id is not None
        self.store.fail(review_id, "RUNTIME_FAILED")
        self.assertEqual(self.recovery.reconcile(self.pool)["requested"], 0)

    def test_concurrent_reconcilers_create_one_action_and_assignment(self) -> None:
        _, first, _ = self.rejected()
        barrier = threading.Barrier(2)

        def run(_: int) -> None:
            barrier.wait(timeout=5)
            RecoveryCoordinator(self.connect).reconcile(self.pool)

        with concurrent.futures.ThreadPoolExecutor(2) as executor:
            list(executor.map(run, (1, 2)))
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM recovery_actions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM worker_pool_assignments WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT recovery_retry_count FROM orchestration_tasks WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 1)

    def test_restart_boundaries_are_idempotent(self) -> None:
        _, first, _ = self.rejected()
        restarted = RecoveryCoordinator(self.connect)
        self.assertEqual(restarted.request_eligible(), 1)
        self.assertEqual(restarted.request_eligible(), 0)
        self.assertEqual(restarted.dispatch_pending(self.pool), 1)
        self.assertEqual(restarted.dispatch_pending(self.pool), 0)
        action = restarted.list(task_id=first[0]["orchestration_task_id"])[0]
        self.assertEqual(action["status"], "DISPATCHED")
        self.assertEqual(self.pool.reconcile(), 1)
        restarted.reconcile(self.pool)
        self.assertEqual(restarted.list(task_id=first[0]["orchestration_task_id"])[0]["status"], "CANCELLED")

    def test_recovery_waits_for_execution_constraints_before_dispatch(self) -> None:
        plan_id = self.create(diamond=True)
        a = self.start(plan_id, "a")
        self.finish(a, "A")
        ORCH.refresh_plan_states(plan_id)
        with self.connect() as connection:
            project_b = connection.execute(
                "SELECT project_id FROM orchestration_tasks WHERE plan_id=? AND task_key='b'",
                (plan_id,),
            ).fetchone()[0]
            connection.execute(
                "UPDATE orchestration_tasks SET project_id=? WHERE plan_id=? AND task_key='c'",
                (project_b, plan_id),
            )
        b = self.start(plan_id, "b")
        rb = self.finish(b, "B1")
        assert rb is not None
        self.judge(rb, "needs_fix")
        c = self.start(plan_id, "c")

        self.assertEqual(self.recovery.request_eligible(), 1)
        self.assertEqual(self.recovery.dispatch_pending(self.pool), 0)
        action = self.recovery.list(task_id=b[0]["orchestration_task_id"])[0]
        self.assertEqual(action["status"], "PENDING")
        self.assertEqual(action["recovery_retry_count"], 0)

        rc = self.finish(c, "C1")
        assert rc is not None
        self.judge(rc, "pass")
        self.assertTrue(self.store.accept(rc))
        ORCH.refresh_plan_states(plan_id)

        self.assertEqual(self.recovery.dispatch_pending(self.pool), 1)
        action = self.recovery.list(task_id=b[0]["orchestration_task_id"])[0]
        self.assertEqual(action["status"], "DISPATCHED")
        self.assertEqual(action["recovery_retry_count"], 1)

    def test_failed_pre_attempt_recovery_cannot_escape_to_normal_scheduler(self) -> None:
        plan_id, first, _ = self.rejected()
        self.assertEqual(self.recovery.request_eligible(), 1)
        self.assertEqual(self.recovery.dispatch_pending(self.pool), 1)
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        with self.connect() as connection:
            assignment = connection.execute(
                "SELECT status FROM worker_pool_assignments WHERE assignment_id=?",
                (action["target_assignment_id"],),
            ).fetchone()
        self.assertEqual(assignment["status"], "RUNNING")

        self.executor.futures[-1].set_exception(RuntimeError("dispatch failed before reserve"))
        self.assertEqual(self.recovery.finish_terminal_actions(), 1)
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        self.assertEqual(action["status"], "CANCELLED")
        with self.connect() as connection:
            task = connection.execute(
                "SELECT status, review_state FROM orchestration_tasks "
                "WHERE orchestration_task_id=?",
                (first[0]["orchestration_task_id"],),
            ).fetchone()
        self.assertEqual((task["status"], task["review_state"]), ("BLOCKED", "NEEDS_FIX"))
        self.assertNotIn(
            first[0]["orchestration_task_id"],
            ORCH.runnable_tasks(set(), capacity=16),
        )
        self.assertEqual(self.states(plan_id), {"b": "BLOCKED", "d": "PENDING"})

    def test_policy_bounds_hash_and_schema_guards(self) -> None:
        self.assertEqual(policy_authority(1), policy_authority(1))
        for invalid in (-1, 4, True):
            with self.assertRaises(RecoveryError):
                policy_authority(invalid)
        _, first, _ = self.rejected()
        self.recovery.request_eligible()
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        with self.connect() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute("UPDATE recovery_actions SET reason_json='{}' WHERE recovery_action_id=?", (action["recovery_action_id"],))

    def test_recovery_preserves_role_runtime_and_pool_capacity(self) -> None:
        _, first, _ = self.rejected()
        self.recovery.request_eligible()
        self.recovery.dispatch_pending(self.pool)
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        self.assertEqual((action["role_id"], action["runtime_kind"]), (first[0]["role_id"], "native"))
        with self.connect() as connection:
            self.assertLessEqual(connection.execute("SELECT count(*) FROM worker_pool_assignments WHERE status='RUNNING'").fetchone()[0], 2)

    def test_query_surface_has_hashed_lineage_without_secret(self) -> None:
        _, first, _ = self.rejected()
        self.recovery.request_eligible()
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        encoded = canonical_json(action)
        self.assertEqual(len(action["reason_sha256"]), 64)
        self.assertNotIn("provider", encoded.lower())
        self.assertEqual(action["source_disposition"], "NEEDS_FIX")
        self.assertEqual(content_sha256(action["reason"]), action["reason_sha256"])

    def test_two_retry_budget_consumes_once_per_corrective_attempt(self) -> None:
        _, first, _ = self.rejected(retries=2)
        second = self.corrective(first)
        second_review = self.finish(second, "R2")
        assert second_review is not None
        self.judge(second_review, "needs_fix")
        self.recovery.reconcile(self.pool)
        actions = self.recovery.list(task_id=first[0]["orchestration_task_id"])
        self.assertEqual([item["recovery_sequence"] for item in actions], [1, 2])
        self.assertEqual([item["status"] for item in actions], ["COMPLETED", "DISPATCHED"])
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT recovery_retry_count FROM orchestration_tasks WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 2)

    def test_interrupted_corrective_attempt_is_not_duplicated(self) -> None:
        _, first, _ = self.rejected(retries=2)
        second = self.corrective(first)
        self.pool.reconcile()
        ORCH.reconcile_interrupted_tasks("graph-controller")
        self.recovery.reconcile(self.pool)
        for _ in range(2):
            self.recovery.reconcile(self.pool)
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM orchestration_attempts WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT status FROM orchestration_attempts WHERE attempt_id=?", (second[1],)).fetchone()[0], "ABANDONED")
            self.assertEqual(connection.execute("SELECT recovery_retry_count FROM orchestration_tasks WHERE orchestration_task_id=?", (first[0]["orchestration_task_id"],)).fetchone()[0], 1)

    def test_cross_project_recovery_provenance_is_rejected(self) -> None:
        _, first, review_id = self.rejected()
        review = self.store.get(review_id)
        with self.connect() as connection, self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO recovery_actions(
                    recovery_action_id,project_id,objective_id,plan_id,task_id,
                    source_attempt_id,source_review_id,source_decision_id,
                    recovery_sequence,max_retries,status,reason_json,reason_sha256,
                    created_at
                ) VALUES ('foreign-recovery','project-d',?,?,?,?,?,?,1,1,
                          'PENDING','{}',?,?)
                """,
                (
                    review["objective_id"], review["plan_id"], review["task_id"],
                    review["attempt_id"], review_id, review["decision_id"],
                    content_sha256({}), graph.NOW,
                ),
            )

    def test_diamond_recovery_preserves_independent_branch(self) -> None:
        plan_id = self.create(diamond=True)
        a = self.start(plan_id, "a")
        self.finish(a, "A")
        ORCH.refresh_plan_states(plan_id)
        b = self.start(plan_id, "b")
        c = self.start(plan_id, "c")
        rb = self.finish(b, "B1")
        rc = self.finish(c, "C1")
        assert rb is not None and rc is not None
        self.judge(rb, "needs_fix")
        self.judge(rc, "pass")
        self.assertTrue(self.store.accept(rc))
        ORCH.refresh_plan_states(plan_id)
        self.assertEqual(self.states(plan_id)["d"], "PENDING")
        second = self.corrective(b)
        rb2 = self.finish(second, "B2")
        assert rb2 is not None
        self.judge(rb2, "pass")
        self.assertTrue(self.store.accept(rb2))
        ORCH.refresh_plan_states(plan_id)
        self.assertEqual(self.states(plan_id), {"a": "COMPLETED", "b": "COMPLETED", "c": "COMPLETED", "d": "READY"})
        with self.connect() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM orchestration_attempts WHERE orchestration_task_id=?", (c[0]["orchestration_task_id"],)).fetchone()[0], 1)

    def test_native_fake_provider_executes_with_recovery_context(self) -> None:
        _, first, _ = self.rejected()
        second = self.corrective(first)
        provider = FakeModelProvider([FakeModelProviderOutcome.success("corrected")])
        runtime = NativeRuntime(provider, "fixed-model")
        request = replace(
            self.runtime_request("worker"),
            context=second[3]["projection"],
            prompt="execute graph task\n" + canonical_json(second[3]["projection"]),
        )
        result = runtime.execute(request)
        self.assertEqual(result.output, "corrected")
        self.assertIn("fix X", provider.requests[0].messages[0].content)

    def test_hermes_runtime_contract_keeps_recovery_context(self) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE roles SET runtime_kind='hermes' WHERE role_id=?", ("worker-native",))
        plan_id = self.create()
        first = self.start(plan_id, "b")
        review_id = self.finish(first, "R1")
        assert review_id is not None
        self.judge(review_id, "needs_fix")
        self.recovery.request_eligible()
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        self.assertEqual(action["runtime_kind"], "hermes")
        with tempfile.TemporaryDirectory() as directory:
            installed = Path(directory)
            (installed / "repo").symlink_to(graph.ROOT, target_is_directory=True)
            runtime = HermesRuntime(installed, required_role=RuntimeRole.WORKER)
            request = replace(self.runtime_request("worker"), context={"recovery": action["reason"]})
            with mock.patch.object(runtime, "execute", return_value=RuntimeResult("corrected")) as execute:
                self.assertEqual(runtime.execute(request).output, "corrected")
            self.assertEqual(execute.call_args.args[0].context["recovery"]["required_changes"], ["fix X"])

    def test_exhaustion_reuses_existing_human_approval_authority(self) -> None:
        plan_id = self.create(retries=0)
        first = self.start(plan_id, "b")
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO runs(run_id,project_id,status,created_at) VALUES (?,?, 'COMPLETED',?)",
                ("run-" + first[1], first[0]["project_id"], graph.NOW),
            )
            connection.execute(
                "UPDATE orchestration_attempts SET run_id=? WHERE attempt_id=?",
                ("run-" + first[1], first[1]),
            )
        review_id = self.finish(first, "R1")
        assert review_id is not None
        self.judge(review_id, "needs_fix")
        self.recovery.reconcile(self.pool)
        action = self.recovery.list(task_id=first[0]["orchestration_task_id"])[0]
        self.assertEqual((action["status"], action["escalation_status"]), ("EXHAUSTED", "PENDING"))
        resolved = self.recovery.resolve_exhaustion(action["approval_id"], "ACKNOWLEDGE")
        self.assertEqual(resolved["escalation_status"], "APPROVED")
        self.assertEqual(self.states(plan_id), {"b": "BLOCKED", "d": "PENDING"})

    def test_configuration_default_and_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "orchestrator.toml"
            path.write_text("schema_version=2\n[worker_pool]\nmax_concurrency=1\n", encoding="utf-8")
            with mock.patch.object(ORCH, "CONFIG_PATH", path):
                self.assertEqual(ORCH.load_config()["recovery_max_retries"], 0)
            path.write_text("schema_version=2\n[worker_pool]\nmax_concurrency=1\n[recovery]\nmax_retries=3\n", encoding="utf-8")
            with mock.patch.object(ORCH, "CONFIG_PATH", path):
                self.assertEqual(ORCH.load_config()["recovery_max_retries"], 3)
            path.write_text("schema_version=2\n[worker_pool]\nmax_concurrency=1\n[recovery]\nmax_retries=4\n", encoding="utf-8")
            with mock.patch.object(ORCH, "CONFIG_PATH", path), self.assertRaises(ORCH.OrchestratorError):
                ORCH.load_config()

    def test_migration_30_and_task_graph_edges_are_stable(self) -> None:
        plan_id = self.create(diamond=True)
        with self.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 30)
            before = connection.execute("SELECT orchestration_task_id,depends_on_task_id FROM orchestration_dependencies WHERE plan_id=? ORDER BY 1,2", (plan_id,)).fetchall()
        a = self.start(plan_id, "a")
        self.finish(a, "A")
        ORCH.refresh_plan_states(plan_id)
        b = self.start(plan_id, "b")
        review_id = self.finish(b, "B1")
        assert review_id is not None
        self.judge(review_id, "needs_fix")
        self.recovery.reconcile(self.pool)
        with self.connect() as connection:
            after = connection.execute("SELECT orchestration_task_id,depends_on_task_id FROM orchestration_dependencies WHERE plan_id=? ORDER BY 1,2", (plan_id,)).fetchall()
        self.assertEqual([tuple(row) for row in before], [tuple(row) for row in after])


if __name__ == "__main__":
    unittest.main()
