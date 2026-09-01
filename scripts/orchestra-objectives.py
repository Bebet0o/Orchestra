#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sqlite3
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


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
ORCHESTRATOR_SCRIPT = REPO / "scripts/orchestra-orchestrator.py"
OBJECTIVE_ID_PATTERN = re.compile(r"^objective-[a-f0-9]{32}$")
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


class ObjectiveError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise ObjectiveError(message)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def normalize_timestamp(value: str | None) -> str:
    if value is None or not value.strip():
        return utc_now()

    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as error:
        fail(f"Invalid ISO-8601 timestamp: {value}")

    if parsed.tzinfo is None:
        fail("Timestamp must include a timezone")

    return (
        parsed.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def load_orchestrator() -> Any:
    specification = importlib.util.spec_from_file_location(
        "orchestra_objective_orchestrator",
        ORCHESTRATOR_SCRIPT,
    )
    if specification is None or specification.loader is None:
        fail("Unable to load Orchestra orchestrator module")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def validate_priority(value: int) -> int:
    if not -1000 <= value <= 1000:
        fail("Priority must be -1000..1000; lower values run first")
    return value


def validate_projects(project_ids: list[str]) -> list[str]:
    normalized = sorted({item.strip() for item in project_ids if item.strip()})
    if not normalized:
        fail("At least one project is required")

    placeholders = ",".join("?" for _ in normalized)
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT project_id, enabled
            FROM projects
            WHERE project_id IN ({placeholders})
            """,
            tuple(normalized),
        ).fetchall()

    found = {row["project_id"] for row in rows}
    missing = set(normalized) - found
    if missing:
        fail("Unknown projects: " + ", ".join(sorted(missing)))
    disabled = sorted(row["project_id"] for row in rows if not row["enabled"])
    if disabled:
        fail("Disabled projects: " + ", ".join(disabled))
    return normalized


def add_event(
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


def insert_objective(
    *,
    objective: str,
    source: str,
    priority: int,
    not_before: str,
    project_ids: list[str],
    max_parallel_tasks: int,
    planning_max_attempts: int,
    plan_id: str | None,
) -> str:
    text = objective.strip()
    if not text:
        fail("Objective is empty")
    if len(text.encode()) > 16_384:
        fail("Objective exceeds 16 KiB")
    validate_priority(priority)
    if not 1 <= max_parallel_tasks <= 16:
        fail("max_parallel_tasks must be 1..16")
    if not 1 <= planning_max_attempts <= 5:
        fail("planning_max_attempts must be 1..5")

    objective_id = "objective-" + uuid.uuid4().hex
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO objective_queue (
                objective_id,
                objective,
                source,
                status,
                priority,
                not_before,
                project_scope_json,
                max_parallel_tasks,
                planning_max_attempts,
                planning_attempt_count,
                plan_id,
                planner_execution_id,
                created_at,
                started_at,
                heartbeat_at,
                finished_at,
                paused_at,
                last_error
            )
            VALUES (?, ?, ?, 'QUEUED', ?, ?, ?, ?, ?, 0, ?, NULL,
                    ?, NULL, ?, NULL, NULL, NULL)
            """,
            (
                objective_id,
                text,
                source,
                priority,
                not_before,
                canonical_json(project_ids),
                max_parallel_tasks,
                planning_max_attempts,
                plan_id,
                now,
                now,
            ),
        )
        add_event(
            connection,
            objective_id=objective_id,
            event_type="OBJECTIVE_SUBMITTED",
            old_status=None,
            new_status="QUEUED",
            payload={
                "source": source,
                "priority": priority,
                "not_before": not_before,
                "projects": project_ids,
                "plan_id": plan_id,
            },
        )
        connection.commit()
    return objective_id


def running_task_count(
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


def integration_point_of_no_return(
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
                  ON task.orchestration_task_id = attempt.orchestration_task_id
                JOIN runs AS run
                  ON run.run_id = attempt.run_id
                WHERE task.plan_id = ?
                  AND (
                        run.status IN ('COMMITTING', 'COMPLETED')
                        OR EXISTS (
                            SELECT 1
                            FROM integration_executions AS integration
                            WHERE integration.run_id = run.run_id
                              AND integration.decision = 'APPROVE'
                              AND integration.status IN (
                                  'PREPARED', 'COMPLETED', 'FAILED'
                              )
                        )
                  )
            )
            """,
            (plan_id,),
        ).fetchone()[0]
    )


def resumed_status_for_plan(plan_status: str | None) -> str:
    if plan_status in {None, "DRAFT", "READY", "RUNNING"}:
        return "QUEUED"
    if plan_status == "BLOCKED":
        return "RUNNING"
    if plan_status in TERMINAL_STATUSES:
        return plan_status
    fail(f"Unsupported orchestration plan status during resume: {plan_status}")


def active_run_count(
    connection: sqlite3.Connection,
    plan_id: str | None,
) -> int:
    if plan_id is None:
        return 0
    return int(
        connection.execute(
            """
            SELECT COUNT(DISTINCT run.run_id)
            FROM orchestration_attempts AS attempt
            JOIN orchestration_tasks AS task
              ON task.orchestration_task_id = attempt.orchestration_task_id
            JOIN runs AS run ON run.run_id = attempt.run_id
            WHERE task.plan_id = ?
              AND run.status IN (
                  'SNAPSHOTTING', 'RUNNING', 'REVIEWING',
                  'WAITING_HUMAN', 'COMMITTING', 'RECOVERING'
              )
            """,
            (plan_id,),
        ).fetchone()[0]
    )


def cancel_plan_in_transaction(
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


def objective_payload(objective_id: str) -> dict[str, Any]:
    if not OBJECTIVE_ID_PATTERN.fullmatch(objective_id):
        fail(f"Invalid objective id: {objective_id}")

    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM objective_queue WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if row is None:
            fail(f"Unknown objective: {objective_id}")
        attempts = connection.execute(
            """
            SELECT *
            FROM objective_attempts
            WHERE objective_id = ?
            ORDER BY attempt_number
            """,
            (objective_id,),
        ).fetchall()
        events = connection.execute(
            """
            SELECT *
            FROM objective_events
            WHERE objective_id = ?
            ORDER BY created_at
            """,
            (objective_id,),
        ).fetchall()

    item = dict(row)
    item["projects"] = json.loads(item.pop("project_scope_json"))
    payload: dict[str, Any] = {
        "objective": item,
        "attempts": [],
        "events": [],
        "plan": None,
    }
    for attempt in attempts:
        value = dict(attempt)
        value["result"] = json.loads(value.pop("result_json"))
        payload["attempts"].append(value)
    for event in events:
        value = dict(event)
        value["payload"] = json.loads(value.pop("payload_json"))
        payload["events"].append(value)

    if row["plan_id"]:
        orchestrator = load_orchestrator()
        payload["plan"] = orchestrator.plan_status_payload(row["plan_id"])
    return payload


def command_submit(arguments: argparse.Namespace) -> None:
    path = Path(arguments.objective_file).resolve()
    if not path.is_file():
        fail(f"Objective file is absent: {path}")
    objective = path.read_text(encoding="utf-8").strip()
    projects = validate_projects(arguments.project)
    objective_id = insert_objective(
        objective=objective,
        source="AI",
        priority=arguments.priority,
        not_before=normalize_timestamp(arguments.not_before),
        project_ids=projects,
        max_parallel_tasks=arguments.max_parallel,
        planning_max_attempts=arguments.planning_attempts,
        plan_id=None,
    )
    print(json.dumps(objective_payload(objective_id), indent=2, sort_keys=True))


def command_submit_plan(arguments: argparse.Namespace) -> None:
    path = Path(arguments.plan_file).resolve()
    if not path.is_file():
        fail(f"Plan file is absent: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        fail(f"Plan JSON is invalid: {error}")
    if not isinstance(raw, dict):
        fail("Plan must be a JSON object")

    orchestrator = load_orchestrator()
    plan = orchestrator.validate_plan(
        raw,
        allow_test_actions=arguments.allow_test_actions,
        require_enabled_projects=True,
    )
    projects = sorted(
        {
            task["project_id"]
            for task in plan["tasks"]
            if task["project_id"] is not None
        }
    )
    if not projects:
        projects = validate_projects(arguments.project)
    else:
        validate_projects(projects)

    source = "TEST" if arguments.allow_test_actions else "DECLARATIVE"
    plan_id = orchestrator.insert_plan(
        plan,
        source=source,
        initial_status="DRAFT",
    )
    try:
        objective_id = insert_objective(
            objective=plan["objective"],
            source=source,
            priority=arguments.priority,
            not_before=normalize_timestamp(arguments.not_before),
            project_ids=projects,
            max_parallel_tasks=plan["max_parallel_tasks"],
            planning_max_attempts=1,
            plan_id=plan_id,
        )
    except Exception:
        with connect() as connection:
            connection.execute(
                "DELETE FROM orchestration_plans WHERE plan_id = ?",
                (plan_id,),
            )
            connection.commit()
        raise
    print(json.dumps(objective_payload(objective_id), indent=2, sort_keys=True))


def command_pause(arguments: argparse.Namespace) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM objective_queue WHERE objective_id = ?",
            (arguments.objective,),
        ).fetchone()
        if row is None:
            connection.rollback()
            fail(f"Unknown objective: {arguments.objective}")
        old = row["status"]
        if old == "CANCEL_REQUESTED":
            connection.rollback()
            fail("Objective cancellation is pending")
        if old in TERMINAL_STATUSES:
            connection.rollback()
            fail(f"Objective is terminal: {old}")
        if old == "PAUSED":
            connection.rollback()
            print(json.dumps(objective_payload(arguments.objective), indent=2, sort_keys=True))
            return
        running = running_task_count(connection, row["plan_id"])
        new = "PAUSE_REQUESTED" if old == "PLANNING" or running else "PAUSED"
        connection.execute(
            """
            UPDATE objective_queue
            SET status = ?,
                heartbeat_at = ?,
                paused_at = CASE WHEN ? = 'PAUSED' THEN ? ELSE paused_at END,
                last_error = NULL
            WHERE objective_id = ?
            """,
            (new, now, new, now, arguments.objective),
        )
        add_event(
            connection,
            objective_id=arguments.objective,
            event_type="OBJECTIVE_PAUSE_REQUESTED" if new == "PAUSE_REQUESTED" else "OBJECTIVE_PAUSED",
            old_status=old,
            new_status=new,
        )
        connection.commit()
    print(json.dumps(objective_payload(arguments.objective), indent=2, sort_keys=True))


def command_resume(arguments: argparse.Namespace) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT objective.*, plan.status AS plan_status
            FROM objective_queue AS objective
            LEFT JOIN orchestration_plans AS plan
              ON plan.plan_id = objective.plan_id
            WHERE objective.objective_id = ?
            """,
            (arguments.objective,),
        ).fetchone()
        if row is None:
            connection.rollback()
            fail(f"Unknown objective: {arguments.objective}")
        if row["status"] != "PAUSED":
            connection.rollback()
            fail("Objective can only resume from PAUSED")
        if row["plan_id"] is not None and row["plan_status"] is None:
            connection.rollback()
            fail("Objective plan disappeared during resume")
        new = resumed_status_for_plan(row["plan_status"])
        terminal = new in TERMINAL_STATUSES
        error = {
            "FAILED": "linked orchestration plan failed",
            "CANCELLED": "linked orchestration plan cancelled",
        }.get(new)
        connection.execute(
            """
            UPDATE objective_queue
            SET status = ?,
                heartbeat_at = ?,
                paused_at = NULL,
                finished_at = CASE WHEN ? THEN ? ELSE NULL END,
                last_error = ?
            WHERE objective_id = ?
            """,
            (
                new,
                now,
                1 if terminal else 0,
                now,
                error,
                arguments.objective,
            ),
        )
        add_event(
            connection,
            objective_id=arguments.objective,
            event_type="OBJECTIVE_RESUMED",
            old_status="PAUSED",
            new_status=new,
        )
        connection.commit()
    print(json.dumps(objective_payload(arguments.objective), indent=2, sort_keys=True))


def command_cancel(arguments: argparse.Namespace) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM objective_queue WHERE objective_id = ?",
            (arguments.objective,),
        ).fetchone()
        if row is None:
            connection.rollback()
            fail(f"Unknown objective: {arguments.objective}")
        old = row["status"]
        if old in TERMINAL_STATUSES:
            connection.rollback()
            fail(f"Objective is terminal: {old}")
        if integration_point_of_no_return(connection, row["plan_id"]):
            connection.rollback()
            fail("Objective passed the integration point of no return")
        running = running_task_count(connection, row["plan_id"])
        active_runs = active_run_count(connection, row["plan_id"])
        if old == "PLANNING" or running or active_runs:
            new = "CANCEL_REQUESTED"
        else:
            new = "CANCELLED"
            cancel_plan_in_transaction(connection, row["plan_id"], now)
        connection.execute(
            """
            UPDATE objective_queue
            SET status = ?,
                heartbeat_at = ?,
                finished_at = CASE WHEN ? = 'CANCELLED' THEN ? ELSE NULL END,
                last_error = CASE WHEN ? = 'CANCELLED' THEN 'cancelled by operator' ELSE last_error END
            WHERE objective_id = ?
            """,
            (new, now, new, now, new, arguments.objective),
        )
        add_event(
            connection,
            objective_id=arguments.objective,
            event_type="OBJECTIVE_CANCEL_REQUESTED" if new == "CANCEL_REQUESTED" else "OBJECTIVE_CANCELLED",
            old_status=old,
            new_status=new,
        )
        connection.commit()
    print(json.dumps(objective_payload(arguments.objective), indent=2, sort_keys=True))


def command_status(arguments: argparse.Namespace) -> None:
    print(json.dumps(objective_payload(arguments.objective), indent=2, sort_keys=True))


def command_list(_: argparse.Namespace) -> None:
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT
                objective_id,
                objective,
                source,
                status,
                priority,
                not_before,
                plan_id,
                planning_attempt_count,
                planning_max_attempts,
                created_at,
                started_at,
                finished_at,
                last_error
            FROM objective_queue
            ORDER BY
                CASE WHEN status IN ('QUEUED', 'PLANNING', 'RUNNING', 'PAUSE_REQUESTED', 'CANCEL_REQUESTED') THEN 0 ELSE 1 END,
                priority,
                created_at
            """
        ).fetchall()
    print(json.dumps([dict(row) for row in rows], indent=2, sort_keys=True))


def command_self_test(_: argparse.Namespace) -> None:
    assert normalize_timestamp("2026-07-17T22:00:00+02:00") == "2026-07-17T20:00:00.000Z"
    assert validate_priority(-1000) == -1000
    assert validate_priority(1000) == 1000
    identifier = "objective-" + "a" * 32
    assert OBJECTIVE_ID_PATTERN.fullmatch(identifier)
    print("Orchestra persistent objective queue CLI: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra persistent objective queue"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    submit = subparsers.add_parser("submit")
    submit.add_argument("--objective-file", required=True)
    submit.add_argument("--project", action="append", default=[])
    submit.add_argument("--priority", type=int, default=100)
    submit.add_argument("--not-before")
    submit.add_argument("--max-parallel", type=int, default=1)
    submit.add_argument("--planning-attempts", type=int, default=3)
    submit.set_defaults(function=command_submit)

    submit_plan = subparsers.add_parser("submit-plan")
    submit_plan.add_argument("--plan-file", required=True)
    submit_plan.add_argument("--project", action="append", default=[])
    submit_plan.add_argument("--priority", type=int, default=100)
    submit_plan.add_argument("--not-before")
    submit_plan.add_argument("--allow-test-actions", action="store_true")
    submit_plan.set_defaults(function=command_submit_plan)

    pause = subparsers.add_parser("pause")
    pause.add_argument("--objective", required=True)
    pause.set_defaults(function=command_pause)

    resume = subparsers.add_parser("resume")
    resume.add_argument("--objective", required=True)
    resume.set_defaults(function=command_resume)

    cancel = subparsers.add_parser("cancel")
    cancel.add_argument("--objective", required=True)
    cancel.set_defaults(function=command_cancel)

    status = subparsers.add_parser("status")
    status.add_argument("--objective", required=True)
    status.set_defaults(function=command_status)

    list_parser = subparsers.add_parser("list")
    list_parser.set_defaults(function=command_list)

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        arguments.function(arguments)
    except ObjectiveError as error:
        print(f"Objective error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
