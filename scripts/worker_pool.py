"""Durable, bounded, runtime-neutral worker execution capacity."""

from __future__ import annotations

import concurrent.futures
import contextlib
import json
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class WorkerAssignment:
    assignment_id: str
    task_id: str
    role_id: str
    runtime_kind: str


class WorkerPool:
    """Coordinate ephemeral execution slots from durable FIFO assignments."""

    def __init__(
        self,
        connection_factory: Callable[[], Any],
        dispatch: Callable[[WorkerAssignment], Any],
        *,
        controller_instance_id: str,
        max_concurrency: int = 1,
        pool_name: str = "default",
        executor: concurrent.futures.Executor | None = None,
        recovery_eligible: Callable[[Any, str], bool] | None = None,
    ) -> None:
        if not callable(connection_factory) or not callable(dispatch):
            raise TypeError("Worker pool dependencies must be callable")
        if (
            not isinstance(max_concurrency, int)
            or isinstance(max_concurrency, bool)
            or not 1 <= max_concurrency <= 16
        ):
            raise ValueError("Worker pool max_concurrency must be 1..16")
        if not controller_instance_id or not pool_name:
            raise ValueError("Worker pool identity is required")
        self._connect = connection_factory
        self._dispatch = dispatch
        self.controller_instance_id = controller_instance_id
        self.max_concurrency = max_concurrency
        self.pool_name = pool_name
        if recovery_eligible is not None and not callable(recovery_eligible):
            raise TypeError("Worker pool recovery eligibility must be callable")
        self._recovery_eligible = recovery_eligible
        self._executor = executor or concurrent.futures.ThreadPoolExecutor(
            max_workers=max_concurrency,
            thread_name_prefix="orchestra-worker-slot",
        )
        self._owns_executor = executor is None
        self._futures: dict[concurrent.futures.Future[Any], WorkerAssignment] = {}
        self._lock = threading.RLock()
        self._stopping = False

    @contextlib.contextmanager
    def _connection(self):  # type: ignore[no-untyped-def]
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            close = getattr(connection, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _event(connection: Any, assignment_id: str, kind: str, now: str) -> None:
        connection.execute(
            "INSERT INTO worker_pool_events "
            "(assignment_id, event_kind, created_at) VALUES (?, ?, ?)",
            (assignment_id, kind, now),
        )

    def submit(self, task_id: str, role_id: str, runtime_kind: str) -> str:
        if runtime_kind not in {"hermes", "native"}:
            raise ValueError("Worker assignment runtime kind is invalid")
        assignment_id = "worker-assignment-" + uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                """
                SELECT assignment_id
                FROM worker_pool_assignments
                WHERE orchestration_task_id = ?
                  AND status IN ('QUEUED', 'RUNNING')
                """,
                (task_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return str(existing[0])
            queue_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(queue_sequence), 0) + 1 "
                    "FROM worker_pool_assignments WHERE pool_name = ?",
                    (self.pool_name,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO worker_pool_assignments (
                    assignment_id, pool_name, queue_sequence,
                    orchestration_task_id,
                    attempt_id, role_id, runtime_kind, status,
                    controller_instance_id, result_json, failure_reason,
                    created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'QUEUED', NULL, '{}', NULL,
                          ?, NULL, NULL)
                """,
                (
                    assignment_id,
                    self.pool_name,
                    queue_sequence,
                    task_id,
                    role_id,
                    runtime_kind,
                    now,
                ),
            )
            self._event(connection, assignment_id, "QUEUED", now)
            connection.commit()
        self.pump()
        return assignment_id

    def submit_recovery(
        self,
        recovery_action_id: str,
        task_id: str,
        role_id: str,
        runtime_kind: str,
    ) -> str | None:
        """Atomically consume one recovery budget unit and queue its assignment."""
        if runtime_kind not in {"hermes", "native"}:
            raise ValueError("Worker assignment runtime kind is invalid")
        assignment_id = "worker-assignment-" + uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            action = connection.execute(
                """
                SELECT action.*, task.status AS task_status,
                       task.review_state, task.role_id,
                       task.recovery_retry_count, task.recovery_max_retries,
                       role.runtime_kind, plan.status AS plan_status,
                       plan.last_error AS plan_error
                FROM recovery_actions action
                JOIN orchestration_tasks task
                  ON task.orchestration_task_id = action.task_id
                JOIN roles role ON role.role_id = task.role_id
                JOIN orchestration_plans plan ON plan.plan_id = task.plan_id
                WHERE action.recovery_action_id = ? AND action.task_id = ?
                """,
                (recovery_action_id, task_id),
            ).fetchone()
            if action is None:
                connection.rollback()
                return None
            if action["status"] == "DISPATCHED":
                connection.rollback()
                return str(action["target_assignment_id"])
            if (
                action["status"] != "PENDING"
                or action["task_status"] != "BLOCKED"
                or action["review_state"] != "NEEDS_FIX"
                or action["role_id"] != role_id
                or action["runtime_kind"] != runtime_kind
                or int(action["recovery_retry_count"]) >= int(action["recovery_max_retries"])
                or int(action["recovery_retry_count"]) + 1 != int(action["recovery_sequence"])
            ):
                connection.rollback()
                return None
            if (
                self._recovery_eligible is not None
                and not self._recovery_eligible(connection, task_id)
            ):
                connection.rollback()
                return None
            existing = connection.execute(
                "SELECT assignment_id FROM worker_pool_assignments "
                "WHERE orchestration_task_id=? AND status IN ('QUEUED','RUNNING')",
                (task_id,),
            ).fetchone()
            if existing is not None:
                connection.rollback()
                return None
            queue_sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(queue_sequence), 0) + 1 "
                    "FROM worker_pool_assignments WHERE pool_name = ?",
                    (self.pool_name,),
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO worker_pool_assignments (
                    assignment_id, pool_name, queue_sequence,
                    orchestration_task_id, attempt_id, role_id, runtime_kind,
                    status, controller_instance_id, result_json, failure_reason,
                    created_at, started_at, finished_at
                ) VALUES (?, ?, ?, ?, NULL, ?, ?, 'QUEUED', NULL, '{}', NULL,
                          ?, NULL, NULL)
                """,
                (assignment_id, self.pool_name, queue_sequence, task_id,
                 role_id, runtime_kind, now),
            )
            self._event(connection, assignment_id, "QUEUED", now)
            changed = connection.execute(
                """
                UPDATE recovery_actions
                SET status='DISPATCHED', target_assignment_id=?, dispatched_at=?
                WHERE recovery_action_id=? AND status='PENDING'
                """,
                (assignment_id, now, recovery_action_id),
            ).rowcount
            if changed != 1:
                connection.rollback()
                return None
            connection.execute(
                "UPDATE orchestration_tasks SET status='READY', review_state='NONE', "
                "recovery_retry_count=recovery_retry_count+1, failure_reason=NULL, "
                "finished_at=NULL WHERE orchestration_task_id=?",
                (task_id,),
            )
            connection.execute(
                "UPDATE orchestration_plans SET status='RUNNING', last_error=NULL, "
                "finished_at=NULL WHERE plan_id=? AND status='BLOCKED'",
                (action["plan_id"],),
            )
            connection.execute(
                """
                INSERT INTO recovery_events (
                    recovery_action_id,event_kind,task_id,source_attempt_id,
                    source_review_id,source_decision_id,target_attempt_id,created_at
                ) VALUES (?, 'recovery_dispatched', ?, ?, ?, ?, NULL, ?)
                """,
                (recovery_action_id, task_id, action["source_attempt_id"],
                 action["source_review_id"], action["source_decision_id"], now),
            )
            connection.commit()
        self.pump()
        return assignment_id

    def _claim_next(self) -> WorkerAssignment | None:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = int(
                connection.execute(
                    "SELECT COUNT(*) FROM worker_pool_assignments "
                    "WHERE pool_name = ? AND status = 'RUNNING'",
                    (self.pool_name,),
                ).fetchone()[0]
            )
            if running >= self.max_concurrency:
                connection.rollback()
                return None
            row = connection.execute(
                """
                SELECT assignment.assignment_id,
                       assignment.orchestration_task_id,
                       assignment.role_id,
                       assignment.runtime_kind
                FROM worker_pool_assignments AS assignment
                JOIN orchestration_tasks AS task
                  ON task.orchestration_task_id = assignment.orchestration_task_id
                WHERE assignment.pool_name = ?
                  AND assignment.status = 'QUEUED'
                  AND task.status = 'READY'
                ORDER BY assignment.queue_sequence
                LIMIT 1
                """,
                (self.pool_name,),
            ).fetchone()
            if row is None:
                connection.rollback()
                return None
            updated = connection.execute(
                """
                UPDATE worker_pool_assignments
                SET status = 'RUNNING', controller_instance_id = ?, started_at = ?
                WHERE assignment_id = ? AND status = 'QUEUED'
                """,
                (self.controller_instance_id, now, row[0]),
            ).rowcount
            if updated != 1:
                connection.rollback()
                return None
            self._event(connection, str(row[0]), "SLOT_ACQUIRED", now)
            connection.commit()
            return WorkerAssignment(
                assignment_id=str(row[0]),
                task_id=str(row[1]),
                role_id=str(row[2]),
                runtime_kind=str(row[3]),
            )

    def pump(self) -> None:
        with self._lock:
            if self._stopping:
                return
            while len(self._futures) < self.max_concurrency:
                assignment = self._claim_next()
                if assignment is None:
                    return
                try:
                    future = self._executor.submit(self._dispatch, assignment)
                except Exception as error:
                    self._finish(
                        assignment,
                        status="FAILED",
                        failure_reason=f"worker dispatch failed: {type(error).__name__}",
                    )
                    continue
                self._futures[future] = assignment
                future.add_done_callback(self._completed)

    def _finish(
        self,
        assignment: WorkerAssignment,
        *,
        status: str,
        result: Any = None,
        failure_reason: str | None = None,
    ) -> None:
        now = utc_now()
        try:
            result_json = (
                json.dumps(result, sort_keys=True) if result is not None else "{}"
            )
        except (TypeError, ValueError):
            status = "FAILED"
            failure_reason = "worker execution returned a non-JSON result"
            result_json = "{}"
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE worker_pool_assignments
                SET status = ?, result_json = ?, failure_reason = ?, finished_at = ?
                WHERE assignment_id = ? AND status = 'RUNNING'
                """,
                (status, result_json, failure_reason, now, assignment.assignment_id),
            ).rowcount
            if updated != 1:
                connection.rollback()
                return
            self._event(connection, assignment.assignment_id, status, now)
            self._event(connection, assignment.assignment_id, "SLOT_RELEASED", now)
            connection.commit()

    def _completed(self, future: concurrent.futures.Future[Any]) -> None:
        with self._lock:
            assignment = self._futures.pop(future)
            try:
                result = future.result()
            except concurrent.futures.CancelledError:
                self._finish(assignment, status="CANCELLED")
            except BaseException as error:
                self._finish(
                    assignment,
                    status="FAILED",
                    failure_reason=f"worker execution failed: {type(error).__name__}",
                )
            else:
                self._finish(assignment, status="COMPLETED", result=result)
            self.pump()

    def bind_attempt(self, assignment_id: str, attempt_id: str) -> None:
        with self._connection() as connection:
            updated = connection.execute(
                """
                UPDATE worker_pool_assignments
                SET attempt_id = ?
                WHERE assignment_id = ? AND status = 'RUNNING' AND attempt_id IS NULL
                """,
                (attempt_id, assignment_id),
            ).rowcount
            if updated != 1:
                raise RuntimeError("Worker pool attempt binding was rejected")
            connection.commit()

    def reconcile(self) -> int:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT assignment_id FROM worker_pool_assignments
                WHERE pool_name = ? AND status = 'RUNNING'
                ORDER BY queue_sequence
                """,
                (self.pool_name,),
            ).fetchall()
            for row in rows:
                connection.execute(
                    """
                    UPDATE worker_pool_assignments
                    SET status = 'INTERRUPTED',
                        failure_reason = 'controller restarted during worker execution',
                        finished_at = ?
                    WHERE assignment_id = ? AND status = 'RUNNING'
                    """,
                    (now, row[0]),
                )
                self._event(connection, str(row[0]), "INTERRUPTED", now)
                self._event(connection, str(row[0]), "SLOT_RELEASED", now)
            stale_queued = connection.execute(
                """
                SELECT assignment.assignment_id
                FROM worker_pool_assignments AS assignment
                JOIN orchestration_tasks AS task
                  ON task.orchestration_task_id = assignment.orchestration_task_id
                WHERE assignment.pool_name = ?
                  AND assignment.status = 'QUEUED'
                  AND task.status <> 'READY'
                """,
                (self.pool_name,),
            ).fetchall()
            for row in stale_queued:
                connection.execute(
                    """
                    UPDATE worker_pool_assignments
                    SET status = 'CANCELLED',
                        failure_reason = 'queued task is no longer dispatchable',
                        finished_at = ?
                    WHERE assignment_id = ? AND status = 'QUEUED'
                    """,
                    (now, row[0]),
                )
                self._event(connection, str(row[0]), "CANCELLED", now)
            connection.commit()
        return len(rows)

    def cancel_queued(self, assignment_id: str) -> bool:
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE worker_pool_assignments
                SET status = 'CANCELLED', finished_at = ?
                WHERE assignment_id = ? AND status = 'QUEUED'
                """,
                (now, assignment_id),
            ).rowcount
            if updated:
                self._event(connection, assignment_id, "CANCELLED", now)
            connection.commit()
        return updated == 1

    def active_task_ids(self) -> set[str]:
        with self._connection() as connection:
            return {
                str(row[0])
                for row in connection.execute(
                    """
                    SELECT orchestration_task_id FROM worker_pool_assignments
                    WHERE pool_name = ? AND status IN ('QUEUED', 'RUNNING')
                    """,
                    (self.pool_name,),
                ).fetchall()
            }

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._stopping = True
        if self._owns_executor:
            self._executor.shutdown(wait=wait, cancel_futures=False)
