"""Runtime-neutral agent execution boundary for Orchestra."""

import os
from enum import Enum
from pathlib import Path

from model_provider import (
    ModelProvider,
    OpenAICompatibleConfig,
    OpenAICompatibleProvider,
)

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


class RuntimeKind(str, Enum):
    HERMES = "hermes"
    NATIVE = "native"


def parse_runtime_kind(value: object) -> RuntimeKind:
    """Validate one persisted/configured runtime selector."""
    try:
        return RuntimeKind(value)
    except (TypeError, ValueError):
        raise ValueError("Runtime kind must be 'hermes' or 'native'") from None


def create_runtime(
    root: Path,
    *,
    required_role: RuntimeRole,
    kind: RuntimeKind | str = RuntimeKind.HERMES,
    model: str | None = None,
    provider: ModelProvider | None = None,
) -> AgentRuntime:
    """Construct the configured runtime implementation for one role."""
    selected = parse_runtime_kind(kind)
    if selected is RuntimeKind.HERMES:
        if provider is not None:
            raise ValueError("Hermes runtime does not accept a ModelProvider")
        return HermesRuntime(root, required_role=required_role)

    if not isinstance(model, str) or not model.strip():
        raise RuntimeError(
            RuntimeErrorKind.INVALID_RESULT,
            "Native runtime model configuration is unavailable",
        )
    if provider is None:
        endpoint = os.environ.get("ORCHESTRA_NATIVE_ENDPOINT_URL", "").strip()
        if not endpoint:
            raise RuntimeError(
                RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                "Native model provider configuration is unavailable",
            )
        api_key = os.environ.get("ORCHESTRA_NATIVE_API_KEY") or None
        try:
            provider = OpenAICompatibleProvider(
                OpenAICompatibleConfig(endpoint_url=endpoint, api_key=api_key)
            )
        except (TypeError, ValueError):
            raise RuntimeError(
                RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                "Native model provider configuration is invalid",
            ) from None
    return NativeRuntime(provider, model.strip())

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
    "RuntimeKind",
    "RuntimeSandboxContext",
    "RuntimeFailureRecord",
    "create_runtime",
    "parse_runtime_kind",
    "record_runtime_failure",
]
