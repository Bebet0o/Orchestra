#!/usr/bin/env python3

from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(
    os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
).resolve()
DATABASE = Path(
    os.environ.get(
        "ORCHESTRA_DB",
        str(ROOT / "state/controller/orchestra.db"),
    )
).resolve()
RECOVERY_SCRIPT = Path(
    os.environ.get(
        "ORCHESTRA_RECOVERY_SCRIPT",
        str(ROOT / "repo/scripts/orchestra-recovery.py"),
    )
).resolve()
CONFIG_PATH = Path(
    os.environ.get(
        "ORCHESTRA_SUPERVISOR_CONFIG",
        str(ROOT / "repo/config/supervisor.toml"),
    )
).resolve()
RUNTIME = ROOT / "runtime/supervisor"
LOCK_PATH = RUNTIME / "supervisor.lock"
STATUS_PATH = RUNTIME / "status.json"
VERSION = "supervisor-v1"
ACTIVE_STATUSES = (
    "SNAPSHOTTING",
    "RUNNING",
    "REVIEWING",
    "WAITING_HUMAN",
    "COMMITTING",
    "RECOVERING",
    "FAILED",
)
CORE_CONTAINERS = (
    "orchestra-sandbox-engine",
    "orchestra-hermes-agent",
)
INFORMATIONAL_CONTAINERS = (
    "orchestra-hermes-webui",
)
PRIVATE_RUNTIME_CONTAINERS = (
    "orchestra-hermes-agent",
    "orchestra-hermes-webui",
)
PRIVATE_DOCKER_ENVIRONMENT = {
    **os.environ,
    "DOCKER_HOST": "unix:///run/orchestra-docker/docker.sock",
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}


class SupervisorError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise SupervisorError(message)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def run_command(
    arguments: list[str],
    *,
    timeout: int = 30,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=timeout,
            env=(
                PRIVATE_DOCKER_ENVIRONMENT
                if arguments and arguments[0] == "docker"
                else None
            ),
        )
    except subprocess.TimeoutExpired as error:
        fail(
            "Command timed out: "
            + " ".join(arguments)
            + f" ({error.timeout}s)"
        )

    if check and result.returncode != 0:
        fail(
            "Command failed: "
            + " ".join(arguments)
            + f"\nstdout:\n{result.stdout}"
            + f"\nstderr:\n{result.stderr}"
        )

    return result


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    content = (
        json.dumps(payload, indent=2, sort_keys=True)
        + "\n"
    )

    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())

    temporary.replace(path)
    path.chmod(0o640)


def load_config() -> dict[str, int]:
    if not CONFIG_PATH.is_file():
        fail(f"Supervisor configuration is absent: {CONFIG_PATH}")

    with CONFIG_PATH.open("rb") as stream:
        document = tomllib.load(stream)

    supervisor = document.get("supervisor") or {}
    result = {
        "interval_seconds": int(
            os.environ.get(
                "ORCHESTRA_SUPERVISOR_INTERVAL_SECONDS",
                supervisor.get("interval_seconds", 60),
            )
        ),
        "stale_seconds": int(
            os.environ.get(
                "ORCHESTRA_SUPERVISOR_STALE_SECONDS",
                supervisor.get("stale_seconds", 300),
            )
        ),
        "startup_wait_seconds": int(
            os.environ.get(
                "ORCHESTRA_SUPERVISOR_STARTUP_WAIT_SECONDS",
                supervisor.get("startup_wait_seconds", 180),
            )
        ),
        "health_retry_seconds": int(
            os.environ.get(
                "ORCHESTRA_SUPERVISOR_HEALTH_RETRY_SECONDS",
                supervisor.get("health_retry_seconds", 5),
            )
        ),
        "command_timeout_seconds": int(
            os.environ.get(
                "ORCHESTRA_SUPERVISOR_COMMAND_TIMEOUT_SECONDS",
                supervisor.get("command_timeout_seconds", 300),
            )
        ),
    }

    bounds = {
        "interval_seconds": (5, 3600),
        "stale_seconds": (30, 86400),
        "startup_wait_seconds": (0, 1800),
        "health_retry_seconds": (1, 60),
        "command_timeout_seconds": (30, 1800),
    }

    for key, (minimum, maximum) in bounds.items():
        value = result[key]
        if not minimum <= value <= maximum:
            fail(
                f"Invalid {key}: {value}; expected "
                f"{minimum}..{maximum}"
            )

    return result


def docker_health() -> dict[str, Any]:
    checked_at = utc_now()
    host = run_command(
        [
            "docker",
            "info",
            "--format",
            "{{.ServerVersion}}",
        ],
        timeout=15,
    )

    result: dict[str, Any] = {
        "checked_at": checked_at,
        "healthy": False,
        "docker": {
            "reachable": host.returncode == 0,
            "version": host.stdout.strip()
            if host.returncode == 0
            else None,
            "error": host.stderr.strip()
            if host.returncode != 0
            else None,
        },
        "containers": {},
    }

    if host.returncode != 0:
        return result

    result["containers"]["orchestra-sandbox-engine"] = {
        "present": True,
        "running": True,
        "health": "healthy",
        "healthy": True,
        "error": None,
    }

    for name in PRIVATE_RUNTIME_CONTAINERS:
        inspected = run_command(
            ["docker", "inspect", "--format", "{{json .State}}", name],
            timeout=10,
        )
        error_message: str | None = inspected.stderr.strip() or None
        state: dict[str, Any] = {}
        if inspected.returncode == 0:
            try:
                state = json.loads(inspected.stdout)
            except json.JSONDecodeError:
                error_message = "private runtime returned invalid container state"
        running = state.get("Running") is True
        health = (state.get("Health") or {}).get("Status")
        healthy = running and health in (None, "healthy")
        result["containers"][name] = {
            "present": inspected.returncode == 0,
            "running": running,
            "health": health,
            "healthy": healthy,
            "error": error_message,
        }

    result["healthy"] = all(
        result["containers"].get(name, {}).get("healthy")
        for name in CORE_CONTAINERS
    )
    return result


def active_counts(connection: sqlite3.Connection) -> dict[str, int]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    active_runs = connection.execute(
        f"""
        SELECT COUNT(*)
        FROM runs
        WHERE status IN ({placeholders})
        """,
        ACTIVE_STATUSES,
    ).fetchone()[0]
    locks = connection.execute(
        "SELECT COUNT(*) FROM project_locks"
    ).fetchone()[0]
    pending = connection.execute(
        """
        SELECT COUNT(*)
        FROM approvals
        WHERE status = 'PENDING'
        """
    ).fetchone()[0]
    return {
        "active_runs": int(active_runs),
        "project_locks": int(locks),
        "pending_approvals": int(pending),
    }


def acquire_lock(*, blocking: bool = False) -> Any | None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    descriptor = LOCK_PATH.open("a+", encoding="utf-8")
    operation = fcntl.LOCK_EX
    if not blocking:
        operation |= fcntl.LOCK_NB

    try:
        fcntl.flock(descriptor.fileno(), operation)
    except BlockingIOError:
        descriptor.close()
        return None

    descriptor.seek(0)
    descriptor.truncate()
    descriptor.write(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "acquired_at": utc_now(),
            },
            sort_keys=True,
        )
        + "\n"
    )
    descriptor.flush()
    os.fsync(descriptor.fileno())
    return descriptor


def lock_is_held() -> bool:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    descriptor = LOCK_PATH.open("a+", encoding="utf-8")

    try:
        fcntl.flock(
            descriptor.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        descriptor.close()
        return True

    fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
    descriptor.close()
    return False


def register_instance(owner: str) -> str:
    instance_id = "supervisor-" + uuid.uuid4().hex
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE supervisor_instances
            SET status = 'ABANDONED',
                stopped_at = ?,
                last_error = CASE
                    WHEN last_error IS NULL
                    THEN 'superseded after supervisor restart'
                    ELSE last_error
                END
            WHERE status IN ('STARTING', 'RUNNING')
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO supervisor_instances (
                instance_id,
                hostname,
                pid,
                owner,
                version,
                status,
                started_at,
                heartbeat_at,
                stopped_at,
                last_sweep_id,
                last_error
            )
            VALUES (
                ?, ?, ?, ?, ?, 'STARTING',
                ?, ?, NULL, NULL, NULL
            )
            """,
            (
                instance_id,
                socket.gethostname(),
                os.getpid(),
                owner,
                VERSION,
                now,
                now,
            ),
        )
        connection.commit()

    return instance_id


def update_instance(
    instance_id: str,
    *,
    status: str | None = None,
    last_sweep_id: str | None = None,
    last_error: str | None = None,
    stopped: bool = False,
) -> None:
    assignments = ["heartbeat_at = ?"]
    parameters: list[Any] = [utc_now()]

    if status is not None:
        assignments.append("status = ?")
        parameters.append(status)
    if last_sweep_id is not None:
        assignments.append("last_sweep_id = ?")
        parameters.append(last_sweep_id)
    if last_error is not None:
        assignments.append("last_error = ?")
        parameters.append(last_error)
    if stopped:
        assignments.append("stopped_at = ?")
        parameters.append(utc_now())

    parameters.append(instance_id)

    with connect() as connection:
        connection.execute(
            f"""
            UPDATE supervisor_instances
            SET {", ".join(assignments)}
            WHERE instance_id = ?
            """,
            tuple(parameters),
        )
        connection.commit()


def start_sweep(
    instance_id: str,
    *,
    owner: str,
    trigger: str,
    stale_seconds: int,
    health: dict[str, Any],
    active_before: int,
) -> str:
    sweep_id = "sweep-" + uuid.uuid4().hex
    now = utc_now()

    with connect() as connection:
        connection.execute(
            """
            INSERT INTO supervisor_sweeps (
                sweep_id,
                instance_id,
                controller_owner,
                trigger,
                status,
                stale_seconds,
                services_healthy,
                health_json,
                active_runs_before,
                active_runs_after,
                recovered_runs,
                orphan_actions,
                result_json,
                failure_reason,
                started_at,
                finished_at
            )
            VALUES (
                ?, ?, ?, ?, 'RUNNING', ?, ?, ?,
                ?, NULL, 0, 0, '{}', NULL, ?, NULL
            )
            """,
            (
                sweep_id,
                instance_id,
                owner,
                trigger,
                stale_seconds,
                1 if health["healthy"] else 0,
                json.dumps(health, sort_keys=True),
                active_before,
                now,
            ),
        )
        connection.commit()

    return sweep_id


def finish_sweep(
    sweep_id: str,
    *,
    status: str,
    active_after: int,
    recovered_runs: int,
    orphan_actions: int,
    result: dict[str, Any],
    failure_reason: str | None = None,
) -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE supervisor_sweeps
            SET status = ?,
                active_runs_after = ?,
                recovered_runs = ?,
                orphan_actions = ?,
                result_json = ?,
                failure_reason = ?,
                finished_at = ?
            WHERE sweep_id = ?
            """,
            (
                status,
                active_after,
                recovered_runs,
                orphan_actions,
                json.dumps(result, sort_keys=True),
                failure_reason,
                utc_now(),
                sweep_id,
            ),
        )
        connection.commit()


def write_runtime_status(
    *,
    instance_id: str | None,
    state: str,
    health: dict[str, Any],
    sweep_id: str | None = None,
    message: str | None = None,
) -> None:
    with connect() as connection:
        counts = active_counts(connection)

    atomic_json(
        STATUS_PATH,
        {
            "version": VERSION,
            "instance_id": instance_id,
            "pid": os.getpid(),
            "state": state,
            "lock_held": lock_is_held(),
            "health": health,
            "counts": counts,
            "last_sweep_id": sweep_id,
            "message": message,
            "updated_at": utc_now(),
        },
    )


def invoke_recovery(
    *,
    owner: str,
    stale_seconds: int,
    timeout: int,
) -> dict[str, Any]:
    if not RECOVERY_SCRIPT.is_file():
        fail(f"Recovery Manager is absent: {RECOVERY_SCRIPT}")

    result = run_command(
        [
            sys.executable,
            str(RECOVERY_SCRIPT),
            "sweep",
            "--owner",
            owner,
            "--stale-seconds",
            str(stale_seconds),
        ],
        timeout=timeout,
    )

    if result.returncode != 0:
        fail(
            "Recovery sweep failed"
            + f"\nstdout:\n{result.stdout}"
            + f"\nstderr:\n{result.stderr}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"Recovery sweep returned invalid JSON: {error}")

    if not isinstance(payload, dict):
        fail("Recovery sweep payload is not an object")

    return payload


def perform_sweep(
    *,
    instance_id: str,
    owner: str,
    trigger: str,
    stale_seconds: int,
    command_timeout_seconds: int,
) -> dict[str, Any]:
    health = docker_health()

    with connect() as connection:
        before = active_counts(connection)["active_runs"]

    sweep_id = start_sweep(
        instance_id,
        owner=owner,
        trigger=trigger,
        stale_seconds=stale_seconds,
        health=health,
        active_before=before,
    )
    update_instance(instance_id, last_sweep_id=sweep_id)

    if not health["healthy"]:
        result = {
            "sweep_id": sweep_id,
            "trigger": trigger,
            "skipped": True,
            "reason": "core-services-unhealthy",
            "health": health,
        }
        finish_sweep(
            sweep_id,
            status="SKIPPED",
            active_after=before,
            recovered_runs=0,
            orphan_actions=0,
            result=result,
            failure_reason="core services are unhealthy",
        )
        write_runtime_status(
            instance_id=instance_id,
            state="RUNNING",
            health=health,
            sweep_id=sweep_id,
            message="sweep skipped: core services unhealthy",
        )
        return result

    try:
        recovery = invoke_recovery(
            owner=owner,
            stale_seconds=stale_seconds,
            timeout=command_timeout_seconds,
        )
        runs = recovery.get("runs") or []
        orphan_actions = (
            (recovery.get("orphans") or {}).get("actions")
            or []
        )

        with connect() as connection:
            after = active_counts(connection)["active_runs"]

        result = {
            "sweep_id": sweep_id,
            "trigger": trigger,
            "skipped": False,
            "health": health,
            "recovery": recovery,
        }
        finish_sweep(
            sweep_id,
            status="COMPLETED",
            active_after=after,
            recovered_runs=len(runs),
            orphan_actions=len(orphan_actions),
            result=result,
        )
        update_instance(
            instance_id,
            last_sweep_id=sweep_id,
            last_error="",
        )
        write_runtime_status(
            instance_id=instance_id,
            state="RUNNING",
            health=health,
            sweep_id=sweep_id,
            message="sweep completed",
        )
        return result
    except Exception as error:
        with connect() as connection:
            after = active_counts(connection)["active_runs"]

        result = {
            "sweep_id": sweep_id,
            "trigger": trigger,
            "skipped": False,
            "health": health,
            "error": str(error),
        }
        finish_sweep(
            sweep_id,
            status="FAILED",
            active_after=after,
            recovered_runs=0,
            orphan_actions=0,
            result=result,
            failure_reason=str(error),
        )
        update_instance(
            instance_id,
            last_sweep_id=sweep_id,
            last_error=str(error),
        )
        write_runtime_status(
            instance_id=instance_id,
            state="DEGRADED",
            health=health,
            sweep_id=sweep_id,
            message=str(error),
        )
        return result


def wait_for_core_health(
    *,
    instance_id: str,
    timeout_seconds: int,
    retry_seconds: int,
    stop_event: threading.Event,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    last = docker_health()

    while (
        not last["healthy"]
        and not stop_event.is_set()
        and time.monotonic() < deadline
    ):
        update_instance(instance_id)
        write_runtime_status(
            instance_id=instance_id,
            state="WAITING_SERVICES",
            health=last,
            message="waiting for Docker, sandbox engine and Agent",
        )
        stop_event.wait(retry_seconds)
        last = docker_health()

    return last


def command_run(arguments: argparse.Namespace) -> None:
    config = load_config()
    lock = acquire_lock()

    if lock is None:
        print(
            json.dumps(
                {
                    "status": "LOCKED",
                    "lock_path": str(LOCK_PATH),
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        raise SystemExit(75)

    owner = arguments.owner or (
        f"ops-supervisor:{socket.gethostname()}"
    )
    instance_id = register_instance(owner)
    stop_event = threading.Event()

    def request_stop(signum: int, _: Any) -> None:
        print(
            json.dumps(
                {
                    "event": "signal",
                    "signal": signum,
                    "instance_id": instance_id,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    try:
        update_instance(instance_id, status="RUNNING")
        health = wait_for_core_health(
            instance_id=instance_id,
            timeout_seconds=config["startup_wait_seconds"],
            retry_seconds=config["health_retry_seconds"],
            stop_event=stop_event,
        )
        write_runtime_status(
            instance_id=instance_id,
            state="RUNNING"
            if health["healthy"]
            else "DEGRADED",
            health=health,
            message="startup health gate complete",
        )

        if not stop_event.is_set():
            startup_result = perform_sweep(
                instance_id=instance_id,
                owner=owner,
                trigger="startup",
                stale_seconds=config["stale_seconds"],
                command_timeout_seconds=(
                    config["command_timeout_seconds"]
                ),
            )
            print(
                json.dumps(
                    {
                        "event": "startup-sweep",
                        "instance_id": instance_id,
                        "result": startup_result,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )

        next_sweep = (
            time.monotonic()
            + config["interval_seconds"]
        )

        while not stop_event.is_set():
            update_instance(instance_id)

            if time.monotonic() >= next_sweep:
                result = perform_sweep(
                    instance_id=instance_id,
                    owner=owner,
                    trigger="periodic",
                    stale_seconds=config["stale_seconds"],
                    command_timeout_seconds=(
                        config["command_timeout_seconds"]
                    ),
                )
                print(
                    json.dumps(
                        {
                            "event": "periodic-sweep",
                            "instance_id": instance_id,
                            "result": result,
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                next_sweep = (
                    time.monotonic()
                    + config["interval_seconds"]
                )

            stop_event.wait(
                min(5, config["health_retry_seconds"])
            )

        update_instance(
            instance_id,
            status="STOPPED",
            stopped=True,
        )
        write_runtime_status(
            instance_id=instance_id,
            state="STOPPED",
            health=docker_health(),
            message="supervisor stopped cleanly",
        )
    except BaseException as error:
        if not isinstance(error, (KeyboardInterrupt, SystemExit)):
            try:
                update_instance(
                    instance_id,
                    status="FAILED",
                    last_error=str(error),
                    stopped=True,
                )
                write_runtime_status(
                    instance_id=instance_id,
                    state="FAILED",
                    health=docker_health(),
                    message=str(error),
                )
            except Exception:
                pass
        raise
    finally:
        lock.close()


def command_sweep_once(arguments: argparse.Namespace) -> None:
    config = load_config()
    lock = acquire_lock()

    if lock is None:
        print(
            json.dumps(
                {
                    "status": "LOCKED",
                    "lock_path": str(LOCK_PATH),
                },
                sort_keys=True,
            )
        )
        raise SystemExit(75)

    owner = arguments.owner or (
        f"manual-supervisor:{socket.gethostname()}"
    )
    instance_id = register_instance(owner)

    try:
        update_instance(instance_id, status="RUNNING")
        result = perform_sweep(
            instance_id=instance_id,
            owner=owner,
            trigger=arguments.trigger,
            stale_seconds=(
                arguments.stale_seconds
                if arguments.stale_seconds is not None
                else config["stale_seconds"]
            ),
            command_timeout_seconds=(
                config["command_timeout_seconds"]
            ),
        )
        update_instance(
            instance_id,
            status="STOPPED",
            stopped=True,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    except Exception as error:
        update_instance(
            instance_id,
            status="FAILED",
            last_error=str(error),
            stopped=True,
        )
        raise
    finally:
        lock.close()


def command_status(_: argparse.Namespace) -> None:
    with connect() as connection:
        instance = connection.execute(
            """
            SELECT *
            FROM supervisor_instances
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        sweep = connection.execute(
            """
            SELECT *
            FROM supervisor_sweeps
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        counts = active_counts(connection)

    payload = {
        "version": VERSION,
        "lock_held": lock_is_held(),
        "health": docker_health(),
        "counts": counts,
        "instance": dict(instance)
        if instance is not None
        else None,
        "last_sweep": dict(sweep)
        if sweep is not None
        else None,
        "runtime_status_path": str(STATUS_PATH),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def command_probe_lock(_: argparse.Namespace) -> None:
    print(
        json.dumps(
            {
                "lock_held": lock_is_held(),
                "lock_path": str(LOCK_PATH),
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_self_test(_: argparse.Namespace) -> None:
    config = load_config()
    required = {
        "interval_seconds",
        "stale_seconds",
        "startup_wait_seconds",
        "health_retry_seconds",
        "command_timeout_seconds",
    }

    if set(config) != required:
        fail("Supervisor configuration contract is incomplete")
    if VERSION != "supervisor-v1":
        fail("Unexpected Supervisor version")
    if set(CORE_CONTAINERS) != {
        "orchestra-sandbox-engine",
        "orchestra-hermes-agent",
    }:
        fail("Core service health contract changed")

    print("Orchestra supervisor decision loop: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra automatic Supervisor and Watchdog"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    run = subparsers.add_parser("run")
    run.add_argument("--owner")
    run.set_defaults(function=command_run)

    sweep = subparsers.add_parser("sweep-once")
    sweep.add_argument("--owner")
    sweep.add_argument(
        "--trigger",
        choices=("manual", "test"),
        default="manual",
    )
    sweep.add_argument("--stale-seconds", type=int)
    sweep.set_defaults(function=command_sweep_once)

    status = subparsers.add_parser("status")
    status.set_defaults(function=command_status)

    probe = subparsers.add_parser("probe-lock")
    probe.set_defaults(function=command_probe_lock)

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        arguments.function(arguments)
    except SupervisorError as error:
        print(f"Supervisor error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
