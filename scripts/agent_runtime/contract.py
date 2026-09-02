"""Stable types consumed by control-plane agent call sites."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol, runtime_checkable

from oci_reference import (
    is_canonical_oci_digest,
    parse_immutable_oci_reference,
)

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _validate_identifier(value: object, description: str) -> None:
    if (
        not isinstance(value, str)
        or value in {".", ".."}
        or _IDENTIFIER.fullmatch(value) is None
    ):
        raise ValueError(f"{description} is invalid")


class RuntimeRole(str, Enum):
    PLANNER = "planner"
    WORKER = "worker"
    REVIEWER = "reviewer"


class RuntimeErrorKind(str, Enum):
    RUNTIME_UNAVAILABLE = "runtime_unavailable"
    EXECUTION_FAILED = "execution_failed"
    TIMEOUT = "timeout"
    INVALID_RESULT = "invalid_result"
    CANCELLED = "cancelled"


class RuntimeEventKind(str, Enum):
    """Runtime execution facts currently consumed by the control plane."""

    STARTED = "started"
    HEARTBEAT = "heartbeat"


@dataclass(frozen=True)
class RuntimeEvent:
    """A bounded runtime fact tied to exactly one request and role."""

    kind: RuntimeEventKind
    request_id: str
    role: RuntimeRole
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.kind, RuntimeEventKind):
            raise TypeError("Runtime event kind must be a RuntimeEventKind")
        _validate_identifier(self.request_id, "Runtime event request identity")
        if not isinstance(self.role, RuntimeRole):
            raise TypeError("Runtime event role must be a RuntimeRole")
        if not isinstance(self.timestamp, datetime):
            raise TypeError("Runtime event timestamp must be a datetime")
        if (
            self.timestamp.tzinfo is None
            or self.timestamp.utcoffset() is None
            or self.timestamp.utcoffset() != timedelta(0)
        ):
            raise ValueError("Runtime event timestamp must be timezone-aware UTC")


@runtime_checkable
class RuntimePreparedEnvironment(Protocol):
    """Image facts consumed by runtimes without owning backend policy."""

    @property
    def executable_image_selector(self) -> str:
        ...

    @property
    def local_image_config_id(self) -> str:
        ...

    @property
    def oci_digest(self) -> str:
        ...

    @property
    def image_reference(self) -> str:
        ...


@dataclass(frozen=True)
class RuntimePreparedEnvironmentData:
    """Validated immutable image-authority snapshot consumed by runtimes."""

    executable_image_selector: str
    local_image_config_id: str
    oci_digest: str
    image_reference: str

    def __post_init__(self) -> None:
        if not is_canonical_oci_digest(self.local_image_config_id):
            raise ValueError(
                "Runtime local image config ID must be canonical sha256"
            )

        parsed = parse_immutable_oci_reference(self.image_reference)
        if self.oci_digest != parsed.digest:
            raise ValueError(
                "Runtime OCI digest does not match its image reference"
            )
        if self.executable_image_selector != self.image_reference:
            raise ValueError(
                "Runtime OCI selector does not match its image reference"
            )
        if self.local_image_config_id == parsed.digest:
            raise ValueError(
                "Runtime OCI digest cannot substitute for local config ID"
            )

    @classmethod
    def from_prepared(
        cls,
        value: object,
    ) -> RuntimePreparedEnvironmentData:
        if isinstance(value, cls):
            return value
        try:
            snapshot = cls(
                executable_image_selector=getattr(
                    value,
                    "executable_image_selector",
                ),
                local_image_config_id=getattr(value, "local_image_config_id"),
                oci_digest=getattr(value, "oci_digest"),
                image_reference=getattr(value, "image_reference"),
            )
        except AttributeError as error:
            raise TypeError("Runtime sandbox preparation is incomplete") from error
        return snapshot


@dataclass(frozen=True)
class RuntimeSandboxContext:
    """Bounded sandbox facts required to invoke an agent role."""

    workspace: Path
    prepared_environment: RuntimePreparedEnvironment
    cpu_limit: int
    memory_mb: int
    read_only: bool
    network_enabled: bool
    sandbox_handle: str
    task_id: str
    runtime_user: str

    def __post_init__(self) -> None:
        if not isinstance(self.workspace, Path) or not self.workspace.is_absolute():
            raise TypeError("Runtime sandbox workspace must be an absolute Path")
        snapshot = RuntimePreparedEnvironmentData.from_prepared(
            self.prepared_environment
        )
        object.__setattr__(self, "prepared_environment", snapshot)
        if (
            not isinstance(self.cpu_limit, int)
            or isinstance(self.cpu_limit, bool)
            or not isinstance(self.memory_mb, int)
            or isinstance(self.memory_mb, bool)
        ):
            raise TypeError("Runtime sandbox limits must be integers")
        if self.cpu_limit <= 0 or self.memory_mb <= 0:
            raise ValueError("Runtime sandbox limits must be positive")
        if not isinstance(self.read_only, bool):
            raise TypeError("Runtime sandbox read-only policy must be boolean")
        if not isinstance(self.network_enabled, bool):
            raise TypeError("Runtime sandbox network policy must be boolean")
        _validate_identifier(
            self.sandbox_handle,
            "Runtime sandbox handle",
        )
        _validate_identifier(self.task_id, "Runtime sandbox task identity")
        if re.fullmatch(
            r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)",
            self.runtime_user,
        ) is None:
            raise ValueError("Runtime sandbox user identity is invalid")


@dataclass(frozen=True)
class RuntimeRequest:
    role: RuntimeRole
    prompt: str = field(repr=False)
    runtime_config_id: str
    request_id: str
    timeout_seconds: int
    completion_marker: str
    sandbox: RuntimeSandboxContext | None = None
    on_event: Callable[[RuntimeEvent], None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.role, RuntimeRole):
            raise TypeError("Runtime role must be a RuntimeRole")
        if not isinstance(self.prompt, str):
            raise TypeError("Runtime prompt must be a string")
        if not self.prompt.strip():
            raise ValueError("Runtime prompt is empty")
        if "\x00" in self.prompt:
            raise ValueError("Runtime prompt contains a null byte")
        if len(self.prompt.encode()) > 262_144:
            raise ValueError("Runtime prompt exceeds 256 KiB")
        _validate_identifier(
            self.runtime_config_id,
            "Runtime configuration identity",
        )
        _validate_identifier(self.request_id, "Runtime request identity")
        if (
            not isinstance(self.timeout_seconds, int)
            or isinstance(self.timeout_seconds, bool)
        ):
            raise TypeError("Runtime timeout must be an integer")
        if self.timeout_seconds <= 0:
            raise ValueError("Runtime timeout must be positive")
        if (
            not isinstance(self.completion_marker, str)
            or not self.completion_marker.strip()
            or self.completion_marker != self.completion_marker.strip()
            or len(self.completion_marker) > 256
            or len(self.completion_marker.splitlines()) != 1
            or any(ord(character) < 32 for character in self.completion_marker)
            or "\x7f" in self.completion_marker
        ):
            raise ValueError("Runtime completion marker must be one non-empty line")
        if self.sandbox is not None and not isinstance(
            self.sandbox,
            RuntimeSandboxContext,
        ):
            raise TypeError("Runtime sandbox must be a RuntimeSandboxContext")
        if self.on_event is not None and not callable(self.on_event):
            raise TypeError("Runtime event sink must be callable")
        if self.role is RuntimeRole.PLANNER and self.sandbox is not None:
            raise ValueError("Planner runtime request must not carry a task sandbox")
        if self.role is not RuntimeRole.PLANNER and self.sandbox is None:
            raise ValueError("Worker and reviewer requests require a sandbox")


@dataclass(frozen=True)
class RuntimeResult:
    output: str

    def __post_init__(self) -> None:
        if not isinstance(self.output, str):
            raise TypeError("Runtime output must be a string")


class RuntimeError(Exception):
    """A runtime-specific failure normalized for control-plane callers."""

    def __init__(
        self,
        kind: RuntimeErrorKind,
        message: str,
        *,
        exit_status: int | None = None,
        output: str = "",
    ) -> None:
        if not isinstance(kind, RuntimeErrorKind):
            raise TypeError("Runtime error kind must be a RuntimeErrorKind")
        if not isinstance(message, str) or not message:
            raise ValueError("Runtime error message must be a non-empty string")
        if (
            exit_status is not None
            and (
                not isinstance(exit_status, int)
                or isinstance(exit_status, bool)
            )
        ):
            raise TypeError("Runtime exit status must be an integer or None")
        if not isinstance(output, str):
            raise TypeError("Runtime error output must be a string")
        super().__init__(message)
        self.kind = kind
        self.exit_status = exit_status
        self.output = output
        self.secondary_errors: tuple[str, ...] = ()


class RuntimeEventDispatcher:
    """Validate and deliver one request's ordered runtime facts."""

    def __init__(self, request: RuntimeRequest) -> None:
        if not isinstance(request, RuntimeRequest):
            raise TypeError("Runtime event dispatcher requires a RuntimeRequest")
        self._request = request
        self._started = False

    def emit(self, event: RuntimeEvent) -> None:
        if not isinstance(event, RuntimeEvent):
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Runtime event does not satisfy the runtime contract",
            )
        if (
            event.request_id != self._request.request_id
            or event.role is not self._request.role
        ):
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Runtime event request or role binding is mismatched",
            )
        if event.kind is RuntimeEventKind.STARTED:
            if self._started:
                raise RuntimeError(
                    RuntimeErrorKind.INVALID_RESULT,
                    "Runtime STARTED event is duplicated",
                )
            self._started = True
        elif event.kind is RuntimeEventKind.HEARTBEAT and not self._started:
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Runtime HEARTBEAT event precedes STARTED",
            )

        if self._request.on_event is not None:
            try:
                self._request.on_event(event)
            except Exception:
                raise RuntimeError(
                    RuntimeErrorKind.EXECUTION_FAILED,
                    "Control-plane runtime event sink failed",
                )


@runtime_checkable
class AgentRuntime(Protocol):
    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        """Execute one bounded role request or raise a normalized error."""
