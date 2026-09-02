"""Runtime-neutral agent execution boundary for Orchestra."""

from pathlib import Path

from .contract import (
    AgentRuntime,
    RuntimeEvent,
    RuntimeEventDispatcher,
    RuntimeEventKind,
    RuntimeError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimePreparedEnvironment,
    RuntimePreparedEnvironmentData,
    RuntimeResult,
    RuntimeRole,
    RuntimeSandboxContext,
)
from .failure import RuntimeFailureRecord, record_runtime_failure
from .fake import FakeRuntime, FakeRuntimeOutcome
from .hermes import HermesRuntime
from .native import NativeRuntime


def create_runtime(
    root: Path,
    *,
    required_role: RuntimeRole,
) -> AgentRuntime:
    """Construct the configured runtime implementation for one role."""
    return HermesRuntime(root, required_role=required_role)

__all__ = [
    "AgentRuntime",
    "FakeRuntime",
    "FakeRuntimeOutcome",
    "HermesRuntime",
    "NativeRuntime",
    "RuntimeEvent",
    "RuntimeEventDispatcher",
    "RuntimeEventKind",
    "RuntimeError",
    "RuntimeErrorKind",
    "RuntimeRequest",
    "RuntimePreparedEnvironment",
    "RuntimePreparedEnvironmentData",
    "RuntimeResult",
    "RuntimeRole",
    "RuntimeSandboxContext",
    "RuntimeFailureRecord",
    "create_runtime",
    "record_runtime_failure",
]
