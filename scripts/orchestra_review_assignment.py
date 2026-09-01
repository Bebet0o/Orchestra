#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.event_journal import EventJournal, utc_now

ASSIGNMENT_ID_PATTERN = re.compile(r"^review-assignment-[0-9a-f]{32}$")
FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
ACTIVE_STATUSES = {"ASSIGNED", "CLAIMED"}
TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


class ReviewerAssignmentError(RuntimeError):
    pass


def validate_assignment_id(value: object) -> str:
    if not isinstance(value, str) or not ASSIGNMENT_ID_PATTERN.fullmatch(value):
        raise ReviewerAssignmentError("Invalid reviewer assignment identifier")
    return value


def _validate_text(value: object, *, field: str, maximum: int = 200) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ReviewerAssignmentError(f"Invalid {field}")
    return value


def _validate_failure_code(value: str) -> str:
    if not FAILURE_CODE_PATTERN.fullmatch(value):
        raise ReviewerAssignmentError("Invalid reviewer assignment failure code")
    return value


def _require_transaction(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise ReviewerAssignmentError(
            "Reviewer assignment mutation requires an active transaction"
        )


def _role(connection: sqlite3.Connection, role_id: str) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT role_id, profile_name, role_kind, workspace_mode,
               may_commit, may_push, network_enabled, enabled
        FROM roles
        WHERE role_id=?
        """,
        (role_id,),
    ).fetchone()
    if row is None:
        raise ReviewerAssignmentError("Reviewer role is unavailable")
    if (
        int(row["enabled"]) != 1
        or str(row["role_kind"]) != "reviewer"
        or str(row["workspace_mode"]) != "read_only"
        or int(row["may_commit"]) != 0
        or int(row["may_push"]) != 0
        or int(row["network_enabled"]) != 0
    ):
        raise ReviewerAssignmentError("Reviewer role violates assignment policy")
    return row


def _run_project(connection: sqlite3.Connection, run_id: str) -> str:
    row = connection.execute(
        "SELECT project_id FROM runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None or not isinstance(row["project_id"], str):
        raise ReviewerAssignmentError("Reviewer assignment run is unavailable")
    return str(row["project_id"])


def _legacy_event(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
    task_id: str | None,
    event_type: str,
    severity: str,
    payload: dict[str, Any],
    occurred_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            project_id, run_id, task_id, event_type,
            severity, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            run_id,
            task_id,
            event_type,
            severity,
            json.dumps(payload, sort_keys=True, separators=(",", ":")),
            occurred_at,
        ),
    )


def _journal(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    assignment_id: str,
    actor_id: str,
    causation_id: str,
    project_id: str,
    data: dict[str, Any],
    occurred_at: str,
) -> None:
    EventJournal.emit(
        connection,
        event_type=event_type,
        actor_type="system",
        actor_id=actor_id,
        aggregate_type="review",
        aggregate_id=assignment_id,
        correlation_id=EventJournal.correlation_for_causation(causation_id),
        causation_id=causation_id,
        project_id=project_id,
        objective_id=None,
        data=data,
        occurred_at=occurred_at,
    )


def next_assignment_number(
    connection: sqlite3.Connection,
    *,
    run_id: str,
) -> int:
    row = connection.execute(
        "SELECT COALESCE(MAX(assignment_number), 0) + 1 "
        "FROM reviewer_assignments WHERE run_id=?",
        (run_id,),
    ).fetchone()
    value = int(row[0])
    if not 1 <= value <= 1000:
        raise ReviewerAssignmentError("Reviewer assignment sequence is exhausted")
    return value


def create_assignment(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    orchestration_attempt_id: str,
    assignment_number: int,
    role_id: str,
    assigned_by: str,
) -> dict[str, Any]:
    _require_transaction(connection)
    run_id = _validate_text(run_id, field="run_id")
    orchestration_attempt_id = _validate_text(
        orchestration_attempt_id,
        field="orchestration_attempt_id",
    )
    assigned_by = _validate_text(assigned_by, field="assigned_by")
    if type(assignment_number) is not int or not 1 <= assignment_number <= 1000:
        raise ReviewerAssignmentError("Invalid reviewer assignment number")
    role = _role(connection, role_id)
    project_id = _run_project(connection, run_id)
    attempt = connection.execute(
        """
        SELECT attempt_id, run_id
        FROM orchestration_attempts
        WHERE attempt_id=?
        """,
        (orchestration_attempt_id,),
    ).fetchone()
    if attempt is None or str(attempt["run_id"] or "") != run_id:
        raise ReviewerAssignmentError(
            "Reviewer assignment does not match the orchestration attempt"
        )
    assignment_id = "review-assignment-" + uuid.uuid4().hex
    now = utc_now()
    connection.execute(
        """
        INSERT INTO reviewer_assignments (
            assignment_id, run_id, orchestration_attempt_id,
            assignment_number, role_id, source_profile, status,
            assigned_by, claim_owner, review_execution_id, review_id,
            failure_code, assigned_at, claimed_at, heartbeat_at, finished_at
        ) VALUES (?, ?, ?, ?, ?, ?, 'ASSIGNED', ?, NULL, NULL, NULL,
                  NULL, ?, NULL, NULL, NULL)
        """,
        (
            assignment_id,
            run_id,
            orchestration_attempt_id,
            assignment_number,
            str(role["role_id"]),
            str(role["profile_name"]),
            assigned_by,
            now,
        ),
    )
    data = {
        "assignment_number": assignment_number,
        "role_id": str(role["role_id"]),
        "source_profile": str(role["profile_name"]),
        "status": "assigned",
    }
    _legacy_event(
        connection,
        project_id=project_id,
        run_id=run_id,
        task_id=None,
        event_type="REVIEW_ASSIGNMENT_CREATED",
        severity="INFO",
        payload={"assignment_id": assignment_id, **data},
        occurred_at=now,
    )
    _journal(
        connection,
        event_type="review.assignment_created",
        assignment_id=assignment_id,
        actor_id=assigned_by,
        causation_id=orchestration_attempt_id,
        project_id=project_id,
        data=data,
        occurred_at=now,
    )
    return {
        "assignment_id": assignment_id,
        "run_id": run_id,
        "assignment_number": assignment_number,
        "role_id": str(role["role_id"]),
        "source_profile": str(role["profile_name"]),
        "status": "ASSIGNED",
    }


def claim_assignment(
    connection: sqlite3.Connection,
    *,
    assignment_id: str,
    run_id: str,
    role_id: str,
    source_profile: str,
    review_execution_id: str,
    task_id: str,
) -> None:
    _require_transaction(connection)
    assignment_id = validate_assignment_id(assignment_id)
    review_execution_id = _validate_text(
        review_execution_id,
        field="review_execution_id",
    )
    task_id = _validate_text(task_id, field="task_id")
    row = connection.execute(
        "SELECT * FROM reviewer_assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        raise ReviewerAssignmentError("Reviewer assignment is unavailable")
    if (
        str(row["status"]) != "ASSIGNED"
        or str(row["run_id"]) != run_id
        or str(row["role_id"]) != role_id
        or str(row["source_profile"]) != source_profile
    ):
        raise ReviewerAssignmentError("Reviewer assignment cannot be claimed")
    now = utc_now()
    owner = "reviewer:" + review_execution_id
    cursor = connection.execute(
        """
        UPDATE reviewer_assignments
        SET status='CLAIMED', claim_owner=?, review_execution_id=?,
            claimed_at=?, heartbeat_at=?
        WHERE assignment_id=? AND status='ASSIGNED'
        """,
        (owner, review_execution_id, now, now, assignment_id),
    )
    if cursor.rowcount != 1:
        raise ReviewerAssignmentError("Reviewer assignment claim was lost")
    project_id = _run_project(connection, run_id)
    data = {
        "assignment_number": int(row["assignment_number"]),
        "review_execution_id": review_execution_id,
        "role_id": role_id,
        "status": "claimed",
    }
    _legacy_event(
        connection,
        project_id=project_id,
        run_id=run_id,
        task_id=task_id,
        event_type="REVIEW_ASSIGNMENT_CLAIMED",
        severity="INFO",
        payload={"assignment_id": assignment_id, **data},
        occurred_at=now,
    )
    _journal(
        connection,
        event_type="review.assignment_claimed",
        assignment_id=assignment_id,
        actor_id=owner,
        causation_id=review_execution_id,
        project_id=project_id,
        data=data,
        occurred_at=now,
    )


def finish_assignment(
    connection: sqlite3.Connection,
    *,
    assignment_id: str,
    run_id: str,
    review_execution_id: str,
    task_id: str,
    success: bool,
    review_id: str | None,
    failure_code: str = "REVIEW_EXECUTION_FAILED",
) -> None:
    _require_transaction(connection)
    assignment_id = validate_assignment_id(assignment_id)
    row = connection.execute(
        "SELECT * FROM reviewer_assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        raise ReviewerAssignmentError("Reviewer assignment is unavailable")
    if (
        str(row["status"]) != "CLAIMED"
        or str(row["run_id"]) != run_id
        or str(row["review_execution_id"]) != review_execution_id
    ):
        raise ReviewerAssignmentError("Reviewer assignment cannot be finished")
    now = utc_now()
    project_id = _run_project(connection, run_id)
    if success:
        if not isinstance(review_id, str) or not review_id:
            raise ReviewerAssignmentError("Completed assignment requires a review")
        status = "COMPLETED"
        stored_review_id = review_id
        stored_failure = None
        event_type = "review.assignment_completed"
        legacy_type = "REVIEW_ASSIGNMENT_COMPLETED"
        severity = "INFO"
    else:
        status = "FAILED"
        stored_review_id = None
        stored_failure = _validate_failure_code(failure_code)
        event_type = "review.assignment_failed"
        legacy_type = "REVIEW_ASSIGNMENT_FAILED"
        severity = "ERROR"
    cursor = connection.execute(
        """
        UPDATE reviewer_assignments
        SET status=?, review_id=?, failure_code=?, heartbeat_at=?, finished_at=?
        WHERE assignment_id=? AND status='CLAIMED'
        """,
        (
            status,
            stored_review_id,
            stored_failure,
            now,
            now,
            assignment_id,
        ),
    )
    if cursor.rowcount != 1:
        raise ReviewerAssignmentError("Reviewer assignment finish was lost")
    data = {
        "assignment_number": int(row["assignment_number"]),
        "review_execution_id": review_execution_id,
        "review_present": stored_review_id is not None,
        "failure_code": stored_failure,
        "status": status.lower(),
    }
    _legacy_event(
        connection,
        project_id=project_id,
        run_id=run_id,
        task_id=task_id,
        event_type=legacy_type,
        severity=severity,
        payload={"assignment_id": assignment_id, **data},
        occurred_at=now,
    )
    _journal(
        connection,
        event_type=event_type,
        assignment_id=assignment_id,
        actor_id="reviewer:" + review_execution_id,
        causation_id=review_execution_id,
        project_id=project_id,
        data=data,
        occurred_at=now,
    )


def fail_active_assignment(
    connection: sqlite3.Connection,
    *,
    assignment_id: str,
    actor_id: str,
    failure_code: str,
) -> bool:
    _require_transaction(connection)
    assignment_id = validate_assignment_id(assignment_id)
    actor_id = _validate_text(actor_id, field="actor_id")
    failure_code = _validate_failure_code(failure_code)
    row = connection.execute(
        "SELECT * FROM reviewer_assignments WHERE assignment_id=?",
        (assignment_id,),
    ).fetchone()
    if row is None:
        raise ReviewerAssignmentError("Reviewer assignment is unavailable")
    status = str(row["status"])
    if status in TERMINAL_STATUSES:
        return False
    if status not in ACTIVE_STATUSES:
        raise ReviewerAssignmentError("Unknown reviewer assignment status")
    now = utc_now()
    cursor = connection.execute(
        """
        UPDATE reviewer_assignments
        SET status='FAILED', review_id=NULL, failure_code=?,
            heartbeat_at=CASE
                WHEN status='CLAIMED' THEN COALESCE(heartbeat_at, ?)
                ELSE NULL
            END,
            finished_at=?
        WHERE assignment_id=? AND status IN ('ASSIGNED', 'CLAIMED')
        """,
        (failure_code, now, now, assignment_id),
    )
    if cursor.rowcount != 1:
        raise ReviewerAssignmentError("Reviewer assignment failure was lost")
    run_id = str(row["run_id"])
    project_id = _run_project(connection, run_id)
    data = {
        "assignment_number": int(row["assignment_number"]),
        "failure_code": failure_code,
        "status": "failed",
    }
    _legacy_event(
        connection,
        project_id=project_id,
        run_id=run_id,
        task_id=None,
        event_type="REVIEW_ASSIGNMENT_FAILED",
        severity="ERROR",
        payload={"assignment_id": assignment_id, **data},
        occurred_at=now,
    )
    _journal(
        connection,
        event_type="review.assignment_failed",
        assignment_id=assignment_id,
        actor_id=actor_id,
        causation_id=str(row["orchestration_attempt_id"]),
        project_id=project_id,
        data=data,
        occurred_at=now,
    )
    return True


def recover_active_assignments(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    actor_id: str,
) -> int:
    _require_transaction(connection)
    rows = list(
        connection.execute(
            """
            SELECT assignment_id
            FROM reviewer_assignments
            WHERE run_id=? AND status IN ('ASSIGNED', 'CLAIMED')
            ORDER BY assignment_number
            """,
            (run_id,),
        )
    )
    recovered = 0
    for row in rows:
        if fail_active_assignment(
            connection,
            assignment_id=str(row["assignment_id"]),
            actor_id=actor_id,
            failure_code="RECOVERY_ABANDONED",
        ):
            recovered += 1
    return recovered
