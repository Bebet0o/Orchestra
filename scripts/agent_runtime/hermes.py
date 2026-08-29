"""Transition adapter for the current Hermes Agent execution mechanism."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from .contract import (
    AgentRuntime,
    RuntimeEvent,
    RuntimeEventDispatcher,
    RuntimeEventKind,
    RuntimeError,
    RuntimeErrorKind,
    RuntimeRequest,
    RuntimeResult,
    RuntimeRole,
)


class HermesRuntime(AgentRuntime):
    """Map runtime-neutral requests to the installed Hermes CLI container."""

    def __init__(
        self,
        root: Path,
        *,
        poll_interval_seconds: float = 1.0,
        required_role: RuntimeRole | None = None,
        event_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = root.resolve()
        self.repo = self.root / "repo"
        self.compose_file = self.repo / "compose/agent.yaml"
        self.lock_file = self.repo / "compose/images.lock.env"
        self.hermes_home = self.root / "state/hermes-home"
        self.profile_root = self.hermes_home / "profiles"
        self.planner_entry = self.repo / "scripts/hermesops-planner-entry.py"
        self.worker_entry = self.repo / "scripts/hermes-worker-entry.py"
        self.poll_interval_seconds = poll_interval_seconds
        if event_clock is not None and not callable(event_clock):
            raise TypeError("Runtime event clock must be callable")
        self.event_clock = event_clock or (
            lambda: datetime.now(timezone.utc)
        )
        if required_role is not None:
            self.validate_role(required_role)

    def validate_role(self, role: RuntimeRole) -> None:
        if not isinstance(role, RuntimeRole):
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Runtime role does not satisfy the runtime contract",
            )
        entry = self.planner_entry if role is RuntimeRole.PLANNER else self.worker_entry
        if not entry.is_file():
            raise RuntimeError(
                RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                f"Runtime entry wrapper is absent: {entry}",
            )

    @staticmethod
    def _execution_name(request: RuntimeRequest) -> str:
        return request.request_id

    @staticmethod
    def _profile_name(request: RuntimeRequest) -> str:
        if request.role is RuntimeRole.PLANNER:
            return request.runtime_config_id
        return request.request_id

    def build_command(self, request: RuntimeRequest) -> list[str]:
        if request.role is RuntimeRole.PLANNER:
            return self._planner_command(request)
        return self._sandboxed_command(request)

    def _planner_command(self, request: RuntimeRequest) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.lock_file),
            "-f",
            str(self.compose_file),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--label",
            "hermesops-runtime-container=1",
            "--label",
            f"hermesops-runtime-request-id={request.request_id}",
            "--name",
            self._execution_name(request),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            "/tmp",
            "--env",
            "HOME=/home/hermes",
            "--env",
            "HERMES_ENABLE_PROJECT_PLUGINS=false",
            "--env",
            "HERMES_MAX_ITERATIONS=30",
            "--volume",
            f"{self.planner_entry}:/opt/hermesops/hermesops-planner-entry.py:ro",
            "--entrypoint",
            "python3",
            "hermes-agent",
            "/opt/hermesops/hermesops-planner-entry.py",
            "-p",
            self._profile_name(request),
            "-z",
            request.prompt,
        ]

    def _sandboxed_command(self, request: RuntimeRequest) -> list[str]:
        sandbox = request.sandbox
        if sandbox is None:
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Sandboxed runtime request has no sandbox context",
            )

        mount_mode = "ro" if sandbox.read_only else "rw"
        maximum_iterations = (
            50 if request.role is RuntimeRole.REVIEWER else 40
        )
        docker_environment = (
            {"GIT_OPTIONAL_LOCKS": "0"} if sandbox.read_only else {}
        )
        environment = {
            "HOME": "/home/hermes",
            "TERMINAL_ENV": "docker",
            "TERMINAL_CWD": "/workspace",
            # Upstream Hermes still needs one Docker create selector.  Keep
            # that adapter-specific translation separate from local evidence.
            "TERMINAL_DOCKER_IMAGE": (
                sandbox.prepared_environment.executable_image_selector
            ),
            "TERMINAL_DOCKER_VOLUMES": json.dumps(
                [f"{sandbox.workspace}:/workspace:{mount_mode}"],
                separators=(",", ":"),
            ),
            "TERMINAL_DOCKER_MOUNT_CWD_TO_WORKSPACE": "false",
            "TERMINAL_DOCKER_RUN_AS_HOST_USER": "true",
            "TERMINAL_DOCKER_NETWORK": str(
                sandbox.network_enabled
            ).lower(),
            "TERMINAL_DOCKER_FORWARD_ENV": "[]",
            "TERMINAL_DOCKER_ENV": json.dumps(
                docker_environment,
                separators=(",", ":"),
            ),
            "TERMINAL_DOCKER_EXTRA_ARGS": "[]",
            "TERMINAL_CONTAINER_CPU": str(sandbox.cpu_limit),
            "TERMINAL_CONTAINER_MEMORY": str(sandbox.memory_mb),
            "TERMINAL_CONTAINER_DISK": "0",
            "TERMINAL_CONTAINER_PERSISTENT": "false",
            "TERMINAL_DOCKER_PERSIST_ACROSS_PROCESSES": "true",
            "TERMINAL_DOCKER_ORPHAN_REAPER": "false",
            "TERMINAL_PERSISTENT_SHELL": "false",
            "TERMINAL_LIFETIME_SECONDS": "900",
            "HERMES_ENABLE_PROJECT_PLUGINS": "false",
            "HERMES_MAX_ITERATIONS": str(maximum_iterations),
            "HERMESOPS_SANDBOX_HANDLE": sandbox.sandbox_handle,
            "HERMESOPS_SANDBOX_TASK_ID": sandbox.task_id,
            "HERMESOPS_SANDBOX_REQUEST_ID": request.request_id,
            "HERMESOPS_SANDBOX_WORKSPACE": str(sandbox.workspace),
            "HERMESOPS_SANDBOX_EXECUTABLE_IMAGE": (
                sandbox.prepared_environment.executable_image_selector
            ),
            "HERMESOPS_SANDBOX_LOCAL_IMAGE_CONFIG_ID": (
                sandbox.prepared_environment.local_image_config_id
            ),
            "HERMESOPS_SANDBOX_READ_ONLY": str(sandbox.read_only).lower(),
            "HERMESOPS_SANDBOX_CPU_LIMIT": str(sandbox.cpu_limit),
            "HERMESOPS_SANDBOX_MEMORY_MB": str(sandbox.memory_mb),
            "HERMESOPS_SANDBOX_PROFILE": self._profile_name(request),
        }
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self.lock_file),
            "-f",
            str(self.compose_file),
            "run",
            "--rm",
            "--no-deps",
            "-T",
            "--label",
            "hermesops-runtime-container=1",
            "--label",
            f"hermesops-runtime-request-id={request.request_id}",
            "--name",
            self._execution_name(request),
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "--workdir",
            str(sandbox.workspace),
        ]
        for key, value in environment.items():
            command.extend(["--env", f"{key}={value}"])
        command.extend(
            [
                "--volume",
                f"{self.worker_entry}:/opt/hermesops/hermes-worker-entry.py:ro",
                "--entrypoint",
                "python3",
                "hermes-agent",
                "/opt/hermesops/hermes-worker-entry.py",
                "-p",
                self._profile_name(request),
                "-z",
                request.prompt,
            ]
        )
        return command

    def _prepare_profile(self, request: RuntimeRequest) -> Path | None:
        sandbox = request.sandbox
        if sandbox is None:
            return None
        source = self.profile_root / request.runtime_config_id
        target = self.profile_root / self._profile_name(request)
        if not source.is_dir():
            raise RuntimeError(
                RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                f"Runtime source profile is absent: {source}",
            )
        if target.exists():
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                f"Ephemeral runtime profile already exists: {target}",
            )

        target.mkdir(mode=0o750)
        try:
            config = yaml.safe_load(
                (source / "config.yaml").read_text(encoding="utf-8")
            ) or {}
            if not isinstance(config, dict):
                raise RuntimeError(
                    RuntimeErrorKind.INVALID_RESULT,
                    "Runtime profile configuration must be a mapping",
                )
            config.pop("toolsets", None)
            config["platform_toolsets"] = {"cli": ["terminal"]}

            mount_mode = "ro" if sandbox.read_only else "rw"
            docker_environment = (
                {"GIT_OPTIONAL_LOCKS": "0"} if sandbox.read_only else {}
            )
            terminal = config.setdefault("terminal", {})
            terminal.update(
                {
                    "backend": "docker",
                    "cwd": "/workspace",
                    "docker_image": (
                        sandbox.prepared_environment.executable_image_selector
                    ),
                    "docker_volumes": [
                        f"{sandbox.workspace}:/workspace:{mount_mode}"
                    ],
                    "docker_mount_cwd_to_workspace": False,
                    "docker_run_as_host_user": True,
                    "docker_forward_env": [],
                    "docker_env": docker_environment,
                    "docker_extra_args": [],
                    "docker_network": sandbox.network_enabled,
                    "container_cpu": sandbox.cpu_limit,
                    "container_memory": sandbox.memory_mb,
                    "container_disk": 0,
                    "container_persistent": False,
                    "docker_persist_across_processes": True,
                    "docker_orphan_reaper": False,
                    "persistent_shell": False,
                    "lifetime_seconds": 900,
                }
            )

            maximum_turns = 50 if request.role is RuntimeRole.REVIEWER else 40
            agent = config.setdefault("agent", {})
            agent["max_turns"] = min(
                int(agent.get("max_turns", maximum_turns)),
                maximum_turns,
            )
            config_path = target / "config.yaml"
            config_path.write_text(
                yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            config_path.chmod(0o600)

            shutil.copy2(source / "SOUL.md", target / "SOUL.md")
            (target / "SOUL.md").chmod(0o640)
            source_skills = source / "skills"
            if source_skills.is_dir():
                shutil.copytree(source_skills, target / "skills")
            else:
                (target / "skills").mkdir(mode=0o750)
            (target / ".no-bundled-skills").touch(mode=0o640)

            metadata: dict[str, Any] = {}
            source_metadata = source / "profile.yaml"
            if source_metadata.is_file():
                metadata = yaml.safe_load(
                    source_metadata.read_text(encoding="utf-8")
                ) or {}
            if not isinstance(metadata, dict):
                raise RuntimeError(
                    RuntimeErrorKind.INVALID_RESULT,
                    "Runtime profile metadata must be a mapping",
                )
            metadata["name"] = self._profile_name(request)
            qualifier = "read-only reviewer" if sandbox.read_only else "worker"
            metadata["description"] = (
                f"Ephemeral HermesOps {qualifier} for {request.request_id}"
            )
            metadata["description_auto"] = False
            metadata_path = target / "profile.yaml"
            metadata_path.write_text(
                yaml.safe_dump(metadata, sort_keys=False, allow_unicode=True),
                encoding="utf-8",
            )
            metadata_path.chmod(0o600)

            auth_path = target / "auth.json"
            auth_path.symlink_to("../../auth.json")
            if (
                auth_path.resolve(strict=True)
                != (self.hermes_home / "auth.json").resolve(strict=True)
            ):
                raise RuntimeError(
                    RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                    "Invalid runtime authentication link",
                )
        except RuntimeError:
            shutil.rmtree(target, ignore_errors=True)
            raise
        except (ValueError, yaml.YAMLError) as error:
            shutil.rmtree(target, ignore_errors=True)
            raise RuntimeError(
                RuntimeErrorKind.INVALID_RESULT,
                "Runtime profile configuration is invalid",
            ) from error
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        return target

    @staticmethod
    def _read_output(stream: Any) -> str:
        stream.flush()
        size = os.fstat(stream.fileno()).st_size
        return os.pread(stream.fileno(), size, 0).decode(
            "utf-8",
            errors="replace",
        )

    @staticmethod
    def _marker_found(output: str, marker: str) -> bool:
        return any(line.strip() == marker for line in output.splitlines())

    @staticmethod
    def _terminate(process: subprocess.Popen[str]) -> None:
        errors: list[Exception] = []
        try:
            if process.poll() is not None:
                return
        except Exception as error:
            errors.append(error)
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(error)
        try:
            process.wait(timeout=10)
            if errors:
                raise errors[0]
            return
        except subprocess.TimeoutExpired:
            pass
        except Exception as error:
            errors.append(error)
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(error)
        try:
            process.wait(timeout=10)
        except Exception as error:
            errors.append(error)
        if errors:
            raise errors[0]

    @staticmethod
    def _inspect_outer_container(reference: str) -> dict[str, Any] | None:
        inspected = subprocess.run(
            ["docker", "container", "inspect", reference],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if inspected.returncode != 0:
            if "No such" in inspected.stderr:
                return None
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container ownership inspection failed",
            )
        try:
            payload = json.loads(inspected.stdout)
            if not isinstance(payload, list) or len(payload) != 1:
                raise TypeError("inspection must contain one container")
            container = payload[0]
            if not isinstance(container, dict):
                raise TypeError("inspection entry must be a mapping")
        except (IndexError, TypeError, json.JSONDecodeError) as error:
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container ownership inspection is invalid",
            ) from error
        return container

    @staticmethod
    def _owned_outer_container_id(
        container: dict[str, Any],
        *,
        execution_name: str,
        expected_container_id: str | None = None,
    ) -> str:
        config = container.get("Config") or {}
        if not isinstance(config, dict):
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container ownership is mismatched",
            )
        labels = config.get("Labels") or {}
        if not isinstance(labels, dict):
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container ownership is mismatched",
            )
        container_id = str(container.get("Id") or "")
        if (
            str(container.get("Name") or "").removeprefix("/")
            != execution_name
            or len(container_id) != 64
            or any(
                character not in "0123456789abcdef"
                for character in container_id
            )
            or labels.get("hermesops-runtime-container") != "1"
            or labels.get("hermesops-runtime-request-id")
            != execution_name
        ):
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container ownership is mismatched",
            )
        if (
            expected_container_id is not None
            and container_id != expected_container_id
        ):
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container immutable identity is mismatched",
            )
        return container_id

    def _capture_outer_container_id(
        self,
        execution_name: str,
        process: subprocess.Popen[str],
    ) -> str | None:
        """Capture the owned immutable ID while the launched process is live."""
        for _ in range(40):
            if process.poll() is not None:
                return None
            container = self._inspect_outer_container(execution_name)
            if container is not None:
                return self._owned_outer_container_id(
                    container,
                    execution_name=execution_name,
                )
            time.sleep(0.25)
        raise RuntimeError(
            RuntimeErrorKind.EXECUTION_FAILED,
            "Runtime container identity was not observable after launch",
        )

    def _stop_outer_container(
        self,
        container_id: str,
        execution_name: str,
    ) -> None:
        container = self._inspect_outer_container(container_id)
        if container is None:
            return
        verified_id = self._owned_outer_container_id(
            container,
            execution_name=execution_name,
            expected_container_id=container_id,
        )
        result = subprocess.run(
            ["docker", "stop", "--time", "10", verified_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container stop failed",
            )

    def _remove_outer_container(
        self,
        container_id: str,
        execution_name: str,
    ) -> None:
        container = self._inspect_outer_container(container_id)
        if container is None:
            return
        verified_id = self._owned_outer_container_id(
            container,
            execution_name=execution_name,
            expected_container_id=container_id,
        )
        result = subprocess.run(
            ["docker", "rm", "-f", verified_id],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if (
            result.returncode != 0
            and "No such container" not in result.stderr
        ):
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Runtime container cleanup failed",
            )

    @staticmethod
    def _remove_profile(profile_directory: Path) -> None:
        try:
            shutil.rmtree(profile_directory)
        except FileNotFoundError:
            pass

    def _cleanup(
        self,
        *,
        process: subprocess.Popen[str] | None,
        execution_name: str | None,
        profile_directory: Path | None,
        execution_container_id: str | None = None,
    ) -> list[Exception]:
        errors: list[Exception] = []
        actions: list[Callable[[], None]] = []
        if process is not None:
            actions.append(lambda: self._terminate(process))
        if execution_name is not None and execution_container_id is not None:
            actions.append(
                lambda: self._remove_outer_container(
                    execution_container_id,
                    execution_name,
                )
            )
        if profile_directory is not None:
            actions.append(lambda: self._remove_profile(profile_directory))
        for action in actions:
            try:
                action()
            except Exception as error:
                errors.append(error)
        return errors

    @staticmethod
    def _normalize_error(
        error: Exception,
        request: object,
        output: str,
    ) -> RuntimeError:
        if isinstance(error, RuntimeError):
            if output and not error.output:
                error.output = output
            return error
        role = (
            request.role.value
            if isinstance(request, RuntimeRequest)
            else "agent"
        )
        if isinstance(error, subprocess.TimeoutExpired):
            return RuntimeError(
                RuntimeErrorKind.TIMEOUT,
                f"{role} runtime did not terminate cleanly",
                output=output,
            )
        if isinstance(error, OSError):
            return RuntimeError(
                RuntimeErrorKind.RUNTIME_UNAVAILABLE,
                "Agent runtime dependency is unavailable: "
                f"{error.filename or type(error).__name__}",
                output=output,
            )
        return RuntimeError(
            RuntimeErrorKind.EXECUTION_FAILED,
            f"{role} runtime failed internally: {type(error).__name__}",
            output=output,
        )

    def execute(self, request: RuntimeRequest) -> RuntimeResult:
        profile_directory: Path | None = None
        process: subprocess.Popen[str] | None = None
        started = 0.0
        execution_name: str | None = None
        execution_container_id: str | None = None
        output = ""
        result: RuntimeResult | None = None
        primary_error: RuntimeError | None = None
        execution_cleanup_errors: list[Exception] = []

        try:
            if not isinstance(request, RuntimeRequest):
                raise TypeError("Runtime request does not satisfy the contract")
            event_dispatcher = RuntimeEventDispatcher(request)
            execution_name = self._execution_name(request)
            self.validate_role(request.role)
            if request.sandbox is not None:
                candidate_profile = self.profile_root / self._profile_name(request)
                if not candidate_profile.exists():
                    profile_directory = candidate_profile
            prepared_profile = self._prepare_profile(request)
            if prepared_profile is not None:
                profile_directory = prepared_profile
            command = self.build_command(request)
            started = time.monotonic()
            with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as output_stream:
                try:
                    process = subprocess.Popen(
                        command,
                        stdout=output_stream,
                        stderr=subprocess.STDOUT,
                        text=True,
                        start_new_session=True,
                    )
                    execution_container_id = self._capture_outer_container_id(
                        execution_name,
                        process,
                    )
                    event_dispatcher.emit(
                        RuntimeEvent(
                            kind=RuntimeEventKind.STARTED,
                            request_id=request.request_id,
                            role=request.role,
                            timestamp=self.event_clock(),
                        )
                    )

                    marker_found = False
                    last_heartbeat = 0.0
                    while True:
                        elapsed = time.monotonic() - started
                        if elapsed > request.timeout_seconds:
                            raise RuntimeError(
                                RuntimeErrorKind.TIMEOUT,
                                f"{request.role.value} runtime exceeded "
                                f"timeout {request.timeout_seconds}s",
                            )
                        if elapsed - last_heartbeat >= 5:
                            event_dispatcher.emit(
                                RuntimeEvent(
                                    kind=RuntimeEventKind.HEARTBEAT,
                                    request_id=request.request_id,
                                    role=request.role,
                                    timestamp=self.event_clock(),
                                )
                            )
                            last_heartbeat = elapsed

                        output = self._read_output(output_stream)
                        marker_found = self._marker_found(
                            output,
                            request.completion_marker,
                        )
                        if process.poll() is not None:
                            break
                        if (
                            request.role is not RuntimeRole.PLANNER
                            and marker_found
                        ):
                            break
                        time.sleep(self.poll_interval_seconds)

                    if marker_found and process.poll() is None:
                        try:
                            process.wait(timeout=20)
                        except subprocess.TimeoutExpired:
                            if execution_container_id is not None:
                                self._stop_outer_container(
                                    execution_container_id,
                                    execution_name,
                                )
                    if process.poll() is None:
                        process.wait(timeout=30)
                except Exception:
                    if process is not None:
                        try:
                            self._terminate(process)
                        except Exception as termination_error:
                            execution_cleanup_errors.append(termination_error)
                        finally:
                            # Termination was already attempted while the
                            # transcript is still readable.  Do not repeat it
                            # during the independent outer/profile cleanup.
                            process = None
                    try:
                        output = self._read_output(output_stream)
                    except Exception as output_error:
                        execution_cleanup_errors.append(output_error)
                    raise
                else:
                    output = self._read_output(output_stream)

            marker_found = self._marker_found(output, request.completion_marker)
            exit_status = int(process.returncode or 0)
            if exit_status != 0:
                raise RuntimeError(
                    RuntimeErrorKind.EXECUTION_FAILED,
                    f"{request.role.value} runtime exited with code {exit_status}",
                    exit_status=exit_status,
                    output=output,
                )
            if not marker_found:
                raise RuntimeError(
                    RuntimeErrorKind.INVALID_RESULT,
                    f"{request.role.value} runtime completion marker is absent",
                    exit_status=exit_status,
                    output=output,
                )

            result = RuntimeResult(output=output)
        except Exception as error:
            primary_error = self._normalize_error(error, request, output)

        cleanup_errors = self._cleanup(
            process=process,
            execution_name=execution_name,
            execution_container_id=execution_container_id,
            profile_directory=profile_directory,
        )
        cleanup_errors = execution_cleanup_errors + cleanup_errors
        if primary_error is not None:
            primary_error.secondary_errors = tuple(
                type(error).__name__ for error in cleanup_errors
            )
            raise primary_error.with_traceback(primary_error.__traceback__)
        if cleanup_errors:
            names = ", ".join(type(error).__name__ for error in cleanup_errors)
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                f"Agent runtime cleanup failed: {names}",
                output=output,
            ) from cleanup_errors[0]
        if result is None:
            raise RuntimeError(
                RuntimeErrorKind.EXECUTION_FAILED,
                "Agent runtime produced no result",
            )
        return result
