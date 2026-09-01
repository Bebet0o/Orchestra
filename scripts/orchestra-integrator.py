#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(
    os.environ.get(
        "ORCHESTRA_ROOT",
        "/opt/orchestra",
    )
).resolve()

DATABASE = Path(
    os.environ.get(
        "ORCHESTRA_DB",
        str(ROOT / "state/controller/orchestra.db"),
    )
).resolve()

TRANSACTION_SCRIPT = ROOT / "repo/scripts/orchestra-transaction.py"


class IntegrationError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise IntegrationError(message)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def run_command(
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )

    if check and result.returncode != 0:
        fail(
            "Command failed: "
            + " ".join(arguments)
            + f"\nstdout:\n{result.stdout}"
            + f"\nstderr:\n{result.stderr}"
        )

    return result


def git(repository: Path, *arguments: str) -> str:
    return run_command(
        ["git", "-C", str(repository), *arguments]
    ).stdout.strip()


def load_transaction_module() -> Any:
    if not TRANSACTION_SCRIPT.is_file():
        fail(f"Transaction manager is absent: {TRANSACTION_SCRIPT}")

    spec = importlib.util.spec_from_file_location(
        "orchestra_transaction_runtime",
        TRANSACTION_SCRIPT,
    )

    if spec is None or spec.loader is None:
        fail("Cannot load transaction manager")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def parse_json_object(value: str, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(value)
    except json.JSONDecodeError as error:
        fail(f"Invalid {label}: {error}")

    if not isinstance(payload, dict):
        fail(f"{label} must be a JSON object")

    return payload


def classify_decision(decision: str, verdict: str) -> str:
    if decision == "APPROVE" and verdict in (
        "PASS",
        "PASS_WITH_DEBT",
    ):
        return "INTEGRATE"

    if decision == "REJECT" and verdict in (
        "FIX",
        "SECURITY",
        "PERFORMANCE",
        "ARCHITECTURE",
    ):
        return "REJECT"

    if decision == "BLOCK_HUMAN" and verdict == "HUMAN":
        return "BLOCK_HUMAN"

    fail(
        "Inconsistent review decision/verdict: "
        f"{decision}/{verdict}"
    )


def get_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
            r.*,
            p.repo_path,
            p.config_source,
            p.enabled AS project_enabled
        FROM runs AS r
        JOIN projects AS p
          ON p.project_id = r.project_id
        WHERE r.run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        fail(f"Unknown run: {run_id}")

    return row


def get_lock(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT *
        FROM project_locks
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        fail("Active transaction lock is absent")

    return row


def get_review(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    rows = connection.execute(
        """
        SELECT
            rr.review_id,
            rr.verdict AS review_verdict,
            rr.summary AS review_summary,
            rr.details_json AS review_details_json,
            rr.created_at AS review_created_at,
            re.execution_id AS review_execution_id,
            re.task_id AS review_task_id,
            re.role_id AS review_role_id,
            re.source_profile AS review_source_profile,
            re.workspace_mode AS review_workspace_mode,
            re.network_enabled AS review_network_enabled,
            re.mount_verified AS review_mount_verified,
            re.isolation_verified AS review_isolation_verified,
            re.repository_unchanged AS review_repository_unchanged,
            re.decision AS review_decision,
            re.verdict AS execution_verdict,
            re.exit_code AS review_exit_code,
            re.result_json AS review_result_json,
            re.failure_reason AS review_failure_reason,
            re.started_at AS review_started_at,
            re.finished_at AS review_finished_at,
            t.role AS review_task_role,
            t.status AS review_task_status
        FROM review_results AS rr
        JOIN reviewer_executions AS re
          ON re.review_id = rr.review_id
         AND re.run_id = rr.run_id
        JOIN tasks AS t
          ON t.task_id = re.task_id
         AND t.run_id = re.run_id
        WHERE rr.run_id = ?
        ORDER BY re.finished_at DESC
        """,
        (run_id,),
    ).fetchall()

    if not rows:
        fail("Review result is absent")

    if len(rows) != 1:
        fail(f"Expected exactly one review, found {len(rows)}")

    return rows[0]


def load_default_branch(config_source: str) -> str:
    path = Path(config_source)

    if not path.is_file():
        fail(f"Project configuration is absent: {path}")

    with path.open("rb") as stream:
        payload = tomllib.load(stream)

    branch = (payload.get("git") or {}).get("default_branch")

    if not isinstance(branch, str) or not branch:
        fail(f"git.default_branch is absent in {path}")

    return branch


def add_event(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
    event_type: str,
    severity: str = "INFO",
    payload: dict[str, Any] | None = None,
) -> None:
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
        VALUES (?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            project_id,
            run_id,
            event_type,
            severity,
            json.dumps(payload or {}, sort_keys=True),
            utc_now(),
        ),
    )


def validate_review(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    review: sqlite3.Row,
) -> tuple[str, str, str, dict[str, Any], dict[str, Any]]:
    decision = review["review_decision"]
    verdict = review["review_verdict"]

    if not isinstance(decision, str) or not isinstance(verdict, str):
        fail("Review decision or verdict is absent")

    action = classify_decision(decision, verdict)

    if review["execution_verdict"] != verdict:
        fail("Review and execution verdicts differ")

    if review["review_role_id"] != "reviewer":
        fail("Review was not produced by the reviewer role")

    if review["review_source_profile"] != "ops-reviewer":
        fail("Unexpected reviewer source profile")

    if review["review_task_role"] != "reviewer":
        fail("Reviewer task role is invalid")

    if review["review_task_status"] != "COMPLETED":
        fail("Reviewer task is not completed")

    if review["review_workspace_mode"] != "read_only":
        fail("Reviewer workspace was not read-only")

    if review["review_network_enabled"] != 0:
        fail("Reviewer network was enabled")

    for field in (
        "review_mount_verified",
        "review_isolation_verified",
        "review_repository_unchanged",
    ):
        if review[field] != 1:
            fail(f"Reviewer evidence is incomplete: {field}")

    if review["review_exit_code"] != 0:
        fail("Reviewer execution did not exit successfully")

    if review["review_failure_reason"] is not None:
        fail("Reviewer execution contains a failure reason")

    if not review["review_finished_at"]:
        fail("Reviewer execution is unfinished")

    if not run["submitted_at"]:
        fail("Run submission timestamp is absent")

    if review["review_created_at"] < run["submitted_at"]:
        fail("Review predates the submitted result")

    if review["review_finished_at"] < run["submitted_at"]:
        fail("Reviewer execution predates the submitted result")

    details = parse_json_object(
        review["review_details_json"],
        "review details_json",
    )
    result = parse_json_object(
        review["review_result_json"],
        "reviewer result_json",
    )

    if details.get("decision") != decision:
        fail("Structured review decision mismatch")

    if details.get("execution_id") != review["review_execution_id"]:
        fail("Structured review execution ID mismatch")

    if result.get("decision") != decision:
        fail("Reviewer result decision mismatch")

    if result.get("verdict") != verdict:
        fail("Reviewer result verdict mismatch")

    if result.get("result_commit") != run["result_commit"]:
        fail("Review is stale for the current result commit")

    if result.get("marker_found") is not True:
        fail("Reviewer completion marker was not verified")

    if result.get("repository_unchanged") is not True:
        fail("Reviewer did not prove repository immutability")

    later_workers = connection.execute(
        """
        SELECT COUNT(*)
        FROM worker_executions
        WHERE run_id = ?
          AND finished_at IS NOT NULL
          AND finished_at > ?
        """,
        (run["run_id"], review["review_finished_at"]),
    ).fetchone()[0]

    if later_workers != 0:
        fail("Review is stale: a worker completed after the review")

    return action, decision, verdict, details, result


def validate_git_state(
    run: sqlite3.Row,
    reviewed_result: dict[str, Any],
    transaction: Any,
) -> dict[str, Any]:
    repository = Path(run["repo_path"]).resolve()
    worktree = Path(run["worktree_path"]).resolve()
    default_branch = load_default_branch(run["config_source"])

    if not repository.is_dir():
        fail(f"Project repository is absent: {repository}")

    if not worktree.is_dir():
        fail(f"Transaction worktree is absent: {worktree}")

    if not transaction.worktree_registered(repository, worktree):
        fail("Transaction worktree is not registered")

    if git(repository, "branch", "--show-current") != default_branch:
        fail("Project repository is not on its default branch")

    repository_status = git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    if repository_status:
        fail("Project repository is not clean")

    main_before = git(repository, "rev-parse", "HEAD")

    if main_before != run["base_commit"]:
        fail(
            "Default branch moved since transaction start: "
            f"{main_before} != {run['base_commit']}"
        )

    if git(worktree, "branch", "--show-current") != run["branch_name"]:
        fail("Transaction worktree branch mismatch")

    worktree_status = git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    if worktree_status:
        fail("Reviewed transaction worktree is no longer clean")

    worktree_head = git(worktree, "rev-parse", "HEAD")

    if worktree_head != run["result_commit"]:
        fail("Transaction worktree no longer matches run.result_commit")

    if reviewed_result.get("result_commit") != worktree_head:
        fail("Reviewed commit no longer matches transaction HEAD")

    ancestor = run_command(
        [
            "git",
            "-C",
            str(repository),
            "merge-base",
            "--is-ancestor",
            run["base_commit"],
            worktree_head,
        ],
        check=False,
    )

    if ancestor.returncode != 0:
        fail("Reviewed result does not descend from transaction base")

    return {
        "repository": str(repository),
        "worktree": str(worktree),
        "default_branch": default_branch,
        "main_before": main_before,
        "reviewed_commit": worktree_head,
        "repository_clean": True,
        "worktree_clean": True,
    }


def verify_snapshot_now(
    connection: sqlite3.Connection,
    run_id: str,
    transaction: Any,
) -> dict[str, Any]:
    connection.execute("BEGIN IMMEDIATE")
    result = transaction.verify_snapshot(connection, run_id)
    connection.commit()

    if result.get("verified") is not True:
        fail("Snapshot verification did not succeed")

    return result


def insert_integration(
    connection: sqlite3.Connection,
    *,
    integration_id: str,
    run: sqlite3.Row,
    review: sqlite3.Row,
    owner: str,
    decision: str,
    verdict: str,
    status: str,
    evidence: dict[str, Any],
    snapshot_verified: bool,
    review_current: bool,
    approval_id: str | None = None,
    finished: bool = False,
) -> None:
    now = utc_now()
    connection.execute(
        """
        INSERT INTO integration_executions (
            integration_id,
            run_id,
            review_id,
            review_execution_id,
            controller_owner,
            decision,
            verdict,
            status,
            base_commit,
            reviewed_commit,
            main_before,
            main_after,
            snapshot_verified,
            review_current,
            approval_id,
            details_json,
            failure_reason,
            created_at,
            started_at,
            finished_at
        )
        VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?
        )
        """,
        (
            integration_id,
            run["run_id"],
            review["review_id"],
            review["review_execution_id"],
            owner,
            decision,
            verdict,
            status,
            run["base_commit"],
            run["result_commit"],
            evidence["main_before"],
            evidence["main_before"],
            1 if snapshot_verified else 0,
            1 if review_current else 0,
            approval_id,
            json.dumps(evidence, sort_keys=True),
            now,
            now,
            now if finished else None,
        ),
    )


def validate_owner(
    run: sqlite3.Row,
    lock: sqlite3.Row,
    owner: str,
) -> None:
    if not owner:
        fail("Controller owner is required")

    if run["transaction_owner"] != owner:
        fail(
            "Controller owner does not match transaction owner: "
            f"{owner} != {run['transaction_owner']}"
        )

    if lock["holder"] != owner:
        fail(
            "Controller owner does not hold the project lock: "
            f"{owner} != {lock['holder']}"
        )

    if lock["project_id"] != run["project_id"]:
        fail("Project lock does not belong to the run project")


def objective_status_for_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> str | None:
    rows = connection.execute(
        """
        SELECT DISTINCT objective.objective_id, objective.status
        FROM orchestration_attempts AS attempt
        JOIN orchestration_tasks AS task
          ON task.orchestration_task_id = attempt.orchestration_task_id
        JOIN objective_queue AS objective
          ON objective.plan_id = task.plan_id
        WHERE attempt.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    if len(rows) > 1:
        fail("Run is linked to multiple objectives")
    return str(rows[0]["status"]) if rows else None


def run_plan_has_active_human_gate(
    connection: sqlite3.Connection,
    run_id: str,
) -> bool:
    rows = connection.execute(
        """
        SELECT DISTINCT plan.plan_id
        FROM orchestration_attempts AS attempt
        JOIN orchestration_tasks AS task
          ON task.orchestration_task_id = attempt.orchestration_task_id
        JOIN orchestration_plans AS plan ON plan.plan_id = task.plan_id
        WHERE attempt.run_id = ?
        """,
        (run_id,),
    ).fetchall()
    if len(rows) > 1:
        fail("Run is linked to multiple orchestration plans")
    if not rows:
        return False

    pending_approval = connection.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM approvals AS approval
            JOIN orchestration_attempts AS gate_attempt
              ON gate_attempt.run_id = approval.run_id
            JOIN orchestration_tasks AS gate_task
              ON gate_task.orchestration_task_id = gate_attempt.orchestration_task_id
            WHERE gate_task.plan_id = ?
              AND approval.status = 'PENDING'
        )
        """,
        (rows[0]["plan_id"],),
    ).fetchone()[0]
    return bool(pending_approval)


def cancelled_integration_result(run_id: str) -> dict[str, Any]:
    return {
        "integration_id": None,
        "run_id": run_id,
        "action": "CANCEL",
        "status": "CANCELLED",
        "integrated": False,
        "reason_code": "objective_cancel_requested",
    }


def blocked_human_integration_result(run_id: str) -> dict[str, Any]:
    return {
        "integration_id": None,
        "run_id": run_id,
        "action": "BLOCK_HUMAN",
        "status": "BLOCKED",
        "integrated": False,
        "reason_code": "plan_waiting_human",
    }


def record_non_integration(
    *,
    run: sqlite3.Row,
    review: sqlite3.Row,
    owner: str,
    decision: str,
    verdict: str,
    action: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    integration_id = "integration-" + uuid.uuid4().hex
    approval_id: str | None = None
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_run = get_run(connection, run["run_id"])
        lock = get_lock(connection, run["run_id"])
        validate_owner(current_run, lock, owner)

        if current_run["status"] != "REVIEWING":
            connection.rollback()
            fail(f"Run is no longer REVIEWING: {current_run['status']}")

        if objective_status_for_run(connection, run["run_id"]) in {
            "CANCEL_REQUESTED",
            "CANCELLED",
        }:
            connection.rollback()
            return cancelled_integration_result(run["run_id"])

        if run_plan_has_active_human_gate(connection, run["run_id"]):
            connection.rollback()
            return blocked_human_integration_result(run["run_id"])

        if action == "REJECT":
            status = "REJECTED"
            event_type = "INTEGRATION_REJECTED"
            severity = "WARNING"
        elif action == "BLOCK_HUMAN":
            status = "BLOCKED"
            event_type = "INTEGRATION_BLOCKED_HUMAN"
            severity = "WARNING"
            approval_id = "approval-" + uuid.uuid4().hex

            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id,
                    run_id,
                    status,
                    question,
                    options_json,
                    decision,
                    created_at,
                    resolved_at
                )
                VALUES (?, ?, 'PENDING', ?, ?, NULL, ?, NULL)
                """,
                (
                    approval_id,
                    run["run_id"],
                    "Reviewer requires a human recovery decision.",
                    json.dumps(
                        ["RESUME_SAFE", "ROLLBACK_SAFE"],
                        sort_keys=True,
                    ),
                    now,
                ),
            )

            connection.execute(
                """
                UPDATE runs
                SET status = 'WAITING_HUMAN',
                    recovery_decision = 'BLOCK_HUMAN',
                    heartbeat_at = ?
                WHERE run_id = ?
                  AND status = 'REVIEWING'
                """,
                (now, run["run_id"]),
            )
        else:
            connection.rollback()
            fail(f"Unsupported non-integration action: {action}")

        insert_integration(
            connection,
            integration_id=integration_id,
            run=current_run,
            review=review,
            owner=owner,
            decision=decision,
            verdict=verdict,
            status=status,
            evidence=evidence,
            snapshot_verified=True,
            review_current=True,
            approval_id=approval_id,
            finished=True,
        )

        connection.execute(
            """
            UPDATE project_locks
            SET heartbeat_at = ?
            WHERE run_id = ?
            """,
            (now, run["run_id"]),
        )

        add_event(
            connection,
            project_id=run["project_id"],
            run_id=run["run_id"],
            event_type=event_type,
            severity=severity,
            payload={
                "integration_id": integration_id,
                "decision": decision,
                "verdict": verdict,
                "approval_id": approval_id,
            },
        )
        connection.commit()

    return {
        "integration_id": integration_id,
        "run_id": run["run_id"],
        "action": action,
        "decision": decision,
        "verdict": verdict,
        "status": status,
        "integrated": False,
        "main_before": evidence["main_before"],
        "main_after": evidence["main_before"],
        "approval_id": approval_id,
        "snapshot_verified": True,
        "review_current": True,
    }


def finalize_completed(
    *,
    integration_id: str,
    run: sqlite3.Row,
    main_after: str,
) -> None:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = get_run(connection, run["run_id"])

        if current["status"] != "COMMITTING":
            connection.rollback()
            fail(
                "Cannot finalize integration from status "
                f"{current['status']}"
            )

        if current["result_commit"] != main_after:
            connection.rollback()
            fail("Final main commit differs from reviewed result")

        connection.execute(
            """
            UPDATE integration_executions
            SET status = 'COMPLETED',
                main_after = ?,
                finished_at = ?
            WHERE integration_id = ?
              AND status = 'PREPARED'
            """,
            (main_after, now, integration_id),
        )

        if connection.total_changes == 0:
            connection.rollback()
            fail("Prepared integration record is absent")

        connection.execute(
            """
            DELETE FROM project_locks
            WHERE run_id = ?
            """,
            (run["run_id"],),
        )

        connection.execute(
            """
            UPDATE runs
            SET status = 'COMPLETED',
                recovery_decision = NULL,
                finished_at = ?,
                heartbeat_at = ?
            WHERE run_id = ?
              AND status = 'COMMITTING'
            """,
            (now, now, run["run_id"]),
        )

        add_event(
            connection,
            project_id=run["project_id"],
            run_id=run["run_id"],
            event_type="INTEGRATION_COMPLETED",
            payload={
                "integration_id": integration_id,
                "result_commit": main_after,
            },
        )
        connection.commit()


def mark_ambiguous_failure(
    *,
    integration_id: str,
    run: sqlite3.Row,
    reason: str,
    main_after: str | None,
) -> None:
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE integration_executions
            SET status = 'FAILED',
                main_after = COALESCE(?, main_after),
                failure_reason = ?,
                finished_at = ?
            WHERE integration_id = ?
            """,
            (main_after, reason, now, integration_id),
        )
        connection.execute(
            """
            UPDATE runs
            SET status = 'RECOVERING',
                recovery_decision = 'BLOCK_HUMAN',
                heartbeat_at = ?
            WHERE run_id = ?
              AND status = 'COMMITTING'
            """,
            (now, run["run_id"]),
        )
        add_event(
            connection,
            project_id=run["project_id"],
            run_id=run["run_id"],
            event_type="INTEGRATION_FAILED_AMBIGUOUS",
            severity="CRITICAL",
            payload={
                "integration_id": integration_id,
                "reason": reason,
                "main_after": main_after,
            },
        )
        connection.commit()


def integrate_approved(
    *,
    run: sqlite3.Row,
    review: sqlite3.Row,
    owner: str,
    decision: str,
    verdict: str,
    evidence: dict[str, Any],
    transaction: Any,
) -> dict[str, Any]:
    integration_id = "integration-" + uuid.uuid4().hex
    repository = Path(evidence["repository"])
    worktree = Path(evidence["worktree"])

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current_run = get_run(connection, run["run_id"])
        lock = get_lock(connection, run["run_id"])
        validate_owner(current_run, lock, owner)

        if current_run["status"] != "REVIEWING":
            connection.rollback()
            fail(f"Run is no longer REVIEWING: {current_run['status']}")

        # This BEGIN IMMEDIATE transaction serializes cancellation with the
        # authoritative transition from REVIEWING to COMMITTING. A cancelled
        # objective never receives a PREPARED integration record.
        if objective_status_for_run(connection, run["run_id"]) in {
            "CANCEL_REQUESTED",
            "CANCELLED",
        }:
            connection.rollback()
            return cancelled_integration_result(run["run_id"])

        if run_plan_has_active_human_gate(connection, run["run_id"]):
            connection.rollback()
            return blocked_human_integration_result(run["run_id"])

        duplicate = connection.execute(
            """
            SELECT COUNT(*)
            FROM integration_executions
            WHERE run_id = ?
              AND status IN ('PREPARED', 'COMPLETED')
            """,
            (run["run_id"],),
        ).fetchone()[0]

        if duplicate != 0:
            connection.rollback()
            fail("Run already has an active or completed integration")

        insert_integration(
            connection,
            integration_id=integration_id,
            run=current_run,
            review=review,
            owner=owner,
            decision=decision,
            verdict=verdict,
            status="PREPARED",
            evidence=evidence,
            snapshot_verified=True,
            review_current=True,
        )

        now = utc_now()
        cursor = connection.execute(
            """
            UPDATE runs
            SET status = 'COMMITTING',
                heartbeat_at = ?
            WHERE run_id = ?
              AND status = 'REVIEWING'
              AND result_commit = ?
              AND base_commit = ?
            """,
            (
                now,
                run["run_id"],
                run["result_commit"],
                run["base_commit"],
            ),
        )

        if cursor.rowcount != 1:
            connection.rollback()
            fail("Run changed before integration preparation")

        connection.execute(
            """
            UPDATE project_locks
            SET heartbeat_at = ?
            WHERE run_id = ?
            """,
            (now, run["run_id"]),
        )

        add_event(
            connection,
            project_id=run["project_id"],
            run_id=run["run_id"],
            event_type="INTEGRATION_PREPARED",
            payload={
                "integration_id": integration_id,
                "review_id": review["review_id"],
                "result_commit": run["result_commit"],
            },
        )
        connection.commit()

    try:
        git(
            repository,
            "merge",
            "--ff-only",
            "--no-edit",
            run["result_commit"],
        )

        main_after = git(repository, "rev-parse", "HEAD")

        if main_after != run["result_commit"]:
            fail("Default branch did not reach the reviewed commit")

        if git(
            repository,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
        ):
            fail("Default branch is dirty after fast-forward")

        transaction.cleanup_worktree(
            repository,
            worktree,
            run["branch_name"],
        )

        finalize_completed(
            integration_id=integration_id,
            run=run,
            main_after=main_after,
        )
    except Exception as error:
        main_after: str | None = None

        try:
            main_after = git(repository, "rev-parse", "HEAD")
        except Exception:
            pass

        if main_after == run["result_commit"]:
            try:
                transaction.cleanup_worktree(
                    repository,
                    worktree,
                    run["branch_name"],
                )
                finalize_completed(
                    integration_id=integration_id,
                    run=run,
                    main_after=main_after,
                )
            except Exception as recovery_error:
                mark_ambiguous_failure(
                    integration_id=integration_id,
                    run=run,
                    reason=(
                        f"{error}; completion recovery failed: "
                        f"{recovery_error}"
                    ),
                    main_after=main_after,
                )
                raise
        else:
            mark_ambiguous_failure(
                integration_id=integration_id,
                run=run,
                reason=str(error),
                main_after=main_after,
            )
            raise

    return {
        "integration_id": integration_id,
        "run_id": run["run_id"],
        "action": "INTEGRATE",
        "decision": decision,
        "verdict": verdict,
        "status": "COMPLETED",
        "integrated": True,
        "base_commit": run["base_commit"],
        "reviewed_commit": run["result_commit"],
        "main_before": evidence["main_before"],
        "main_after": run["result_commit"],
        "snapshot_verified": True,
        "review_current": True,
        "worktree_removed": not worktree.exists(),
    }


def command_apply(arguments: argparse.Namespace) -> None:
    transaction = load_transaction_module()

    with connect() as connection:
        run = get_run(connection, arguments.run)

        if run["status"] != "REVIEWING":
            fail(f"Run cannot be integrated from status {run['status']}")

        if not run["project_enabled"]:
            fail("Run project is disabled")

        lock = get_lock(connection, arguments.run)
        validate_owner(run, lock, arguments.owner)
        review = get_review(connection, arguments.run)
        action, decision, verdict, _, reviewed_result = validate_review(
            connection,
            run,
            review,
        )

    evidence = validate_git_state(
        run,
        reviewed_result,
        transaction,
    )

    with connect() as connection:
        snapshot = verify_snapshot_now(
            connection,
            arguments.run,
            transaction,
        )

    evidence["snapshot"] = snapshot
    evidence["review_id"] = review["review_id"]
    evidence["review_execution_id"] = review["review_execution_id"]
    evidence["review_finished_at"] = review["review_finished_at"]
    evidence["review_current"] = True

    if action == "INTEGRATE":
        result = integrate_approved(
            run=run,
            review=review,
            owner=arguments.owner,
            decision=decision,
            verdict=verdict,
            evidence=evidence,
            transaction=transaction,
        )
    else:
        result = record_non_integration(
            run=run,
            review=review,
            owner=arguments.owner,
            decision=decision,
            verdict=verdict,
            action=action,
            evidence=evidence,
        )

    print(json.dumps(result, indent=2, sort_keys=True))


def command_status(arguments: argparse.Namespace) -> None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                ie.*,
                r.status AS run_status,
                r.recovery_decision,
                a.status AS approval_status,
                a.decision AS approval_decision
            FROM integration_executions AS ie
            JOIN runs AS r
              ON r.run_id = ie.run_id
            LEFT JOIN approvals AS a
              ON a.approval_id = ie.approval_id
            WHERE ie.integration_id = ?
            """,
            (arguments.integration,),
        ).fetchone()

    if row is None:
        fail(f"Unknown integration: {arguments.integration}")

    print(json.dumps(dict(row), indent=2, sort_keys=True))


def command_self_test(_: argparse.Namespace) -> None:
    expected = {
        ("APPROVE", "PASS"): "INTEGRATE",
        ("APPROVE", "PASS_WITH_DEBT"): "INTEGRATE",
        ("REJECT", "FIX"): "REJECT",
        ("REJECT", "SECURITY"): "REJECT",
        ("REJECT", "PERFORMANCE"): "REJECT",
        ("REJECT", "ARCHITECTURE"): "REJECT",
        ("BLOCK_HUMAN", "HUMAN"): "BLOCK_HUMAN",
    }

    for pair, action in expected.items():
        actual = classify_decision(*pair)

        if actual != action:
            fail(f"Decision matrix mismatch: {pair} -> {actual}")

    invalid = (
        ("APPROVE", "FIX"),
        ("REJECT", "PASS"),
        ("BLOCK_HUMAN", "PASS"),
    )

    for pair in invalid:
        try:
            classify_decision(*pair)
        except IntegrationError:
            continue

        fail(f"Invalid decision matrix pair was accepted: {pair}")

    print("Orchestra integration decision matrix: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra reviewed integration gate"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    apply_parser = subparsers.add_parser("apply")
    apply_parser.add_argument("--run", required=True)
    apply_parser.add_argument("--owner", required=True)
    apply_parser.set_defaults(function=command_apply)

    status = subparsers.add_parser("status")
    status.add_argument("--integration", required=True)
    status.set_defaults(function=command_status)

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        arguments.function(arguments)
    except IntegrationError as error:
        print(f"Integration error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
