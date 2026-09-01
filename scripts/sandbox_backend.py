"""Typed image preparation and container-authority checks for sandboxes.

OCI distribution identity and daemon-local image evidence deliberately remain
separate here.  A local Docker config object ID is never a pullable reference.
"""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, TypeAlias, runtime_checkable

from environment_resolution import DEFAULT_PLATFORM, ResolvedEnvironment
from oci_reference import parse_immutable_oci_reference


_LOCAL_IMAGE_CONFIG_ID = re.compile(r"sha256:[0-9a-f]{64}")
_FULL_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_DOCKER_SOCKET_DESTINATIONS = {
    "/var/run/docker.sock",
    "/run/docker.sock",
    "/run/orchestra-docker/docker.sock",
}
_PRIVATE_DOCKER_SOCKET = "unix:///run/orchestra-docker/docker.sock"
_HOST_PRIVATE_DOCKER_SOCKET = (
    "unix:///opt/orchestra/runtime/sandbox-engine-socket/docker.sock"
)
_DOCKER_PULL_TIMEOUT_SECONDS = 900
_DOCKER_INSPECT_TIMEOUT_SECONDS = 30
_DOCKER_OUTPUT_LIMIT_BYTES = 262_144
_PRIVATE_DOCKER_ENVIRONMENT = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "DOCKER_HOST": _PRIVATE_DOCKER_SOCKET,
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}


class SandboxPreparationError(RuntimeError):
    """Image preparation or sandbox authority did not validate."""


def _run_bounded_command(
    arguments: list[str],
    *,
    timeout: int,
    output_limit: int,
    env: Mapping[str, str],
    **_ignored: object,
) -> subprocess.CompletedProcess[str]:
    """Run one command while bounding time and combined captured output."""
    process = subprocess.Popen(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env),
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        process.wait()
        raise subprocess.SubprocessError("bounded Docker pipes unavailable")
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    chunks: dict[str, list[bytes]] = {"stdout": [], "stderr": []}
    total = 0
    deadline = time.monotonic() + timeout
    try:
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                process.kill()
                raise subprocess.TimeoutExpired(arguments, timeout)
            events = selector.select(remaining)
            if not events:
                process.kill()
                raise subprocess.TimeoutExpired(arguments, timeout)
            for key, _mask in events:
                chunk = os.read(key.fileobj.fileno(), 65_536)
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                total += len(chunk)
                if total > output_limit:
                    process.kill()
                    raise subprocess.SubprocessError(
                        "bounded Docker output limit exceeded"
                    )
                chunks[key.data].append(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            process.kill()
            raise subprocess.TimeoutExpired(arguments, timeout)
        returncode = process.wait(timeout=remaining)
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
            process.wait()
        for stream in (process.stdout, process.stderr):
            if not stream.closed:
                stream.close()
    return subprocess.CompletedProcess(
        arguments,
        returncode,
        b"".join(chunks["stdout"]).decode("utf-8", errors="replace"),
        b"".join(chunks["stderr"]).decode("utf-8", errors="replace"),
    )


class NestedDockerImageClient:
    """Exact OCI image operations isolated behind the dedicated DIND CLI."""

    def __init__(
        self,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = (
            _run_bounded_command
        ),
        *,
        docker_host: str = _PRIVATE_DOCKER_SOCKET,
    ) -> None:
        if not callable(command_runner):
            raise TypeError("Nested Docker command runner must be callable")
        if docker_host not in {
            _PRIVATE_DOCKER_SOCKET,
            _HOST_PRIVATE_DOCKER_SOCKET,
        }:
            raise ValueError("Private Docker socket authority is invalid")
        self._command_runner = command_runner
        self._environment = {
            **_PRIVATE_DOCKER_ENVIRONMENT,
            "DOCKER_HOST": docker_host,
        }

    def _run(
        self,
        *arguments: str,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]:
        try:
            result = self._command_runner(
                ["docker", *arguments],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
                timeout=timeout,
                output_limit=_DOCKER_OUTPUT_LIMIT_BYTES,
                env=self._environment,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SandboxPreparationError(
                "Nested Docker image operation is unavailable"
            ) from error
        if (
            not isinstance(result, subprocess.CompletedProcess)
            or type(result.returncode) is not int
            or not isinstance(result.stdout, str)
            or not isinstance(result.stderr, str)
        ):
            raise SandboxPreparationError(
                "Nested Docker command result is invalid"
            )
        if result.returncode != 0:
            raise SandboxPreparationError(
                "Nested Docker image operation failed"
            )
        return result

    def pull_exact(self, image_reference: str, platform: str) -> None:
        try:
            parsed = parse_immutable_oci_reference(image_reference)
        except ValueError as error:
            raise SandboxPreparationError(
                "Nested Docker image reference is invalid"
            ) from error
        if platform != DEFAULT_PLATFORM:
            raise SandboxPreparationError(
                f"Nested Docker platform must equal {DEFAULT_PLATFORM}"
            )
        self._run(
            "image",
            "pull",
            "--platform",
            platform,
            parsed.image_reference,
            timeout=_DOCKER_PULL_TIMEOUT_SECONDS,
        )

    def inspect_exact(self, image_reference: str) -> Mapping[str, Any]:
        try:
            parsed = parse_immutable_oci_reference(image_reference)
        except ValueError as error:
            raise SandboxPreparationError(
                "Nested Docker image reference is invalid"
            ) from error
        result = self._run(
            "image",
            "inspect",
            parsed.image_reference,
            timeout=_DOCKER_INSPECT_TIMEOUT_SECONDS,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise SandboxPreparationError(
                "Nested Docker image inspection is malformed"
            ) from error
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], Mapping)
        ):
            raise SandboxPreparationError(
                "Nested Docker image inspection is invalid"
            )
        return payload[0]


def _validate_local_image_config_id(value: object) -> None:
    if (
        not isinstance(value, str)
        or _LOCAL_IMAGE_CONFIG_ID.fullmatch(value) is None
    ):
        raise ValueError(
            "Sandbox local_image_config_id must be sha256:<64 lowercase hex>"
        )


@dataclass(frozen=True)
class PreparedEnvironment:
    """An OCI environment paired with evidence from the current daemon."""

    resolved_environment: ResolvedEnvironment
    local_image_config_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.resolved_environment, ResolvedEnvironment):
            raise TypeError(
                "Prepared environment requires a ResolvedEnvironment"
            )
        _validate_local_image_config_id(self.local_image_config_id)
        if self.local_image_config_id == self.resolved_environment.oci_digest:
            raise ValueError(
                "OCI digest cannot be substituted for local image config ID"
            )

    @property
    def executable_image_selector(self) -> str:
        """The immutable OCI reference Docker must receive at create time."""
        return self.resolved_environment.image_reference

    @property
    def oci_digest(self) -> str:
        return self.resolved_environment.oci_digest

    @property
    def image_reference(self) -> str:
        return self.resolved_environment.image_reference


SandboxPreparation: TypeAlias = PreparedEnvironment


@runtime_checkable
class SandboxBackend(Protocol):
    """Runtime-neutral materialization of a resolved OCI environment."""

    def materialize(
        self,
        environment: ResolvedEnvironment,
    ) -> PreparedEnvironment:
        ...

    def prepare(
        self,
        environment: ResolvedEnvironment,
    ) -> PreparedEnvironment:
        ...


class NestedDaemonSandboxBackend:
    """Materialize or verify one OCI image in the dedicated nested daemon."""

    def __init__(
        self,
        image_inspector: Callable[[str], Mapping[str, Any]],
        image_puller: Callable[[str, str], None] | None = None,
    ) -> None:
        if not callable(image_inspector):
            raise TypeError("Sandbox image_inspector must be callable")
        self._image_inspector = image_inspector
        if image_puller is not None and not callable(image_puller):
            raise TypeError("Sandbox image_puller must be callable")
        self._image_puller = image_puller

    @classmethod
    def for_dedicated_nested_daemon(
        cls,
        client: NestedDockerImageClient | None = None,
    ) -> NestedDaemonSandboxBackend:
        nested_client = NestedDockerImageClient() if client is None else client
        if not isinstance(nested_client, NestedDockerImageClient):
            raise TypeError("Dedicated nested Docker client is invalid")
        return cls(nested_client.inspect_exact, nested_client.pull_exact)

    def materialize(
        self,
        environment: ResolvedEnvironment,
    ) -> PreparedEnvironment:
        """Pull and re-inspect one exact reference in the dedicated daemon."""
        if not isinstance(environment, ResolvedEnvironment):
            raise TypeError("Sandbox materializer requires a ResolvedEnvironment")
        if self._image_puller is None:
            raise SandboxPreparationError(
                "Sandbox backend has no OCI materialization capability"
            )
        try:
            self._image_puller(
                environment.image_reference,
                environment.platform,
            )
        except SandboxPreparationError:
            raise
        except Exception as error:
            raise SandboxPreparationError(
                "Resolved environment materialization failed"
            ) from error
        return self.prepare(environment)

    def prepare(
        self,
        environment: ResolvedEnvironment,
    ) -> PreparedEnvironment:
        if not isinstance(environment, ResolvedEnvironment):
            raise TypeError("Sandbox backend requires a ResolvedEnvironment")
        try:
            inspected = self._image_inspector(environment.image_reference)
        except Exception as error:
            raise SandboxPreparationError(
                "Resolved environment is not materialized in the sandbox daemon"
            ) from error
        if not isinstance(inspected, Mapping):
            raise SandboxPreparationError("Sandbox image inspection is invalid")
        repo_digests = inspected.get("RepoDigests")
        if (
            not isinstance(repo_digests, list)
            or environment.image_reference not in repo_digests
        ):
            raise SandboxPreparationError(
                "Sandbox daemon image lacks the authoritative OCI reference"
            )
        try:
            return PreparedEnvironment(
                resolved_environment=environment,
                local_image_config_id=inspected.get("Id"),
            )
        except (TypeError, ValueError) as error:
            raise SandboxPreparationError(str(error)) from error


@dataclass(frozen=True)
class SandboxContainerExpectation:
    """Durable facts required to authorize use or deletion of one container."""

    container_id: str
    preparation: SandboxPreparation
    task_id: str
    runtime_request_id: str
    workspace: Path
    read_only: bool
    expected_user: str

    def __post_init__(self) -> None:
        if _FULL_CONTAINER_ID.fullmatch(self.container_id) is None:
            raise ValueError("Sandbox authority requires a full container ID")
        if not isinstance(self.preparation, PreparedEnvironment):
            raise TypeError("Sandbox authority requires typed preparation")
        if not isinstance(self.workspace, Path) or not self.workspace.is_absolute():
            raise TypeError("Sandbox authority workspace must be an absolute Path")
        if not isinstance(self.read_only, bool):
            raise TypeError("Sandbox authority read_only must be boolean")
        if not isinstance(self.task_id, str) or not self.task_id:
            raise ValueError("Sandbox authority task identity is required")
        if not isinstance(self.runtime_request_id, str) or not self.runtime_request_id:
            raise ValueError("Sandbox authority runtime request is required")
        if re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", self.expected_user) is None:
            raise ValueError("Sandbox authority expected user is invalid")


def verify_prepared_container(
    container: Mapping[str, Any],
    expectation: SandboxContainerExpectation,
) -> str:
    """Return the full ID only when all image and ownership facts agree."""
    if not isinstance(container, Mapping):
        raise SandboxPreparationError("Sandbox inspection is invalid")
    if container.get("Id") != expectation.container_id:
        raise SandboxPreparationError("Sandbox full container ID mismatched")

    config = container.get("Config")
    if not isinstance(config, Mapping):
        raise SandboxPreparationError("Sandbox configuration is invalid")
    labels = config.get("Labels")
    if not isinstance(labels, Mapping):
        raise SandboxPreparationError("Sandbox ownership labels are invalid")
    if labels.get("orchestra-sandbox") != "1":
        raise SandboxPreparationError("Sandbox ownership label mismatched")
    if labels.get("orchestra-task-id") != expectation.task_id:
        raise SandboxPreparationError("Sandbox task binding mismatched")
    if (
        labels.get("orchestra-runtime-request-id")
        != expectation.runtime_request_id
    ):
        raise SandboxPreparationError("Sandbox runtime request binding mismatched")
    if config.get("Image") != expectation.preparation.executable_image_selector:
        raise SandboxPreparationError("Sandbox executable image selector mismatched")
    if container.get("Image") != expectation.preparation.local_image_config_id:
        raise SandboxPreparationError("Sandbox local image config ID mismatched")
    if str(config.get("User") or "") != expectation.expected_user:
        raise SandboxPreparationError("Sandbox user mismatched")

    mounts = container.get("Mounts")
    if not isinstance(mounts, list) or not all(
        isinstance(mount, Mapping) for mount in mounts
    ):
        raise SandboxPreparationError("Sandbox mount inspection is invalid")
    workspace_mounts = [
        mount for mount in mounts if mount.get("Destination") == "/workspace"
    ]
    if len(workspace_mounts) != 1:
        raise SandboxPreparationError("Sandbox workspace mount is ambiguous")
    workspace_mount = workspace_mounts[0]
    mount_rw = workspace_mount.get("RW")
    if (
        Path(str(workspace_mount.get("Source") or "")).resolve()
        != expectation.workspace.resolve()
        or not isinstance(mount_rw, bool)
        or mount_rw != (not expectation.read_only)
    ):
        raise SandboxPreparationError("Sandbox workspace binding mismatched")

    for mount in mounts:
        source = str(mount.get("Source") or "")
        destination = str(mount.get("Destination") or "")
        if (
            destination in _FORBIDDEN_DOCKER_SOCKET_DESTINATIONS
            or source == "/var/run/docker.sock"
            or source.endswith("/docker.sock")
        ):
            raise SandboxPreparationError("Sandbox Docker socket mount is forbidden")
    return expectation.container_id
