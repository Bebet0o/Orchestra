#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")).resolve()
REPO = ROOT / "repo"
DATABASE = ROOT / "state/controller/orchestra.db"
OBJECTIVES = REPO / "scripts/orchestra-objectives.py"
NOTIFIER = REPO / "scripts/orchestra-notifier.py"
RECOVERY = REPO / "scripts/orchestra-recovery.py"


class ControlError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise ControlError(message)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection


def run_json(command: list[str]) -> Any:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        fail(
            (result.stderr or result.stdout or "command failed").strip()
        )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        fail(f"Command returned invalid JSON: {result.stdout[:500]}")


def print_table(rows: list[dict[str, Any]], columns: list[str]) -> None:
    if not rows:
        print("Aucun résultat.")
        return
    widths = {
        column: max(
            len(column),
            *[
                len(str(row.get(column, "") or ""))
                for row in rows
            ],
        )
        for column in columns
    }
    print(
        "  ".join(
            column.ljust(widths[column])
            for column in columns
        )
    )
    print(
        "  ".join("-" * widths[column] for column in columns)
    )
    for row in rows:
        print(
            "  ".join(
                str(row.get(column, "") or "").ljust(widths[column])
                for column in columns
            )
        )


def command_submit(arguments: argparse.Namespace) -> None:
    if bool(arguments.text) == bool(arguments.file):
        fail("Use exactly one of --text or --file")
    temporary: tempfile.NamedTemporaryFile[str] | None = None
    try:
        if arguments.text:
            temporary = tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                delete=False,
            )
            temporary.write(arguments.text)
            temporary.close()
            objective_file = temporary.name
        else:
            objective_file = arguments.file
        command = [
            str(OBJECTIVES),
            "submit",
            "--objective-file",
            objective_file,
            "--priority",
            str(arguments.priority),
            "--max-parallel",
            str(arguments.max_parallel),
            "--planning-attempts",
            str(arguments.planning_attempts),
        ]
        for project in arguments.project:
            command.extend(["--project", project])
        if arguments.not_before:
            command.extend(["--not-before", arguments.not_before])
        payload = run_json(command)
        print(json.dumps(payload, indent=2, sort_keys=True))
    finally:
        if temporary is not None:
            Path(temporary.name).unlink(missing_ok=True)


def command_queue(arguments: argparse.Namespace) -> None:
    query = """
        SELECT
            objective_id,
            source,
            status,
            priority,
            project_scope_json,
            created_at,
            started_at,
            finished_at,
            last_error
        FROM objective_queue
    """
    parameters: list[Any] = []
    if arguments.active:
        query += """
            WHERE status NOT IN ('COMPLETED', 'FAILED', 'CANCELLED')
        """
    query += " ORDER BY priority, created_at LIMIT ?"
    parameters.append(arguments.limit)
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(query, parameters)]
    if arguments.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    compact = []
    for row in rows:
        compact.append(
            {
                "objective_id": row["objective_id"],
                "status": row["status"],
                "priority": row["priority"],
                "projects": row["project_scope_json"],
                "created_at": row["created_at"],
            }
        )
    print_table(
        compact,
        [
            "objective_id",
            "status",
            "priority",
            "projects",
            "created_at",
        ],
    )


def command_show(arguments: argparse.Namespace) -> None:
    payload = run_json(
        [
            str(OBJECTIVES),
            "status",
            "--objective",
            arguments.objective,
        ]
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


def command_objective_action(arguments: argparse.Namespace) -> None:
    payload = run_json(
        [
            str(OBJECTIVES),
            arguments.command,
            "--objective",
            arguments.objective,
        ]
    )
    print(json.dumps(payload, indent=2, sort_keys=True))


def command_approvals(arguments: argparse.Namespace) -> None:
    query = """
        SELECT
            approval_id,
            run_id,
            status,
            question,
            options_json,
            decision,
            created_at,
            resolved_at
        FROM approvals
    """
    parameters: list[Any] = []
    if not arguments.all:
        query += " WHERE status = 'PENDING'"
    query += " ORDER BY created_at DESC LIMIT ?"
    parameters.append(arguments.limit)
    with connect() as connection:
        rows = [dict(row) for row in connection.execute(query, parameters)]
    if arguments.json:
        print(json.dumps(rows, indent=2, sort_keys=True))
        return
    print_table(
        rows,
        [
            "approval_id",
            "run_id",
            "status",
            "created_at",
            "question",
        ],
    )


def command_resolve(arguments: argparse.Namespace) -> None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM approvals
            WHERE approval_id = ?
            """,
            (arguments.approval,),
        ).fetchone()
    if row is None:
        fail(f"Unknown approval: {arguments.approval}")
    if row["status"] != "PENDING":
        fail(f"Approval is not pending: {row['status']}")
    options = json.loads(row["options_json"])
    if arguments.decision not in options:
        fail(
            f"Decision {arguments.decision} is not allowed; "
            f"allowed={options}"
        )
    from reviewer_judge import ReviewStore
    from recovery_coordinator import RecoveryCoordinator
    with connect() as connection:
        judge_gate = connection.execute("SELECT 1 FROM judge_decisions WHERE approval_id=?",
                                        (arguments.approval,)).fetchone()
    if judge_gate:
        payload = ReviewStore(connect).resolve_human(arguments.approval, arguments.decision)
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    with connect() as connection:
        recovery_gate = connection.execute(
            "SELECT 1 FROM recovery_actions WHERE approval_id=?",
            (arguments.approval,),
        ).fetchone()
    if recovery_gate:
        payload = RecoveryCoordinator(connect).resolve_exhaustion(
            arguments.approval, arguments.decision
        )
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    command = [
        str(RECOVERY),
        "recover",
        "--run",
        row["run_id"],
        "--owner",
        f"ops-operator:{socket.gethostname()}",
        "--force",
        "--expected-decision",
        arguments.decision,
    ]
    payload = run_json(command)
    print(json.dumps(payload, indent=2, sort_keys=True))


def command_notifications(arguments: argparse.Namespace) -> None:
    if arguments.status:
        payload = run_json([str(NOTIFIER), "status"])
    else:
        command = [str(NOTIFIER), "list", "--limit", str(arguments.limit)]
        if arguments.channel:
            command.extend(["--channel", arguments.channel])
        if arguments.delivery_status:
            command.extend(["--status", arguments.delivery_status])
        payload = run_json(command)
    print(json.dumps(payload, indent=2, sort_keys=True))


def command_self_test(_: argparse.Namespace) -> None:
    commands = {
        "submit",
        "queue",
        "show",
        "pause",
        "resume",
        "cancel",
        "approvals",
        "resolve",
        "notifications",
    }
    if len(commands) != 9:
        fail("Operator command matrix is incomplete")
    print("Orchestra operator control CLI: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="orchestractl",
        description="Orchestra operator control plane",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    submit = sub.add_parser("submit")
    submit.add_argument("--project", action="append", required=True)
    submit.add_argument("--text")
    submit.add_argument("--file")
    submit.add_argument("--priority", type=int, default=100)
    submit.add_argument("--not-before")
    submit.add_argument("--max-parallel", type=int, default=1)
    submit.add_argument("--planning-attempts", type=int, default=3)
    submit.set_defaults(function=command_submit)

    queue = sub.add_parser("queue")
    queue.add_argument("--active", action="store_true")
    queue.add_argument("--limit", type=int, default=50)
    queue.add_argument("--json", action="store_true")
    queue.set_defaults(function=command_queue)

    show = sub.add_parser("show")
    show.add_argument("objective")
    show.set_defaults(function=command_show)

    for name in ("pause", "resume", "cancel"):
        action = sub.add_parser(name)
        action.add_argument("objective")
        action.set_defaults(function=command_objective_action)

    approvals = sub.add_parser("approvals")
    approvals.add_argument("--all", action="store_true")
    approvals.add_argument("--limit", type=int, default=50)
    approvals.add_argument("--json", action="store_true")
    approvals.set_defaults(function=command_approvals)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("approval")
    resolve.add_argument(
        "decision",
        choices=("RESUME_SAFE", "ROLLBACK_SAFE", "ACKNOWLEDGE"),
    )
    resolve.set_defaults(function=command_resolve)

    notifications = sub.add_parser("notifications")
    notifications.add_argument("--status", action="store_true")
    notifications.add_argument("--limit", type=int, default=50)
    notifications.add_argument(
        "--channel",
        choices=("FILE", "TELEGRAM"),
    )
    notifications.add_argument(
        "--delivery-status",
        choices=(
            "PENDING",
            "DELIVERING",
            "RETRY",
            "DELIVERED",
            "DEAD_LETTER",
            "SUPPRESSED",
        ),
    )
    notifications.set_defaults(function=command_notifications)

    self_test = sub.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)
    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        arguments.function(arguments)
    except ControlError as error:
        print(f"Control error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
