"""Durable, bounded authority for corrective task attempts."""

from __future__ import annotations

import contextlib
import json
import uuid
from typing import Any, Callable

from shared_context import canonical_json, content_sha256, utc_now


RECOVERY_POLICY_VERSION = 1
MAX_RECOVERY_RETRIES = 3


class RecoveryError(RuntimeError):
    pass


def policy_authority(max_retries: int) -> tuple[str, str]:
    if (
        not isinstance(max_retries, int)
        or isinstance(max_retries, bool)
        or not 0 <= max_retries <= MAX_RECOVERY_RETRIES
    ):
        raise RecoveryError("Recovery max_retries must be 0..3")
    value = {
        "schema_version": RECOVERY_POLICY_VERSION,
        "max_retries": max_retries,
    }
    return canonical_json(value), content_sha256(value)


class RecoveryCoordinator:
    """Translate each durable NEEDS_FIX decision into at most one action."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise TypeError("Recovery connection factory must be callable")
        self._connect = connection_factory

    @contextlib.contextmanager
    def _transaction(self):  # type: ignore[no-untyped-def]
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _event(
        connection: Any,
        action: Any,
        kind: str,
        *,
        target_attempt_id: str | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO recovery_events (
                recovery_action_id, event_kind, task_id, source_attempt_id,
                source_review_id, source_decision_id, target_attempt_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                action["recovery_action_id"],
                kind,
                action["task_id"],
                action["source_attempt_id"],
                action["source_review_id"],
                action["source_decision_id"],
                target_attempt_id,
                utc_now(),
            ),
        )

    @staticmethod
    def _reason(review: Any) -> dict[str, Any]:
        structured = json.loads(review["canonical_json"])
        return {
            "schema_version": 1,
            "source_attempt_id": review["attempt_id"],
            "previous_result": {
                "reference": review["worker_result_reference"],
                "sha256": review["worker_result_sha256"],
            },
            "source_review": {
                "review_id": review["review_id"],
                "sha256": review["result_sha256"],
            },
            "judge": {
                "decision_id": review["decision_id"],
                "disposition": "NEEDS_FIX",
            },
            "summary": structured["summary"],
            "findings": structured["findings"],
            "required_changes": structured["required_changes"],
        }

    def request_eligible(self) -> int:
        """Create one action or one exhaustion record for each new decision."""
        created = 0
        with self._transaction() as connection:
            reviews = connection.execute(
                """
                SELECT r.*, result.canonical_json, result.result_sha256,
                       decision.decision_id, decision.disposition,
                       task.recovery_max_retries, task.recovery_retry_count,
                       task.recovery_policy_sha256
                FROM task_reviews r
                JOIN structured_review_results result USING (review_id)
                JOIN judge_decisions decision USING (review_id)
                JOIN orchestration_tasks task
                  ON task.orchestration_task_id = r.task_id
                WHERE decision.disposition = 'NEEDS_FIX'
                  AND r.status = 'COMPLETED'
                  AND task.status = 'BLOCKED'
                  AND task.review_state = 'NEEDS_FIX'
                  AND task.recovery_policy_sha256 IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM recovery_actions action
                      WHERE action.source_decision_id = decision.decision_id
                  )
                ORDER BY r.created_at, r.review_id
                """
            ).fetchall()
            for review in reviews:
                reason = self._reason(review)
                reason_text = canonical_json(reason)
                if len(reason_text.encode("utf-8")) > 65_536:
                    raise RecoveryError("Recovery reason exceeds 64 KiB")
                action_id = "recovery-action-" + uuid.uuid4().hex
                retry_count = int(review["recovery_retry_count"])
                maximum = int(review["recovery_max_retries"])
                exhausted = retry_count >= maximum
                approval_id: str | None = None
                now = utc_now()
                if exhausted:
                    run = connection.execute(
                        "SELECT run_id FROM orchestration_attempts WHERE attempt_id = ?",
                        (review["attempt_id"],),
                    ).fetchone()
                    if run is not None and run["run_id"] is not None:
                        approval_id = "approval-" + uuid.uuid4().hex
                        connection.execute(
                            """
                            INSERT INTO approvals (
                                approval_id, run_id, status, question,
                                options_json, decision, created_at, resolved_at
                            ) VALUES (?, ?, 'PENDING', ?, '["ACKNOWLEDGE"]',
                                      NULL, ?, NULL)
                            """,
                            (
                                approval_id,
                                run["run_id"],
                                "Recovery budget exhausted for " + review["task_id"],
                                now,
                            ),
                        )
                connection.execute(
                    """
                    INSERT INTO recovery_actions (
                        recovery_action_id, project_id, objective_id, plan_id,
                        task_id, source_attempt_id, source_review_id,
                        source_decision_id, recovery_sequence, max_retries,
                        status, reason_json, reason_sha256, target_assignment_id,
                        target_attempt_id, approval_id, created_at, dispatched_at,
                        attempt_created_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              NULL, NULL, ?, ?, NULL, NULL, ?)
                    """,
                    (
                        action_id,
                        review["project_id"],
                        review["objective_id"],
                        review["plan_id"],
                        review["task_id"],
                        review["attempt_id"],
                        review["review_id"],
                        review["decision_id"],
                        retry_count + 1,
                        maximum,
                        "EXHAUSTED" if exhausted else "PENDING",
                        reason_text,
                        content_sha256(reason),
                        approval_id,
                        now,
                        now if exhausted else None,
                    ),
                )
                action = connection.execute(
                    "SELECT * FROM recovery_actions WHERE recovery_action_id = ?",
                    (action_id,),
                ).fetchone()
                self._event(
                    connection,
                    action,
                    "recovery_exhausted" if exhausted else "recovery_requested",
                )
                if approval_id is not None:
                    self._event(connection, action, "recovery_escalated")
                created += 1
        return created

    def pending(self) -> list[dict[str, Any]]:
        return self.list(status="PENDING")

    def dispatch_pending(self, pool: Any) -> int:
        dispatched = 0
        for action in self.pending():
            assignment_id = pool.submit_recovery(
                action["recovery_action_id"],
                action["task_id"],
                action["role_id"],
                action["runtime_kind"],
            )
            if assignment_id is not None:
                dispatched += 1
        return dispatched

    def bind_attempt(
        self,
        connection: Any,
        *,
        assignment_id: str,
        attempt_id: str,
    ) -> str | None:
        action = connection.execute(
            """
            SELECT * FROM recovery_actions
            WHERE target_assignment_id = ? AND status = 'DISPATCHED'
            """,
            (assignment_id,),
        ).fetchone()
        if action is None:
            return None
        now = utc_now()
        changed = connection.execute(
            """
            UPDATE recovery_actions
            SET status = 'ATTEMPT_CREATED', target_attempt_id = ?,
                attempt_created_at = ?
            WHERE recovery_action_id = ? AND status = 'DISPATCHED'
              AND target_attempt_id IS NULL
            """,
            (attempt_id, now, action["recovery_action_id"]),
        ).rowcount
        if changed != 1:
            raise RecoveryError("Corrective attempt linkage lost its race")
        linked = connection.execute(
            "SELECT * FROM recovery_actions WHERE recovery_action_id = ?",
            (action["recovery_action_id"],),
        ).fetchone()
        self._event(
            connection,
            linked,
            "recovery_attempt_created",
            target_attempt_id=attempt_id,
        )
        return str(action["recovery_action_id"])

    def link_attempt(self, *, assignment_id: str, attempt_id: str) -> str | None:
        with self._transaction() as connection:
            return self.bind_attempt(
                connection,
                assignment_id=assignment_id,
                attempt_id=attempt_id,
            )

    def finish_terminal_actions(self) -> int:
        finished = 0
        with self._transaction() as connection:
            abandoned = connection.execute(
                """
                SELECT action.* FROM recovery_actions action
                JOIN worker_pool_assignments assignment
                  ON assignment.assignment_id=action.target_assignment_id
                WHERE action.status='DISPATCHED'
                  AND assignment.status IN ('FAILED','CANCELLED','INTERRUPTED')
                """
            ).fetchall()
            for action in abandoned:
                connection.execute(
                    "UPDATE recovery_actions SET status='CANCELLED',finished_at=? "
                    "WHERE recovery_action_id=? AND status='DISPATCHED'",
                    (utc_now(), action["recovery_action_id"]),
                )
                self._event(connection, action, "recovery_cancelled")
                finished += 1
            actions = connection.execute(
                """
                SELECT action.*
                FROM recovery_actions action
                JOIN orchestration_attempts attempt
                  ON attempt.attempt_id = action.target_attempt_id
                WHERE action.status = 'ATTEMPT_CREATED'
                  AND (
                      attempt.status IN ('FAILED', 'ABANDONED', 'CANCELLED')
                      OR EXISTS (
                          SELECT 1 FROM task_reviews review
                          WHERE review.attempt_id = attempt.attempt_id
                            AND review.status IN ('COMPLETED', 'FAILED')
                      )
                  )
                ORDER BY action.created_at
                """
            ).fetchall()
            for action in actions:
                now = utc_now()
                connection.execute(
                    """
                    UPDATE recovery_actions
                    SET status = 'COMPLETED', finished_at = ?
                    WHERE recovery_action_id = ? AND status = 'ATTEMPT_CREATED'
                    """,
                    (now, action["recovery_action_id"]),
                )
                self._event(
                    connection,
                    action,
                    "recovery_completed",
                    target_attempt_id=action["target_attempt_id"],
                )
                finished += 1
        return finished

    def cancel_ineligible(self, pool: Any) -> int:
        cancelled = 0
        with contextlib.closing(self._connect()) as connection:
            actions = connection.execute(
                """
                SELECT action.*
                FROM recovery_actions action
                LEFT JOIN objective_queue objective
                  ON objective.plan_id = action.plan_id
                JOIN orchestration_plans plan ON plan.plan_id = action.plan_id
                WHERE action.status IN ('PENDING', 'DISPATCHED')
                  AND (plan.status IN ('FAILED', 'CANCELLED')
                       OR objective.status IN ('CANCEL_REQUESTED', 'CANCELLED', 'FAILED'))
                """
            ).fetchall()
        for action in actions:
            assignment_id = action["target_assignment_id"]
            if assignment_id is not None and not pool.cancel_queued(assignment_id):
                continue
            with self._transaction() as connection:
                connection.execute(
                    """
                    UPDATE recovery_actions
                    SET status = 'CANCELLED', finished_at = ?
                    WHERE recovery_action_id = ?
                      AND status IN ('PENDING', 'DISPATCHED')
                    """,
                    (utc_now(), action["recovery_action_id"]),
                )
                self._event(connection, action, "recovery_cancelled")
                cancelled += 1
        return cancelled

    def resolve_exhaustion(self, approval_id: str, decision: str) -> dict[str, Any]:
        if decision != "ACKNOWLEDGE":
            raise RecoveryError("Recovery exhaustion only permits ACKNOWLEDGE")
        with self._transaction() as connection:
            action = connection.execute(
                "SELECT * FROM recovery_actions WHERE approval_id = ?",
                (approval_id,),
            ).fetchone()
            if action is None or action["status"] != "EXHAUSTED":
                raise RecoveryError("Unknown recovery exhaustion approval")
            changed = connection.execute(
                "UPDATE approvals SET status='APPROVED', decision=?, resolved_at=? "
                "WHERE approval_id=? AND status='PENDING'",
                (decision, utc_now(), approval_id),
            ).rowcount
            if changed != 1:
                raise RecoveryError("Recovery exhaustion approval is not pending")
        return next(
            item
            for item in self.list(task_id=str(action["task_id"]))
            if item["approval_id"] == approval_id
        )

    def reconcile(self, pool: Any) -> dict[str, int]:
        completed = self.finish_terminal_actions()
        requested = self.request_eligible()
        cancelled = self.cancel_ineligible(pool)
        dispatched = self.dispatch_pending(pool)
        return {
            "completed": completed,
            "requested": requested,
            "cancelled": cancelled,
            "dispatched": dispatched,
        }

    def list(
        self,
        *,
        task_id: str | None = None,
        plan_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        with contextlib.closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT action.*, task.recovery_retry_count,
                       task.recovery_max_retries, task.recovery_policy_sha256,
                       task.role_id, role.runtime_kind,
                       review.worker_result_reference,
                       result.result_sha256 AS source_review_sha256,
                       decision.disposition AS source_disposition,
                       approval.status AS escalation_status
                FROM recovery_actions action
                JOIN orchestration_tasks task
                  ON task.orchestration_task_id = action.task_id
                JOIN roles role ON role.role_id = task.role_id
                JOIN task_reviews review
                  ON review.review_id = action.source_review_id
                JOIN structured_review_results result
                  ON result.review_id = review.review_id
                JOIN judge_decisions decision
                  ON decision.decision_id = action.source_decision_id
                LEFT JOIN approvals approval
                  ON approval.approval_id = action.approval_id
                WHERE (? IS NULL OR action.task_id = ?)
                  AND (? IS NULL OR action.plan_id = ?)
                  AND (? IS NULL OR action.status = ?)
                ORDER BY action.created_at, action.recovery_action_id
                LIMIT 1000
                """,
                (task_id, task_id, plan_id, plan_id, status, status),
            ).fetchall()
        result = []
        for row in rows:
            value = dict(row)
            value["reason"] = json.loads(value.pop("reason_json"))
            result.append(value)
        return result

    def lineage(self, task_id: str) -> dict[str, Any]:
        with contextlib.closing(self._connect()) as connection:
            task = connection.execute(
                "SELECT * FROM orchestration_tasks WHERE orchestration_task_id = ?",
                (task_id,),
            ).fetchone()
            if task is None:
                raise RecoveryError("Unknown recovery task")
            attempts = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT attempt.*, snapshot.context_snapshot_id,
                           snapshot.projection_sha256, snapshot.recovery_action_id,
                           review.review_id, review.status AS review_status,
                           decision.decision_id, decision.disposition
                    FROM orchestration_attempts attempt
                    LEFT JOIN context_snapshots snapshot
                      ON snapshot.context_snapshot_id = attempt.context_snapshot_id
                    LEFT JOIN task_reviews review
                      ON review.attempt_id = attempt.attempt_id
                    LEFT JOIN judge_decisions decision
                      ON decision.review_id = review.review_id
                    WHERE attempt.orchestration_task_id = ?
                    ORDER BY attempt.attempt_number
                    """,
                    (task_id,),
                ).fetchall()
            ]
        return {
            "task": dict(task),
            "attempts": attempts,
            "recovery_actions": self.list(task_id=task_id),
        }
