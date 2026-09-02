#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn
import orchestra_review_assignment as ASSIGNMENTS


ROOT = Path(
    os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
).resolve()
REPO = ROOT / "repo"
DATABASE = Path(
    os.environ.get(
        "ORCHESTRA_DB",
        str(ROOT / "state/controller/orchestra.db"),
    )
).resolve()
CONFIG_PATH = Path(
    os.environ.get(
        "ORCHESTRA_ORCHESTRATOR_CONFIG",
        str(REPO / "config/orchestrator.toml"),
    )
).resolve()
RUNTIME = ROOT / "runtime/orchestrator"
LOCK_PATH = RUNTIME / "orchestrator.lock"
STATUS_PATH = RUNTIME / "status.json"
SUPERVISOR_STATUS_PATH = ROOT / "runtime/supervisor/status.json"
TRANSACTION = REPO / "scripts/orchestra-transaction.py"
WORKER = REPO / "scripts/orchestra-worker.py"
REVIEWER = REPO / "scripts/orchestra-reviewer.py"
INTEGRATOR = REPO / "scripts/orchestra-integrator.py"
RECOVERY = REPO / "scripts/orchestra-recovery.py"
PLANNER = REPO / "scripts/orchestra-planner.py"
OBJECTIVE_RUNTIME = RUNTIME / "objectives"
VERSION = "orchestrator-v2"
TASK_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
TERMINAL_TASK_STATUSES = {
    "COMPLETED",
    "FAILED",
    "BLOCKED",
    "CANCELLED",
}
ACTIVE_RUN_STATUSES = {
    "SNAPSHOTTING",
    "RUNNING",
    "REVIEWING",
    "WAITING_HUMAN",
    "COMMITTING",
    "RECOVERING",
}
HUMAN_GATE_ERROR = "waiting for human decision"
HUMAN_GATE_DEFERRED_ERROR = (
    "waiting for in-flight integration before human decision"
)
CANCELLATION_RECOVERY_ERROR = "cancellation cleanup requires recovery"
CANCELLATION_PENDING_ERROR = "objective cancellation cleanup pending"
PRIVATE_DOCKER_ENVIRONMENT = {
    **os.environ,
    "DOCKER_HOST": "unix:///run/orchestra-docker/docker.sock",
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}


class OrchestratorError(RuntimeError):
    pass


class CommandExecutionError(OrchestratorError):
    def __init__(
        self,
        arguments: list[str],
        returncode: int,
        stdout: str,
        stderr: str,
    ) -> None:
        self.arguments = list(arguments)
        self.returncode = int(returncode)
        self.stdout = stdout
        self.stderr = stderr
        super().__init__(
            "Command failed: "
            + " ".join(arguments)
            + f"\nstdout:\n{stdout}"
            + f"\nstderr:\n{stderr}"
        )


def fail(message: str) -> NoReturn:
    raise OrchestratorError(message)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def atomic_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    content = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
        ensure_ascii=False,
    ) + "\n"

    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())

    temporary.replace(path)
    path.chmod(0o640)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def run_command(
    arguments: list[str],
    *,
    timeout: int,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
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
        raise CommandExecutionError(
            arguments,
            result.returncode,
            result.stdout,
            result.stderr,
        )

    return result


def run_json(arguments: list[str], *, timeout: int) -> dict[str, Any]:
    result = run_command(arguments, timeout=timeout)

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(
            "Command returned invalid JSON: "
            + " ".join(arguments)
            + f"\n{error}\nstdout:\n{result.stdout}"
        )

    if not isinstance(payload, dict):
        fail("Command JSON result is not an object")

    return payload


def load_config() -> dict[str, int]:
    if not CONFIG_PATH.is_file():
        fail(f"Orchestrator configuration is absent: {CONFIG_PATH}")

    with CONFIG_PATH.open("rb") as stream:
        document = tomllib.load(stream)

    section = document.get("orchestrator") or {}
    result = {
        "poll_seconds": int(
            os.environ.get(
                "ORCHESTRA_ORCHESTRATOR_POLL_SECONDS",
                section.get("poll_seconds", 3),
            )
        ),
        "global_parallel_tasks": int(
            os.environ.get(
                "ORCHESTRA_ORCHESTRATOR_GLOBAL_PARALLEL_TASKS",
                section.get("global_parallel_tasks", 3),
            )
        ),
        "global_parallel_objectives": int(
            os.environ.get(
                "ORCHESTRA_ORCHESTRATOR_GLOBAL_PARALLEL_OBJECTIVES",
                section.get("global_parallel_objectives", 2),
            )
        ),
        "planning_timeout_seconds": int(
            section.get("planning_timeout_seconds", 900)
        ),
        "planning_retry_backoff_seconds": int(
            section.get("planning_retry_backoff_seconds", 30)
        ),
        "worker_timeout_seconds": int(
            section.get("worker_timeout_seconds", 1200)
        ),
        "review_timeout_seconds": int(
            section.get("review_timeout_seconds", 1200)
        ),
        "review_transport_attempts": int(
            section.get("review_transport_attempts", 3)
        ),
        "review_retry_backoff_seconds": int(
            section.get("review_retry_backoff_seconds", 15)
        ),
        "command_timeout_seconds": int(
            section.get("command_timeout_seconds", 300)
        ),
        "heartbeat_seconds": int(
            section.get("heartbeat_seconds", 5)
        ),
    }

    bounds = {
        "poll_seconds": (1, 60),
        "global_parallel_tasks": (1, 16),
        "global_parallel_objectives": (1, 16),
        "planning_timeout_seconds": (60, 3600),
        "planning_retry_backoff_seconds": (1, 900),
        "worker_timeout_seconds": (60, 7200),
        "review_timeout_seconds": (60, 7200),
        "review_transport_attempts": (1, 5),
        "review_retry_backoff_seconds": (1, 120),
        "command_timeout_seconds": (30, 1800),
        "heartbeat_seconds": (1, 30),
    }

    for key, (minimum, maximum) in bounds.items():
        value = result[key]
        if not minimum <= value <= maximum:
            fail(
                f"Invalid {key}: {value}; expected "
                f"{minimum}..{maximum}"
            )

    return result


def acquire_lock() -> Any | None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    descriptor = LOCK_PATH.open("a+", encoding="utf-8")

    try:
        fcntl.flock(
            descriptor.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError:
        descriptor.close()
        return None

    descriptor.seek(0)
    descriptor.truncate()
    descriptor.write(
        canonical_json(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "acquired_at": utc_now(),
            }
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


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        fail(f"Plan file is absent: {path}")

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"Invalid plan JSON: {error}")

    if not isinstance(payload, dict):
        fail("Plan must be a JSON object")

    return payload


def topological_order(tasks: list[dict[str, Any]]) -> list[str]:
    keys = {task["key"] for task in tasks}
    dependencies = {
        task["key"]: set(task.get("dependencies") or [])
        for task in tasks
    }
    reverse: dict[str, set[str]] = {key: set() for key in keys}

    for key, parents in dependencies.items():
        for parent in parents:
            reverse[parent].add(key)

    ready = sorted(
        key for key, parents in dependencies.items() if not parents
    )
    order: list[str] = []

    while ready:
        key = ready.pop(0)
        order.append(key)

        for child in sorted(reverse[key]):
            dependencies[child].discard(key)
            if not dependencies[child] and child not in order and child not in ready:
                ready.append(child)
                ready.sort()

    if len(order) != len(keys):
        fail("Plan dependency graph contains a cycle")

    return order


def validate_plan(
    plan: dict[str, Any],
    *,
    allow_test_actions: bool,
    require_enabled_projects: bool = True,
) -> dict[str, Any]:
    if plan.get("schema_version") != 1:
        fail("Plan schema_version must be 1")

    objective = plan.get("objective")
    if not isinstance(objective, str) or not objective.strip():
        fail("Plan objective is required")
    if len(objective.encode()) > 16_384:
        fail("Plan objective exceeds 16 KiB")

    maximum = plan.get("max_parallel_tasks", 1)
    if not isinstance(maximum, int) or not 1 <= maximum <= 16:
        fail("max_parallel_tasks must be an integer from 1 to 16")

    tasks = plan.get("tasks")
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 32:
        fail("Plan tasks must contain 1..32 entries")

    normalized: list[dict[str, Any]] = []
    keys: set[str] = set()

    with connect() as connection:
        roles = {
            row["role_id"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM roles WHERE enabled = 1"
            )
        }
        projects = {
            row["project_id"]: dict(row)
            for row in connection.execute(
                "SELECT * FROM projects"
            )
        }

    for index, raw in enumerate(tasks):
        if not isinstance(raw, dict):
            fail(f"Task {index} must be an object")

        key = raw.get("key")
        if not isinstance(key, str) or not TASK_KEY_PATTERN.fullmatch(key):
            fail(f"Invalid task key at index {index}: {key!r}")
        if key in keys:
            fail(f"Duplicate task key: {key}")
        keys.add(key)

        kind = raw.get("kind", "PIPELINE")
        if kind not in {"PIPELINE", "NOOP", "TEST_SLEEP", "TEST_FAIL"}:
            fail(f"Unsupported task kind: {kind}")
        if kind.startswith("TEST_") and not allow_test_actions:
            fail(f"Test action is forbidden outside test plans: {kind}")

        dependencies = raw.get("dependencies") or []
        if not isinstance(dependencies, list) or not all(
            isinstance(item, str) for item in dependencies
        ):
            fail(f"Task {key} dependencies must be a string list")
        if len(set(dependencies)) != len(dependencies):
            fail(f"Task {key} contains duplicate dependencies")
        if key in dependencies:
            fail(f"Task {key} depends on itself")

        instruction = raw.get("instruction", "")
        if not isinstance(instruction, str):
            fail(f"Task {key} instruction must be a string")
        if len(instruction.encode()) > 32_768:
            fail(f"Task {key} instruction exceeds 32 KiB")

        acceptance = raw.get("acceptance_criteria") or []
        if not isinstance(acceptance, list) or not all(
            isinstance(item, str) and item.strip() for item in acceptance
        ):
            fail(f"Task {key} acceptance_criteria must be strings")

        max_attempts = raw.get("max_attempts", 1)
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 10:
            fail(f"Task {key} max_attempts must be 1..10")

        priority = raw.get("priority", 100)
        if not isinstance(priority, int) or not -1000 <= priority <= 1000:
            fail(f"Task {key} priority is invalid")

        project_id: str | None = None
        role_id: str | None = None
        marker: str | None = None

        if kind == "PIPELINE":
            project_id = raw.get("project_id")
            role_id = raw.get("role_id")
            marker = raw.get("marker")

            if project_id not in projects:
                fail(f"Task {key} has unknown project: {project_id}")
            if require_enabled_projects and not projects[project_id]["enabled"]:
                fail(f"Task {key} project is disabled: {project_id}")
            if role_id not in roles:
                fail(f"Task {key} has unknown role: {role_id}")
            role = roles[role_id]
            if role["role_kind"] != "worker" or not role["may_commit"]:
                fail(f"Task {key} role is not a committing worker: {role_id}")
            if role["may_push"]:
                fail(f"Task {key} role may push, which is forbidden")
            if not instruction.strip():
                fail(f"Task {key} pipeline instruction is empty")
            if not acceptance:
                fail(f"Task {key} acceptance criteria are required")
            if not isinstance(marker, str) or not marker.strip() or "\n" in marker:
                fail(f"Task {key} marker must be one non-empty line")
        elif allow_test_actions and raw.get("project_id") is not None:
            project_id = raw.get("project_id")
            if project_id not in projects:
                fail(f"Task {key} has unknown project affinity: {project_id}")
            if require_enabled_projects and not projects[project_id]["enabled"]:
                fail(f"Task {key} project affinity is disabled: {project_id}")

        if kind == "TEST_SLEEP":
            duration = raw.get("duration_seconds", 1)
            if not isinstance(duration, int) or not 1 <= duration <= 120:
                fail(f"Task {key} duration_seconds must be 1..120")
        else:
            duration = None

        normalized.append(
            {
                "key": key,
                "kind": kind,
                "project_id": project_id,
                "role_id": role_id,
                "instruction": instruction.strip(),
                "acceptance_criteria": acceptance,
                "marker": marker.strip() if marker else None,
                "dependencies": dependencies,
                "priority": priority,
                "max_attempts": max_attempts,
                "duration_seconds": duration,
            }
        )

    for task in normalized:
        unknown = set(task["dependencies"]) - keys
        if unknown:
            fail(
                f"Task {task['key']} has unknown dependencies: "
                + ", ".join(sorted(unknown))
            )

    order = topological_order(normalized)
    by_key = {task["key"]: task for task in normalized}

    # One project has writer concurrency 1. Independent write tasks on the
    # same project would produce competing base commits, so every same-project
    # PIPELINE pair must be dependency-ordered.
    ancestors: dict[str, set[str]] = {key: set() for key in by_key}
    for key in order:
        for parent in by_key[key]["dependencies"]:
            ancestors[key].add(parent)
            ancestors[key].update(ancestors[parent])

    pipeline_by_project: dict[str, list[str]] = {}
    for task in normalized:
        if task["kind"] == "PIPELINE":
            pipeline_by_project.setdefault(task["project_id"], []).append(task["key"])

    for project_id, project_tasks in pipeline_by_project.items():
        for index, left in enumerate(project_tasks):
            for right in project_tasks[index + 1 :]:
                if left not in ancestors[right] and right not in ancestors[left]:
                    fail(
                        "Same-project PIPELINE tasks must be dependency-ordered: "
                        f"{project_id}: {left}, {right}"
                    )

    ordered_tasks = [by_key[key] for key in order]

    return {
        "schema_version": 1,
        "objective": objective.strip(),
        "max_parallel_tasks": maximum,
        "tasks": ordered_tasks,
    }


def insert_plan(
    plan: dict[str, Any],
    *,
    source: str,
    initial_status: str,
) -> str:
    if source not in {"AI", "DECLARATIVE", "TEST"}:
        fail(f"Invalid plan source: {source}")
    if initial_status not in {"DRAFT", "READY"}:
        fail(f"Invalid initial plan status: {initial_status}")

    plan_id = "plan-" + uuid.uuid4().hex
    now = utc_now()
    plan_text = canonical_json(plan)
    digest = hashlib.sha256(plan_text.encode()).hexdigest()
    task_ids = {
        task["key"]: "orchestration-task-" + uuid.uuid4().hex
        for task in plan["tasks"]
    }

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO orchestration_plans (
                plan_id,
                objective,
                source,
                planner_role_id,
                status,
                max_parallel_tasks,
                plan_sha256,
                plan_json,
                created_at,
                started_at,
                heartbeat_at,
                finished_at,
                last_error
            )
            VALUES (?, ?, ?, 'orchestrator', ?, ?, ?, ?, ?, NULL, ?, NULL, NULL)
            """,
            (
                plan_id,
                plan["objective"],
                source,
                initial_status,
                plan["max_parallel_tasks"],
                digest,
                plan_text,
                now,
                now,
            ),
        )

        for task in plan["tasks"]:
            connection.execute(
                """
                INSERT INTO orchestration_tasks (
                    orchestration_task_id,
                    plan_id,
                    task_key,
                    kind,
                    project_id,
                    role_id,
                    status,
                    priority,
                    instruction,
                    acceptance_json,
                    marker,
                    max_attempts,
                    attempt_count,
                    result_json,
                    failure_reason,
                    created_at,
                    started_at,
                    heartbeat_at,
                    finished_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?, ?, ?, ?, 0,
                    '{}', NULL, ?, NULL, NULL, NULL
                )
                """,
                (
                    task_ids[task["key"]],
                    plan_id,
                    task["key"],
                    task["kind"],
                    task["project_id"],
                    task["role_id"],
                    task["priority"],
                    task["instruction"],
                    canonical_json(task["acceptance_criteria"]),
                    task["marker"],
                    task["max_attempts"],
                    now,
                ),
            )

        for task in plan["tasks"]:
            for parent in task["dependencies"]:
                connection.execute(
                    """
                    INSERT INTO orchestration_dependencies (
                        plan_id,
                        orchestration_task_id,
                        depends_on_task_id,
                        dependency_condition
                    )
                    VALUES (?, ?, ?, 'SUCCESS')
                    """,
                    (
                        plan_id,
                        task_ids[task["key"]],
                        task_ids[parent],
                    ),
                )

        connection.commit()

    return plan_id


def add_event(
    connection: sqlite3.Connection,
    *,
    plan_id: str,
    task_id: str | None,
    event_type: str,
    severity: str,
    payload: dict[str, Any],
) -> None:
    # Orchestration events do not have a runs FK target, so plan identity is
    # carried in payload_json and task_id remains NULL for the legacy table.
    connection.execute(
        """
        INSERT INTO events (
            project_id,
            run_id,
            task_id,
            event_type,
            severity,
            payload_json,
            created_at
        )
        VALUES (NULL, NULL, NULL, ?, ?, ?, ?)
        """,
        (
            event_type,
            severity,
            canonical_json(
                {
                    "plan_id": plan_id,
                    "orchestration_task_id": task_id,
                    **payload,
                }
            ),
            utc_now(),
        ),
    )


def register_instance(owner: str) -> str:
    instance_id = "orchestrator-instance-" + uuid.uuid4().hex
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE orchestrator_instances
            SET status = 'ABANDONED',
                stopped_at = ?,
                last_error = CASE
                    WHEN last_error IS NULL OR last_error = ''
                    THEN 'superseded after orchestrator restart'
                    ELSE last_error
                END
            WHERE status IN ('STARTING', 'RUNNING')
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO orchestrator_instances (
                instance_id,
                hostname,
                pid,
                owner,
                version,
                status,
                started_at,
                heartbeat_at,
                stopped_at,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, 'STARTING', ?, ?, NULL, NULL)
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
    last_error: str | None = None,
    stopped: bool = False,
) -> None:
    assignments = ["heartbeat_at = ?"]
    parameters: list[Any] = [utc_now()]

    if status is not None:
        assignments.append("status = ?")
        parameters.append(status)
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
            UPDATE orchestrator_instances
            SET {', '.join(assignments)}
            WHERE instance_id = ?
            """,
            tuple(parameters),
        )
        connection.commit()


def plan_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM orchestration_plans
        GROUP BY status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def task_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM orchestration_tasks
        GROUP BY status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def objective_counts(connection: sqlite3.Connection) -> dict[str, int]:
    rows = connection.execute(
        """
        SELECT status, COUNT(*) AS count
        FROM objective_queue
        GROUP BY status
        """
    ).fetchall()
    return {row["status"]: int(row["count"]) for row in rows}


def write_status(instance_id: str, *, message: str) -> None:
    with connect() as connection:
        plans = plan_counts(connection)
        tasks = task_counts(connection)
        objectives = objective_counts(connection)
        latest = connection.execute(
            """
            SELECT plan_id, objective, status, heartbeat_at, last_error
            FROM orchestration_plans
            ORDER BY created_at DESC
            LIMIT 1
            """
        ).fetchone()

    atomic_json(
        STATUS_PATH,
        {
            "version": VERSION,
            "instance_id": instance_id,
            "pid": os.getpid(),
            "lock_held": lock_is_held(),
            "plan_counts": plans,
            "task_counts": tasks,
            "objective_counts": objectives,
            "latest_plan": dict(latest) if latest else None,
            "message": message,
            "updated_at": utc_now(),
        },
    )


def refresh_plan_states(plan_id: str) -> None:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        plan = connection.execute(
            "SELECT * FROM orchestration_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()

        if plan is None or plan["status"] in {
            "DRAFT",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }:
            connection.rollback()
            return

        tasks = connection.execute(
            """
            SELECT *
            FROM orchestration_tasks
            WHERE plan_id = ?
            ORDER BY priority, created_at
            """,
            (plan_id,),
        ).fetchall()

        for task in tasks:
            if task["status"] != "PENDING":
                continue

            parents = connection.execute(
                """
                SELECT parent.status
                FROM orchestration_dependencies AS dependency
                JOIN orchestration_tasks AS parent
                  ON parent.orchestration_task_id = dependency.depends_on_task_id
                WHERE dependency.orchestration_task_id = ?
                """,
                (task["orchestration_task_id"],),
            ).fetchall()
            parent_statuses = {row["status"] for row in parents}

            if parent_statuses & {"FAILED", "BLOCKED", "CANCELLED"}:
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = 'BLOCKED',
                        failure_reason = 'dependency did not complete successfully',
                        heartbeat_at = ?,
                        finished_at = ?
                    WHERE orchestration_task_id = ?
                      AND status = 'PENDING'
                    """,
                    (now, now, task["orchestration_task_id"]),
                )
                add_event(
                    connection,
                    plan_id=plan_id,
                    task_id=task["orchestration_task_id"],
                    event_type="ORCHESTRATION_TASK_BLOCKED",
                    severity="WARNING",
                    payload={"reason": "dependency-failed"},
                )
            elif all(status == "COMPLETED" for status in parent_statuses):
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = 'READY',
                        heartbeat_at = ?
                    WHERE orchestration_task_id = ?
                      AND status = 'PENDING'
                    """,
                    (now, task["orchestration_task_id"]),
                )

        statuses = [
            row[0]
            for row in connection.execute(
                """
                SELECT status
                FROM orchestration_tasks
                WHERE plan_id = ?
                """,
                (plan_id,),
            ).fetchall()
        ]
        human_gate_active = plan_has_active_human_gate(connection, plan_id)

        if (
            human_gate_active
            and plan["last_error"] == HUMAN_GATE_DEFERRED_ERROR
        ):
            if plan_has_authorized_integration_in_flight(connection, plan_id):
                connection.execute(
                    """
                    UPDATE orchestration_plans
                    SET heartbeat_at = ?
                    WHERE plan_id = ?
                    """,
                    (now, plan_id),
                )
            else:
                connection.execute(
                    """
                    UPDATE orchestration_plans
                    SET status = 'BLOCKED',
                        heartbeat_at = ?,
                        last_error = ?
                    WHERE plan_id = ?
                    """,
                    (now, HUMAN_GATE_ERROR, plan_id),
                )
                add_event(
                    connection,
                    plan_id=plan_id,
                    task_id=None,
                    event_type="ORCHESTRATION_PLAN_BLOCKED_HUMAN",
                    severity="WARNING",
                    payload={"deferred": True},
                )
        elif (
            plan["status"] == "BLOCKED"
            and (
                (
                    human_gate_active
                    and plan["last_error"] == HUMAN_GATE_ERROR
                )
                or plan["last_error"] in {
                    CANCELLATION_RECOVERY_ERROR,
                    CANCELLATION_PENDING_ERROR,
                }
            )
        ):
            connection.execute(
                """
                UPDATE orchestration_plans
                SET heartbeat_at = ?
                WHERE plan_id = ?
                """,
                (now, plan_id),
            )
        elif statuses and all(status == "COMPLETED" for status in statuses):
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status = 'COMPLETED',
                    heartbeat_at = ?,
                    finished_at = ?,
                    last_error = NULL
                WHERE plan_id = ?
                """,
                (now, now, plan_id),
            )
            add_event(
                connection,
                plan_id=plan_id,
                task_id=None,
                event_type="ORCHESTRATION_PLAN_COMPLETED",
                severity="INFO",
                payload={},
            )
        elif any(
            status in {"FAILED", "BLOCKED", "CANCELLED"}
            for status in statuses
        ) and not any(
            status in {"READY", "RUNNING", "PENDING"}
            for status in statuses
        ):
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status = 'FAILED',
                    heartbeat_at = ?,
                    finished_at = ?,
                    last_error = 'one or more tasks failed'
                WHERE plan_id = ?
                """,
                (now, now, plan_id),
            )
            add_event(
                connection,
                plan_id=plan_id,
                task_id=None,
                event_type="ORCHESTRATION_PLAN_FAILED",
                severity="ERROR",
                payload={},
            )
        else:
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status = CASE
                        WHEN status = 'READY' THEN 'RUNNING'
                        ELSE status
                    END,
                    started_at = COALESCE(started_at, ?),
                    heartbeat_at = ?
                WHERE plan_id = ?
                """,
                (now, now, plan_id),
            )

        connection.commit()


def reservation_ineligibility_reason(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
) -> str | None:
    if task["status"] != "READY":
        return f"Task cannot start from status {task['status']}"
    if task["attempt_count"] >= task["max_attempts"]:
        return "Task attempt budget is exhausted"

    plan = connection.execute(
        "SELECT status, max_parallel_tasks FROM orchestration_plans WHERE plan_id=?",
        (task["plan_id"],),
    ).fetchone()
    if plan is None:
        return "Task orchestration plan disappeared before reservation"
    if plan["status"] not in {"READY", "RUNNING"}:
        return f"Plan cannot start work from status {plan['status']}"

    objectives = connection.execute(
        "SELECT objective_id, status FROM objective_queue WHERE plan_id=?",
        (task["plan_id"],),
    ).fetchall()
    if len(objectives) > 1:
        return "Plan is linked to multiple objectives"
    if objectives and objectives[0]["status"] != "RUNNING":
        return (
            "Objective cannot start work from status "
            f"{objectives[0]['status']}"
        )

    if plan_has_active_human_gate(connection, task["plan_id"]):
        return "Plan is waiting for a human decision"

    running_for_plan = int(
        connection.execute(
            "SELECT COUNT(*) FROM orchestration_tasks "
            "WHERE plan_id=? AND status='RUNNING'",
            (task["plan_id"],),
        ).fetchone()[0]
    )
    if running_for_plan >= int(plan["max_parallel_tasks"]):
        return "Plan parallel task limit is exhausted"

    if project_is_busy(
        connection,
        task["project_id"],
        task["orchestration_task_id"],
    ):
        return "Task project already has active work"

    return None


def reserve_attempt(
    task_id: str,
    *,
    instance_id: str,
) -> tuple[str, int, sqlite3.Row]:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        task = connection.execute(
            "SELECT * FROM orchestration_tasks WHERE orchestration_task_id = ?",
            (task_id,),
        ).fetchone()

        if task is None:
            connection.rollback()
            fail(f"Unknown orchestration task: {task_id}")
        ineligibility_reason = reservation_ineligibility_reason(
            connection,
            task,
        )
        if ineligibility_reason is not None:
            connection.rollback()
            fail(ineligibility_reason)

        attempt_number = int(task["attempt_count"]) + 1
        attempt_id = "orchestration-attempt-" + uuid.uuid4().hex
        updated = connection.execute(
            """
            UPDATE orchestration_tasks
            SET status = 'RUNNING',
                attempt_count = ?,
                started_at = COALESCE(started_at, ?),
                heartbeat_at = ?,
                failure_reason = NULL
            WHERE orchestration_task_id = ?
              AND status = 'READY'
            """,
            (attempt_number, now, now, task_id),
        ).rowcount

        if updated != 1:
            connection.rollback()
            fail("Task reservation lost a concurrent race")

        connection.execute(
            """
            INSERT INTO orchestration_attempts (
                attempt_id,
                orchestration_task_id,
                attempt_number,
                status,
                executor_instance_id,
                run_id,
                worker_execution_id,
                review_execution_id,
                integration_id,
                result_json,
                failure_reason,
                started_at,
                heartbeat_at,
                finished_at
            )
            VALUES (?, ?, ?, 'RUNNING', ?, NULL, NULL, NULL, NULL,
                    '{}', NULL, ?, ?, NULL)
            """,
            (
                attempt_id,
                task_id,
                attempt_number,
                instance_id,
                now,
                now,
            ),
        )
        add_event(
            connection,
            plan_id=task["plan_id"],
            task_id=task_id,
            event_type="ORCHESTRATION_TASK_STARTED",
            severity="INFO",
            payload={
                "attempt_id": attempt_id,
                "attempt_number": attempt_number,
                "kind": task["kind"],
            },
        )
        connection.commit()

    return attempt_id, attempt_number, task


def heartbeat_attempt(
    task_id: str,
    attempt_id: str,
    stop_event: threading.Event,
    seconds: int,
) -> None:
    while not stop_event.wait(seconds):
        now = utc_now()
        try:
            with connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET heartbeat_at = ?
                    WHERE orchestration_task_id = ?
                      AND status = 'RUNNING'
                    """,
                    (now, task_id),
                )
                connection.execute(
                    """
                    UPDATE orchestration_attempts
                    SET heartbeat_at = ?
                    WHERE attempt_id = ?
                      AND status = 'RUNNING'
                    """,
                    (now, attempt_id),
                )
                connection.execute(
                    """
                    UPDATE orchestration_plans
                    SET heartbeat_at = ?
                    WHERE plan_id = (
                        SELECT plan_id
                        FROM orchestration_tasks
                        WHERE orchestration_task_id = ?
                    )
                    """,
                    (now, task_id),
                )
                connection.commit()
        except sqlite3.Error:
            continue


def set_attempt_links(
    attempt_id: str,
    *,
    run_id: str | None = None,
    worker_execution_id: str | None = None,
    review_execution_id: str | None = None,
    integration_id: str | None = None,
) -> None:
    assignments: list[str] = []
    parameters: list[Any] = []

    for column, value in (
        ("run_id", run_id),
        ("worker_execution_id", worker_execution_id),
        ("review_execution_id", review_execution_id),
        ("integration_id", integration_id),
    ):
        if value is not None:
            assignments.append(f"{column} = ?")
            parameters.append(value)

    if not assignments:
        return

    assignments.append("heartbeat_at = ?")
    parameters.append(utc_now())
    parameters.append(attempt_id)

    with connect() as connection:
        connection.execute(
            f"""
            UPDATE orchestration_attempts
            SET {', '.join(assignments)}
            WHERE attempt_id = ?
            """,
            tuple(parameters),
        )
        connection.commit()


def plan_is_waiting_human(
    connection: sqlite3.Connection,
    plan_id: str,
) -> bool:
    row = connection.execute(
        "SELECT status, last_error FROM orchestration_plans WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    return bool(
        row is not None
        and plan_has_active_human_gate(connection, plan_id)
        and row["status"] == "BLOCKED"
        and row["last_error"] == HUMAN_GATE_ERROR
    )


def plan_has_pending_human_gate(
    connection: sqlite3.Connection,
    plan_id: str,
) -> bool:
    row = connection.execute(
        "SELECT last_error FROM orchestration_plans WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    return bool(
        row is not None
        and plan_has_active_human_gate(connection, plan_id)
        and row["last_error"] == HUMAN_GATE_DEFERRED_ERROR
    )


def plan_has_active_human_gate(
    connection: sqlite3.Connection,
    plan_id: str,
) -> bool:
    return bool(
        connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM approvals AS approval
                JOIN orchestration_attempts AS attempt
                  ON attempt.run_id=approval.run_id
                JOIN orchestration_tasks AS task
                  ON task.orchestration_task_id=attempt.orchestration_task_id
                WHERE task.plan_id=?
                  AND approval.status='PENDING'
            )
            """,
            (plan_id,),
        ).fetchone()[0]
    )


def plan_has_authorized_integration_in_flight(
    connection: sqlite3.Connection,
    plan_id: str,
) -> bool:
    return bool(
        connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM orchestration_tasks AS task
                JOIN orchestration_attempts AS attempt
                  ON attempt.orchestration_task_id=task.orchestration_task_id
                JOIN runs AS run ON run.run_id=attempt.run_id
                JOIN integration_executions AS integration
                  ON integration.run_id=run.run_id
                WHERE task.plan_id=?
                  AND (
                        run.status IN ('COMMITTING', 'RECOVERING')
                        OR (
                            run.status='COMPLETED'
                            AND task.status='RUNNING'
                        )
                  )
                  AND integration.decision='APPROVE'
                  AND integration.status IN ('PREPARED', 'COMPLETED', 'FAILED')
            )
            """,
            (plan_id,),
        ).fetchone()[0]
    )


def finish_running_task_blocked_by_human_gate(
    connection: sqlite3.Connection,
    task: sqlite3.Row,
    attempt_id: str,
    *,
    attempt_status: str,
    result_json: str | None,
    failure_reason: str | None,
    now: str,
) -> None:
    connection.execute(
        """
        UPDATE orchestration_attempts
        SET status = ?,
            result_json = COALESCE(?, result_json),
            failure_reason = ?,
            heartbeat_at = ?,
            finished_at = ?
        WHERE attempt_id = ?
          AND status = 'RUNNING'
        """,
        (
            attempt_status,
            result_json,
            failure_reason,
            now,
            now,
            attempt_id,
        ),
    )
    connection.execute(
        """
        UPDATE orchestration_tasks
        SET status = 'BLOCKED',
            result_json = COALESCE(?, result_json),
            failure_reason = 'waiting for human decision',
            heartbeat_at = ?,
            finished_at = ?
        WHERE orchestration_task_id = ?
          AND status = 'RUNNING'
        """,
        (
            result_json,
            now,
            now,
            task["orchestration_task_id"],
        ),
    )
    add_event(
        connection,
        plan_id=task["plan_id"],
        task_id=task["orchestration_task_id"],
        event_type="ORCHESTRATION_TASK_BLOCKED_HUMAN",
        severity="WARNING",
        payload={
            "attempt_id": attempt_id,
            "parallel_gate": True,
            "attempt_status": attempt_status,
        },
    )


def finish_task_success(
    task: sqlite3.Row,
    attempt_id: str,
    result: dict[str, Any],
) -> None:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result_json = canonical_json(result)
        human_gate = plan_has_active_human_gate(connection, task["plan_id"])
        authorized_integration = bool(
            result.get("kind") == "PIPELINE"
            and result.get("integration", {}).get("integrated")
        )
        if human_gate and not authorized_integration:
            finish_running_task_blocked_by_human_gate(
                connection,
                task,
                attempt_id,
                attempt_status="COMPLETED",
                result_json=result_json,
                failure_reason=None,
                now=now,
            )
            connection.commit()
            return
        connection.execute(
            """
            UPDATE orchestration_attempts
            SET status = 'COMPLETED',
                result_json = ?,
                heartbeat_at = ?,
                finished_at = ?
            WHERE attempt_id = ?
              AND status = 'RUNNING'
            """,
            (result_json, now, now, attempt_id),
        )
        connection.execute(
            """
            UPDATE orchestration_tasks
            SET status = 'COMPLETED',
                result_json = ?,
                failure_reason = NULL,
                heartbeat_at = ?,
                finished_at = ?
            WHERE orchestration_task_id = ?
              AND status = 'RUNNING'
            """,
            (
                result_json,
                now,
                now,
                task["orchestration_task_id"],
            ),
        )
        add_event(
            connection,
            plan_id=task["plan_id"],
            task_id=task["orchestration_task_id"],
            event_type="ORCHESTRATION_TASK_COMPLETED",
            severity="INFO",
            payload={"attempt_id": attempt_id},
        )
        connection.commit()


def finish_task_waiting_human(
    task: sqlite3.Row,
    attempt_id: str,
    result: dict[str, Any],
) -> None:
    now = utc_now()
    result_json = canonical_json(result)

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        cancellation_requested = bool(
            connection.execute(
                """
                SELECT EXISTS (
                    SELECT 1 FROM objective_queue
                    WHERE plan_id=?
                      AND status IN ('CANCEL_REQUESTED', 'CANCELLED')
                )
                """,
                (task["plan_id"],),
            ).fetchone()[0]
        )
        if cancellation_requested:
            connection.execute(
                """
                UPDATE orchestration_attempts
                SET status='COMPLETED', result_json=?, heartbeat_at=?, finished_at=?
                WHERE attempt_id=? AND status='RUNNING'
                """,
                (result_json, now, now, attempt_id),
            )
            connection.execute(
                """
                UPDATE orchestration_tasks
                SET status='BLOCKED',
                    result_json=?,
                    failure_reason=CASE
                        WHEN failure_reason=? THEN failure_reason
                        ELSE ?
                    END,
                    heartbeat_at=?, finished_at=?
                WHERE orchestration_task_id=?
                  AND status IN ('RUNNING', 'BLOCKED')
                """,
                (
                    result_json,
                    CANCELLATION_RECOVERY_ERROR,
                    CANCELLATION_PENDING_ERROR,
                    now,
                    now,
                    task["orchestration_task_id"],
                ),
            )
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status='BLOCKED',
                    heartbeat_at=?,
                    last_error=CASE
                        WHEN last_error=? THEN last_error
                        ELSE ?
                    END
                WHERE plan_id=?
                  AND status IN ('READY', 'RUNNING', 'BLOCKED')
                """,
                (
                    now,
                    CANCELLATION_RECOVERY_ERROR,
                    CANCELLATION_PENDING_ERROR,
                    task["plan_id"],
                ),
            )
            add_event(
                connection,
                plan_id=task["plan_id"],
                task_id=task["orchestration_task_id"],
                event_type="ORCHESTRATION_HUMAN_GATE_SUPERSEDED_BY_CANCELLATION",
                severity="WARNING",
                payload={"attempt_id": attempt_id},
            )
            connection.commit()
            return
        deferred = plan_has_authorized_integration_in_flight(
            connection,
            task["plan_id"],
        )
        connection.execute(
            """
            UPDATE orchestration_attempts
            SET status = 'COMPLETED',
                result_json = ?,
                heartbeat_at = ?,
                finished_at = ?
            WHERE attempt_id = ?
              AND status = 'RUNNING'
            """,
            (result_json, now, now, attempt_id),
        )
        connection.execute(
            """
            UPDATE orchestration_tasks
            SET status = 'BLOCKED',
                result_json = CASE
                    WHEN orchestration_task_id = ? THEN ?
                    ELSE result_json
                END,
                failure_reason = ?,
                heartbeat_at = ?,
                finished_at = ?
            WHERE plan_id = ?
              AND (
                    status IN ('PENDING', 'READY')
                    OR (
                        orchestration_task_id = ?
                        AND status = 'RUNNING'
                    )
              )
            """,
            (
                task["orchestration_task_id"],
                result_json,
                HUMAN_GATE_ERROR,
                now,
                now,
                task["plan_id"],
                task["orchestration_task_id"],
            ),
        )
        if deferred:
            connection.execute(
                """
                UPDATE orchestration_plans
                SET heartbeat_at = ?,
                    last_error = ?
                WHERE plan_id = ?
                  AND status IN ('READY', 'RUNNING')
                """,
                (now, HUMAN_GATE_DEFERRED_ERROR, task["plan_id"]),
            )
        else:
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status = 'BLOCKED',
                    heartbeat_at = ?,
                    last_error = ?
                WHERE plan_id = ?
                  AND status IN ('READY', 'RUNNING', 'BLOCKED')
                """,
                (now, HUMAN_GATE_ERROR, task["plan_id"]),
            )
        add_event(
            connection,
            plan_id=task["plan_id"],
            task_id=task["orchestration_task_id"],
            event_type="ORCHESTRATION_TASK_BLOCKED_HUMAN",
            severity="WARNING",
            payload={"attempt_id": attempt_id, "deferred": deferred},
        )
        connection.commit()


def finish_task_cancellation_recovery(
    task: sqlite3.Row,
    attempt_id: str,
    result: dict[str, Any],
) -> None:
    now = utc_now()
    result_json = canonical_json(result)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE orchestration_attempts
            SET status='COMPLETED', result_json=?, heartbeat_at=?, finished_at=?
            WHERE attempt_id=? AND status='RUNNING'
            """,
            (result_json, now, now, attempt_id),
        )
        connection.execute(
            """
            UPDATE orchestration_tasks
            SET status='BLOCKED',
                result_json=CASE WHEN orchestration_task_id=? THEN ? ELSE result_json END,
                failure_reason='cancellation cleanup requires recovery',
                heartbeat_at=?, finished_at=?
            WHERE plan_id=?
              AND (
                    status IN ('PENDING', 'READY')
                    OR (orchestration_task_id=? AND status='RUNNING')
              )
            """,
            (
                task["orchestration_task_id"],
                result_json,
                now,
                now,
                task["plan_id"],
                task["orchestration_task_id"],
            ),
        )
        connection.execute(
            """
            UPDATE orchestration_plans
            SET status='BLOCKED', heartbeat_at=?,
                last_error='cancellation cleanup requires recovery'
            WHERE plan_id=? AND status IN ('READY', 'RUNNING', 'BLOCKED')
            """,
            (now, task["plan_id"]),
        )
        add_event(
            connection,
            plan_id=task["plan_id"],
            task_id=task["orchestration_task_id"],
            event_type="ORCHESTRATION_CANCELLATION_RECOVERY_REQUIRED",
            severity="ERROR",
            payload={"attempt_id": attempt_id, "run_id": result.get("run_id")},
        )
        connection.commit()


def mark_cancellation_cleanup_recovery(run_id: str, reason: str) -> bool:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = connection.execute(
            """
            SELECT run.status,
                   EXISTS (
                       SELECT 1 FROM project_locks AS lock
                       WHERE lock.run_id=run.run_id
                   ) AS lock_present
            FROM runs AS run
            WHERE run.run_id=?
            """,
            (run_id,),
        ).fetchone()
        if run is None:
            connection.rollback()
            return False
        if run["status"] == "CANCELLED" and not run["lock_present"]:
            connection.rollback()
            return True
        if run["status"] == "COMPLETED":
            connection.rollback()
            return False
        connection.execute(
            """
            UPDATE integration_executions
            SET status='FAILED', failure_reason=COALESCE(failure_reason, ?),
                finished_at=COALESCE(finished_at, ?)
            WHERE run_id=? AND status='PREPARED'
            """,
            (reason, now, run_id),
        )
        connection.execute(
            """
            UPDATE runs
            SET status='RECOVERING', recovery_decision='ROLLBACK_SAFE',
                heartbeat_at=?
            WHERE run_id=?
            """,
            (now, run_id),
        )
        connection.execute(
            """
            UPDATE approvals
            SET status='CANCELLED', resolved_at=COALESCE(resolved_at, ?)
            WHERE run_id=? AND status='PENDING'
            """,
            (now, run_id),
        )
        connection.execute(
            """
            UPDATE orchestration_tasks
            SET status='BLOCKED', failure_reason=?, heartbeat_at=?, finished_at=?
            WHERE orchestration_task_id IN (
                SELECT orchestration_task_id
                FROM orchestration_attempts
                WHERE run_id=?
            )
              AND status IN ('PENDING', 'READY', 'RUNNING', 'BLOCKED')
            """,
            (CANCELLATION_RECOVERY_ERROR, now, now, run_id),
        )
        connection.execute(
            """
            UPDATE orchestration_plans
            SET status='BLOCKED', heartbeat_at=?, last_error=?
            WHERE plan_id IN (
                SELECT task.plan_id
                FROM orchestration_attempts AS attempt
                JOIN orchestration_tasks AS task
                  ON task.orchestration_task_id=attempt.orchestration_task_id
                WHERE attempt.run_id=?
            )
              AND status IN ('READY', 'RUNNING', 'BLOCKED')
            """,
            (now, CANCELLATION_RECOVERY_ERROR, run_id),
        )
        attempt = connection.execute(
            """
            SELECT task.plan_id, task.orchestration_task_id
            FROM orchestration_attempts AS attempt
            JOIN orchestration_tasks AS task
              ON task.orchestration_task_id=attempt.orchestration_task_id
            WHERE attempt.run_id=?
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()
        if attempt is not None:
            add_event(
                connection,
                plan_id=attempt["plan_id"],
                task_id=attempt["orchestration_task_id"],
                event_type="ORCHESTRATION_CANCELLATION_ROLLBACK_FAILED",
                severity="ERROR",
                payload={"run_id": run_id, "reason": reason},
            )
        connection.commit()
    return False


def rollback_run_best_effort(run_id: str | None, timeout: int) -> bool:
    if not run_id:
        return True

    try:
        with connect() as connection:
            run = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()

        if run is None or run["status"] not in ACTIVE_RUN_STATUSES | {"FAILED"}:
            return True
        result = run_command(
            [str(TRANSACTION), "rollback", "--run", run_id],
            timeout=timeout,
            check=False,
        )
        if result is None:
            return True
        if result.returncode == 0:
            with connect() as connection:
                final = connection.execute(
                    """
                    SELECT run.status,
                           EXISTS (
                               SELECT 1 FROM project_locks AS lock
                               WHERE lock.run_id=run.run_id
                           ) AS lock_present
                    FROM runs AS run
                    WHERE run.run_id=?
                    """,
                    (run_id,),
                ).fetchone()
            if (
                final is not None
                and final["status"] == "CANCELLED"
                and not final["lock_present"]
            ):
                return True
            return mark_cancellation_cleanup_recovery(
                run_id,
                "rollback reported success without terminal cleanup",
            )
        return mark_cancellation_cleanup_recovery(
            run_id,
            f"rollback exited with status {result.returncode}",
        )
    except Exception as error:
        return mark_cancellation_cleanup_recovery(run_id, str(error))


def cleanup_cancel_requested_human_runs(timeout: int) -> None:
    with connect() as connection:
        run_ids = [
            str(row[0])
            for row in connection.execute(
                """
                SELECT DISTINCT run.run_id
                FROM objective_queue AS objective
                JOIN orchestration_tasks AS task
                  ON task.plan_id=objective.plan_id
                JOIN orchestration_attempts AS attempt
                  ON attempt.orchestration_task_id=task.orchestration_task_id
                JOIN runs AS run ON run.run_id=attempt.run_id
                WHERE objective.status='CANCEL_REQUESTED'
                  AND run.status='WAITING_HUMAN'
                ORDER BY run.run_id
                """
            ).fetchall()
        ]

    for run_id in run_ids:
        rollback_run_best_effort(run_id, timeout)


def finish_task_failure(
    task: sqlite3.Row,
    attempt_id: str,
    error: str,
) -> None:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT attempt_count, max_attempts, status
            FROM orchestration_tasks
            WHERE orchestration_task_id = ?
            """,
            (task["orchestration_task_id"],),
        ).fetchone()

        if current is None:
            connection.rollback()
            return

        if plan_has_active_human_gate(connection, task["plan_id"]):
            finish_running_task_blocked_by_human_gate(
                connection,
                task,
                attempt_id,
                attempt_status="FAILED",
                result_json=None,
                failure_reason=error,
                now=now,
            )
            connection.commit()
            return

        connection.execute(
            """
            UPDATE orchestration_attempts
            SET status = 'FAILED',
                failure_reason = ?,
                heartbeat_at = ?,
                finished_at = ?
            WHERE attempt_id = ?
              AND status = 'RUNNING'
            """,
            (error, now, now, attempt_id),
        )

        retry = int(current["attempt_count"]) < int(current["max_attempts"])
        next_status = "READY" if retry else "FAILED"
        connection.execute(
            """
            UPDATE orchestration_tasks
            SET status = ?,
                failure_reason = ?,
                heartbeat_at = ?,
                finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END
            WHERE orchestration_task_id = ?
            """,
            (
                next_status,
                error,
                now,
                next_status,
                now,
                task["orchestration_task_id"],
            ),
        )
        add_event(
            connection,
            plan_id=task["plan_id"],
            task_id=task["orchestration_task_id"],
            event_type=(
                "ORCHESTRATION_TASK_RETRY"
                if retry
                else "ORCHESTRATION_TASK_FAILED"
            ),
            severity="WARNING" if retry else "ERROR",
            payload={
                "attempt_id": attempt_id,
                "failure_reason": error,
                "retry": retry,
            },
        )
        connection.commit()


def read_log_tail(path_value: str | None, limit: int = 32768) -> str:
    if not path_value:
        return ""

    path = Path(path_value)
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - limit), os.SEEK_SET)
            return stream.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def latest_reviewer_evidence(run_id: str) -> dict[str, Any]:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                execution_id,
                task_id,
                runtime_profile,
                outer_container_name,
                sandbox_container_id,
                output_path,
                failure_reason,
                exit_code,
                created_at,
                finished_at
            FROM reviewer_executions
            WHERE run_id = ?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

    if row is None:
        return {}

    evidence = dict(row)
    evidence["log_tail"] = read_log_tail(evidence.get("output_path"))
    return evidence


def is_transient_review_transport_failure(
    error: BaseException,
    evidence: dict[str, Any],
) -> bool:
    combined = "\n".join(
        str(value)
        for value in (
            error,
            evidence.get("failure_reason"),
            evidence.get("log_tail"),
        )
        if value
    ).lower()

    exact_signatures = (
        "codex stream produced no sse events",
        "connection reset by peer",
        "server disconnected",
        "temporarily unavailable",
        "service unavailable",
        "gateway timeout",
        "bad gateway",
        "too many requests",
        "rate limit",
    )
    return any(signature in combined for signature in exact_signatures)


def contained_path(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def owned_runtime_container(
    name: str,
    *,
    timeout: int,
) -> str | None:
    """Return the immutable ID of an explicitly owned runtime container."""
    if not re.fullmatch(r"agent-runtime-[a-f0-9]{12}", name):
        return None
    inspected = run_command(
        ["docker", "container", "inspect", name],
        timeout=timeout,
        check=False,
    )
    if inspected.returncode != 0:
        return None
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) != 1:
        return None
    container = payload[0]
    if not isinstance(container, dict):
        return None
    actual_name = str(container.get("Name") or "").removeprefix("/")
    container_id = str(container.get("Id") or "")
    if (
        actual_name != name
        or re.fullmatch(r"[a-f0-9]{64}", container_id) is None
    ):
        return None
    labels = ((container.get("Config") or {}).get("Labels") or {})
    owned = (
        labels.get("orchestra-runtime-container") == "1"
        and labels.get("orchestra-runtime-request-id") == name
    )
    return container_id if owned else None


def owned_reviewer_sandbox(
    container_id: str,
    evidence: dict[str, Any],
    *,
    timeout: int,
) -> str | None:
    """Return the immutable ID of an owned generic or historical sandbox."""
    inspected = run_command(
        ["docker", "container", "inspect", container_id],
        timeout=timeout,
        check=False,
    )
    if inspected.returncode != 0:
        return None
    try:
        payload = json.loads(inspected.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) != 1:
        return None
    container = payload[0]
    if not isinstance(container, dict):
        return None
    actual_id = str(container.get("Id") or "")
    if (
        re.fullmatch(r"[a-f0-9]{64}", actual_id) is None
        or not actual_id.startswith(container_id)
    ):
        return None
    labels = ((container.get("Config") or {}).get("Labels") or {})
    task_id = str(evidence.get("task_id") or "")
    outer = str(evidence.get("outer_container_name") or "")
    profile = str(evidence.get("runtime_profile") or "")
    owned = (
        labels.get("orchestra-sandbox") == "1"
        and re.fullmatch(r"[a-f0-9]{64}", container_id) is not None
        and labels.get("orchestra-task-id") == task_id
        and labels.get("orchestra-runtime-request-id") == outer
    )
    return actual_id if owned else None


def cleanup_reviewer_resources(
    evidence: dict[str, Any],
    *,
    timeout: int,
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    def record(arguments: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            result = run_command(
                arguments,
                timeout=timeout,
                check=False,
            )
        except Exception as error:
            result = subprocess.CompletedProcess(
                arguments,
                1,
                "",
                str(error),
            )

        actions.append(
            {
                "arguments": arguments,
                "returncode": result.returncode,
                "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(),
            }
        )
        return result

    outer = str(
        evidence.get("outer_container_name")
        or evidence.get("outer_container")
        or ""
    )
    outer_id = owned_runtime_container(outer, timeout=timeout)
    if outer_id is not None:
        record(["docker", "rm", "-f", outer_id])

    sandbox = str(evidence.get("sandbox_container_id") or "")
    sandbox_id = (
        owned_reviewer_sandbox(sandbox, evidence, timeout=timeout)
        if re.fullmatch(r"[a-f0-9]{12,64}", sandbox)
        else None
    )
    if sandbox_id is not None:
        record(["docker", "rm", "-f", sandbox_id])

    profile = str(evidence.get("runtime_profile") or "")
    current_profile = re.fullmatch(
        r"agent-runtime-[a-f0-9]{12}",
        profile,
    )
    if current_profile:
        profile_root = ROOT / "state/hermes-home/profiles"
        profile_path = profile_root / profile
        if contained_path(profile_path, profile_root):
            shutil.rmtree(profile_path, ignore_errors=True)
            actions.append(
                {
                    "action": "remove_runtime_profile",
                    "path": str(profile_path),
                }
            )

    task_id = str(evidence.get("task_id") or "")
    if re.fullmatch(r"task-[a-f0-9]{32}", task_id):
        clone_root = ROOT / "workspaces/.orchestra-reviewer-clones"
        if clone_root.is_dir():
            for candidate in clone_root.rglob(task_id):
                if (
                    candidate.name == task_id
                    and contained_path(candidate, clone_root)
                ):
                    shutil.rmtree(candidate, ignore_errors=True)
                    actions.append(
                        {
                            "action": "remove_reviewer_clone",
                            "path": str(candidate),
                        }
                    )

    return actions


def record_review_retry(
    task: sqlite3.Row,
    *,
    attempt_id: str,
    run_id: str,
    review_attempt: int,
    evidence: dict[str, Any],
    cleanup_actions: list[dict[str, Any]],
) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        add_event(
            connection,
            plan_id=task["plan_id"],
            task_id=task["orchestration_task_id"],
            event_type="ORCHESTRATION_REVIEW_TRANSPORT_RETRY",
            severity="WARNING",
            payload={
                "attempt_id": attempt_id,
                "run_id": run_id,
                "review_attempt": review_attempt,
                "review_execution_id": evidence.get("execution_id"),
                "review_task_id": evidence.get("task_id"),
                "failure_reason": evidence.get("failure_reason"),
                "output_path": evidence.get("output_path"),
                "cleanup_actions": cleanup_actions,
            },
        )
        connection.commit()


def launch_reviewer_with_transport_retry(
    task: sqlite3.Row,
    *,
    attempt_id: str,
    run_id: str,
    review_path: Path,
    config: dict[str, int],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    marker = "ORCHESTRA_ORCHESTRATION_REVIEW_OK"
    failures: list[dict[str, Any]] = []
    attempts = config["review_transport_attempts"]

    for review_attempt in range(1, attempts + 1):
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            assignment_number = ASSIGNMENTS.next_assignment_number(
                connection,
                run_id=run_id,
            )
            assignment = ASSIGNMENTS.create_assignment(
                connection,
                run_id=run_id,
                orchestration_attempt_id=attempt_id,
                assignment_number=assignment_number,
                role_id="reviewer",
                assigned_by=f"orchestrator:{attempt_id}",
            )
            connection.commit()

        assignment_id = str(assignment["assignment_id"])
        arguments = [
            str(REVIEWER),
            "launch",
            "--run",
            run_id,
            "--role",
            "reviewer",
            "--assignment",
            assignment_id,
            "--instruction-file",
            str(review_path),
            "--marker",
            marker,
            "--timeout",
            str(config["review_timeout_seconds"]),
        ]

        try:
            reviewer = run_json(
                arguments,
                timeout=config["review_timeout_seconds"] + 120,
            )
            if reviewer.get("assignment_id") != assignment_id:
                fail("Reviewer returned a mismatched assignment identifier")
            evidence = latest_reviewer_evidence(run_id)
            cleanup_actions = cleanup_reviewer_resources(
                evidence,
                timeout=config["command_timeout_seconds"],
            )
            return reviewer, failures, cleanup_actions
        except CommandExecutionError as error:
            with connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                ASSIGNMENTS.fail_active_assignment(
                    connection,
                    assignment_id=assignment_id,
                    actor_id=f"orchestrator:{attempt_id}",
                    failure_code="REVIEW_TRANSPORT_FAILED",
                )
                connection.commit()

            evidence = latest_reviewer_evidence(run_id)
            transient = is_transient_review_transport_failure(error, evidence)
            failure = {
                "review_attempt": review_attempt,
                "assignment_id": assignment_id,
                "assignment_number": assignment_number,
                "transient": transient,
                "command_returncode": error.returncode,
                "command_stderr": error.stderr.strip(),
                "review_execution_id": evidence.get("execution_id"),
                "review_task_id": evidence.get("task_id"),
                "failure_reason": evidence.get("failure_reason"),
                "output_path": evidence.get("output_path"),
                "log_tail": evidence.get("log_tail"),
            }

            if not transient:
                raise

            cleanup_actions = cleanup_reviewer_resources(
                evidence,
                timeout=config["command_timeout_seconds"],
            )
            failure["cleanup_actions"] = cleanup_actions
            failures.append(failure)

            if review_attempt >= attempts:
                fail(
                    "Reviewer transport failed after "
                    f"{attempts} controlled attempts: "
                    + canonical_json(failures)
                )

            record_review_retry(
                task,
                attempt_id=attempt_id,
                run_id=run_id,
                review_attempt=review_attempt,
                evidence=evidence,
                cleanup_actions=cleanup_actions,
            )
            time.sleep(
                config["review_retry_backoff_seconds"] * review_attempt
            )
        except Exception:
            with connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                ASSIGNMENTS.fail_active_assignment(
                    connection,
                    assignment_id=assignment_id,
                    actor_id=f"orchestrator:{attempt_id}",
                    failure_code="REVIEW_LAUNCH_FAILED",
                )
                connection.commit()
            raise

    fail("Reviewer transport retry loop exited unexpectedly")


def build_review_instruction(task: sqlite3.Row) -> str:
    acceptance = json.loads(task["acceptance_json"])
    numbered = "\n".join(
        f"{index}. {criterion}"
        for index, criterion in enumerate(acceptance, start=1)
    )
    return f"""Review the exact transaction result for orchestration task {task['task_key']}.

Acceptance criteria:
{numbered}

Use read-only Git commands and verify the actual diff, commit ancestry,
repository cleanliness, branch identity, and absence of unrelated changes or
remotes. Return APPROVE/PASS only when every criterion is satisfied. Return
REJECT with the appropriate non-pass verdict for a correctable defect, or
BLOCK_HUMAN/HUMAN for an ambiguity that cannot be resolved safely.
"""


def execute_pipeline(
    task: sqlite3.Row,
    attempt_id: str,
    instance_id: str,
    config: dict[str, int],
) -> dict[str, Any]:
    task_directory = (
        RUNTIME
        / "plans"
        / task["plan_id"]
        / task["task_key"]
        / attempt_id
    )
    task_directory.mkdir(parents=True, mode=0o750)
    instruction_path = task_directory / "worker-instruction.txt"
    review_path = task_directory / "review-instruction.txt"
    instruction_path.write_text(task["instruction"] + "\n", encoding="utf-8")
    instruction_path.chmod(0o600)
    review_path.write_text(build_review_instruction(task), encoding="utf-8")
    review_path.chmod(0o600)

    owner = f"orchestrator:{instance_id}:{task['task_key']}"
    begin = run_json(
        [
            str(TRANSACTION),
            "begin",
            "--project",
            task["project_id"],
            "--owner",
            owner,
            "--metadata-json",
            canonical_json(
                {
                    "plan_id": task["plan_id"],
                    "orchestration_task_id": task["orchestration_task_id"],
                    "task_key": task["task_key"],
                    "attempt_id": attempt_id,
                }
            ),
        ],
        timeout=config["command_timeout_seconds"],
    )
    run_id = begin["run_id"]
    set_attempt_links(attempt_id, run_id=run_id)

    try:
        worker = run_json(
            [
                str(WORKER),
                "launch",
                "--run",
                run_id,
                "--role",
                task["role_id"],
                "--instruction-file",
                str(instruction_path),
                "--marker",
                task["marker"],
                "--timeout",
                str(config["worker_timeout_seconds"]),
            ],
            timeout=config["worker_timeout_seconds"] + 120,
        )
        set_attempt_links(
            attempt_id,
            worker_execution_id=worker["execution_id"],
        )

        submitted = run_json(
            [str(TRANSACTION), "submit", "--run", run_id],
            timeout=config["command_timeout_seconds"],
        )

        reviewer, review_transport_failures, reviewer_cleanup = (
            launch_reviewer_with_transport_retry(
                task,
                attempt_id=attempt_id,
                run_id=run_id,
                review_path=review_path,
                config=config,
            )
        )
        set_attempt_links(
            attempt_id,
            review_execution_id=reviewer["execution_id"],
        )

        # Avoid review-to-integration work after cancellation. The integrator
        # repeats this check transactionally; this early barrier is not the
        # authoritative race protection.
        if objective_cancellation_requested(task["plan_id"]):
            integration = {
                "integration_id": None,
                "run_id": run_id,
                "action": "CANCEL",
                "status": "CANCELLED",
                "integrated": False,
                "reason_code": "objective_cancel_requested",
            }
            cleanup_complete = rollback_run_best_effort(
                run_id,
                config["command_timeout_seconds"],
            )
            if cleanup_complete is False:
                integration["status"] = "RECOVERING"
                integration["cleanup_completed"] = False
            return {
                "kind": "PIPELINE",
                "run_id": run_id,
                "worker": worker,
                "submitted": submitted,
                "reviewer": reviewer,
                "review_transport_failures": review_transport_failures,
                "reviewer_cleanup": reviewer_cleanup,
                "integration": integration,
            }

        if plan_waiting_human_requested(task["plan_id"]):
            integration = {
                "integration_id": None,
                "run_id": run_id,
                "action": "BLOCK_HUMAN",
                "status": "BLOCKED",
                "integrated": False,
                "reason_code": "plan_waiting_human",
            }
            cleanup_complete = rollback_run_best_effort(
                run_id,
                config["command_timeout_seconds"],
            )
            integration["cleanup_completed"] = cleanup_complete is not False
            return {
                "kind": "PIPELINE",
                "run_id": run_id,
                "worker": worker,
                "submitted": submitted,
                "reviewer": reviewer,
                "review_transport_failures": review_transport_failures,
                "reviewer_cleanup": reviewer_cleanup,
                "integration": integration,
            }

        integration = run_json(
            [
                str(INTEGRATOR),
                "apply",
                "--run",
                run_id,
                "--owner",
                owner,
            ],
            timeout=config["command_timeout_seconds"],
        )
        if integration.get("integration_id") is not None:
            set_attempt_links(
                attempt_id,
                integration_id=integration["integration_id"],
            )

        if (
            integration.get("action") == "CANCEL"
            and integration.get("status") == "CANCELLED"
            and not integration.get("integrated")
        ):
            cleanup_complete = rollback_run_best_effort(
                run_id,
                config["command_timeout_seconds"],
            )
            if cleanup_complete is False:
                integration = dict(integration)
                integration["status"] = "RECOVERING"
                integration["cleanup_completed"] = False
            return {
                "kind": "PIPELINE",
                "run_id": run_id,
                "worker": worker,
                "submitted": submitted,
                "reviewer": reviewer,
                "review_transport_failures": review_transport_failures,
                "reviewer_cleanup": reviewer_cleanup,
                "integration": integration,
            }

        # A human gate is a stable operational outcome, not a technical
        # pipeline failure. Returning it bypasses the exception rollback path.
        if (
            integration.get("action") == "BLOCK_HUMAN"
            and integration.get("status") == "BLOCKED"
            and not integration.get("integrated")
        ):
            if integration.get("reason_code") == "plan_waiting_human":
                cleanup_complete = rollback_run_best_effort(
                    run_id,
                    config["command_timeout_seconds"],
                )
                integration = dict(integration)
                integration["cleanup_completed"] = cleanup_complete is not False
            return {
                "kind": "PIPELINE",
                "run_id": run_id,
                "worker": worker,
                "submitted": submitted,
                "reviewer": reviewer,
                "review_transport_failures": review_transport_failures,
                "reviewer_cleanup": reviewer_cleanup,
                "integration": integration,
            }

        if integration.get("status") != "COMPLETED" or not integration.get("integrated"):
            fail(
                "Pipeline was not integrated: "
                + canonical_json(integration)
            )

        return {
            "kind": "PIPELINE",
            "run_id": run_id,
            "worker": worker,
            "submitted": submitted,
            "reviewer": reviewer,
            "review_transport_failures": review_transport_failures,
            "reviewer_cleanup": reviewer_cleanup,
            "integration": integration,
        }
    except Exception:
        rollback_run_best_effort(run_id, config["command_timeout_seconds"])
        raise


def execute_task(
    task_id: str,
    *,
    instance_id: str,
    config: dict[str, int],
) -> None:
    attempt_id, _, task = reserve_attempt(
        task_id,
        instance_id=instance_id,
    )
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat_attempt,
        args=(
            task["orchestration_task_id"],
            attempt_id,
            heartbeat_stop,
            config["heartbeat_seconds"],
        ),
        daemon=True,
    )
    heartbeat_thread.start()

    try:
        kind = task["kind"]

        if kind == "NOOP":
            result = {"kind": kind, "completed_at": utc_now()}
        elif kind == "TEST_SLEEP":
            with connect() as connection:
                plan_row = connection.execute(
                    "SELECT plan_json FROM orchestration_plans WHERE plan_id = ?",
                    (task["plan_id"],),
                ).fetchone()
            if plan_row is None:
                fail(f"Unknown plan during task execution: {task['plan_id']}")
            plan = json.loads(plan_row[0])
            raw = next(
                item for item in plan["tasks"]
                if item["key"] == task["task_key"]
            )
            duration = int(raw["duration_seconds"])
            time.sleep(duration)
            result = {
                "kind": kind,
                "duration_seconds": duration,
                "completed_at": utc_now(),
            }
        elif kind == "TEST_FAIL":
            fail(f"Synthetic failure requested by task {task['task_key']}")
        elif kind == "PIPELINE":
            result = execute_pipeline(
                task,
                attempt_id,
                instance_id,
                config,
            )
        else:
            fail(f"Unhandled task kind: {kind}")

        if (
            result.get("kind") == "PIPELINE"
            and result.get("integration", {}).get("action") == "CANCEL"
            and result.get("integration", {}).get("status") == "RECOVERING"
        ):
            finish_task_cancellation_recovery(task, attempt_id, result)
        elif (
            result.get("kind") == "PIPELINE"
            and result.get("integration", {}).get("action") == "BLOCK_HUMAN"
        ):
            finish_task_waiting_human(task, attempt_id, result)
        else:
            finish_task_success(task, attempt_id, result)
    except Exception as error:
        finish_task_failure(task, attempt_id, str(error))
    finally:
        heartbeat_stop.set()
        heartbeat_thread.join(timeout=2)
        refresh_plan_states(task["plan_id"])


def reconcile_interrupted_tasks(instance_id: str) -> None:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        tasks = connection.execute(
            """
            SELECT t.*, a.attempt_id, a.run_id, a.attempt_number
            FROM orchestration_tasks AS t
            LEFT JOIN orchestration_attempts AS a
              ON a.orchestration_task_id = t.orchestration_task_id
             AND a.status = 'RUNNING'
            WHERE t.status = 'RUNNING'
            """
        ).fetchall()

        for task in tasks:
            if task["attempt_id"] is None:
                next_status = (
                    "READY"
                    if task["attempt_count"] < task["max_attempts"]
                    else "FAILED"
                )
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = ?,
                        failure_reason = 'missing running attempt after restart',
                        heartbeat_at = ?,
                        finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END
                    WHERE orchestration_task_id = ?
                    """,
                    (
                        next_status,
                        now,
                        next_status,
                        now,
                        task["orchestration_task_id"],
                    ),
                )
                continue

            if task["kind"] != "PIPELINE":
                connection.execute(
                    """
                    UPDATE orchestration_attempts
                    SET status = 'ABANDONED',
                        failure_reason = 'orchestrator process restarted',
                        heartbeat_at = ?,
                        finished_at = ?
                    WHERE attempt_id = ?
                      AND status = 'RUNNING'
                    """,
                    (now, now, task["attempt_id"]),
                )
                retry = task["attempt_count"] < task["max_attempts"]
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = ?,
                        failure_reason = 'rescheduled after orchestrator restart',
                        heartbeat_at = ?,
                        finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END
                    WHERE orchestration_task_id = ?
                    """,
                    (
                        "READY" if retry else "FAILED",
                        now,
                        "READY" if retry else "FAILED",
                        now,
                        task["orchestration_task_id"],
                    ),
                )
                continue

            if task["run_id"] is None:
                connection.execute(
                    """
                    UPDATE orchestration_attempts
                    SET status = 'ABANDONED',
                        failure_reason = 'pipeline restart before transaction reservation',
                        heartbeat_at = ?,
                        finished_at = ?
                    WHERE attempt_id = ?
                    """,
                    (now, now, task["attempt_id"]),
                )
                retry = task["attempt_count"] < task["max_attempts"]
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = ?,
                        failure_reason = 'pipeline rescheduled after restart',
                        heartbeat_at = ?
                    WHERE orchestration_task_id = ?
                    """,
                    (
                        "READY" if retry else "FAILED",
                        now,
                        task["orchestration_task_id"],
                    ),
                )
                continue

            run = connection.execute(
                "SELECT status FROM runs WHERE run_id = ?",
                (task["run_id"],),
            ).fetchone()

            if run is None or run["status"] in {"CANCELLED", "FAILED"}:
                connection.execute(
                    """
                    UPDATE orchestration_attempts
                    SET status = 'ABANDONED',
                        failure_reason = 'pipeline run is terminal after restart',
                        heartbeat_at = ?,
                        finished_at = ?
                    WHERE attempt_id = ?
                    """,
                    (now, now, task["attempt_id"]),
                )
                retry = task["attempt_count"] < task["max_attempts"]
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = ?,
                        failure_reason = 'pipeline run did not complete',
                        heartbeat_at = ?,
                        finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END
                    WHERE orchestration_task_id = ?
                    """,
                    (
                        "READY" if retry else "FAILED",
                        now,
                        "READY" if retry else "FAILED",
                        now,
                        task["orchestration_task_id"],
                    ),
                )
            elif run["status"] == "COMPLETED":
                integration = connection.execute(
                    """
                    SELECT integration_id, result_json
                    FROM orchestration_attempts
                    WHERE attempt_id = ?
                    """,
                    (task["attempt_id"],),
                ).fetchone()
                result = {
                    "kind": "PIPELINE",
                    "run_id": task["run_id"],
                    "reconciled_after_restart": True,
                    "integration_id": (
                        integration["integration_id"]
                        if integration else None
                    ),
                }
                connection.execute(
                    """
                    UPDATE orchestration_attempts
                    SET status = 'COMPLETED',
                        result_json = ?,
                        heartbeat_at = ?,
                        finished_at = ?
                    WHERE attempt_id = ?
                    """,
                    (
                        canonical_json(result),
                        now,
                        now,
                        task["attempt_id"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE orchestration_tasks
                    SET status = 'COMPLETED',
                        result_json = ?,
                        failure_reason = NULL,
                        heartbeat_at = ?,
                        finished_at = ?
                    WHERE orchestration_task_id = ?
                    """,
                    (
                        canonical_json(result),
                        now,
                        now,
                        task["orchestration_task_id"],
                    ),
                )
            else:
                # The deterministic Recovery Manager owns active pipeline
                # reconciliation. Keep the task RUNNING and never duplicate it.
                connection.execute(
                    """
                    UPDATE orchestration_attempts
                    SET executor_instance_id = ?,
                        heartbeat_at = ?
                    WHERE attempt_id = ?
                    """,
                    (instance_id, now, task["attempt_id"]),
                )

        connection.commit()

    with connect() as connection:
        plan_ids = [
            row[0]
            for row in connection.execute(
                """
                SELECT DISTINCT plan_id
                FROM orchestration_tasks
                WHERE status IN ('PENDING', 'READY', 'RUNNING', 'BLOCKED', 'FAILED')
                """
            ).fetchall()
        ]

    for plan_id in plan_ids:
        refresh_plan_states(plan_id)


def supervisor_is_healthy() -> bool:
    try:
        payload = json.loads(
            SUPERVISOR_STATUS_PATH.read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return False

    return bool(
        payload.get("lock_held")
        and (payload.get("health") or {}).get("healthy")
    )



def future_utc(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def add_objective_event(
    connection: sqlite3.Connection,
    *,
    objective_id: str,
    event_type: str,
    old_status: str | None,
    new_status: str | None,
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO objective_events (
            objective_event_id,
            objective_id,
            event_type,
            old_status,
            new_status,
            payload_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "objective-event-" + uuid.uuid4().hex,
            objective_id,
            event_type,
            old_status,
            new_status,
            canonical_json(payload or {}),
            utc_now(),
        ),
    )


def objective_running_tasks(
    connection: sqlite3.Connection,
    plan_id: str | None,
) -> int:
    if plan_id is None:
        return 0
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM orchestration_tasks
            WHERE plan_id = ? AND status = 'RUNNING'
            """,
            (plan_id,),
        ).fetchone()[0]
    )


def cancellation_cleanup_pending(
    connection: sqlite3.Connection,
    plan_id: str | None,
) -> bool:
    if plan_id is None:
        return False
    return bool(
        connection.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM orchestration_attempts AS attempt
                JOIN orchestration_tasks AS task
                  ON task.orchestration_task_id=attempt.orchestration_task_id
                JOIN runs AS run ON run.run_id=attempt.run_id
                WHERE task.plan_id=?
                  AND run.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
            )
            """,
            (plan_id,),
        ).fetchone()[0]
    )


def cancel_objective_plan(
    connection: sqlite3.Connection,
    plan_id: str | None,
    now: str,
) -> None:
    if plan_id is None:
        return
    connection.execute(
        """
        UPDATE orchestration_tasks
        SET status = 'CANCELLED',
            heartbeat_at = ?,
            finished_at = ?,
            failure_reason = COALESCE(failure_reason, 'objective cancelled')
        WHERE plan_id = ?
          AND status IN ('PENDING', 'READY', 'BLOCKED')
        """,
        (now, now, plan_id),
    )
    connection.execute(
        """
        UPDATE orchestration_plans
        SET status = 'CANCELLED',
            heartbeat_at = ?,
            finished_at = ?,
            last_error = 'objective cancelled'
        WHERE plan_id = ?
          AND status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
        """,
        (now, now, plan_id),
    )


def active_objective_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            """
            SELECT COUNT(*)
            FROM objective_queue
            WHERE status IN (
                'PLANNING',
                'RUNNING',
                'PAUSE_REQUESTED',
                'CANCEL_REQUESTED'
            )
            """
        ).fetchone()[0]
    )


def synchronize_objective_states() -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT objective.*, plan.status AS plan_status
            FROM objective_queue AS objective
            LEFT JOIN orchestration_plans AS plan
              ON plan.plan_id = objective.plan_id
            WHERE objective.status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
            ORDER BY objective.created_at
            """
        ).fetchall()

        for row in rows:
            objective_id = row["objective_id"]
            old = row["status"]
            plan_id = row["plan_id"]
            running = objective_running_tasks(connection, plan_id)
            new: str | None = None
            error: str | None = None

            if old == "CANCEL_REQUESTED":
                if running == 0 and not cancellation_cleanup_pending(
                    connection,
                    plan_id,
                ):
                    cancel_objective_plan(connection, plan_id, now)
                    new = "CANCELLED"
                    error = "cancelled by operator"
            elif old == "PAUSE_REQUESTED" and running == 0:
                new = "PAUSED"
            elif row["plan_status"] == "COMPLETED":
                new = "COMPLETED"
            elif row["plan_status"] == "FAILED":
                new = "FAILED"
                error = "linked orchestration plan failed"
            elif row["plan_status"] == "CANCELLED":
                new = "CANCELLED"
                error = "linked orchestration plan cancelled"

            if new is None or new == old:
                connection.execute(
                    """
                    UPDATE objective_queue
                    SET heartbeat_at = ?
                    WHERE objective_id = ?
                    """,
                    (now, objective_id),
                )
                continue

            terminal = new in {"COMPLETED", "FAILED", "CANCELLED"}
            connection.execute(
                """
                UPDATE objective_queue
                SET status = ?,
                    heartbeat_at = ?,
                    paused_at = CASE WHEN ? = 'PAUSED' THEN ? ELSE paused_at END,
                    finished_at = CASE WHEN ? THEN ? ELSE NULL END,
                    last_error = ?
                WHERE objective_id = ?
                """,
                (
                    new,
                    now,
                    new,
                    now,
                    1 if terminal else 0,
                    now,
                    error,
                    objective_id,
                ),
            )
            add_objective_event(
                connection,
                objective_id=objective_id,
                event_type=f"OBJECTIVE_{new}",
                old_status=old,
                new_status=new,
                payload={"plan_id": plan_id},
            )
        connection.commit()


def next_queued_objective() -> dict[str, Any] | None:
    now = utc_now()
    with connect() as connection:
        row = connection.execute(
            """
            SELECT objective_id, source, priority, created_at, plan_id
            FROM objective_queue
            WHERE status = 'QUEUED'
              AND not_before <= ?
            ORDER BY priority, created_at
            LIMIT 1
            """,
            (now,),
        ).fetchone()
    return dict(row) if row else None


def promote_planned_objective(objective_id: str) -> str | None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT objective.*, plan.status AS plan_status
            FROM objective_queue AS objective
            JOIN orchestration_plans AS plan
              ON plan.plan_id = objective.plan_id
            WHERE objective.objective_id = ?
              AND objective.status = 'QUEUED'
              AND objective.source IN ('AI', 'DECLARATIVE', 'TEST')
              AND objective.not_before <= ?
            """,
            (objective_id, now),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None

        plan_status = str(row["plan_status"])
        if plan_status == "DRAFT":
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status = 'READY',
                    heartbeat_at = ?,
                    last_error = NULL
                WHERE plan_id = ? AND status = 'DRAFT'
                """,
                (now, row["plan_id"]),
            )
            new = "RUNNING"
            error = None
        elif plan_status in {"READY", "RUNNING", "BLOCKED"}:
            new = "RUNNING"
            error = None
        elif plan_status in {"COMPLETED", "FAILED", "CANCELLED"}:
            new = plan_status
            error = {
                "FAILED": "linked orchestration plan failed",
                "CANCELLED": "linked orchestration plan cancelled",
            }.get(new)
        else:
            connection.rollback()
            return None

        updated = connection.execute(
            """
            UPDATE objective_queue
            SET status = ?,
                started_at = COALESCE(started_at, ?),
                heartbeat_at = ?,
                finished_at = CASE
                    WHEN ? IN ('COMPLETED', 'FAILED', 'CANCELLED') THEN ?
                    ELSE NULL
                END,
                paused_at = NULL,
                last_error = ?
            WHERE objective_id = ? AND status = 'QUEUED'
            """,
            (new, now, now, new, now, error, objective_id),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None
        add_objective_event(
            connection,
            objective_id=objective_id,
            event_type=(
                "OBJECTIVE_DISPATCHED"
                if new == "RUNNING"
                else f"OBJECTIVE_{new}"
            ),
            old_status="QUEUED",
            new_status=new,
            payload={"plan_id": row["plan_id"]},
        )
        connection.commit()
        return str(row["plan_id"])


def reserve_ai_objective(
    objective_id: str,
    *,
    instance_id: str,
    config: dict[str, int],
) -> tuple[dict[str, Any], str] | None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if active_objective_count(connection) >= config["global_parallel_objectives"]:
            connection.rollback()
            return None

        row = connection.execute(
            """
            SELECT *
            FROM objective_queue
            WHERE objective_id = ?
              AND status = 'QUEUED'
              AND source = 'AI'
              AND plan_id IS NULL
              AND not_before <= ?
            """,
            (objective_id, now),
        ).fetchone()
        if row is None:
            connection.rollback()
            return None

        attempt_number = int(row["planning_attempt_count"]) + 1
        if attempt_number > int(row["planning_max_attempts"]):
            connection.execute(
                """
                UPDATE objective_queue
                SET status = 'FAILED',
                    heartbeat_at = ?,
                    finished_at = ?,
                    last_error = 'planning attempt budget exhausted'
                WHERE objective_id = ?
                """,
                (now, now, row["objective_id"]),
            )
            connection.commit()
            return None

        attempt_id = "objective-attempt-" + uuid.uuid4().hex
        updated = connection.execute(
            """
            UPDATE objective_queue
            SET status = 'PLANNING',
                planning_attempt_count = ?,
                started_at = COALESCE(started_at, ?),
                heartbeat_at = ?,
                last_error = NULL
            WHERE objective_id = ? AND status = 'QUEUED'
            """,
            (attempt_number, now, now, row["objective_id"]),
        ).rowcount
        if updated != 1:
            connection.rollback()
            return None

        connection.execute(
            """
            INSERT INTO objective_attempts (
                objective_attempt_id,
                objective_id,
                attempt_number,
                status,
                executor_instance_id,
                planner_execution_id,
                plan_id,
                result_json,
                failure_reason,
                started_at,
                heartbeat_at,
                finished_at,
                next_attempt_at
            )
            VALUES (?, ?, ?, 'RUNNING', ?, NULL, NULL, '{}', NULL,
                    ?, ?, NULL, NULL)
            """,
            (
                attempt_id,
                row["objective_id"],
                attempt_number,
                instance_id,
                now,
                now,
            ),
        )
        add_objective_event(
            connection,
            objective_id=row["objective_id"],
            event_type="OBJECTIVE_PLANNING_STARTED",
            old_status="QUEUED",
            new_status="PLANNING",
            payload={"attempt_id": attempt_id, "attempt_number": attempt_number},
        )
        connection.commit()
        return dict(row), attempt_id


def objective_cancellation_requested(plan_id: str) -> bool:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT status
            FROM objective_queue
            WHERE plan_id = ?
            """,
            (plan_id,),
        ).fetchone()
    return row is not None and row["status"] in {
        "CANCEL_REQUESTED",
        "CANCELLED",
    }


def plan_waiting_human_requested(plan_id: str) -> bool:
    with connect() as connection:
        return plan_has_active_human_gate(connection, plan_id)


def finish_objective_planning_success(
    objective_id: str,
    attempt_id: str,
    result: dict[str, Any],
) -> None:
    now = utc_now()
    plan_id = str(result["plan_id"])
    execution_id = str(result["execution_id"])
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM objective_queue WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            fail(f"Objective disappeared during planning: {objective_id}")

        connection.execute(
            """
            UPDATE objective_attempts
            SET status = 'COMPLETED',
                planner_execution_id = ?,
                plan_id = ?,
                result_json = ?,
                heartbeat_at = ?,
                finished_at = ?
            WHERE objective_attempt_id = ? AND status = 'RUNNING'
            """,
            (execution_id, plan_id, canonical_json(result), now, now, attempt_id),
        )

        old = row["status"]
        if old == "CANCEL_REQUESTED":
            cancel_objective_plan(connection, plan_id, now)
            new = "CANCELLED"
            finished_at: str | None = now
            error = "cancelled by operator during planning"
        elif old == "PAUSE_REQUESTED":
            new = "PAUSED"
            finished_at = None
            error = None
        elif old == "PLANNING":
            connection.execute(
                """
                UPDATE orchestration_plans
                SET status = 'READY',
                    heartbeat_at = ?,
                    last_error = NULL
                WHERE plan_id = ? AND status = 'DRAFT'
                """,
                (now, plan_id),
            )
            new = "RUNNING"
            finished_at = None
            error = None
        else:
            connection.rollback()
            fail(f"Unexpected objective status after planning: {old}")

        connection.execute(
            """
            UPDATE objective_queue
            SET status = ?,
                plan_id = ?,
                planner_execution_id = ?,
                heartbeat_at = ?,
                finished_at = ?,
                paused_at = CASE WHEN ? = 'PAUSED' THEN ? ELSE NULL END,
                last_error = ?
            WHERE objective_id = ?
            """,
            (
                new,
                plan_id,
                execution_id,
                now,
                finished_at,
                new,
                now,
                error,
                objective_id,
            ),
        )
        add_objective_event(
            connection,
            objective_id=objective_id,
            event_type="OBJECTIVE_PLANNED",
            old_status=old,
            new_status=new,
            payload={
                "attempt_id": attempt_id,
                "plan_id": plan_id,
                "planner_execution_id": execution_id,
            },
        )
        connection.commit()


def finish_objective_planning_failure(
    objective_id: str,
    attempt_id: str,
    error: str,
    config: dict[str, int],
) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM objective_queue WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if row is None:
            connection.rollback()
            return

        if row["status"] == "CANCEL_REQUESTED":
            next_status = "CANCELLED"
            next_attempt_at = None
        elif row["status"] == "PAUSE_REQUESTED":
            next_status = "PAUSED"
            next_attempt_at = None
        elif int(row["planning_attempt_count"]) < int(row["planning_max_attempts"]):
            next_status = "QUEUED"
            next_attempt_at = future_utc(
                config["planning_retry_backoff_seconds"]
                * int(row["planning_attempt_count"])
            )
        else:
            next_status = "FAILED"
            next_attempt_at = None

        connection.execute(
            """
            UPDATE objective_attempts
            SET status = 'FAILED',
                failure_reason = ?,
                heartbeat_at = ?,
                finished_at = ?,
                next_attempt_at = ?
            WHERE objective_attempt_id = ? AND status = 'RUNNING'
            """,
            (error, now, now, next_attempt_at, attempt_id),
        )
        connection.execute(
            """
            UPDATE objective_queue
            SET status = ?,
                not_before = COALESCE(?, not_before),
                heartbeat_at = ?,
                finished_at = CASE WHEN ? IN ('FAILED', 'CANCELLED') THEN ? ELSE NULL END,
                paused_at = CASE WHEN ? = 'PAUSED' THEN ? ELSE paused_at END,
                last_error = ?
            WHERE objective_id = ?
            """,
            (
                next_status,
                next_attempt_at,
                now,
                next_status,
                now,
                next_status,
                now,
                error,
                objective_id,
            ),
        )
        add_objective_event(
            connection,
            objective_id=objective_id,
            event_type="OBJECTIVE_PLANNING_RETRY" if next_status == "QUEUED" else "OBJECTIVE_PLANNING_FAILED",
            old_status=row["status"],
            new_status=next_status,
            payload={
                "attempt_id": attempt_id,
                "error": error,
                "next_attempt_at": next_attempt_at,
            },
        )
        connection.commit()


def execute_objective_planning(
    objective: dict[str, Any],
    attempt_id: str,
    *,
    config: dict[str, int],
) -> None:
    objective_id = objective["objective_id"]
    directory = OBJECTIVE_RUNTIME / objective_id / attempt_id
    directory.mkdir(parents=True, exist_ok=True, mode=0o750)
    objective_path = directory / "objective.txt"
    objective_path.write_text(objective["objective"].strip() + "\n", encoding="utf-8")
    objective_path.chmod(0o600)
    projects = json.loads(objective["project_scope_json"])
    marker = "ORCHESTRA_OBJECTIVE_PLAN_OK"

    try:
        result = run_json(
            [
                str(PLANNER),
                "generate",
                "--objective-file",
                str(objective_path),
                "--projects",
                ",".join(projects),
                "--marker",
                marker,
                "--status",
                "DRAFT",
                "--timeout",
                str(config["planning_timeout_seconds"]),
            ],
            timeout=config["planning_timeout_seconds"] + 120,
        )
        finish_objective_planning_success(objective_id, attempt_id, result)
    except Exception as error:
        finish_objective_planning_failure(
            objective_id,
            attempt_id,
            str(error),
            config,
        )


def reconcile_interrupted_planner_executions() -> None:
    now = utc_now()
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT execution_id, outer_container_name
            FROM orchestrator_executions
            WHERE finished_at IS NULL
            ORDER BY created_at
            """
        ).fetchall()

    for row in rows:
        outer_name = str(row["outer_container_name"] or "")
        outer_id = owned_runtime_container(outer_name, timeout=60)
        if outer_id is not None:
            run_command(
                ["docker", "rm", "-f", outer_id],
                timeout=60,
                check=False,
            )

    if rows:
        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in rows:
                connection.execute(
                    """
                    UPDATE orchestrator_executions
                    SET exit_code = COALESCE(exit_code, 137),
                        failure_reason = COALESCE(
                            failure_reason,
                            'orchestrator process restarted during planning'
                        ),
                        finished_at = ?
                    WHERE execution_id = ? AND finished_at IS NULL
                    """,
                    (now, row["execution_id"]),
                )
            connection.commit()


def reconcile_interrupted_objectives(instance_id: str) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        rows = connection.execute(
            """
            SELECT objective.*, attempt.objective_attempt_id
            FROM objective_queue AS objective
            LEFT JOIN objective_attempts AS attempt
              ON attempt.objective_id = objective.objective_id
             AND attempt.status = 'RUNNING'
            WHERE objective.status = 'PLANNING'
            """
        ).fetchall()

        for row in rows:
            if row["objective_attempt_id"]:
                connection.execute(
                    """
                    UPDATE objective_attempts
                    SET status = 'ABANDONED',
                        heartbeat_at = ?,
                        finished_at = ?,
                        failure_reason = 'orchestrator process restarted'
                    WHERE objective_attempt_id = ? AND status = 'RUNNING'
                    """,
                    (now, now, row["objective_attempt_id"]),
                )
            retry = int(row["planning_attempt_count"]) < int(row["planning_max_attempts"])
            new = "QUEUED" if retry else "FAILED"
            connection.execute(
                """
                UPDATE objective_queue
                SET status = ?,
                    not_before = ?,
                    heartbeat_at = ?,
                    finished_at = CASE WHEN ? = 'FAILED' THEN ? ELSE NULL END,
                    last_error = 'orchestrator process restarted during planning'
                WHERE objective_id = ? AND status = 'PLANNING'
                """,
                (new, now, now, new, now, row["objective_id"]),
            )
            add_objective_event(
                connection,
                objective_id=row["objective_id"],
                event_type="OBJECTIVE_PLANNING_ABANDONED",
                old_status="PLANNING",
                new_status=new,
                payload={"instance_id": instance_id},
            )
        connection.commit()


def project_is_busy(
    connection: sqlite3.Connection,
    project_id: str | None,
    task_id: str,
) -> bool:
    if project_id is None:
        return False

    count = connection.execute(
        """
        SELECT COUNT(*)
        FROM orchestration_tasks
        WHERE project_id = ?
          AND status = 'RUNNING'
          AND orchestration_task_id <> ?
        """,
        (project_id, task_id),
    ).fetchone()[0]
    return int(count) > 0


def runnable_tasks(
    active_task_ids: set[str],
    *,
    capacity: int,
) -> list[str]:
    if capacity <= 0:
        return []

    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
                task.*,
                plan.max_parallel_tasks,
                plan.status AS plan_status
            FROM orchestration_tasks AS task
            JOIN orchestration_plans AS plan
              ON plan.plan_id = task.plan_id
            LEFT JOIN objective_queue AS objective
              ON objective.plan_id = plan.plan_id
            WHERE task.status = 'READY'
              AND plan.status IN ('READY', 'RUNNING')
              AND (
                    objective.objective_id IS NULL
                    OR objective.status = 'RUNNING'
              )
            ORDER BY
                COALESCE(objective.priority, 100),
                task.priority,
                COALESCE(objective.created_at, task.created_at),
                task.created_at
            """
        ).fetchall()

        selected: list[str] = []
        selected_projects: set[str] = set()
        per_plan_running: dict[str, int] = {}
        pipeline_health_ready = supervisor_is_healthy()

        for row in rows:
            task_id = row["orchestration_task_id"]
            if task_id in active_task_ids:
                continue
            if row["kind"] == "PIPELINE" and not pipeline_health_ready:
                continue

            plan_id = row["plan_id"]
            if plan_id not in per_plan_running:
                per_plan_running[plan_id] = int(
                    connection.execute(
                        """
                        SELECT COUNT(*)
                        FROM orchestration_tasks
                        WHERE plan_id = ?
                          AND status = 'RUNNING'
                        """,
                        (plan_id,),
                    ).fetchone()[0]
                )

            if per_plan_running[plan_id] >= int(row["max_parallel_tasks"]):
                continue
            if project_is_busy(connection, row["project_id"], task_id):
                continue
            if row["project_id"] is not None and row["project_id"] in selected_projects:
                continue

            selected.append(task_id)
            if row["project_id"] is not None:
                selected_projects.add(row["project_id"])
            per_plan_running[plan_id] += 1

            if len(selected) >= capacity:
                break

    return selected


def active_plan_ids() -> list[str]:
    with connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                """
                SELECT plan_id
                FROM orchestration_plans
                WHERE status IN ('READY', 'RUNNING', 'BLOCKED')
                ORDER BY created_at
                """
            ).fetchall()
        ]


def daemon_loop(arguments: argparse.Namespace) -> None:
    config = load_config()
    lock = acquire_lock()

    if lock is None:
        print(
            canonical_json(
                {"status": "LOCKED", "lock_path": str(LOCK_PATH)}
            ),
            file=sys.stderr,
        )
        raise SystemExit(75)

    owner = arguments.owner or f"ops-orchestrator:{socket.gethostname()}"
    instance_id = register_instance(owner)
    stop_event = threading.Event()

    def request_stop(signum: int, _: Any) -> None:
        print(
            canonical_json(
                {
                    "event": "signal",
                    "signal": signum,
                    "instance_id": instance_id,
                }
            ),
            flush=True,
        )
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=config["global_parallel_tasks"],
        thread_name_prefix="orchestra-task",
    )
    planning_executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=1,
        thread_name_prefix="orchestra-objective",
    )
    futures: dict[concurrent.futures.Future[None], str] = {}
    planning_futures: dict[concurrent.futures.Future[None], str] = {}

    try:
        update_instance(instance_id, status="RUNNING")
        reconcile_interrupted_tasks(instance_id)
        reconcile_interrupted_planner_executions()
        reconcile_interrupted_objectives(instance_id)
        write_status(instance_id, message="orchestrator started")

        while not stop_event.is_set():
            update_instance(instance_id)
            cleanup_cancel_requested_human_runs(
                config["command_timeout_seconds"]
            )
            synchronize_objective_states()

            completed_planning = [
                future for future in planning_futures if future.done()
            ]
            for future in completed_planning:
                objective_id = planning_futures.pop(future)
                try:
                    future.result()
                except Exception as error:
                    print(
                        canonical_json(
                            {
                                "event": "objective-planner-error",
                                "objective_id": objective_id,
                                "error": str(error),
                            }
                        ),
                        flush=True,
                    )

            while True:
                with connect() as connection:
                    available_objective_slots = (
                        config["global_parallel_objectives"]
                        - active_objective_count(connection)
                    )
                if available_objective_slots <= 0:
                    break

                queued = next_queued_objective()
                if queued is None:
                    break

                # Only plan AI objectives that do not already own a plan.
                # Resumed planned AI objectives follow the same promotion path
                # as declarative plans and preserve plan identity/history.
                if queued["source"] == "AI" and queued["plan_id"] is None:
                    # Preserve global priority: a high-priority AI objective
                    # is never overtaken by a lower-priority declarative one.
                    if planning_futures:
                        break
                    reserved = reserve_ai_objective(
                        queued["objective_id"],
                        instance_id=instance_id,
                        config=config,
                    )
                    if reserved is None:
                        break
                    objective, objective_attempt_id = reserved
                    future = planning_executor.submit(
                        execute_objective_planning,
                        objective,
                        objective_attempt_id,
                        config=config,
                    )
                    planning_futures[future] = objective["objective_id"]
                    continue

                plan_id = promote_planned_objective(
                    queued["objective_id"]
                )
                if plan_id is None:
                    break
                refresh_plan_states(plan_id)

            for plan_id in active_plan_ids():
                refresh_plan_states(plan_id)

            completed = [future for future in futures if future.done()]
            for future in completed:
                task_id = futures.pop(future)
                try:
                    future.result()
                except Exception as error:
                    print(
                        canonical_json(
                            {
                                "event": "executor-error",
                                "task_id": task_id,
                                "error": str(error),
                            }
                        ),
                        flush=True,
                    )

            capacity = config["global_parallel_tasks"] - len(futures)
            active_ids = set(futures.values())
            for task_id in runnable_tasks(active_ids, capacity=capacity):
                future = executor.submit(
                    execute_task,
                    task_id,
                    instance_id=instance_id,
                    config=config,
                )
                futures[future] = task_id

            write_status(instance_id, message="scheduler sweep complete")
            stop_event.wait(config["poll_seconds"])

        # Graceful service stops wait for current tasks. SIGKILL is validated
        # separately and is recovered from durable task/attempt records.
        planning_executor.shutdown(wait=True, cancel_futures=False)
        executor.shutdown(wait=True, cancel_futures=False)
        update_instance(instance_id, status="STOPPED", stopped=True)
        write_status(instance_id, message="orchestrator stopped cleanly")
    except BaseException as error:
        try:
            update_instance(
                instance_id,
                status="FAILED",
                last_error=str(error),
                stopped=True,
            )
            write_status(instance_id, message=str(error))
        except Exception:
            pass
        raise
    finally:
        lock.close()


def plan_status_payload(plan_id: str) -> dict[str, Any]:
    with connect() as connection:
        plan = connection.execute(
            "SELECT * FROM orchestration_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if plan is None:
            fail(f"Unknown orchestration plan: {plan_id}")

        tasks = connection.execute(
            """
            SELECT *
            FROM orchestration_tasks
            WHERE plan_id = ?
            ORDER BY priority, created_at
            """,
            (plan_id,),
        ).fetchall()
        dependencies = connection.execute(
            """
            SELECT
                child.task_key AS task_key,
                parent.task_key AS depends_on
            FROM orchestration_dependencies AS dependency
            JOIN orchestration_tasks AS child
              ON child.orchestration_task_id = dependency.orchestration_task_id
            JOIN orchestration_tasks AS parent
              ON parent.orchestration_task_id = dependency.depends_on_task_id
            WHERE dependency.plan_id = ?
            ORDER BY child.task_key, parent.task_key
            """,
            (plan_id,),
        ).fetchall()
        attempts = connection.execute(
            """
            SELECT attempt.*
            FROM orchestration_attempts AS attempt
            JOIN orchestration_tasks AS task
              ON task.orchestration_task_id = attempt.orchestration_task_id
            WHERE task.plan_id = ?
            ORDER BY attempt.started_at
            """,
            (plan_id,),
        ).fetchall()

    dependency_map: dict[str, list[str]] = {}
    for row in dependencies:
        dependency_map.setdefault(row["task_key"], []).append(row["depends_on"])

    task_payload = []
    for task in tasks:
        item = dict(task)
        item["dependencies"] = dependency_map.get(task["task_key"], [])
        item["acceptance_criteria"] = json.loads(item.pop("acceptance_json"))
        item["result"] = json.loads(item.pop("result_json"))
        task_payload.append(item)

    attempt_payload = []
    for attempt in attempts:
        item = dict(attempt)
        item["result"] = json.loads(item.pop("result_json"))
        attempt_payload.append(item)

    plan_item = dict(plan)
    plan_item["plan"] = json.loads(plan_item.pop("plan_json"))
    return {
        "plan": plan_item,
        "tasks": task_payload,
        "attempts": attempt_payload,
    }


def command_import(arguments: argparse.Namespace) -> None:
    path = Path(arguments.plan_file).resolve()
    plan = validate_plan(
        load_json_file(path),
        allow_test_actions=arguments.allow_test_actions,
    )
    source = "TEST" if arguments.allow_test_actions else "DECLARATIVE"
    plan_id = insert_plan(
        plan,
        source=source,
        initial_status=arguments.status,
    )
    print(json.dumps(plan_status_payload(plan_id), indent=2, sort_keys=True))


def command_activate(arguments: argparse.Namespace) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE orchestration_plans
            SET status = 'READY',
                heartbeat_at = ?,
                last_error = NULL
            WHERE plan_id = ?
              AND status = 'DRAFT'
            """,
            (now, arguments.plan),
        ).rowcount
        if updated != 1:
            connection.rollback()
            fail("Plan can only be activated from DRAFT")
        connection.commit()

    refresh_plan_states(arguments.plan)
    print(json.dumps(plan_status_payload(arguments.plan), indent=2, sort_keys=True))


def command_cancel(arguments: argparse.Namespace) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        plan = connection.execute(
            "SELECT status FROM orchestration_plans WHERE plan_id = ?",
            (arguments.plan,),
        ).fetchone()
        if plan is None:
            connection.rollback()
            fail(f"Unknown plan: {arguments.plan}")
        if plan["status"] in {"COMPLETED", "FAILED", "CANCELLED"}:
            connection.rollback()
            fail(f"Plan is already terminal: {plan['status']}")

        running = connection.execute(
            """
            SELECT COUNT(*)
            FROM orchestration_tasks
            WHERE plan_id = ? AND status = 'RUNNING'
            """,
            (arguments.plan,),
        ).fetchone()[0]
        if running:
            connection.rollback()
            fail("Cannot cancel a plan while a task is RUNNING")

        connection.execute(
            """
            UPDATE orchestration_tasks
            SET status = 'CANCELLED',
                heartbeat_at = ?,
                finished_at = ?
            WHERE plan_id = ?
              AND status IN ('PENDING', 'READY', 'BLOCKED')
            """,
            (now, now, arguments.plan),
        )
        connection.execute(
            """
            UPDATE orchestration_plans
            SET status = 'CANCELLED',
                heartbeat_at = ?,
                finished_at = ?
            WHERE plan_id = ?
            """,
            (now, now, arguments.plan),
        )
        connection.commit()

    print(json.dumps(plan_status_payload(arguments.plan), indent=2, sort_keys=True))


def command_status(arguments: argparse.Namespace) -> None:
    print(json.dumps(plan_status_payload(arguments.plan), indent=2, sort_keys=True))


def command_list(_: argparse.Namespace) -> None:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
                plan.plan_id,
                plan.objective,
                plan.source,
                plan.status,
                plan.max_parallel_tasks,
                plan.created_at,
                plan.started_at,
                plan.finished_at,
                plan.last_error,
                SUM(CASE WHEN task.status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed_tasks,
                COUNT(task.orchestration_task_id) AS total_tasks
            FROM orchestration_plans AS plan
            LEFT JOIN orchestration_tasks AS task
              ON task.plan_id = plan.plan_id
            GROUP BY plan.plan_id
            ORDER BY plan.created_at DESC
            """
        ).fetchall()

    print(json.dumps([dict(row) for row in rows], indent=2, sort_keys=True))


def command_daemon_status(_: argparse.Namespace) -> None:
    with connect() as connection:
        instance = connection.execute(
            """
            SELECT *
            FROM orchestrator_instances
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
        plans = plan_counts(connection)
        tasks = task_counts(connection)
        objectives = objective_counts(connection)

    print(
        json.dumps(
            {
                "version": VERSION,
                "lock_held": lock_is_held(),
                "instance": dict(instance) if instance else None,
                "plan_counts": plans,
                "task_counts": tasks,
                "objective_counts": objectives,
                "supervisor_healthy": supervisor_is_healthy(),
                "runtime_status_path": str(STATUS_PATH),
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_self_test(_: argparse.Namespace) -> None:
    sample = {
        "schema_version": 1,
        "objective": "Validate deterministic DAG ordering",
        "max_parallel_tasks": 2,
        "tasks": [
            {
                "key": "a",
                "kind": "TEST_SLEEP",
                "duration_seconds": 1,
                "dependencies": [],
            },
            {
                "key": "b",
                "kind": "NOOP",
                "dependencies": ["a"],
            },
        ],
    }

    # The full validator needs the Controller database. The graph contract is
    # independently checked here so package validation remains useful offline.
    order = topological_order(sample["tasks"])
    if order != ["a", "b"]:
        fail(f"Unexpected topological order: {order}")

    cyclic = [
        {"key": "a", "dependencies": ["b"]},
        {"key": "b", "dependencies": ["a"]},
    ]
    try:
        topological_order(cyclic)
    except OrchestratorError:
        pass
    else:
        fail("Cyclic plan was accepted")

    transient = {
        "log_tail": (
            "API call failed after 3 retries: "
            "Codex stream produced no SSE events for 12s after first byte"
        )
    }
    if not is_transient_review_transport_failure(
        OrchestratorError("Expected reviewer marker is absent"),
        transient,
    ):
        fail("Known transient reviewer transport failure was not classified")

    if is_transient_review_transport_failure(
        OrchestratorError("Expected reviewer marker is absent"),
        {},
    ):
        fail("Marker absence alone was classified as transport failure")

    before = future_utc(1)
    after = future_utc(2)
    if not before < after:
        fail("Objective deferred retry timestamps are not ordered")

    print("Orchestra orchestration DAG and objective queue engine: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra persistent multi-task DAG orchestrator"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon = subparsers.add_parser("daemon")
    daemon.add_argument("--owner")
    daemon.set_defaults(function=daemon_loop)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--plan-file", required=True)
    import_parser.add_argument("--allow-test-actions", action="store_true")
    import_parser.add_argument(
        "--status",
        choices=("DRAFT", "READY"),
        default="READY",
    )
    import_parser.set_defaults(function=command_import)

    activate = subparsers.add_parser("activate")
    activate.add_argument("--plan", required=True)
    activate.set_defaults(function=command_activate)

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--plan", required=True)
    cancel.set_defaults(function=command_cancel)

    status = subparsers.add_parser("status")
    status.add_argument("--plan", required=True)
    status.set_defaults(function=command_status)

    list_parser = subparsers.add_parser("list")
    list_parser.set_defaults(function=command_list)

    daemon_status = subparsers.add_parser("daemon-status")
    daemon_status.set_defaults(function=command_daemon_status)

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        arguments.function(arguments)
    except OrchestratorError as error:
        print(f"Orchestrator error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
