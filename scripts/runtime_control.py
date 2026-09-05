"""Control-plane helpers for selecting and journaling AgentRuntime execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_runtime import (
    AgentRuntime,
    RuntimeEvent,
    RuntimeKind,
    RuntimeRole,
    create_runtime,
    parse_runtime_kind,
)
from model_provider import ModelProvider
from model_router import (
    ModelRouteDecision,
    ModelRouteRequest,
    ModelRouteStore,
    ModelRoutingPolicy,
    ModelRouter,
    load_model_routing_policy,
)


def require_runtime_state(root: Path) -> None:
    """Fail before command side effects when durable runtime state is absent."""
    database = root / "state/controller/orchestra.db"
    if not database.is_file():
        from agent_runtime import RuntimeError, RuntimeErrorKind

        raise RuntimeError(
            RuntimeErrorKind.RUNTIME_UNAVAILABLE,
            "Agent runtime configuration is unavailable",
        )


def runtime_kind_of(runtime: AgentRuntime) -> RuntimeKind:
    """Return a runtime's stable identity, preserving fake-injection defaults."""
    return parse_runtime_kind(getattr(runtime, "runtime_kind", "hermes"))


def runtime_from_role(
    root: Path,
    role: Any,
    *,
    required_role: RuntimeRole,
    provider: ModelProvider | None = None,
    model: str | None = None,
) -> tuple[RuntimeKind, AgentRuntime]:
    """Resolve a synchronized role snapshot through the common factory."""
    kind = parse_runtime_kind(role["runtime_kind"])
    runtime = create_runtime(
        root,
        required_role=required_role,
        kind=kind,
        model=role["model_id"] if model is None else model,
        provider=provider,
    )
    return kind, runtime


@dataclass(frozen=True)
class PreparedModelRoute:
    policy: ModelRoutingPolicy
    request: ModelRouteRequest
    decision: ModelRouteDecision


def prepare_model_route(
    root: Path,
    role: Any,
    *,
    required_role: RuntimeRole,
    runtime_request_id: str,
    task_kind: str | None = None,
) -> PreparedModelRoute:
    role_name = {
        RuntimeRole.PLANNER: "planner",
        RuntimeRole.WORKER: "worker",
        RuntimeRole.REVIEWER: "reviewer",
    }[required_role]
    policy = load_model_routing_policy(root / "repo/config/orchestrator.toml")
    request = ModelRouteRequest(
        runtime_request_id=runtime_request_id,
        role_id=str(role["role_id"]),
        runtime_role=role_name,
        runtime_kind=str(role["runtime_kind"]),
        configured_model_id=str(role["model_id"]),
        task_kind=task_kind,
    )
    return PreparedModelRoute(
        policy=policy,
        request=request,
        decision=ModelRouter(policy).route(request),
    )


def persist_model_route(
    connection_factory: Any,
    prepared: PreparedModelRoute,
    *,
    execution_kind: str,
    execution_id: str,
    orchestration_task_id: str | None = None,
) -> str:
    return ModelRouteStore(connection_factory).record(
        request=prepared.request,
        policy=prepared.policy,
        decision=prepared.decision,
        execution_kind=execution_kind,
        execution_id=execution_id,
        orchestration_task_id=orchestration_task_id,
        link_execution=True,
    )


def runtime_from_prepared_route(
    root: Path,
    role: Any,
    prepared: PreparedModelRoute,
    *,
    required_role: RuntimeRole,
    provider: ModelProvider | None = None,
) -> tuple[RuntimeKind, AgentRuntime]:
    return runtime_from_role(
        root,
        role,
        required_role=required_role,
        provider=provider,
        model=prepared.decision.selected_model_id,
    )

def persist_runtime_event(
    connection: Any,
    *,
    execution_id: str,
    runtime_kind: RuntimeKind,
    event: RuntimeEvent,
) -> None:
    """Append one validated, secret-free runtime fact to durable state."""
    connection.execute(
        """
        INSERT INTO runtime_events (
            execution_id,
            runtime_request_id,
            runtime_kind,
            role,
            event_kind,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            execution_id,
            event.request_id,
            runtime_kind.value,
            event.role.value,
            event.kind.value,
            event.timestamp.isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
        ),
    )
    connection.commit()
