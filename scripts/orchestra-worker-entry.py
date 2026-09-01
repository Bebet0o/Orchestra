#!/usr/bin/env python3
"""Orchestra entrypoint for a controller-created reusable sandbox."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

from tools import credential_files

credential_files.get_credential_file_mounts = lambda: []
credential_files.get_skills_directory_mount = lambda: []
credential_files.get_cache_directory_mounts = lambda: []

sandbox_handle = os.environ.get("ORCHESTRA_SANDBOX_HANDLE", "").strip()
task_id = os.environ.get("ORCHESTRA_SANDBOX_TASK_ID", "").strip()
request_id = os.environ.get("ORCHESTRA_SANDBOX_REQUEST_ID", "").strip()
profile_name = os.environ.get("ORCHESTRA_SANDBOX_PROFILE", "").strip()
workspace = os.environ.get("ORCHESTRA_SANDBOX_WORKSPACE", "").strip()
runtime_user = os.environ.get("ORCHESTRA_SANDBOX_USER", "").strip()
executable_image = os.environ.get(
    "ORCHESTRA_SANDBOX_EXECUTABLE_IMAGE", ""
).strip()
local_image_config_id = os.environ.get(
    "ORCHESTRA_SANDBOX_LOCAL_IMAGE_CONFIG_ID", ""
).strip()
read_only_value = os.environ.get(
    "ORCHESTRA_SANDBOX_READ_ONLY", ""
).strip().lower()
cpu_value = os.environ.get("ORCHESTRA_SANDBOX_CPU_LIMIT", "").strip()
memory_value = os.environ.get("ORCHESTRA_SANDBOX_MEMORY_MB", "").strip()
network_value = os.environ.get("TERMINAL_DOCKER_NETWORK", "").strip().lower()

if not re.fullmatch(r"[a-f0-9]{64}", sandbox_handle):
    raise RuntimeError("Orchestra sandbox handle is invalid")

if not re.fullmatch(r"task-[a-f0-9]{32}", task_id):
    raise RuntimeError("Orchestra sandbox task identity is invalid")

if not re.fullmatch(r"agent-runtime-[a-f0-9]{12}", request_id):
    raise RuntimeError("Orchestra sandbox request identity is invalid")

if not profile_name or not workspace or not executable_image:
    raise RuntimeError("Orchestra sandbox reuse identity is absent")
if re.fullmatch(r"(?:0|[1-9][0-9]*):(?:0|[1-9][0-9]*)", runtime_user) is None:
    raise RuntimeError("Orchestra sandbox user identity is invalid")
if not re.fullmatch(r"sha256:[a-f0-9]{64}", local_image_config_id):
    raise RuntimeError("Orchestra sandbox local image config ID is invalid")

if network_value not in {"true", "false"}:
    raise RuntimeError("Orchestra sandbox network policy is invalid")

if read_only_value not in {"true", "false"}:
    raise RuntimeError("Orchestra sandbox mount policy is invalid")

try:
    cpu_limit = int(cpu_value)
    memory_mb = int(memory_value)
except ValueError as error:
    raise RuntimeError("Orchestra sandbox resource policy is invalid") from error

if cpu_limit <= 0 or memory_mb <= 0:
    raise RuntimeError("Orchestra sandbox resource policy is invalid")

network_enabled = network_value == "true"
read_only = read_only_value == "true"

from tools.environments import docker as docker_backend

docker_backend._get_active_profile_name = lambda: profile_name
_original_docker_init = docker_backend.DockerEnvironment.__init__


def _validate_authorized_sandbox(
    container: dict[str, Any],
    *,
    expected_handle: str,
    expected_task_id: str,
    expected_request_id: str,
    expected_workspace: str,
    expected_executable_image: str,
    expected_local_image_config_id: str,
    expected_read_only: bool,
    expected_network_enabled: bool,
    expected_cpu_limit: int,
    expected_memory_mb: int,
    expected_user: str,
) -> str:
    """Fail closed unless the inspected sandbox still matches its grant."""
    config = container.get("Config") or {}
    host_config = container.get("HostConfig") or {}
    labels = config.get("Labels") or {}
    state = str((container.get("State") or {}).get("Status") or "")

    if container.get("Id") != expected_handle:
        raise RuntimeError("Authorized Orchestra sandbox identity changed")
    if labels.get("orchestra-sandbox") != "1":
        raise RuntimeError("Authorized Orchestra sandbox owner is mismatched")
    if labels.get("orchestra-task-id") != expected_task_id:
        raise RuntimeError("Authorized Orchestra sandbox task is mismatched")
    if labels.get("orchestra-runtime-request-id") != expected_request_id:
        raise RuntimeError("Authorized Orchestra sandbox request is mismatched")
    if state != "running":
        raise RuntimeError("Authorized Orchestra sandbox is not running")
    if config.get("Image") != expected_executable_image:
        raise RuntimeError(
            "Authorized Orchestra sandbox executable image is mismatched"
        )
    if container.get("Image") != expected_local_image_config_id:
        raise RuntimeError(
            "Authorized Orchestra sandbox local image config ID is mismatched"
        )

    workspace_mounts = [
        mount
        for mount in (container.get("Mounts") or [])
        if mount.get("Destination") == "/workspace"
    ]
    if len(workspace_mounts) != 1:
        raise RuntimeError("Authorized Orchestra workspace mount is invalid")
    mount = workspace_mounts[0]
    mount_rw = mount.get("RW")
    if (
        Path(str(mount.get("Source") or "")).resolve()
        != Path(expected_workspace).resolve()
        or not isinstance(mount_rw, bool)
        or mount_rw != (not expected_read_only)
    ):
        raise RuntimeError("Authorized Orchestra workspace policy is mismatched")
    for candidate in (container.get("Mounts") or []):
        destination = str(candidate.get("Destination") or "")
        if destination == "/workspace":
            continue
        if (
            candidate.get("Type") != "tmpfs"
            or destination not in {"/tmp", "/var/tmp", "/run"}
        ):
            raise RuntimeError("Authorized Orchestra mount set is mismatched")

    actual_network_mode = str(host_config.get("NetworkMode") or "")
    attached_networks = set(
        ((container.get("NetworkSettings") or {}).get("Networks") or {})
    )
    effective_networks = attached_networks - {"none"}
    if expected_network_enabled:
        if actual_network_mode == "none" or not effective_networks:
            raise RuntimeError("Authorized Orchestra network policy is mismatched")
    elif actual_network_mode != "none" or effective_networks:
        raise RuntimeError("Authorized Orchestra network policy is mismatched")

    if int(host_config.get("NanoCpus") or 0) != expected_cpu_limit * 1_000_000_000:
        raise RuntimeError("Authorized Orchestra CPU policy is mismatched")
    if int(host_config.get("Memory") or 0) != expected_memory_mb * 1024 * 1024:
        raise RuntimeError("Authorized Orchestra memory policy is mismatched")
    if int(host_config.get("PidsLimit") or 0) != 256:
        raise RuntimeError("Authorized Orchestra PID policy is mismatched")
    if bool(host_config.get("Privileged")):
        raise RuntimeError("Authorized Orchestra sandbox is privileged")

    security_options = {
        str(value) for value in (host_config.get("SecurityOpt") or [])
    }
    if not any(
        value.startswith("no-new-privileges")
        for value in security_options
    ):
        raise RuntimeError("Authorized Orchestra security policy is mismatched")
    cap_drop = {
        str(value).upper() for value in (host_config.get("CapDrop") or [])
    }
    if "ALL" not in cap_drop:
        raise RuntimeError("Authorized Orchestra capability policy is mismatched")
    if str(config.get("User") or "") != expected_user:
        raise RuntimeError("Authorized Orchestra user policy is mismatched")
    sensitive_fragments = (
        "OPENAI",
        "CODEX",
        "API_SERVER_KEY",
        "WEBUI_PASSWORD",
        "TELEGRAM_BOT_TOKEN",
    )
    if any(
        any(fragment in str(entry).upper() for fragment in sensitive_fragments)
        for entry in (config.get("Env") or [])
    ):
        raise RuntimeError("Authorized Orchestra environment is sensitive")

    return state


def _find_authorized_sandbox(
    self: Any,
    _task_label: str,
    _profile_label: str,
) -> tuple[str, str]:
    inspected = subprocess.run(
        [self._docker_exe, "inspect", sandbox_handle],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if inspected.returncode != 0:
        raise RuntimeError("Authorized Orchestra sandbox is unavailable")

    payload = json.loads(inspected.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise RuntimeError("Authorized Orchestra sandbox inspection is invalid")
    container = payload[0]
    if _task_label != task_id:
        raise RuntimeError("Orchestra terminal task identity is mismatched")
    if _profile_label != profile_name:
        raise RuntimeError("Orchestra terminal profile identity is mismatched")
    state = _validate_authorized_sandbox(
        container,
        expected_handle=sandbox_handle,
        expected_task_id=task_id,
        expected_request_id=request_id,
        expected_workspace=workspace,
        expected_executable_image=executable_image,
        expected_local_image_config_id=local_image_config_id,
        expected_read_only=read_only,
        expected_network_enabled=network_enabled,
        expected_cpu_limit=cpu_limit,
        expected_memory_mb=memory_mb,
        expected_user=runtime_user,
    )
    return sandbox_handle, state


docker_backend.DockerEnvironment._find_reusable_container = (
    _find_authorized_sandbox
)


def _strip_network_args(arguments: list[str] | None) -> list[str]:
    source = list(arguments or [])
    result: list[str] = []
    index = 0

    while index < len(source):
        token = source[index]

        if token == "--network":
            index += 2
            continue

        if token.startswith("--network="):
            index += 1
            continue

        result.append(token)
        index += 1

    return result


def _orchestra_docker_init(
    self: Any,
    *args: Any,
    **kwargs: Any,
) -> None:
    kwargs["network"] = network_enabled
    kwargs["disk"] = 0
    kwargs["persist_across_processes"] = True
    kwargs["extra_args"] = _strip_network_args(
        kwargs.get("extra_args")
    )
    _original_docker_init(self, *args, **kwargs)


docker_backend.DockerEnvironment.__init__ = _orchestra_docker_init

from tools import terminal_tool as terminal_runtime

_original_get_env_config = terminal_runtime._get_env_config


def _orchestra_get_env_config() -> dict[str, Any]:
    config = _original_get_env_config()

    if config.get("env_type") == "docker":
        config["docker_network"] = network_enabled
        config["container_disk"] = 0
        config["docker_persist_across_processes"] = True
        config["docker_orphan_reaper"] = False
        config["docker_extra_args"] = []

    return config


terminal_runtime._resolve_container_task_id = lambda _: task_id
terminal_runtime._get_env_config = _orchestra_get_env_config
terminal_runtime._DockerEnvironment = docker_backend.DockerEnvironment

effective = terminal_runtime._get_env_config()

if effective.get("env_type") != "docker":
    raise RuntimeError("Orchestra terminal backend is not Docker")

if effective.get("docker_network") is not network_enabled:
    raise RuntimeError("Orchestra sandbox network policy is not active")

print("ORCHESTRA_SANDBOX_AUTOMOUNTS_DISABLED", flush=True)
print(
    "ORCHESTRA_PRECREATED_SANDBOX_REUSE "
    f"request={request_id} profile={profile_name}",
    flush=True,
)

from hermes_cli.main import main

if __name__ == "__main__":
    main()
