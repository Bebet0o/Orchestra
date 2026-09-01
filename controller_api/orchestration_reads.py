from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .core import ControllerError, PROJECT_ID_PATTERN, ReadOnlyDatabase, Settings

PLAN_ID_PATTERN = re.compile(r"^plan-[a-f0-9]{32}$")
TASK_ID_PATTERN = re.compile(r"^orchestration-task-[a-f0-9]{32}$")
RUN_ID_PATTERN = re.compile(r"^orchestration-attempt-[a-f0-9]{32}$")
ASSIGNMENT_ID_PATTERN = re.compile(r"^review-assignment-[a-f0-9]{32}$")
OBJECTIVE_ID_PATTERN = re.compile(r"^objective-[a-f0-9]{32}$")
ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,127}$")
WORKER_EXECUTION_ID_PATTERN = re.compile(r"^execution-[a-f0-9]{32}$")
REVIEW_EXECUTION_ID_PATTERN = re.compile(r"^review-execution-[a-f0-9]{32}$")
REVIEW_ID_PATTERN = re.compile(r"^review-[a-f0-9]{32}$")
INTEGRATION_ID_PATTERN = re.compile(r"^integration-[a-f0-9]{32}$")
FAILURE_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
TASK_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
HEX_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
MAX_CURSOR_BYTES = 2048

PLAN_STATES = {
    "draft": "DRAFT",
    "ready": "READY",
    "running": "RUNNING",
    "blocked": "BLOCKED",
    "succeeded": "COMPLETED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
}
TASK_STATES = {
    "PENDING": "pending",
    "READY": "ready",
    "RUNNING": "running",
    "BLOCKED": "blocked",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "CANCELLED": "cancelled",
}
ATTEMPT_STATES = {
    "RUNNING": "running",
    "COMPLETED": "succeeded",
    "FAILED": "failed",
    "ABANDONED": "interrupted",
    "CANCELLED": "cancelled",
}
ASSIGNMENT_STATES = {
    "assigned": "ASSIGNED",
    "claimed": "CLAIMED",
    "completed": "COMPLETED",
    "failed": "FAILED",
    "cancelled": "CANCELLED",
}


@dataclass(frozen=True)
class ListCursor:
    created_at: str
    item_id: str


@dataclass(frozen=True)
class TaskCursor:
    priority: int
    created_at: str
    task_id: str


@dataclass(frozen=True)
class DependencyCursor:
    task_id: str
    parent_id: str


@dataclass(frozen=True)
class AttemptCursor:
    attempt_number: int
    attempt_id: str


class OrchestrationReadStore:
    """Read-only, redacted projections for orchestration and reviewer assignment state."""

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
        return int(hashlib.sha256(canonical).hexdigest()[:13], 16)

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
    def _identifier(
        cls,
        value: Any,
        pattern: re.Pattern[str],
        *,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
        nullable: bool = False,
    ) -> str | None:
        if value is None and nullable:
            return None
        candidate = str(value) if value is not None else ""
        if not pattern.fullmatch(candidate):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored orchestration data contains an invalid public identifier.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return candidate

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
                detail="Stored orchestration data is missing a required timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        candidate = str(value)
        if not TIMESTAMP_PATTERN.fullmatch(candidate):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored orchestration data contains an invalid timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        try:
            datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError as error:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored orchestration data contains an invalid timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            ) from error
        return candidate

    @classmethod
    def _integer(
        cls,
        value: Any,
        *,
        minimum: int,
        maximum: int | None,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> int:
        if type(value) is not int or value < minimum or (
            maximum is not None and value > maximum
        ):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored orchestration data contains an invalid numeric value.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return value

    @classmethod
    def _bounded_text(
        cls,
        value: Any,
        *,
        minimum: int,
        maximum: int,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> str:
        candidate = str(value) if value is not None else ""
        if not minimum <= len(candidate) <= maximum or any(
            ord(character) < 32 or ord(character) == 127
            for character in candidate
        ):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored orchestration data contains invalid public text.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return candidate

    @staticmethod
    def _cursor_signature(raw: bytes, secret: str, namespace: bytes) -> bytes:
        return hmac.new(
            secret.encode("ascii"),
            namespace + b"\0" + raw,
            hashlib.sha256,
        ).digest()

    @classmethod
    def _encode_cursor(
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
    def _decode_cursor(
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

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 200:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )

    @staticmethod
    def _validate_project_filter(project_id: str | None) -> str | None:
        if project_id is not None and not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ControllerError(400, "invalid_project_id", "Invalid project identifier")
        return project_id

    @staticmethod
    def _transaction_reference(value: Any) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ControllerError(
                503,
                "orchestration_projection_invalid",
                "Orchestration projection unavailable",
                "A linked transaction contains an invalid internal identifier.",
            )
        encoded = value.encode("utf-8")
        if (
            not value
            or len(encoded) > 512
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ControllerError(
                503,
                "orchestration_projection_invalid",
                "Orchestration projection unavailable",
                "A linked transaction contains an invalid internal identifier.",
            )
        digest = hashlib.sha256(
            b"hermesops-transaction-reference-v1\0" + encoded
        ).hexdigest()
        return "transaction-" + digest[:32]

    @staticmethod
    def _plan_projection_sql() -> str:
        return """
            SELECT
                p.plan_id,
                p.source,
                p.planner_role_id,
                p.status AS raw_status,
                p.max_parallel_tasks,
                p.plan_sha256,
                p.plan_json,
                p.created_at,
                p.started_at,
                p.heartbeat_at,
                p.finished_at,
                p.last_error,
                (
                    SELECT q.objective_id
                    FROM objective_queue AS q
                    WHERE q.plan_id = p.plan_id
                    ORDER BY q.created_at DESC, q.objective_id DESC
                    LIMIT 1
                ) AS objective_id,
                (
                    SELECT COUNT(*)
                    FROM objective_queue AS q
                    WHERE q.plan_id = p.plan_id
                ) AS objective_count,
                (
                    SELECT GROUP_CONCAT(project_id)
                    FROM (
                        SELECT DISTINCT t.project_id AS project_id
                        FROM orchestration_tasks AS t
                        WHERE t.plan_id = p.plan_id
                          AND t.project_id IS NOT NULL
                        ORDER BY t.project_id
                    )
                ) AS project_ids,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id
                ) AS task_total,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'PENDING'
                ) AS task_pending,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'READY'
                ) AS task_ready,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'RUNNING'
                ) AS task_running,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'BLOCKED'
                ) AS task_blocked,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'COMPLETED'
                ) AS task_succeeded,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'FAILED'
                ) AS task_failed,
                (
                    SELECT COUNT(*) FROM orchestration_tasks AS t
                    WHERE t.plan_id = p.plan_id AND t.status = 'CANCELLED'
                ) AS task_cancelled,
                (
                    SELECT COUNT(*)
                    FROM orchestration_attempts AS a
                    JOIN orchestration_tasks AS t
                      ON t.orchestration_task_id = a.orchestration_task_id
                    WHERE t.plan_id = p.plan_id
                ) AS attempt_count,
                (
                    SELECT COUNT(*)
                    FROM reviewer_assignments AS ra
                    JOIN orchestration_attempts AS a
                      ON a.attempt_id = ra.orchestration_attempt_id
                    JOIN orchestration_tasks AS t
                      ON t.orchestration_task_id = a.orchestration_task_id
                    WHERE t.plan_id = p.plan_id
                ) AS assignment_count
            FROM orchestration_plans AS p
        """

    @classmethod
    def _plan_state(cls, raw: Any, *, plan_id: str) -> str:
        inverse = {value: key for key, value in PLAN_STATES.items()}
        try:
            return inverse[str(raw)]
        except KeyError as error:
            raise cls._projection_error(
                code="plan_projection_invalid",
                title="Plan projection unavailable",
                detail="A plan contains an unsupported runtime state.",
                resource_type="plan",
                resource_id=plan_id,
            ) from error

    def _plan(self, row: sqlite3.Row) -> dict[str, Any]:
        code = "plan_projection_invalid"
        title = "Plan projection unavailable"
        plan_id = self._identifier(
            row["plan_id"], PLAN_ID_PATTERN,
            code=code, title=title, resource_type="plan", resource_id="unavailable",
        )
        assert plan_id is not None
        objective_count = self._integer(
            row["objective_count"], minimum=0, maximum=1,
            code=code, title=title, resource_type="plan", resource_id=plan_id,
        )
        objective_id = None
        if objective_count == 1:
            objective_id = self._identifier(
                row["objective_id"], OBJECTIVE_ID_PATTERN,
                code=code, title=title, resource_type="plan", resource_id=plan_id,
            )
        source = str(row["source"])
        if source not in {"AI", "DECLARATIVE", "TEST"}:
            raise self._projection_error(
                code=code, title=title,
                detail="A plan contains an unsupported source.",
                resource_type="plan", resource_id=plan_id,
            )
        role_id = self._identifier(
            row["planner_role_id"], ROLE_ID_PATTERN,
            code=code, title=title, resource_type="plan", resource_id=plan_id,
        )
        assert role_id is not None
        plan_digest = str(row["plan_sha256"] or "")
        if not HEX_SHA256_PATTERN.fullmatch(plan_digest):
            raise self._projection_error(
                code=code, title=title,
                detail="A plan contains an invalid digest.",
                resource_type="plan", resource_id=plan_id,
            )
        project_ids = [] if row["project_ids"] is None else str(row["project_ids"]).split(",")
        if (
            len(project_ids) != len(set(project_ids))
            or any(not PROJECT_ID_PATTERN.fullmatch(value) for value in project_ids)
        ):
            raise self._projection_error(
                code=code, title=title,
                detail="A plan contains an invalid project projection.",
                resource_type="plan", resource_id=plan_id,
            )
        created_at = self._timestamp(
            row["created_at"], required=True, code=code, title=title,
            resource_type="plan", resource_id=plan_id,
        )
        started_at = self._timestamp(
            row["started_at"], required=False, code=code, title=title,
            resource_type="plan", resource_id=plan_id,
        )
        heartbeat_at = self._timestamp(
            row["heartbeat_at"], required=False, code=code, title=title,
            resource_type="plan", resource_id=plan_id,
        )
        finished_at = self._timestamp(
            row["finished_at"], required=False, code=code, title=title,
            resource_type="plan", resource_id=plan_id,
        )
        counts = {
            "total": self._integer(
                row["task_total"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "pending": self._integer(
                row["task_pending"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "ready": self._integer(
                row["task_ready"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "running": self._integer(
                row["task_running"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "blocked": self._integer(
                row["task_blocked"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "succeeded": self._integer(
                row["task_succeeded"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "failed": self._integer(
                row["task_failed"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
            "cancelled": self._integer(
                row["task_cancelled"], minimum=0, maximum=None, code=code, title=title,
                resource_type="plan", resource_id=plan_id,
            ),
        }
        if counts["total"] != sum(value for key, value in counts.items() if key != "total"):
            raise self._projection_error(
                code=code, title=title,
                detail="A plan contains inconsistent task counts.",
                resource_type="plan", resource_id=plan_id,
            )
        payload: dict[str, Any] = {
            "id": plan_id,
            "objective_id": objective_id,
            "project_ids": project_ids,
            "state": self._plan_state(row["raw_status"], plan_id=plan_id),
            "raw_state": str(row["raw_status"]),
            "source": source.lower(),
            "planner_role_id": role_id,
            "max_parallel_tasks": self._integer(
                row["max_parallel_tasks"], minimum=1, maximum=64,
                code=code, title=title, resource_type="plan", resource_id=plan_id,
            ),
            "plan_digest": plan_digest,
            "task_counts": counts,
            "attempt_count": self._integer(
                row["attempt_count"], minimum=0, maximum=None,
                code=code, title=title, resource_type="plan", resource_id=plan_id,
            ),
            "reviewer_assignment_count": self._integer(
                row["assignment_count"], minimum=0, maximum=None,
                code=code, title=title, resource_type="plan", resource_id=plan_id,
            ),
            "created_at": created_at,
            "updated_at": finished_at or heartbeat_at or started_at or created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "definition_redacted": True,
            "error": (
                {"available": True, "legacy_payload_redacted": True}
                if row["last_error"] is not None else None
            ),
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def list_plans(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: str | None,
        state: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        project_id = self._validate_project_filter(project_id)
        if state is not None and state not in PLAN_STATES:
            raise ControllerError(400, "invalid_state", "Invalid plan state")
        decoded: ListCursor | None = None
        if cursor is not None:
            payload = self._decode_cursor(
                cursor, secret=cursor_secret, namespace=b"orchestra-plan-cursor-v1"
            )
            if (
                payload.get("v") != 1
                or payload.get("p") != project_id
                or payload.get("s") != state
                or not isinstance(payload.get("c"), str)
                or not isinstance(payload.get("i"), str)
                or not PLAN_ID_PATTERN.fullmatch(payload["i"])
            ):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            decoded = ListCursor(payload["c"], payload["i"])
        sql = self._plan_projection_sql() + " WHERE 1=1"
        parameters: list[Any] = []
        if project_id is not None:
            sql += """
                AND EXISTS (
                    SELECT 1 FROM orchestration_tasks AS tf
                    WHERE tf.plan_id = p.plan_id AND tf.project_id = ?
                )
            """
            parameters.append(project_id)
        if state is not None:
            sql += " AND p.status = ?"
            parameters.append(PLAN_STATES[state])
        if decoded is not None:
            sql += " AND (p.created_at < ? OR (p.created_at = ? AND p.plan_id < ?))"
            parameters.extend([decoded.created_at, decoded.created_at, decoded.item_id])
        sql += " ORDER BY p.created_at DESC, p.plan_id DESC LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        selected = rows[:limit]
        items = [self._plan(row) for row in selected]
        next_cursor = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                {
                    "v": 1,
                    "p": project_id,
                    "s": state,
                    "c": str(last["created_at"]),
                    "i": str(last["plan_id"]),
                },
                secret=cursor_secret,
                namespace=b"orchestra-plan-cursor-v1",
            )
        return items, next_cursor

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        if not PLAN_ID_PATTERN.fullmatch(plan_id):
            raise ControllerError(400, "invalid_plan_id", "Invalid plan identifier")
        sql = self._plan_projection_sql() + " WHERE p.plan_id = ?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (plan_id,)).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404, "plan_not_found", "Plan not found",
                resource={"type": "plan", "id": plan_id},
            )
        return self._plan(row)

    def _require_plan(self, plan_id: str) -> None:
        if not PLAN_ID_PATTERN.fullmatch(plan_id):
            raise ControllerError(400, "invalid_plan_id", "Invalid plan identifier")
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(
                    "SELECT 1 FROM orchestration_plans WHERE plan_id=?",
                    (plan_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404, "plan_not_found", "Plan not found",
                resource={"type": "plan", "id": plan_id},
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
                r.role_id AS registered_role_id,
                r.workspace_mode,
                COALESCE((
                    SELECT COUNT(*) FROM orchestration_dependencies AS d
                    WHERE d.orchestration_task_id=t.orchestration_task_id
                ), 0) AS dependency_count,
                COALESCE((
                    SELECT COUNT(*) FROM orchestration_dependencies AS d
                    WHERE d.depends_on_task_id=t.orchestration_task_id
                ), 0) AS dependent_count
            FROM orchestration_tasks AS t
            LEFT JOIN roles AS r ON r.role_id=t.role_id
        """

    def _task(self, row: sqlite3.Row) -> dict[str, Any]:
        code = "plan_task_projection_invalid"
        title = "Plan task projection unavailable"
        task_id = self._identifier(
            row["orchestration_task_id"], TASK_ID_PATTERN,
            code=code, title=title, resource_type="task", resource_id="unavailable",
        )
        assert task_id is not None
        plan_id = self._identifier(
            row["plan_id"], PLAN_ID_PATTERN,
            code=code, title=title, resource_type="task", resource_id=task_id,
        )
        assert plan_id is not None
        task_key = str(row["task_key"])
        if not TASK_KEY_PATTERN.fullmatch(task_key):
            raise self._projection_error(
                code=code, title=title, detail="A task contains an invalid key.",
                resource_type="task", resource_id=task_id,
            )
        kind = str(row["kind"])
        if kind not in {"PIPELINE", "NOOP", "TEST_SLEEP", "TEST_FAIL"}:
            raise self._projection_error(
                code=code, title=title, detail="A task contains an unsupported kind.",
                resource_type="task", resource_id=task_id,
            )
        project_id = self._identifier(
            row["project_id"], PROJECT_ID_PATTERN,
            code=code, title=title, resource_type="task", resource_id=task_id,
            nullable=True,
        )
        role_id = "system"
        writer = False
        if row["role_id"] is not None:
            role_id = self._identifier(
                row["role_id"], ROLE_ID_PATTERN,
                code=code, title=title, resource_type="task", resource_id=task_id,
            )
            if row["registered_role_id"] != role_id:
                raise self._projection_error(
                    code=code, title=title,
                    detail="A task references an unregistered role.",
                    resource_type="task", resource_id=task_id,
                )
            workspace_mode = str(row["workspace_mode"] or "")
            if workspace_mode not in {"read", "write", "read_only", "controller_only"}:
                raise self._projection_error(
                    code=code, title=title,
                    detail="A task references an invalid workspace mode.",
                    resource_type="task", resource_id=task_id,
                )
            writer = workspace_mode == "write"
        raw_status = str(row["raw_status"])
        if raw_status not in TASK_STATES:
            raise self._projection_error(
                code=code, title=title, detail="A task contains an unsupported state.",
                resource_type="task", resource_id=task_id,
            )
        created_at = self._timestamp(
            row["created_at"], required=True, code=code, title=title,
            resource_type="task", resource_id=task_id,
        )
        started_at = self._timestamp(
            row["started_at"], required=False, code=code, title=title,
            resource_type="task", resource_id=task_id,
        )
        heartbeat_at = self._timestamp(
            row["heartbeat_at"], required=False, code=code, title=title,
            resource_type="task", resource_id=task_id,
        )
        finished_at = self._timestamp(
            row["finished_at"], required=False, code=code, title=title,
            resource_type="task", resource_id=task_id,
        )
        title_text = " ".join(task_key.replace("_", " ").replace("-", " ").split())
        payload: dict[str, Any] = {
            "id": task_id,
            "plan_id": plan_id,
            "task_key": task_key,
            "title": title_text[:200] or "Untitled task",
            "kind": kind.lower(),
            "project_id": project_id,
            "role_id": role_id,
            "writer": writer,
            "state": TASK_STATES[raw_status],
            "raw_state": raw_status,
            "priority": self._integer(
                row["priority"], minimum=0, maximum=None, code=code, title=title,
                resource_type="task", resource_id=task_id,
            ),
            "attempt_count": self._integer(
                row["attempt_count"], minimum=0, maximum=10, code=code, title=title,
                resource_type="task", resource_id=task_id,
            ),
            "max_attempts": self._integer(
                row["max_attempts"], minimum=1, maximum=10, code=code, title=title,
                resource_type="task", resource_id=task_id,
            ),
            "dependency_count": self._integer(
                row["dependency_count"], minimum=0, maximum=None, code=code, title=title,
                resource_type="task", resource_id=task_id,
            ),
            "dependent_count": self._integer(
                row["dependent_count"], minimum=0, maximum=None, code=code, title=title,
                resource_type="task", resource_id=task_id,
            ),
            "created_at": created_at,
            "updated_at": finished_at or heartbeat_at or started_at or created_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "result": (
                {"available": True, "legacy_payload_redacted": True}
                if str(row["result_json"] or "") not in {"", "{}", "null"} else None
            ),
            "error": (
                {"available": True, "legacy_payload_redacted": True}
                if row["failure_reason"] is not None else None
            ),
            "instruction_redacted": True,
            "acceptance_redacted": True,
            "marker_redacted": True,
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def list_plan_tasks(
        self,
        plan_id: str,
        *,
        limit: int,
        cursor: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        self._require_plan(plan_id)
        decoded: TaskCursor | None = None
        if cursor is not None:
            payload = self._decode_cursor(
                cursor, secret=cursor_secret, namespace=b"orchestra-plan-task-cursor-v1"
            )
            if (
                payload.get("v") != 1
                or payload.get("p") != plan_id
                or not isinstance(payload.get("r"), int)
                or not isinstance(payload.get("c"), str)
                or not isinstance(payload.get("i"), str)
                or not TASK_ID_PATTERN.fullmatch(payload["i"])
            ):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            decoded = TaskCursor(payload["r"], payload["c"], payload["i"])
        sql = self._task_projection_sql() + " WHERE t.plan_id=?"
        parameters: list[Any] = [plan_id]
        if decoded is not None:
            sql += """
                AND (
                    t.priority > ?
                    OR (t.priority=? AND t.created_at>?)
                    OR (t.priority=? AND t.created_at=? AND t.orchestration_task_id>?)
                )
            """
            parameters.extend([
                decoded.priority,
                decoded.priority, decoded.created_at,
                decoded.priority, decoded.created_at, decoded.task_id,
            ])
        sql += " ORDER BY t.priority,t.created_at,t.orchestration_task_id LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        selected = rows[:limit]
        items = [self._task(row) for row in selected]
        next_cursor = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                {
                    "v": 1, "p": plan_id, "r": int(last["priority"]),
                    "c": str(last["created_at"]),
                    "i": str(last["orchestration_task_id"]),
                },
                secret=cursor_secret,
                namespace=b"orchestra-plan-task-cursor-v1",
            )
        return items, next_cursor

    def list_plan_dependencies(
        self,
        plan_id: str,
        *,
        limit: int,
        cursor: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        self._require_plan(plan_id)
        decoded: DependencyCursor | None = None
        if cursor is not None:
            payload = self._decode_cursor(
                cursor, secret=cursor_secret,
                namespace=b"orchestra-plan-dependency-cursor-v1",
            )
            if (
                payload.get("v") != 1
                or payload.get("p") != plan_id
                or not isinstance(payload.get("t"), str)
                or not isinstance(payload.get("d"), str)
                or not TASK_ID_PATTERN.fullmatch(payload["t"])
                or not TASK_ID_PATTERN.fullmatch(payload["d"])
            ):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            decoded = DependencyCursor(payload["t"], payload["d"])
        sql = """
            SELECT plan_id,orchestration_task_id,depends_on_task_id,dependency_condition
            FROM orchestration_dependencies
            WHERE plan_id=?
        """
        parameters: list[Any] = [plan_id]
        if decoded is not None:
            sql += """
                AND (
                    orchestration_task_id > ?
                    OR (orchestration_task_id=? AND depends_on_task_id>?)
                )
            """
            parameters.extend([decoded.task_id, decoded.task_id, decoded.parent_id])
        sql += " ORDER BY orchestration_task_id,depends_on_task_id LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        selected = rows[:limit]
        items: list[dict[str, Any]] = []
        for row in selected:
            task_id = self._identifier(
                row["orchestration_task_id"], TASK_ID_PATTERN,
                code="dependency_projection_invalid",
                title="Dependency projection unavailable",
                resource_type="plan", resource_id=plan_id,
            )
            parent_id = self._identifier(
                row["depends_on_task_id"], TASK_ID_PATTERN,
                code="dependency_projection_invalid",
                title="Dependency projection unavailable",
                resource_type="plan", resource_id=plan_id,
            )
            if row["dependency_condition"] != "SUCCESS":
                raise self._projection_error(
                    code="dependency_projection_invalid",
                    title="Dependency projection unavailable",
                    detail="A dependency contains an unsupported condition.",
                    resource_type="plan", resource_id=plan_id,
                )
            assert task_id is not None and parent_id is not None
            dependency_id = "dependency-" + hashlib.sha256(
                f"{plan_id}\0{task_id}\0{parent_id}".encode("utf-8")
            ).hexdigest()[:32]
            item = {
                "id": dependency_id,
                "plan_id": plan_id,
                "task_id": task_id,
                "depends_on_task_id": parent_id,
                "condition": "success",
            }
            item["resource_revision"] = self._revision(item)
            items.append(item)
        next_cursor = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                {
                    "v": 1, "p": plan_id,
                    "t": str(last["orchestration_task_id"]),
                    "d": str(last["depends_on_task_id"]),
                },
                secret=cursor_secret,
                namespace=b"orchestra-plan-dependency-cursor-v1",
            )
        return items, next_cursor

    @staticmethod
    def _attempt_projection_sql() -> str:
        return """
            SELECT
                a.attempt_id,
                a.orchestration_task_id,
                a.attempt_number,
                a.status AS raw_status,
                a.run_id AS internal_run_id,
                a.worker_execution_id,
                a.review_execution_id,
                a.integration_id,
                a.result_json,
                a.failure_reason,
                a.started_at,
                a.heartbeat_at,
                a.finished_at,
                t.plan_id,
                (
                    SELECT COUNT(*) FROM reviewer_assignments AS ra
                    WHERE ra.orchestration_attempt_id=a.attempt_id
                ) AS assignment_count
            FROM orchestration_attempts AS a
            JOIN orchestration_tasks AS t
              ON t.orchestration_task_id=a.orchestration_task_id
        """

    def _attempt(self, row: sqlite3.Row) -> dict[str, Any]:
        code = "plan_attempt_projection_invalid"
        title = "Plan attempt projection unavailable"
        attempt_id = self._identifier(
            row["attempt_id"], RUN_ID_PATTERN,
            code=code, title=title, resource_type="run", resource_id="unavailable",
        )
        assert attempt_id is not None
        task_id = self._identifier(
            row["orchestration_task_id"], TASK_ID_PATTERN,
            code=code, title=title, resource_type="run", resource_id=attempt_id,
        )
        plan_id = self._identifier(
            row["plan_id"], PLAN_ID_PATTERN,
            code=code, title=title, resource_type="run", resource_id=attempt_id,
        )
        assert task_id is not None and plan_id is not None
        raw_status = str(row["raw_status"])
        if raw_status not in ATTEMPT_STATES:
            raise self._projection_error(
                code=code, title=title, detail="An attempt contains an unsupported state.",
                resource_type="run", resource_id=attempt_id,
            )
        worker_id = self._identifier(
            row["worker_execution_id"], WORKER_EXECUTION_ID_PATTERN,
            code=code, title=title, resource_type="run", resource_id=attempt_id,
            nullable=True,
        )
        review_execution_id = self._identifier(
            row["review_execution_id"], REVIEW_EXECUTION_ID_PATTERN,
            code=code, title=title, resource_type="run", resource_id=attempt_id,
            nullable=True,
        )
        integration_id = self._identifier(
            row["integration_id"], INTEGRATION_ID_PATTERN,
            code=code, title=title, resource_type="run", resource_id=attempt_id,
            nullable=True,
        )
        started_at = self._timestamp(
            row["started_at"], required=True, code=code, title=title,
            resource_type="run", resource_id=attempt_id,
        )
        heartbeat_at = self._timestamp(
            row["heartbeat_at"], required=True, code=code, title=title,
            resource_type="run", resource_id=attempt_id,
        )
        finished_at = self._timestamp(
            row["finished_at"], required=False, code=code, title=title,
            resource_type="run", resource_id=attempt_id,
        )
        payload: dict[str, Any] = {
            "id": attempt_id,
            "run_id": attempt_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "attempt_number": self._integer(
                row["attempt_number"], minimum=1, maximum=10,
                code=code, title=title, resource_type="run", resource_id=attempt_id,
            ),
            "state": ATTEMPT_STATES[raw_status],
            "raw_state": raw_status,
            "transaction_reference": self._transaction_reference(row["internal_run_id"]),
            "worker_execution_id": worker_id,
            "review_execution_id": review_execution_id,
            "integration_id": integration_id,
            "reviewer_assignment_count": self._integer(
                row["assignment_count"], minimum=0, maximum=None,
                code=code, title=title, resource_type="run", resource_id=attempt_id,
            ),
            "created_at": started_at,
            "updated_at": finished_at or heartbeat_at,
            "started_at": started_at,
            "finished_at": finished_at,
            "result": (
                {"available": True, "legacy_payload_redacted": True}
                if str(row["result_json"] or "") not in {"", "{}", "null"} else None
            ),
            "error": (
                {"available": True, "legacy_payload_redacted": True}
                if row["failure_reason"] is not None else None
            ),
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def list_plan_attempts(
        self,
        plan_id: str,
        *,
        limit: int,
        cursor: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        self._require_plan(plan_id)
        decoded: AttemptCursor | None = None
        if cursor is not None:
            payload = self._decode_cursor(
                cursor, secret=cursor_secret,
                namespace=b"orchestra-plan-attempt-cursor-v1",
            )
            if (
                payload.get("v") != 1
                or payload.get("p") != plan_id
                or not isinstance(payload.get("n"), int)
                or not isinstance(payload.get("i"), str)
                or not RUN_ID_PATTERN.fullmatch(payload["i"])
            ):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            decoded = AttemptCursor(payload["n"], payload["i"])
        sql = self._attempt_projection_sql() + " WHERE t.plan_id=?"
        parameters: list[Any] = [plan_id]
        if decoded is not None:
            sql += """
                AND (
                    a.attempt_number < ?
                    OR (a.attempt_number=? AND a.attempt_id<?)
                )
            """
            parameters.extend([
                decoded.attempt_number, decoded.attempt_number, decoded.attempt_id
            ])
        sql += " ORDER BY a.attempt_number DESC,a.attempt_id DESC LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        selected = rows[:limit]
        items = [self._attempt(row) for row in selected]
        next_cursor = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                {
                    "v": 1, "p": plan_id,
                    "n": int(last["attempt_number"]),
                    "i": str(last["attempt_id"]),
                },
                secret=cursor_secret,
                namespace=b"orchestra-plan-attempt-cursor-v1",
            )
        return items, next_cursor

    @staticmethod
    def _assignment_projection_sql() -> str:
        return """
            SELECT
                ra.assignment_id,
                ra.orchestration_attempt_id,
                ra.assignment_number,
                ra.role_id,
                ra.source_profile,
                ra.status AS raw_status,
                ra.review_execution_id,
                ra.review_id,
                ra.failure_code,
                ra.assigned_at,
                ra.claimed_at,
                ra.heartbeat_at,
                ra.finished_at,
                a.orchestration_task_id,
                t.plan_id,
                t.project_id
            FROM reviewer_assignments AS ra
            JOIN orchestration_attempts AS a
              ON a.attempt_id=ra.orchestration_attempt_id
            JOIN orchestration_tasks AS t
              ON t.orchestration_task_id=a.orchestration_task_id
        """

    def _assignment(self, row: sqlite3.Row) -> dict[str, Any]:
        code = "reviewer_assignment_projection_invalid"
        title = "Reviewer assignment projection unavailable"
        assignment_id = self._identifier(
            row["assignment_id"], ASSIGNMENT_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id="unavailable",
        )
        assert assignment_id is not None
        run_id = self._identifier(
            row["orchestration_attempt_id"], RUN_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id,
        )
        task_id = self._identifier(
            row["orchestration_task_id"], TASK_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id,
        )
        plan_id = self._identifier(
            row["plan_id"], PLAN_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id,
        )
        project_id = self._identifier(
            row["project_id"], PROJECT_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id, nullable=True,
        )
        role_id = self._identifier(
            row["role_id"], ROLE_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id,
        )
        source_profile = self._identifier(
            row["source_profile"], PROFILE_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id,
        )
        assert all(value is not None for value in (run_id, task_id, plan_id, role_id, source_profile))
        raw_status = str(row["raw_status"])
        inverse = {value: key for key, value in ASSIGNMENT_STATES.items()}
        if raw_status not in inverse:
            raise self._projection_error(
                code=code, title=title,
                detail="A reviewer assignment contains an unsupported state.",
                resource_type="reviewer_assignment", resource_id=assignment_id,
            )
        review_execution_id = self._identifier(
            row["review_execution_id"], REVIEW_EXECUTION_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id, nullable=True,
        )
        review_id = self._identifier(
            row["review_id"], REVIEW_ID_PATTERN,
            code=code, title=title, resource_type="reviewer_assignment",
            resource_id=assignment_id, nullable=True,
        )
        failure_code = None
        if row["failure_code"] is not None:
            failure_code = str(row["failure_code"])
            if not FAILURE_CODE_PATTERN.fullmatch(failure_code):
                raise self._projection_error(
                    code=code, title=title,
                    detail="A reviewer assignment contains an invalid failure code.",
                    resource_type="reviewer_assignment", resource_id=assignment_id,
                )
        assigned_at = self._timestamp(
            row["assigned_at"], required=True, code=code, title=title,
            resource_type="reviewer_assignment", resource_id=assignment_id,
        )
        claimed_at = self._timestamp(
            row["claimed_at"], required=False, code=code, title=title,
            resource_type="reviewer_assignment", resource_id=assignment_id,
        )
        heartbeat_at = self._timestamp(
            row["heartbeat_at"], required=False, code=code, title=title,
            resource_type="reviewer_assignment", resource_id=assignment_id,
        )
        finished_at = self._timestamp(
            row["finished_at"], required=False, code=code, title=title,
            resource_type="reviewer_assignment", resource_id=assignment_id,
        )
        payload: dict[str, Any] = {
            "id": assignment_id,
            "plan_id": plan_id,
            "task_id": task_id,
            "run_id": run_id,
            "project_id": project_id,
            "assignment_number": self._integer(
                row["assignment_number"], minimum=1, maximum=1000,
                code=code, title=title, resource_type="reviewer_assignment",
                resource_id=assignment_id,
            ),
            "role_id": role_id,
            "source_profile": source_profile,
            "state": inverse[raw_status],
            "raw_state": raw_status,
            "review_execution_id": review_execution_id,
            "review_id": review_id,
            "failure_code": failure_code,
            "created_at": assigned_at,
            "updated_at": finished_at or heartbeat_at or claimed_at or assigned_at,
            "claimed_at": claimed_at,
            "finished_at": finished_at,
            "claim_owner_redacted": True,
            "assigned_by_redacted": True,
            "internal_transaction_redacted": True,
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def list_assignments(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: str | None,
        state: str | None,
        run_id: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        project_id = self._validate_project_filter(project_id)
        if state is not None and state not in ASSIGNMENT_STATES:
            raise ControllerError(400, "invalid_state", "Invalid reviewer assignment state")
        if run_id is not None and not RUN_ID_PATTERN.fullmatch(run_id):
            raise ControllerError(400, "invalid_run_id", "Invalid run identifier")
        decoded: ListCursor | None = None
        if cursor is not None:
            payload = self._decode_cursor(
                cursor, secret=cursor_secret,
                namespace=b"orchestra-reviewer-assignment-cursor-v1",
            )
            if (
                payload.get("v") != 1
                or payload.get("p") != project_id
                or payload.get("s") != state
                or payload.get("r") != run_id
                or not isinstance(payload.get("c"), str)
                or not isinstance(payload.get("i"), str)
                or not ASSIGNMENT_ID_PATTERN.fullmatch(payload["i"])
            ):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            decoded = ListCursor(payload["c"], payload["i"])
        sql = self._assignment_projection_sql() + " WHERE 1=1"
        parameters: list[Any] = []
        if project_id is not None:
            sql += " AND t.project_id=?"
            parameters.append(project_id)
        if state is not None:
            sql += " AND ra.status=?"
            parameters.append(ASSIGNMENT_STATES[state])
        if run_id is not None:
            sql += " AND ra.orchestration_attempt_id=?"
            parameters.append(run_id)
        if decoded is not None:
            sql += """
                AND (
                    ra.assigned_at < ?
                    OR (ra.assigned_at=? AND ra.assignment_id<?)
                )
            """
            parameters.extend([decoded.created_at, decoded.created_at, decoded.item_id])
        sql += " ORDER BY ra.assigned_at DESC,ra.assignment_id DESC LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        selected = rows[:limit]
        items = [self._assignment(row) for row in selected]
        next_cursor = None
        if len(rows) > limit and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                {
                    "v": 1, "p": project_id, "s": state, "r": run_id,
                    "c": str(last["assigned_at"]),
                    "i": str(last["assignment_id"]),
                },
                secret=cursor_secret,
                namespace=b"orchestra-reviewer-assignment-cursor-v1",
            )
        return items, next_cursor

    def get_assignment(self, assignment_id: str) -> dict[str, Any]:
        if not ASSIGNMENT_ID_PATTERN.fullmatch(assignment_id):
            raise ControllerError(
                400,
                "invalid_reviewer_assignment_id",
                "Invalid reviewer assignment identifier",
            )
        sql = self._assignment_projection_sql() + " WHERE ra.assignment_id=?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (assignment_id,)).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "reviewer_assignment_not_found",
                "Reviewer assignment not found",
                resource={"type": "reviewer_assignment", "id": assignment_id},
            )
        return self._assignment(row)
