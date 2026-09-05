"""Durable model-routing policy and decision authority."""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .contract import (
    ModelRouteDecision,
    ModelRouteRequest,
    ModelRouterError,
    ModelRoutingPolicy,
    canonical_json,
)
from .router import ModelRouter


_MAX_POLICY_BYTES = 131_072
_MAX_REQUEST_BYTES = 4_096
_EXECUTION_KINDS = {"PLANNER", "WORKER", "REVIEWER"}
_EXECUTION_RUNTIME_ROLES = {
    "PLANNER": "planner",
    "WORKER": "worker",
    "REVIEWER": "reviewer",
}
_ROLE_KINDS = {
    "planner": "orchestrator",
    "worker": "worker",
    "reviewer": "reviewer",
}


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ModelRouteStore:
    """Persist one immutable policy snapshot and one decision per execution."""

    def __init__(self, connect: Callable[[], sqlite3.Connection]) -> None:
        if not callable(connect):
            raise TypeError("Model route store connection factory must be callable")
        self._connect = connect

    def _transaction(self) -> contextlib.AbstractContextManager[sqlite3.Connection]:
        store = self

        class Transaction(contextlib.AbstractContextManager[sqlite3.Connection]):
            def __enter__(self) -> sqlite3.Connection:
                self.connection = store._connect()
                self.connection.row_factory = sqlite3.Row
                self.connection.execute("PRAGMA foreign_keys=ON")
                self.connection.execute("BEGIN IMMEDIATE")
                return self.connection

            def __exit__(self, exc_type, exc, tb) -> bool:
                try:
                    if exc_type is None:
                        self.connection.commit()
                    else:
                        self.connection.rollback()
                finally:
                    self.connection.close()
                return False

        return Transaction()

    @staticmethod
    def _policy_text(policy: ModelRoutingPolicy) -> str:
        if type(policy) is not ModelRoutingPolicy:
            raise TypeError("Model route policy is invalid")
        text = canonical_json(policy.as_dict())
        if len(text.encode("utf-8")) > _MAX_POLICY_BYTES:
            raise ModelRouterError("Model routing policy exceeds the durable limit")
        return text

    @staticmethod
    def _request_text(request: ModelRouteRequest) -> str:
        if type(request) is not ModelRouteRequest:
            raise TypeError("Model route request is invalid")
        text = canonical_json(request.as_dict())
        if len(text.encode("utf-8")) > _MAX_REQUEST_BYTES:
            raise ModelRouterError("Model route request exceeds the durable limit")
        return text

    @staticmethod
    def _validate_execution_identity(execution_kind: str, execution_id: str) -> tuple[str, str]:
        if execution_kind not in _EXECUTION_KINDS:
            raise ValueError("Model route execution kind is invalid")
        if not isinstance(execution_id, str) or not execution_id.strip() or len(execution_id) > 256:
            raise ValueError("Model route execution identity is invalid")
        return execution_kind, execution_id.strip()

    @staticmethod
    def _link_execution(
        connection: sqlite3.Connection,
        execution_kind: str,
        execution_id: str,
        decision_id: str,
    ) -> None:
        table = {
            "PLANNER": "orchestrator_executions",
            "WORKER": "worker_executions",
            "REVIEWER": "reviewer_executions",
        }[execution_kind]
        current = connection.execute(
            f"SELECT model_route_decision_id FROM {table} WHERE execution_id=?",
            (execution_id,),
        ).fetchone()
        if current is None:
            raise ModelRouterError("Model route execution is not reserved")
        if current[0] is not None and current[0] != decision_id:
            raise ModelRouterError("Model route execution is already linked differently")
        connection.execute(
            f"UPDATE {table} SET model_route_decision_id=? WHERE execution_id=?",
            (decision_id, execution_id),
        )

    def record(
        self,
        *,
        request: ModelRouteRequest,
        policy: ModelRoutingPolicy,
        decision: ModelRouteDecision,
        execution_kind: str,
        execution_id: str,
        orchestration_task_id: str | None = None,
        link_execution: bool = False,
    ) -> str:
        if type(decision) is not ModelRouteDecision:
            raise TypeError("Model route decision is invalid")
        execution_kind, execution_id = self._validate_execution_identity(
            execution_kind, execution_id
        )
        if request.runtime_role != _EXECUTION_RUNTIME_ROLES[execution_kind]:
            raise ModelRouterError(
                "Model route execution kind does not match the runtime role"
            )
        if orchestration_task_id is not None and (
            not isinstance(orchestration_task_id, str)
            or not orchestration_task_id.strip()
            or len(orchestration_task_id) > 256
        ):
            raise ValueError("Model route task identity is invalid")
        if type(link_execution) is not bool:
            raise TypeError("Model route execution linkage flag must be a boolean")
        expected = ModelRouter(policy).route(request)
        if decision != expected:
            raise ModelRouterError("Model route decision does not match the routing policy")

        policy_text = self._policy_text(policy)
        request_text = self._request_text(request)
        request_sha256 = _sha256(request_text)
        decision_id = "model-route-" + uuid.uuid4().hex
        now = _utc_now()

        try:
            with self._transaction() as connection:
                role = connection.execute(
                    "SELECT role_kind, runtime_kind, model_id FROM roles "
                    "WHERE role_id=? AND enabled=1",
                    (request.role_id,),
                ).fetchone()
                if role is None:
                    raise ModelRouterError("Model route role is unknown or disabled")
                if (
                    role[0] != _ROLE_KINDS[request.runtime_role]
                    or role[1] != request.runtime_kind
                    or role[2] != request.configured_model_id
                ):
                    raise ModelRouterError(
                        "Model route request does not match the role snapshot"
                    )

                existing_policy = connection.execute(
                    "SELECT policy_version, canonical_json FROM model_routing_policies "
                    "WHERE policy_sha256=?",
                    (policy.sha256,),
                ).fetchone()
                if existing_policy is None:
                    connection.execute(
                        "INSERT INTO model_routing_policies("
                        "policy_sha256,policy_version,canonical_json,created_at) "
                        "VALUES (?,?,?,?)",
                        (policy.sha256, policy.version, policy_text, now),
                    )
                elif (
                    int(existing_policy[0]) != policy.version
                    or existing_policy[1] != policy_text
                ):
                    raise ModelRouterError("Model routing policy digest collision")

                rows = connection.execute(
                    "SELECT * FROM model_route_decisions "
                    "WHERE runtime_request_id=? OR (execution_kind=? AND execution_id=?)",
                    (request.runtime_request_id, execution_kind, execution_id),
                ).fetchall()
                if rows:
                    if len(rows) != 1:
                        raise ModelRouterError("Conflicting model route identities")
                    row = rows[0]
                    exact = (
                        row["runtime_request_id"] == request.runtime_request_id
                        and row["execution_kind"] == execution_kind
                        and row["execution_id"] == execution_id
                        and row["role_id"] == request.role_id
                        and row["runtime_role"] == request.runtime_role
                        and row["runtime_kind"] == request.runtime_kind
                        and row["orchestration_task_id"] == orchestration_task_id
                        and row["task_kind"] == request.task_kind
                        and row["configured_model_id"] == request.configured_model_id
                        and row["selected_model_id"] == decision.selected_model_id
                        and row["request_json"] == request_text
                        and row["request_sha256"] == request_sha256
                        and row["policy_sha256"] == policy.sha256
                        and int(row["policy_version"]) == policy.version
                        and row["rule_id"] == decision.rule_id
                        and row["reason"] == decision.reason.value
                    )
                    if not exact:
                        raise ModelRouterError("Model route identity was already used differently")
                    if link_execution:
                        self._link_execution(
                            connection, execution_kind, execution_id, str(row["decision_id"])
                        )
                    return str(row["decision_id"])

                if orchestration_task_id is not None:
                    exists = connection.execute(
                        "SELECT 1 FROM orchestration_tasks WHERE orchestration_task_id=?",
                        (orchestration_task_id,),
                    ).fetchone()
                    if exists is None:
                        raise ModelRouterError("Model route task is unknown")

                connection.execute(
                    """
                    INSERT INTO model_route_decisions(
                        decision_id,runtime_request_id,execution_kind,execution_id,
                        role_id,runtime_role,runtime_kind,orchestration_task_id,
                        task_kind,configured_model_id,selected_model_id,request_json,
                        request_sha256,policy_sha256,policy_version,rule_id,reason,created_at
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        decision_id, request.runtime_request_id, execution_kind, execution_id,
                        request.role_id, request.runtime_role, request.runtime_kind,
                        orchestration_task_id, request.task_kind,
                        request.configured_model_id, decision.selected_model_id,
                        request_text, request_sha256, policy.sha256, policy.version,
                        decision.rule_id, decision.reason.value, now,
                    ),
                )
                if link_execution:
                    self._link_execution(
                        connection, execution_kind, execution_id, decision_id
                    )
        except sqlite3.DatabaseError as error:
            raise ModelRouterError("Model route persistence failed") from error
        return decision_id

    def get(self, decision_id: str) -> dict[str, Any]:
        if not isinstance(decision_id, str) or not decision_id:
            raise ValueError("Model route decision identity is invalid")
        with contextlib.closing(self._connect()) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM model_route_decisions WHERE decision_id=?",
                (decision_id,),
            ).fetchone()
        if row is None:
            raise ModelRouterError("Unknown model route decision")
        result = dict(row)
        result["request"] = json.loads(result.pop("request_json"))
        return result
