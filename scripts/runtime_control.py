"""Control-plane helpers for selecting and journaling AgentRuntime execution."""

from __future__ import annotations

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
) -> tuple[RuntimeKind, AgentRuntime]:
    """Resolve a synchronized role snapshot through the common factory."""
    kind = parse_runtime_kind(role["runtime_kind"])
    runtime = create_runtime(
        root,
        required_role=required_role,
        kind=kind,
        model=role["model_id"],
        provider=provider,
    )
    return kind, runtime


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
