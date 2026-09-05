from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from model_router import (  # noqa: E402
    ModelRouteRequest,
    ModelRouteRule,
    ModelRouteStore,
    ModelRouter,
    ModelRouterError,
    ModelRoutingPolicy,
    canonical_json,
)


NOW = "2026-09-05T00:00:00.000Z"


class ModelRouterPersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name) / "router.db"
        self.apply_through(31)
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO roles(
                    role_id,profile_name,role_kind,description,reasoning_effort,
                    max_turns,toolsets_json,skills_json,workspace_mode,may_commit,
                    may_push,network_enabled,cpu_limit,memory_mb,enabled,
                    config_source,config_hash,registered_at,updated_at,runtime_kind,
                    model_id
                ) VALUES ('router-worker','router-worker','worker','test','high',10,
                          '[]','[]','write',1,0,0,1,512,1,'test',?,?,?,'native','base-model')
                """,
                ("a" * 64, NOW, NOW),
            )
            connection.commit()
        self.store = ModelRouteStore(self.connect)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def apply_through(self, version: int) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                if int(migration.name[:3]) <= version:
                    connection.executescript(migration.read_text(encoding="utf-8"))

    def request(self, **overrides: object) -> ModelRouteRequest:
        values: dict[str, object] = {
            "runtime_request_id": "runtime-request-persist",
            "role_id": "router-worker",
            "runtime_role": "worker",
            "runtime_kind": "native",
            "configured_model_id": "base-model",
            "task_kind": "PIPELINE",
        }
        values.update(overrides)
        return ModelRouteRequest(**values)  # type: ignore[arg-type]

    def policy(self) -> ModelRoutingPolicy:
        return ModelRoutingPolicy(
            version=7,
            rules=(
                ModelRouteRule(
                    "native-pipeline",
                    "Qwen/Qwen3.8-27B",
                    runtime_kind="native",
                    task_kind="PIPELINE",
                ),
            ),
        )

    def test_policy_and_decision_are_durable_idempotent_and_explainable(self) -> None:
        request = self.request()
        policy = self.policy()
        decision = ModelRouter(policy).route(request)
        first = self.store.record(
            request=request,
            policy=policy,
            decision=decision,
            execution_kind="WORKER",
            execution_id="worker-execution-route",
        )
        second = self.store.record(
            request=request,
            policy=policy,
            decision=decision,
            execution_kind="WORKER",
            execution_id="worker-execution-route",
        )
        self.assertEqual(first, second)
        row = self.store.get(first)
        self.assertEqual(row["selected_model_id"], "Qwen/Qwen3.8-27B")
        self.assertEqual(row["rule_id"], "native-pipeline")
        self.assertEqual(row["reason"], "rule_match")
        self.assertEqual(row["policy_sha256"], policy.sha256)
        self.assertEqual(row["request"]["task_kind"], "PIPELINE")
        self.assertEqual(
            row["request_sha256"],
            hashlib.sha256(canonical_json(request.as_dict()).encode()).hexdigest(),
        )
        with self.connect() as connection:
            self.assertEqual(
                connection.execute("SELECT count(*) FROM model_routing_policies").fetchone()[0],
                1,
            )
            self.assertEqual(
                connection.execute("SELECT count(*) FROM model_route_decisions").fetchone()[0],
                1,
            )

    def test_identity_reuse_with_different_route_fails_closed(self) -> None:
        request = self.request()
        policy = self.policy()
        decision = ModelRouter(policy).route(request)
        self.store.record(
            request=request, policy=policy, decision=decision,
            execution_kind="WORKER", execution_id="worker-execution-route",
        )
        with self.assertRaises(ModelRouterError):
            self.store.record(
                request=self.request(runtime_request_id="runtime-request-other"),
                policy=policy,
                decision=ModelRouter(policy).route(
                    self.request(runtime_request_id="runtime-request-other")
                ),
                execution_kind="WORKER",
                execution_id="worker-execution-route",
            )

    def test_execution_kind_must_match_runtime_role(self) -> None:
        request = self.request()
        policy = self.policy()
        with self.assertRaises(ModelRouterError):
            self.store.record(
                request=request,
                policy=policy,
                decision=ModelRouter(policy).route(request),
                execution_kind="PLANNER",
                execution_id="planner-with-worker-role",
            )

    def test_store_rejects_role_snapshot_or_decision_mismatch(self) -> None:
        request = self.request()
        policy = self.policy()
        with self.assertRaises(ModelRouterError):
            self.store.record(
                request=request,
                policy=policy,
                decision=ModelRouter(ModelRoutingPolicy(version=1)).route(request),
                execution_kind="WORKER",
                execution_id="worker-execution-route",
            )
        with self.assertRaises(ModelRouterError):
            self.store.record(
                request=self.request(configured_model_id="different"),
                policy=policy,
                decision=ModelRouter(policy).route(
                    self.request(configured_model_id="different")
                ),
                execution_kind="WORKER",
                execution_id="worker-execution-route-2",
            )

    def test_database_guards_reject_forged_route_and_history_mutation(self) -> None:
        request = self.request()
        policy = self.policy()
        decision = ModelRouter(policy).route(request)
        decision_id = self.store.record(
            request=request, policy=policy, decision=decision,
            execution_kind="WORKER", execution_id="worker-execution-route",
        )
        with self.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE model_route_decisions SET selected_model_id='forged' WHERE decision_id=?",
                    (decision_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM model_routing_policies WHERE policy_sha256=?",
                    (policy.sha256,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO model_route_decisions(
                        decision_id,runtime_request_id,execution_kind,execution_id,
                        role_id,runtime_role,runtime_kind,task_kind,configured_model_id,
                        selected_model_id,request_json,request_sha256,policy_sha256,
                        policy_version,rule_id,reason,created_at
                    ) VALUES ('forged','forged-request','WORKER','forged-exec',
                        'router-worker','worker','native','PIPELINE','base-model','wrong-model',
                        ?,?,?,7,'native-pipeline','rule_match',?)
                    """,
                    (
                        canonical_json(self.request(runtime_request_id="forged-request").as_dict()),
                        "b" * 64,
                        policy.sha256,
                        NOW,
                    ),
                )

    def test_database_rejects_execution_role_and_role_snapshot_forgery(self) -> None:
        policy = self.policy()
        request = self.request(runtime_request_id="db-provenance-request")
        # Snapshot the policy legitimately first.
        self.store.record(
            request=request,
            policy=policy,
            decision=ModelRouter(policy).route(request),
            execution_kind="WORKER",
            execution_id="db-provenance-valid",
        )
        request_text = canonical_json(request.as_dict())
        request_hash = hashlib.sha256(request_text.encode()).hexdigest()
        with self.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO model_route_decisions(
                        decision_id,runtime_request_id,execution_kind,execution_id,
                        role_id,runtime_role,runtime_kind,task_kind,configured_model_id,
                        selected_model_id,request_json,request_sha256,policy_sha256,
                        policy_version,rule_id,reason,created_at
                    ) VALUES ('bad-kind-role','db-provenance-request','PLANNER','bad-kind-exec',
                        'router-worker','worker','native','PIPELINE','base-model',
                        'Qwen/Qwen3.8-27B',?,?,?,7,'native-pipeline','rule_match',?)
                    """,
                    (request_text, request_hash, policy.sha256, NOW),
                )
            forged_request = self.request(
                runtime_request_id="bad-role-model-request",
                configured_model_id="other-model",
            )
            forged_text = canonical_json(forged_request.as_dict())
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO model_route_decisions(
                        decision_id,runtime_request_id,execution_kind,execution_id,
                        role_id,runtime_role,runtime_kind,task_kind,configured_model_id,
                        selected_model_id,request_json,request_sha256,policy_sha256,
                        policy_version,rule_id,reason,created_at
                    ) VALUES ('bad-role-model','bad-role-model-request','WORKER','bad-role-exec',
                        'router-worker','worker','native','PIPELINE','other-model',
                        'Qwen/Qwen3.8-27B',?,?,?,7,'native-pipeline','rule_match',?)
                    """,
                    (
                        forged_text,
                        hashlib.sha256(forged_text.encode()).hexdigest(),
                        policy.sha256,
                        NOW,
                    ),
                )

    def test_database_enforces_first_match_and_rejects_false_default(self) -> None:
        policy = ModelRoutingPolicy(
            version=9,
            rules=(
                ModelRouteRule("first-worker", "first-model", runtime_role="worker"),
                ModelRouteRule("second-native", "second-model", runtime_kind="native"),
            ),
        )
        request = self.request(runtime_request_id="first-match-request")
        expected = ModelRouter(policy).route(request)
        self.store.record(
            request=request,
            policy=policy,
            decision=expected,
            execution_kind="WORKER",
            execution_id="first-match-execution",
        )
        request_text = canonical_json(
            self.request(runtime_request_id="forged-order-request").as_dict()
        )
        request_hash = hashlib.sha256(request_text.encode()).hexdigest()
        with self.connect() as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO model_route_decisions(
                        decision_id,runtime_request_id,execution_kind,execution_id,
                        role_id,runtime_role,runtime_kind,task_kind,configured_model_id,
                        selected_model_id,request_json,request_sha256,policy_sha256,
                        policy_version,rule_id,reason,created_at
                    ) VALUES ('forged-order','forged-order-request','WORKER','forged-order-exec',
                        'router-worker','worker','native','PIPELINE','base-model','second-model',
                        ?,?,?,9,'second-native','rule_match',?)
                    """,
                    (request_text, request_hash, policy.sha256, NOW),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO model_route_decisions(
                        decision_id,runtime_request_id,execution_kind,execution_id,
                        role_id,runtime_role,runtime_kind,task_kind,configured_model_id,
                        selected_model_id,request_json,request_sha256,policy_sha256,
                        policy_version,rule_id,reason,created_at
                    ) VALUES ('forged-default','forged-order-request','WORKER','forged-default-exec',
                        'router-worker','worker','native','PIPELINE','base-model','base-model',
                        ?,?,?,9,'configured-role-model','configured_default',?)
                    """,
                    (request_text, request_hash, policy.sha256, NOW),
                )

    def test_store_can_atomically_link_reserved_execution(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO roles(
                    role_id,profile_name,role_kind,description,reasoning_effort,
                    max_turns,toolsets_json,skills_json,workspace_mode,may_commit,
                    may_push,network_enabled,cpu_limit,memory_mb,enabled,
                    config_source,config_hash,registered_at,updated_at,runtime_kind,
                    model_id
                ) VALUES ('router-planner-linked','router-planner-linked','orchestrator',
                          'test','high',10,'[]','[]','none',0,0,0,1,512,1,'test',?,?,?,
                          'native','base-model')
                """,
                ("e" * 64, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO orchestrator_executions(
                    execution_id,role_id,source_profile,outer_container_name,prompt_path,
                    output_path,marker,result_json,created_at,started_at,runtime_kind
                ) VALUES ('planner-linked','router-planner-linked','router-planner-linked',
                          'container-linked','/tmp/prompt-linked','/tmp/output-linked',
                          'DONE','{}',?,?, 'native')
                """,
                (NOW, NOW),
            )
            connection.commit()
        request = ModelRouteRequest(
            runtime_request_id="runtime-request-linked",
            role_id="router-planner-linked",
            runtime_role="planner",
            runtime_kind="native",
            configured_model_id="base-model",
        )
        policy = ModelRoutingPolicy(version=1)
        decision = ModelRouter(policy).route(request)
        decision_id = self.store.record(
            request=request,
            policy=policy,
            decision=decision,
            execution_kind="PLANNER",
            execution_id="planner-linked",
            link_execution=True,
        )
        with self.connect() as connection:
            linked = connection.execute(
                "SELECT model_route_decision_id FROM orchestrator_executions "
                "WHERE execution_id='planner-linked'"
            ).fetchone()[0]
        self.assertEqual(linked, decision_id)

    def test_atomic_link_failure_does_not_leave_route_history(self) -> None:
        request = self.request(runtime_request_id="runtime-request-unreserved")
        policy = self.policy()
        decision = ModelRouter(policy).route(request)
        with self.assertRaises(ModelRouterError):
            self.store.record(
                request=request,
                policy=policy,
                decision=decision,
                execution_kind="WORKER",
                execution_id="missing-worker-execution",
                link_execution=True,
            )
        with self.connect() as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM model_route_decisions "
                    "WHERE runtime_request_id='runtime-request-unreserved'"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM model_routing_policies"
                ).fetchone()[0],
                0,
            )

    def test_execution_link_must_match_route_identity_and_is_immutable(self) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO roles(
                    role_id,profile_name,role_kind,description,reasoning_effort,
                    max_turns,toolsets_json,skills_json,workspace_mode,may_commit,
                    may_push,network_enabled,cpu_limit,memory_mb,enabled,
                    config_source,config_hash,registered_at,updated_at,runtime_kind,
                    model_id
                ) VALUES ('router-planner','router-planner','orchestrator','test','high',10,
                          '[]','[]','none',0,0,0,1,512,1,'test',?,?,?,'hermes','base-model')
                """,
                ("d" * 64, NOW, NOW),
            )
            connection.commit()
        request = ModelRouteRequest(
            runtime_request_id="runtime-request-planner",
            role_id="router-planner",
            runtime_role="planner",
            runtime_kind="hermes",
            configured_model_id="base-model",
        )
        policy = self.policy()
        decision = ModelRouter(policy).route(request)
        decision_id = self.store.record(
            request=request, policy=policy, decision=decision,
            execution_kind="PLANNER", execution_id="planner-execution-route",
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO orchestrator_executions(
                    execution_id,role_id,source_profile,outer_container_name,prompt_path,
                    output_path,marker,result_json,created_at,started_at,runtime_kind,
                    model_route_decision_id
                ) VALUES (?,?,?,?,?,?,?,'{}',?,?,?,?)
                """,
                (
                    "planner-execution-route","router-planner","router-planner","container-route",
                    "/tmp/route-prompt","/tmp/route-output","DONE",NOW,NOW,"hermes",decision_id,
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE orchestrator_executions SET model_route_decision_id=NULL "
                    "WHERE execution_id='planner-execution-route'"
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO orchestrator_executions(
                        execution_id,role_id,source_profile,outer_container_name,prompt_path,
                        output_path,marker,result_json,created_at,started_at,runtime_kind,
                        model_route_decision_id
                    ) VALUES ('wrong-execution','router-planner','router-planner','container-wrong',
                              '/tmp/wrong-prompt','/tmp/wrong-output','DONE','{}',?,?,?,?)
                    """,
                    (NOW,NOW,"hermes",decision_id),
                )
            for statement in (
                "UPDATE orchestrator_executions SET execution_id='planner-execution-renamed' "
                "WHERE execution_id='planner-execution-route'",
                "UPDATE orchestrator_executions SET runtime_kind='native' "
                "WHERE execution_id='planner-execution-route'",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(statement)
            triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE '%model_route_identity_immutable'"
                )
            }
            self.assertEqual(
                triggers,
                {
                    'orchestrator_model_route_identity_immutable',
                    'worker_model_route_identity_immutable',
                    'reviewer_model_route_identity_immutable',
                },
            )
            delete_triggers = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='trigger' "
                    "AND name LIKE '%model_route_execution_immutable_delete'"
                )
            }
            self.assertEqual(
                delete_triggers,
                {
                    'orchestrator_model_route_execution_immutable_delete',
                    'worker_model_route_execution_immutable_delete',
                    'reviewer_model_route_execution_immutable_delete',
                },
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM orchestrator_executions "
                    "WHERE execution_id='planner-execution-route'"
                )
            self.assertEqual(
                connection.execute(
                    "SELECT count(*) FROM orchestrator_executions "
                    "WHERE execution_id='planner-execution-route'"
                ).fetchone()[0],
                1,
            )

    def test_schema_31_preserves_historical_execution_rows_without_fabrication(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        database = Path(temporary.name) / "history.db"
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                if int(migration.name[:3]) <= 30:
                    connection.executescript(migration.read_text(encoding="utf-8"))
            connection.execute(
                """
                INSERT INTO roles(
                    role_id,profile_name,role_kind,description,reasoning_effort,max_turns,
                    toolsets_json,skills_json,workspace_mode,may_commit,may_push,network_enabled,
                    cpu_limit,memory_mb,enabled,config_source,config_hash,registered_at,updated_at,
                    runtime_kind,model_id
                ) VALUES ('history-role','history-role','orchestrator','test','high',10,'[]','[]',
                          'none',0,0,0,1,512,1,'test',?,?,?,'hermes','history-model')
                """,
                ("c"*64,NOW,NOW),
            )
            connection.execute(
                """
                INSERT INTO orchestrator_executions(
                    execution_id,role_id,source_profile,outer_container_name,prompt_path,
                    output_path,marker,result_json,created_at,started_at,runtime_kind
                ) VALUES ('history-exec','history-role','history','history-container',
                          '/tmp/history-prompt','/tmp/history-output','DONE','{}',?,?, 'hermes')
                """,
                (NOW,NOW),
            )
            connection.executescript(
                (ROOT / "migrations/031_model_router.sql").read_text(encoding="utf-8")
            )
            row = connection.execute(
                "SELECT execution_id,model_route_decision_id FROM orchestrator_executions"
            ).fetchone()
            self.assertEqual(row, ("history-exec", None))
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 31)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")


if __name__ == "__main__":
    unittest.main()
