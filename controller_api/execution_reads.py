from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from datetime import datetime
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from scripts.oci_reference import parse_immutable_oci_reference

from .core import (
    ControllerError,
    PROJECT_ID_PATTERN,
    ReadOnlyDatabase,
    Settings,
)
from .objective_reads import OBJECTIVE_ID_PATTERN

TASK_ID_PATTERN = re.compile(r"^orchestration-task-[a-f0-9]{32}$")
RUN_ID_PATTERN = re.compile(r"^orchestration-attempt-[a-f0-9]{32}$")
TRANSACTION_REFERENCE_PATTERN = re.compile(r"^transaction-[a-f0-9]{32}$")
MAX_INTERNAL_TRANSACTION_KEY_BYTES = 512
WORKER_EXECUTION_ID_PATTERN = re.compile(r"^execution-[a-f0-9]{32}$")
ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,127}$")
TASK_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
EVENT_TYPE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
PLAN_ID_PATTERN = re.compile(r"^plan-[a-f0-9]{32}$")
MAX_CURSOR_BYTES = 1024
TASK_STATES = {
    "pending",
    "ready",
    "claimed",
    "running",
    "reviewing",
    "integrating",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
}
RUN_STATES = {
    "created",
    "preparing",
    "running",
    "waiting_review",
    "waiting_integration",
    "succeeded",
    "failed",
    "cancelled",
    "interrupted",
    "recovery_required",
}
EVENT_SEVERITIES = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
WORKSPACE_MODES = {"read", "write"}


@dataclass(frozen=True)
class TaskCursor:
    objective_id: str
    priority: int
    created_at: str
    task_id: str


@dataclass(frozen=True)
class RunCursor:
    task_id: str
    attempt_number: int
    run_id: str


class ExecutionReadStore:
    """Read-only projections for tasks, execution attempts, and event logs."""

    def __init__(self, settings: Settings) -> None:
        self.database = ReadOnlyDatabase(settings)

    @staticmethod
    def _revision(payload: dict[str, Any]) -> int:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        # 13 hexadecimal digits stay below JavaScript's 2**53 safe integer.
        return int(digest[:13], 16)

    @staticmethod
    def _require_identifier(
        value: Any,
        pattern: re.Pattern[str],
        *,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> str:
        candidate = str(value) if value is not None else ""
        if not pattern.fullmatch(candidate):
            raise ControllerError(
                503,
                code,
                title,
                "Stored execution data contains an invalid public identifier.",
                resource={"type": resource_type, "id": resource_id},
            )
        return candidate

    @staticmethod
    def _projection_error(
        *,
        code: str,
        title: str,
        detail: str,
        resource_type: str,
        resource_id: str,
    ) -> ControllerError:
        return ControllerError(
            503,
            code,
            title,
            detail,
            resource={"type": resource_type, "id": resource_id},
        )

    @classmethod
    def _integer(
        cls,
        value: Any,
        *,
        minimum: int | None,
        maximum: int | None = None,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> int:
        if type(value) is not int:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains an invalid numeric value.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        if minimum is not None and value < minimum:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains an out-of-range numeric value.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        if maximum is not None and value > maximum:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains an out-of-range numeric value.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return value

    @classmethod
    def _flag(
        cls,
        value: Any,
        *,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> bool:
        integer = cls._integer(
            value,
            minimum=0,
            maximum=1,
            code=code,
            title=title,
            resource_type=resource_type,
            resource_id=resource_id,
        )
        return bool(integer)

    @classmethod
    def _timestamp(
        cls,
        value: Any,
        *,
        required: bool,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> str | None:
        if value is None:
            if not required:
                return None
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains a missing timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        if not isinstance(value, str):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains an invalid timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        encoded = value.encode("utf-8")
        if (
            not value
            or len(encoded) > 64
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains an invalid timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
        except ValueError as error:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains an invalid timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            ) from error
        if parsed.tzinfo is None:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored execution data contains a timezone-free timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return value

    @staticmethod
    def _task_state(raw: str) -> str:
        mapping = {
            "PENDING": "pending",
            "READY": "ready",
            "RUNNING": "running",
            "BLOCKED": "blocked",
            "COMPLETED": "succeeded",
            "FAILED": "failed",
            "CANCELLED": "cancelled",
        }
        try:
            return mapping[raw]
        except KeyError as error:
            raise ControllerError(
                503,
                "task_projection_invalid",
                "Task projection unavailable",
                "A task contains an unsupported runtime state.",
            ) from error

    @staticmethod
    def _internal_transaction_key(
        value: Any,
        *,
        run_id: str,
    ) -> str:
        if not isinstance(value, str):
            raise ControllerError(
                503,
                "run_projection_invalid",
                "Run projection unavailable",
                "A linked transaction contains an invalid internal key.",
                resource={"type": "run", "id": run_id},
            )
        encoded = value.encode("utf-8")
        if (
            not value
            or len(encoded) > MAX_INTERNAL_TRANSACTION_KEY_BYTES
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ControllerError(
                503,
                "run_projection_invalid",
                "Run projection unavailable",
                "A linked transaction contains an invalid internal key.",
                resource={"type": "run", "id": run_id},
            )
        return value

    @staticmethod
    def _transaction_reference(internal_key: str) -> str:
        digest = hashlib.sha256(
            b"hermesops-transaction-reference-v1\0"
            + internal_key.encode("utf-8")
        ).hexdigest()
        return "transaction-" + digest[:32]

    @staticmethod
    def _run_state(attempt_status: str, legacy_status: str | None) -> str:
        if attempt_status == "COMPLETED":
            return "succeeded"
        if attempt_status == "FAILED":
            return "failed"
        if attempt_status == "ABANDONED":
            return "interrupted"
        if attempt_status == "CANCELLED":
            return "cancelled"
        if attempt_status != "RUNNING":
            raise ControllerError(
                503,
                "run_projection_invalid",
                "Run projection unavailable",
                "An execution attempt contains an unsupported runtime state.",
            )
        mapping = {
            None: "running",
            "QUEUED": "created",
            "SNAPSHOTTING": "preparing",
            "RUNNING": "running",
            "REVIEWING": "waiting_review",
            "WAITING_HUMAN": "waiting_integration",
            "COMMITTING": "waiting_integration",
            "COMPLETED": "succeeded",
            "FAILED": "interrupted",
            "CANCELLED": "cancelled",
            "RECOVERING": "recovery_required",
        }
        try:
            return mapping[legacy_status]
        except KeyError as error:
            raise ControllerError(
                503,
                "run_projection_invalid",
                "Run projection unavailable",
                "A linked transaction contains an unsupported runtime state.",
            ) from error

    @staticmethod
    def _title(task_key: str) -> str:
        normalized = " ".join(task_key.replace("_", " ").replace("-", " ").split())
        return normalized[:200] or "Untitled task"

    @staticmethod
    def _cursor_signature(raw: bytes, secret: str, namespace: bytes) -> bytes:
        return hmac.new(
            secret.encode("ascii"),
            namespace + b"\0" + raw,
            hashlib.sha256,
        ).digest()

    @classmethod
    def _encode_signed_cursor(
        cls,
        payload: dict[str, Any],
        *,
        secret: str,
        namespace: bytes,
    ) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signed = raw + cls._cursor_signature(raw, secret, namespace)
        return base64.urlsafe_b64encode(signed).rstrip(b"=").decode("ascii")

    @classmethod
    def _decode_signed_cursor(
        cls,
        value: str,
        *,
        secret: str,
        namespace: bytes,
    ) -> dict[str, Any]:
        if not value or len(value) > MAX_CURSOR_BYTES:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        try:
            padding = "=" * (-len(value) % 4)
            signed = base64.b64decode(
                value + padding,
                altchars=b"-_",
                validate=True,
            )
            digest_size = hashlib.sha256().digest_size
            if len(signed) <= digest_size:
                raise ValueError("cursor payload is too short")
            raw = signed[:-digest_size]
            signature = signed[-digest_size:]
            expected = cls._cursor_signature(raw, secret, namespace)
            if not hmac.compare_digest(signature, expected):
                raise ValueError("cursor signature mismatch")
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
            ) from error
        if not isinstance(payload, dict):
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        return payload

    @classmethod
    def _encode_task_cursor(cls, cursor: TaskCursor, *, secret: str) -> str:
        return cls._encode_signed_cursor(
            {
                "v": 1,
                "o": cursor.objective_id,
                "p": cursor.priority,
                "c": cursor.created_at,
                "i": cursor.task_id,
            },
            secret=secret,
            namespace=b"hermesops-task-cursor-v1",
        )

    @classmethod
    def _decode_task_cursor(
        cls,
        value: str,
        *,
        objective_id: str,
        secret: str,
    ) -> TaskCursor:
        payload = cls._decode_signed_cursor(
            value,
            secret=secret,
            namespace=b"hermesops-task-cursor-v1",
        )
        if (
            payload.get("v") != 1
            or payload.get("o") != objective_id
            or not isinstance(payload.get("p"), int)
            or not isinstance(payload.get("c"), str)
            or not isinstance(payload.get("i"), str)
            or not TASK_ID_PATTERN.fullmatch(payload["i"])
        ):
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
                "The cursor is malformed or belongs to a different objective.",
            )
        return TaskCursor(
            objective_id=objective_id,
            priority=payload["p"],
            created_at=payload["c"],
            task_id=payload["i"],
        )

    @classmethod
    def _encode_run_cursor(cls, cursor: RunCursor, *, secret: str) -> str:
        return cls._encode_signed_cursor(
            {
                "v": 1,
                "t": cursor.task_id,
                "n": cursor.attempt_number,
                "i": cursor.run_id,
            },
            secret=secret,
            namespace=b"hermesops-run-cursor-v1",
        )

    @classmethod
    def _decode_run_cursor(
        cls,
        value: str,
        *,
        task_id: str,
        secret: str,
    ) -> RunCursor:
        payload = cls._decode_signed_cursor(
            value,
            secret=secret,
            namespace=b"hermesops-run-cursor-v1",
        )
        if (
            payload.get("v") != 1
            or payload.get("t") != task_id
            or not isinstance(payload.get("n"), int)
            or not isinstance(payload.get("i"), str)
            or not RUN_ID_PATTERN.fullmatch(payload["i"])
        ):
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
                "The cursor is malformed or belongs to a different task.",
            )
        return RunCursor(
            task_id=task_id,
            attempt_number=payload["n"],
            run_id=payload["i"],
        )

    @staticmethod
    def _task_projection_sql() -> str:
        return """
            SELECT
                t.orchestration_task_id,
                t.plan_id,
                t.task_key,
                t.kind,
                t.project_id,
                t.role_id,
                t.status AS raw_status,
                t.priority,
                t.max_attempts,
                t.attempt_count,
                t.result_json,
                t.failure_reason,
                t.created_at,
                t.started_at,
                t.heartbeat_at,
                t.finished_at,
                (
                    SELECT q2.objective_id
                    FROM objective_queue AS q2
                    WHERE q2.plan_id = t.plan_id
                    ORDER BY q2.created_at DESC, q2.objective_id DESC
                    LIMIT 1
                ) AS objective_id,
                (
                    SELECT COUNT(*)
                    FROM objective_queue AS q3
                    WHERE q3.plan_id = t.plan_id
                ) AS objective_count,
                r.role_id AS registered_role_id,
                r.profile_name,
                r.workspace_mode,
                COALESCE(dependencies.dependency_count, 0) AS dependency_count,
                COALESCE(dependents.dependent_count, 0) AS dependent_count
            FROM orchestration_tasks AS t
            LEFT JOIN roles AS r ON r.role_id = t.role_id
            LEFT JOIN (
                SELECT orchestration_task_id, COUNT(*) AS dependency_count
                FROM orchestration_dependencies
                GROUP BY orchestration_task_id
            ) AS dependencies
                ON dependencies.orchestration_task_id = t.orchestration_task_id
            LEFT JOIN (
                SELECT depends_on_task_id, COUNT(*) AS dependent_count
                FROM orchestration_dependencies
                GROUP BY depends_on_task_id
            ) AS dependents
                ON dependents.depends_on_task_id = t.orchestration_task_id
        """

    def _task(self, row: sqlite3.Row) -> dict[str, Any]:
        task_id = self._require_identifier(
            row["orchestration_task_id"],
            TASK_ID_PATTERN,
            code="task_projection_invalid",
            title="Task projection unavailable",
            resource_type="task",
            resource_id="unavailable",
        )
        if int(row["objective_count"]) != 1:
            raise ControllerError(
                503,
                "task_projection_invalid",
                "Task projection unavailable",
                "A task is not linked to exactly one public objective.",
                resource={"type": "task", "id": task_id},
            )
        objective_id = self._require_identifier(
            row["objective_id"],
            OBJECTIVE_ID_PATTERN,
            code="task_projection_invalid",
            title="Task projection unavailable",
            resource_type="task",
            resource_id=task_id,
        )
        plan_id = self._require_identifier(
            row["plan_id"],
            PLAN_ID_PATTERN,
            code="task_projection_invalid",
            title="Task projection unavailable",
            resource_type="task",
            resource_id=task_id,
        )
        raw_role = str(row["role_id"]) if row["role_id"] else "system"
        registered_role = (
            str(row["registered_role_id"])
            if row["registered_role_id"] is not None
            else None
        )
        if raw_role != "system" and (
            not ROLE_ID_PATTERN.fullmatch(raw_role)
            or registered_role != raw_role
        ):
            raise ControllerError(
                503,
                "task_projection_invalid",
                "Task projection unavailable",
                "A task references an invalid or unregistered role identifier.",
                resource={"type": "task", "id": task_id},
            )
        task_key = str(row["task_key"])
        if not TASK_KEY_PATTERN.fullmatch(task_key):
            raise ControllerError(
                503,
                "task_projection_invalid",
                "Task projection unavailable",
                "A task contains an invalid public key.",
                resource={"type": "task", "id": task_id},
            )
        kind = str(row["kind"])
        if kind not in {"PIPELINE", "NOOP", "TEST_SLEEP", "TEST_FAIL"}:
            raise ControllerError(
                503,
                "task_projection_invalid",
                "Task projection unavailable",
                "A task contains an unsupported kind.",
                resource={"type": "task", "id": task_id},
            )
        project_id = str(row["project_id"]) if row["project_id"] else None
        if project_id is not None and not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ControllerError(
                503,
                "task_projection_invalid",
                "Task projection unavailable",
                "A task references an invalid project identifier.",
                resource={"type": "task", "id": task_id},
            )
        workspace_mode = str(row["workspace_mode"] or "")
        if raw_role != "system" and workspace_mode not in WORKSPACE_MODES:
            raise self._projection_error(
                code="task_projection_invalid",
                title="Task projection unavailable",
                detail="A task references an invalid registered workspace mode.",
                resource_type="task",
                resource_id=task_id,
            )
        state = self._task_state(str(row["raw_status"]))
        created_at = self._timestamp(
            row["created_at"], required=True,
            code="task_projection_invalid", title="Task projection unavailable",
            resource_type="task", resource_id=task_id,
        )
        started_at = self._timestamp(
            row["started_at"], required=False,
            code="task_projection_invalid", title="Task projection unavailable",
            resource_type="task", resource_id=task_id,
        )
        heartbeat_at = self._timestamp(
            row["heartbeat_at"], required=False,
            code="task_projection_invalid", title="Task projection unavailable",
            resource_type="task", resource_id=task_id,
        )
        finished_at = self._timestamp(
            row["finished_at"], required=False,
            code="task_projection_invalid", title="Task projection unavailable",
            resource_type="task", resource_id=task_id,
        )
        updated_at = finished_at or heartbeat_at or started_at or created_at
        priority = self._integer(
            row["priority"], minimum=0, code="task_projection_invalid",
            title="Task projection unavailable", resource_type="task",
            resource_id=task_id,
        )
        attempt_count = self._integer(
            row["attempt_count"], minimum=0, code="task_projection_invalid",
            title="Task projection unavailable", resource_type="task",
            resource_id=task_id,
        )
        max_attempts = self._integer(
            row["max_attempts"], minimum=1, code="task_projection_invalid",
            title="Task projection unavailable", resource_type="task",
            resource_id=task_id,
        )
        dependency_count = self._integer(
            row["dependency_count"], minimum=0, code="task_projection_invalid",
            title="Task projection unavailable", resource_type="task",
            resource_id=task_id,
        )
        dependent_count = self._integer(
            row["dependent_count"], minimum=0, code="task_projection_invalid",
            title="Task projection unavailable", resource_type="task",
            resource_id=task_id,
        )
        has_result = str(row["result_json"] or "") not in {"", "{}", "null"}
        payload: dict[str, Any] = {
            "id": task_id,
            "created_at": created_at,
            "updated_at": updated_at,
            "state": state,
            "objective_id": objective_id,
            "title": self._title(task_key),
            "role_id": raw_role,
            "writer": workspace_mode == "write",
            "plan_id": plan_id,
            "task_key": task_key,
            "kind": kind,
            "project_id": project_id,
            "priority": priority,
            "attempt_count": attempt_count,
            "max_attempts": max_attempts,
            "dependency_count": dependency_count,
            "dependent_count": dependent_count,
            "started_at": started_at,
            "finished_at": finished_at,
            "result": (
                {"available": True, "legacy_payload_redacted": True}
                if has_result else None
            ),
            "error": (
                {"available": True, "legacy_payload_redacted": True}
                if row["failure_reason"] is not None else None
            ),
            "instruction_redacted": True,
            "raw_state": str(row["raw_status"]),
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def _objective_plan(self, objective_id: str) -> str | None:
        if not OBJECTIVE_ID_PATTERN.fullmatch(objective_id):
            raise ControllerError(400, "invalid_objective_id", "Invalid objective identifier")
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(
                    "SELECT plan_id FROM objective_queue WHERE objective_id = ?",
                    (objective_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "objective_not_found",
                "Objective not found",
                resource={"type": "objective", "id": objective_id},
            )
        plan_id = row["plan_id"]
        if plan_id is None:
            return None
        return self._require_identifier(
            plan_id,
            PLAN_ID_PATTERN,
            code="task_projection_invalid",
            title="Task projection unavailable",
            resource_type="objective",
            resource_id=objective_id,
        )

    def list_objective_tasks(
        self,
        objective_id: str,
        *,
        limit: int,
        cursor: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not 1 <= limit <= 200:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )
        plan_id = self._objective_plan(objective_id)
        if plan_id is None:
            if cursor is not None:
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            return [], None
        decoded = (
            self._decode_task_cursor(
                cursor,
                objective_id=objective_id,
                secret=cursor_secret,
            )
            if cursor is not None else None
        )
        sql = self._task_projection_sql() + " WHERE t.plan_id = ?"
        parameters: list[Any] = [plan_id]
        if decoded is not None:
            sql += """
                AND (
                    t.priority > ?
                    OR (t.priority = ? AND t.created_at > ?)
                    OR (t.priority = ? AND t.created_at = ?
                        AND t.orchestration_task_id > ?)
                )
            """
            parameters.extend([
                decoded.priority,
                decoded.priority,
                decoded.created_at,
                decoded.priority,
                decoded.created_at,
                decoded.task_id,
            ])
        sql += " ORDER BY t.priority, t.created_at, t.orchestration_task_id LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        has_more = len(rows) > limit
        selected = rows[:limit]
        tasks = [self._task(row) for row in selected]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = self._encode_task_cursor(
                TaskCursor(
                    objective_id=objective_id,
                    priority=int(last["priority"]),
                    created_at=str(last["created_at"]),
                    task_id=str(last["orchestration_task_id"]),
                ),
                secret=cursor_secret,
            )
        return tasks, next_cursor

    def get_task(self, task_id: str) -> dict[str, Any]:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ControllerError(400, "invalid_task_id", "Invalid task identifier")
        sql = self._task_projection_sql() + " WHERE t.orchestration_task_id = ?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (task_id,)).fetchone()
                if row is None:
                    exists = connection.execute(
                        "SELECT 1 FROM orchestration_tasks WHERE orchestration_task_id = ?",
                        (task_id,),
                    ).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            if exists is not None:
                raise ControllerError(
                    503,
                    "task_projection_invalid",
                    "Task projection unavailable",
                    "The task is not linked to a public objective.",
                    resource={"type": "task", "id": task_id},
                )
            raise ControllerError(
                404,
                "task_not_found",
                "Task not found",
                resource={"type": "task", "id": task_id},
            )
        return self._task(row)

    @staticmethod
    def _run_projection_sql() -> str:
        return """
            SELECT
                a.attempt_id,
                a.orchestration_task_id,
                a.attempt_number,
                a.status AS attempt_status,
                a.executor_instance_id,
                a.run_id AS legacy_run_id,
                a.worker_execution_id,
                a.review_execution_id,
                a.integration_id,
                a.result_json AS attempt_result_json,
                a.failure_reason AS attempt_failure_reason,
                a.started_at AS attempt_started_at,
                a.heartbeat_at AS attempt_heartbeat_at,
                a.finished_at AS attempt_finished_at,
                t.plan_id,
                t.project_id AS task_project_id,
                t.role_id AS task_role_id,
                (
                    SELECT q2.objective_id
                    FROM objective_queue AS q2
                    WHERE q2.plan_id = t.plan_id
                    ORDER BY q2.created_at DESC, q2.objective_id DESC
                    LIMIT 1
                ) AS objective_id,
                (
                    SELECT COUNT(*)
                    FROM objective_queue AS q3
                    WHERE q3.plan_id = t.plan_id
                ) AS objective_count,
                r.role_id AS registered_role_id,
                r.profile_name,
                r.workspace_mode AS registered_workspace_mode,
                lr.run_id AS joined_legacy_run_id,
                lr.project_id AS transaction_project_id,
                lr.status AS legacy_run_status,
                w.execution_id AS joined_worker_execution_id,
                w.run_id AS worker_legacy_run_id,
                w.role_id AS worker_role_id,
                w.source_profile,
                w.runtime_profile,
                w.workspace_mode,
                w.network_enabled,
                w.cpu_limit,
                w.memory_mb,
                w.mount_verified,
                w.isolation_verified,
                w.exit_code,
                w.result_json AS worker_result_json,
                w.failure_reason AS worker_failure_reason,
                w.created_at AS worker_created_at,
                w.started_at AS worker_started_at,
                w.finished_at AS worker_finished_at
            FROM orchestration_attempts AS a
            JOIN orchestration_tasks AS t
                ON t.orchestration_task_id = a.orchestration_task_id
            LEFT JOIN roles AS r
                ON r.role_id = COALESCE(t.role_id, (
                    SELECT role_id FROM worker_executions
                    WHERE execution_id = a.worker_execution_id
                ))
            LEFT JOIN runs AS lr ON lr.run_id = a.run_id
            LEFT JOIN worker_executions AS w
                ON w.execution_id = a.worker_execution_id
        """

    @staticmethod
    def _image_digest(raw: Any) -> str | None:
        if not isinstance(raw, str) or raw in {"", "{}", "null"}:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict):
            return None
        image_reference = payload.get("sandbox_image_reference")
        if not isinstance(image_reference, str):
            return None
        try:
            parsed = parse_immutable_oci_reference(image_reference)
        except ValueError:
            return None
        digest = parsed.digest
        projected = payload.get("sandbox_image_digest")
        if projected is not None and projected != digest:
            return None
        return digest

    def _run(self, row: sqlite3.Row) -> dict[str, Any]:
        run_id = self._require_identifier(
            row["attempt_id"],
            RUN_ID_PATTERN,
            code="run_projection_invalid",
            title="Run projection unavailable",
            resource_type="run",
            resource_id="unavailable",
        )
        task_id = self._require_identifier(
            row["orchestration_task_id"],
            TASK_ID_PATTERN,
            code="run_projection_invalid",
            title="Run projection unavailable",
            resource_type="run",
            resource_id=run_id,
        )
        if int(row["objective_count"]) != 1:
            raise ControllerError(
                503,
                "run_projection_invalid",
                "Run projection unavailable",
                "A run is not linked to exactly one public objective.",
                resource={"type": "run", "id": run_id},
            )
        objective_id = self._require_identifier(
            row["objective_id"],
            OBJECTIVE_ID_PATTERN,
            code="run_projection_invalid",
            title="Run projection unavailable",
            resource_type="run",
            resource_id=run_id,
        )
        internal_transaction_key = None
        transaction_reference = None
        if row["legacy_run_id"] is not None:
            internal_transaction_key = self._internal_transaction_key(
                row["legacy_run_id"],
                run_id=run_id,
            )
            if str(row["joined_legacy_run_id"] or "") != internal_transaction_key:
                raise ControllerError(
                    503,
                    "run_projection_invalid",
                    "Run projection unavailable",
                    "The run references a missing linked transaction.",
                    resource={"type": "run", "id": run_id},
                )
            transaction_reference = self._transaction_reference(
                internal_transaction_key
            )
            if not TRANSACTION_REFERENCE_PATTERN.fullmatch(transaction_reference):
                raise ControllerError(
                    503,
                    "run_projection_invalid",
                    "Run projection unavailable",
                    "The linked transaction could not be projected safely.",
                    resource={"type": "run", "id": run_id},
                )
        worker_id = None
        if row["worker_execution_id"] is not None:
            worker_id = self._require_identifier(
                row["worker_execution_id"],
                WORKER_EXECUTION_ID_PATTERN,
                code="run_projection_invalid",
                title="Run projection unavailable",
                resource_type="run",
                resource_id=run_id,
            )
            if str(row["joined_worker_execution_id"] or "") != worker_id:
                raise ControllerError(
                    503,
                    "run_projection_invalid",
                    "Run projection unavailable",
                    "The run references a missing worker execution.",
                    resource={"type": "run", "id": run_id},
                )
            worker_transaction_key = self._internal_transaction_key(
                row["worker_legacy_run_id"],
                run_id=run_id,
            )
            if (
                internal_transaction_key is None
                or worker_transaction_key != internal_transaction_key
            ):
                raise ControllerError(
                    503,
                    "run_projection_invalid",
                    "Run projection unavailable",
                    "The worker execution is linked to a different transaction.",
                    resource={"type": "run", "id": run_id},
                )
        task_project_id = (
            str(row["task_project_id"])
            if row["task_project_id"] is not None
            else None
        )
        if task_project_id is not None and not PROJECT_ID_PATTERN.fullmatch(task_project_id):
            raise self._projection_error(
                code="run_projection_invalid", title="Run projection unavailable",
                detail="A run references an invalid task project identifier.",
                resource_type="run", resource_id=run_id,
            )
        transaction_project_id = (
            str(row["transaction_project_id"])
            if row["transaction_project_id"] is not None
            else None
        )
        if internal_transaction_key is not None:
            if (
                transaction_project_id is None
                or not PROJECT_ID_PATTERN.fullmatch(transaction_project_id)
                or transaction_project_id != task_project_id
            ):
                raise self._projection_error(
                    code="run_projection_invalid", title="Run projection unavailable",
                    detail="The linked transaction belongs to a different or invalid project.",
                    resource_type="run", resource_id=run_id,
                )
        task_role_id = str(row["task_role_id"]) if row["task_role_id"] else None
        worker_role_id = str(row["worker_role_id"]) if row["worker_role_id"] else None
        if worker_id is not None and (
            worker_role_id is None
            or (task_role_id is not None and worker_role_id != task_role_id)
        ):
            raise self._projection_error(
                code="run_projection_invalid", title="Run projection unavailable",
                detail="The worker execution role does not match the task role.",
                resource_type="run", resource_id=run_id,
            )
        role_id = str(task_role_id or worker_role_id or "system")
        registered_role = (
            str(row["registered_role_id"])
            if row["registered_role_id"] is not None
            else None
        )
        if role_id != "system" and (
            not ROLE_ID_PATTERN.fullmatch(role_id)
            or registered_role != role_id
        ):
            raise ControllerError(
                503,
                "run_projection_invalid",
                "Run projection unavailable",
                "A run references an invalid or unregistered role identifier.",
                resource={"type": "run", "id": run_id},
            )
        registered_profile = str(row["profile_name"] or "")
        registered_workspace_mode = str(row["registered_workspace_mode"] or "")
        if role_id != "system" and (
            not PROFILE_ID_PATTERN.fullmatch(registered_profile)
            or registered_workspace_mode not in WORKSPACE_MODES
        ):
            raise self._projection_error(
                code="run_projection_invalid", title="Run projection unavailable",
                detail="The registered role contains invalid public execution metadata.",
                resource_type="run", resource_id=run_id,
            )
        source_profile = str(row["source_profile"] or "")
        workspace_mode = str(row["workspace_mode"] or "")
        if worker_id is not None and (
            source_profile != registered_profile
            or workspace_mode != registered_workspace_mode
        ):
            raise self._projection_error(
                code="run_projection_invalid", title="Run projection unavailable",
                detail="The worker execution metadata does not match its registered role.",
                resource_type="run", resource_id=run_id,
            )
        sandbox_profile_id = source_profile or registered_profile or role_id
        if sandbox_profile_id != "system" and not PROFILE_ID_PATTERN.fullmatch(sandbox_profile_id):
            raise self._projection_error(
                code="run_projection_invalid", title="Run projection unavailable",
                detail="A run references an invalid sandbox profile.",
                resource_type="run", resource_id=run_id,
            )
        runtime_profile = str(row["runtime_profile"] or "")
        if worker_id is not None and not PROFILE_ID_PATTERN.fullmatch(runtime_profile):
            raise self._projection_error(
                code="run_projection_invalid", title="Run projection unavailable",
                detail="A worker execution references an invalid runtime profile.",
                resource_type="run", resource_id=run_id,
            )
        state = self._run_state(
            str(row["attempt_status"]),
            str(row["legacy_run_status"]) if row["legacy_run_status"] else None,
        )
        attempt_started_at = self._timestamp(
            row["attempt_started_at"], required=True, code="run_projection_invalid",
            title="Run projection unavailable", resource_type="run", resource_id=run_id,
        )
        attempt_heartbeat_at = self._timestamp(
            row["attempt_heartbeat_at"], required=True, code="run_projection_invalid",
            title="Run projection unavailable", resource_type="run", resource_id=run_id,
        )
        attempt_finished_at = self._timestamp(
            row["attempt_finished_at"], required=False, code="run_projection_invalid",
            title="Run projection unavailable", resource_type="run", resource_id=run_id,
        )
        updated_at = attempt_finished_at or attempt_heartbeat_at or attempt_started_at
        attempt_number = self._integer(
            row["attempt_number"], minimum=1, code="run_projection_invalid",
            title="Run projection unavailable", resource_type="run", resource_id=run_id,
        )
        attempt_has_result = str(row["attempt_result_json"] or "") not in {"", "{}", "null"}
        worker: dict[str, Any] | None = None
        if worker_id is not None:
            worker_has_result = str(row["worker_result_json"] or "") not in {"", "{}", "null"}
            worker_created_at = self._timestamp(
                row["worker_created_at"], required=True, code="run_projection_invalid",
                title="Run projection unavailable", resource_type="run", resource_id=run_id,
            )
            worker_started_at = self._timestamp(
                row["worker_started_at"], required=False, code="run_projection_invalid",
                title="Run projection unavailable", resource_type="run", resource_id=run_id,
            )
            worker_finished_at = self._timestamp(
                row["worker_finished_at"], required=False, code="run_projection_invalid",
                title="Run projection unavailable", resource_type="run", resource_id=run_id,
            )
            exit_code = (
                self._integer(
                    row["exit_code"], minimum=-255, maximum=255,
                    code="run_projection_invalid", title="Run projection unavailable",
                    resource_type="run", resource_id=run_id,
                )
                if row["exit_code"] is not None else None
            )
            worker = {
                "id": worker_id,
                "kind": "worker",
                "source_profile": source_profile,
                "runtime_profile": runtime_profile,
                "workspace_mode": workspace_mode,
                "network_enabled": self._flag(
                    row["network_enabled"], code="run_projection_invalid",
                    title="Run projection unavailable", resource_type="run", resource_id=run_id,
                ),
                "cpu_limit": self._integer(
                    row["cpu_limit"], minimum=1, code="run_projection_invalid",
                    title="Run projection unavailable", resource_type="run", resource_id=run_id,
                ),
                "memory_mb": self._integer(
                    row["memory_mb"], minimum=1, code="run_projection_invalid",
                    title="Run projection unavailable", resource_type="run", resource_id=run_id,
                ),
                "mount_verified": self._flag(
                    row["mount_verified"], code="run_projection_invalid",
                    title="Run projection unavailable", resource_type="run", resource_id=run_id,
                ),
                "isolation_verified": self._flag(
                    row["isolation_verified"], code="run_projection_invalid",
                    title="Run projection unavailable", resource_type="run", resource_id=run_id,
                ),
                "exit_code": exit_code,
                "created_at": worker_created_at,
                "started_at": worker_started_at,
                "finished_at": worker_finished_at,
                "result": (
                    {"available": True, "legacy_payload_redacted": True}
                    if worker_has_result else None
                ),
                "error": (
                    {"available": True, "legacy_payload_redacted": True}
                    if row["worker_failure_reason"] is not None else None
                ),
            }
        payload: dict[str, Any] = {
            "id": run_id,
            "created_at": attempt_started_at,
            "updated_at": updated_at,
            "state": state,
            "task_id": task_id,
            "role_id": role_id,
            "sandbox_profile_id": sandbox_profile_id,
            "sandbox_image_digest": self._image_digest(row["worker_result_json"]),
            "objective_id": objective_id,
            "attempt_number": attempt_number,
            "transaction_run_id": transaction_reference,
            "worker_execution": worker,
            "review_execution_available": row["review_execution_id"] is not None,
            "integration_available": row["integration_id"] is not None,
            "executor_instance_available": row["executor_instance_id"] is not None,
            "finished_at": attempt_finished_at,
            "result": (
                {"available": True, "legacy_payload_redacted": True}
                if attempt_has_result else None
            ),
            "error": (
                {"available": True, "legacy_payload_redacted": True}
                if row["attempt_failure_reason"] is not None else None
            ),
            "raw_state": str(row["attempt_status"]),
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def _task_exists(self, task_id: str) -> None:
        if not TASK_ID_PATTERN.fullmatch(task_id):
            raise ControllerError(400, "invalid_task_id", "Invalid task identifier")
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(
                    "SELECT 1 FROM orchestration_tasks WHERE orchestration_task_id = ?",
                    (task_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "task_not_found",
                "Task not found",
                resource={"type": "task", "id": task_id},
            )

    def list_task_runs(
        self,
        task_id: str,
        *,
        limit: int,
        cursor: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not 1 <= limit <= 200:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )
        self._task_exists(task_id)
        decoded = (
            self._decode_run_cursor(cursor, task_id=task_id, secret=cursor_secret)
            if cursor is not None else None
        )
        sql = self._run_projection_sql() + " WHERE a.orchestration_task_id = ?"
        parameters: list[Any] = [task_id]
        if decoded is not None:
            sql += " AND (a.attempt_number < ? OR (a.attempt_number = ? AND a.attempt_id < ?))"
            parameters.extend([decoded.attempt_number, decoded.attempt_number, decoded.run_id])
        sql += " ORDER BY a.attempt_number DESC, a.attempt_id DESC LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        has_more = len(rows) > limit
        selected = rows[:limit]
        runs = [self._run(row) for row in selected]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = self._encode_run_cursor(
                RunCursor(
                    task_id=task_id,
                    attempt_number=int(last["attempt_number"]),
                    run_id=str(last["attempt_id"]),
                ),
                secret=cursor_secret,
            )
        return runs, next_cursor

    def _run_row(self, run_id: str) -> sqlite3.Row:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ControllerError(400, "invalid_run_id", "Invalid run identifier")
        sql = self._run_projection_sql() + " WHERE a.attempt_id = ?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (run_id,)).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "run_not_found",
                "Run not found",
                resource={"type": "run", "id": run_id},
            )
        return row

    def get_run(self, run_id: str) -> dict[str, Any]:
        return self._run(self._run_row(run_id))

    @staticmethod
    def _event_message(event_type: str) -> str:
        words = " ".join(event_type.replace("_", " ").split())
        return words[:200].title() if words else "Runtime event"

    def get_run_logs(
        self,
        run_id: str,
        *,
        after_sequence: int,
        limit: int,
    ) -> tuple[dict[str, Any], int | None]:
        if after_sequence < 0:
            raise ControllerError(
                400,
                "invalid_after_sequence",
                "Invalid log sequence",
                "after_sequence must be zero or greater.",
            )
        if not 1 <= limit <= 500:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 500.",
            )
        row = self._run_row(run_id)
        self._run(row)
        legacy_run_id = row["legacy_run_id"]
        if legacy_run_id is None:
            return {"entries": [], "next_sequence": None, "truncated": False}, None
        legacy_run_id = self._internal_transaction_key(
            legacy_run_id,
            run_id=run_id,
        )
        try:
            with closing(self.database.connect()) as connection:
                snapshot_row = connection.execute(
                    "SELECT MAX(event_id) FROM events WHERE run_id = ?",
                    (legacy_run_id,),
                ).fetchone()
                snapshot = int(snapshot_row[0]) if snapshot_row and snapshot_row[0] is not None else None
                if snapshot is None:
                    rows: list[sqlite3.Row] = []
                else:
                    rows = list(connection.execute(
                        """
                        SELECT event_id, project_id, event_type, severity, payload_json, created_at
                        FROM events
                        WHERE run_id = ? AND event_id > ? AND event_id <= ?
                        ORDER BY event_id
                        LIMIT ?
                        """,
                        (legacy_run_id, after_sequence, snapshot, limit + 1),
                    ))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        has_more = len(rows) > limit
        selected = rows[:limit]
        entries: list[dict[str, Any]] = []
        for event in selected:
            sequence = self._integer(
                event["event_id"], minimum=1, code="log_projection_invalid",
                title="Run logs unavailable", resource_type="run", resource_id=run_id,
            )
            event_type = str(event["event_type"])
            severity = str(event["severity"])
            event_project_id = (
                str(event["project_id"]) if event["project_id"] is not None else None
            )
            expected_project_id = (
                str(row["transaction_project_id"])
                if row["transaction_project_id"] is not None else None
            )
            if (
                not EVENT_TYPE_PATTERN.fullmatch(event_type)
                or severity not in EVENT_SEVERITIES
                or (
                    event_project_id is not None
                    and event_project_id != expected_project_id
                )
            ):
                raise ControllerError(
                    503,
                    "log_projection_invalid",
                    "Run logs unavailable",
                    "A persisted event cannot be projected safely.",
                    resource={"type": "run", "id": run_id},
                )
            raw_payload = str(event["payload_json"] or "")
            payload_available = raw_payload not in {"", "{}", "null"}
            payload_valid = True
            if payload_available:
                try:
                    json.loads(raw_payload)
                except json.JSONDecodeError:
                    payload_valid = False
            entries.append({
                "sequence": sequence,
                "timestamp": self._timestamp(
                    event["created_at"], required=True,
                    code="log_projection_invalid", title="Run logs unavailable",
                    resource_type="run", resource_id=run_id,
                ),
                "severity": severity.lower(),
                "event_type": event_type,
                "message": self._event_message(event_type),
                "payload_available": payload_available,
                "payload_redacted": payload_available,
                "payload_valid": payload_valid,
            })
        next_sequence = int(selected[-1]["event_id"]) if has_more and selected else None
        return {
            "entries": entries,
            "next_sequence": next_sequence,
            "truncated": has_more,
        }, snapshot
