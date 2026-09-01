from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from typing import Any

from .core import (
    ControllerError,
    PROJECT_ID_PATTERN,
    ReadOnlyDatabase,
    Settings,
)

OBJECTIVE_ID_PATTERN = re.compile(r"^objective-[a-f0-9]{32}$")
OPERATION_ID_PATTERN = re.compile(r"^objective-attempt-[a-f0-9]{32}$")
OBJECTIVE_STATES = {
    "draft",
    "planning",
    "planned",
    "running",
    "paused",
    "blocked",
    "succeeded",
    "failed",
    "cancelled",
    "archived",
}
MAX_CURSOR_BYTES = 1024


@dataclass(frozen=True)
class ObjectiveCursor:
    created_at: str
    objective_id: str
    project_id: str | None
    state: str | None


class ObjectiveReadStore:
    """Read-only projection over the existing objective queue tables."""

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
    def _title(description: str) -> str:
        for raw_line in description.splitlines():
            line = " ".join(raw_line.split())
            if line:
                return line[:200]
        return "Untitled objective"

    @staticmethod
    def _projects(raw: str, objective_id: str) -> list[str]:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ControllerError(
                503,
                "objective_projection_invalid",
                "Objective projection unavailable",
                "An objective contains invalid project scope data.",
                resource={"type": "objective", "id": objective_id},
            ) from error
        if (
            not isinstance(value, list)
            or not value
            or any(
                not isinstance(item, str)
                or not PROJECT_ID_PATTERN.fullmatch(item)
                for item in value
            )
            or len(set(value)) != len(value)
        ):
            raise ControllerError(
                503,
                "objective_projection_invalid",
                "Objective projection unavailable",
                "An objective contains an invalid project scope.",
                resource={"type": "objective", "id": objective_id},
            )
        return value

    @staticmethod
    def _state(
        raw: str,
        joined_plan_id: str | None,
        plan_status: str | None,
    ) -> tuple[str, str | None]:
        if raw == "QUEUED":
            return ("planned" if joined_plan_id is not None else "draft"), None
        if raw == "PLANNING":
            return "planning", None
        if raw == "RUNNING":
            return ("blocked" if plan_status == "BLOCKED" else "running"), None
        if raw == "PAUSE_REQUESTED":
            return "running", "pause"
        if raw == "PAUSED":
            return "paused", None
        if raw == "CANCEL_REQUESTED":
            return "running", "cancel"
        if raw == "COMPLETED":
            return "succeeded", None
        if raw == "FAILED":
            return "failed", None
        if raw == "CANCELLED":
            return "cancelled", None
        raise ControllerError(
            503,
            "objective_projection_invalid",
            "Objective projection unavailable",
            "An objective contains an unsupported runtime state.",
        )

    @staticmethod
    def _operation_state(raw: str) -> str:
        mapping = {
            "RUNNING": "running",
            "COMPLETED": "succeeded",
            "FAILED": "failed",
            "ABANDONED": "failed",
            "CANCELLED": "cancelled",
        }
        try:
            return mapping[raw]
        except KeyError as error:
            raise ControllerError(
                503,
                "operation_projection_invalid",
                "Operation projection unavailable",
                "An objective attempt contains an unsupported runtime state.",
            ) from error

    @staticmethod
    def _cursor_signature(raw: bytes, secret: str) -> bytes:
        return hmac.new(
            secret.encode("ascii"),
            b"orchestra-objective-cursor-v1\0" + raw,
            hashlib.sha256,
        ).digest()

    @classmethod
    def _encode_cursor(
        cls,
        cursor: ObjectiveCursor,
        *,
        secret: str,
    ) -> str:
        raw = json.dumps(
            {
                "v": 1,
                "c": cursor.created_at,
                "i": cursor.objective_id,
                "p": cursor.project_id,
                "s": cursor.state,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signed = raw + cls._cursor_signature(raw, secret)
        return base64.urlsafe_b64encode(signed).rstrip(b"=").decode("ascii")

    @classmethod
    def _decode_cursor(
        cls,
        value: str,
        *,
        project_id: str | None,
        state: str | None,
        secret: str,
    ) -> ObjectiveCursor:
        if not value or len(value) > MAX_CURSOR_BYTES:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        try:
            padding = "=" * (-len(value) % 4)
            signed = base64.b64decode(
                value + padding,
                altchars=b"-_",
                validate=True,
            )
            if len(signed) <= hashlib.sha256().digest_size:
                raise ValueError("cursor payload is too short")
            raw = signed[:-hashlib.sha256().digest_size]
            signature = signed[-hashlib.sha256().digest_size:]
            expected = cls._cursor_signature(raw, secret)
            if not hmac.compare_digest(signature, expected):
                raise ValueError("cursor signature mismatch")
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, json.JSONDecodeError) as error:
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
            ) from error
        if (
            not isinstance(payload, dict)
            or payload.get("v") != 1
            or not isinstance(payload.get("c"), str)
            or not isinstance(payload.get("i"), str)
            or not OBJECTIVE_ID_PATTERN.fullmatch(payload["i"])
            or payload.get("p") != project_id
            or payload.get("s") != state
        ):
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
                "The cursor is malformed or belongs to different filters.",
            )
        return ObjectiveCursor(
            created_at=payload["c"],
            objective_id=payload["i"],
            project_id=project_id,
            state=state,
        )

    @staticmethod
    def _projection_sql() -> str:
        return """
            SELECT
                q.objective_id,
                q.objective,
                q.source,
                q.status AS raw_status,
                q.priority,
                q.not_before,
                q.project_scope_json,
                q.max_parallel_tasks,
                q.planning_max_attempts,
                q.planning_attempt_count,
                q.plan_id,
                q.planner_execution_id,
                q.created_at,
                q.started_at,
                q.heartbeat_at,
                q.finished_at,
                q.paused_at,
                q.last_error,
                p.plan_id AS joined_plan_id,
                p.status AS plan_status,
                COALESCE(a.attempt_count, 0) AS attempt_count,
                a.latest_operation_id,
                COALESCE(e.event_count, 0) AS event_count
            FROM objective_queue AS q
            LEFT JOIN orchestration_plans AS p
                ON p.plan_id = q.plan_id
            LEFT JOIN (
                SELECT
                    attempts.objective_id,
                    COUNT(*) AS attempt_count,
                    (
                        SELECT latest.objective_attempt_id
                        FROM objective_attempts AS latest
                        WHERE latest.objective_id = attempts.objective_id
                        ORDER BY
                            latest.attempt_number DESC,
                            latest.objective_attempt_id DESC
                        LIMIT 1
                    ) AS latest_operation_id
                FROM objective_attempts AS attempts
                GROUP BY attempts.objective_id
            ) AS a ON a.objective_id = q.objective_id
            LEFT JOIN (
                SELECT objective_id, COUNT(*) AS event_count
                FROM objective_events
                GROUP BY objective_id
            ) AS e ON e.objective_id = q.objective_id
        """

    def _objective(self, row: sqlite3.Row) -> dict[str, Any]:
        identifier = str(row["objective_id"])
        if not OBJECTIVE_ID_PATTERN.fullmatch(identifier):
            raise ControllerError(
                503,
                "objective_projection_invalid",
                "Objective projection unavailable",
                "An objective contains an invalid identifier.",
            )

        description = str(row["objective"])
        projects = self._projects(str(row["project_scope_json"]), identifier)
        joined_plan_id = (
            str(row["joined_plan_id"])
            if row["joined_plan_id"] is not None
            else None
        )
        plan_status = (
            str(row["plan_status"])
            if row["plan_status"] is not None
            else None
        )
        state, requested_transition = self._state(
            str(row["raw_status"]),
            joined_plan_id,
            plan_status,
        )

        latest = row["latest_operation_id"]
        latest_operation_id = str(latest) if latest is not None else None
        if (
            latest_operation_id is not None
            and not OPERATION_ID_PATTERN.fullmatch(latest_operation_id)
        ):
            raise ControllerError(
                503,
                "objective_projection_invalid",
                "Objective projection unavailable",
                "An objective references an invalid operation identifier.",
                resource={"type": "objective", "id": identifier},
            )

        operation_ids = (
            [latest_operation_id]
            if latest_operation_id is not None
            else []
        )
        payload: dict[str, Any] = {
            "id": identifier,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["heartbeat_at"]),
            "state": state,
            "title": self._title(description),
            "description": description,
            "priority": int(row["priority"]),
            "project_ids": projects,
            "not_before": str(row["not_before"]),
            "max_parallel_tasks": int(row["max_parallel_tasks"]),
            "planning_max_attempts": int(row["planning_max_attempts"]),
            "source": str(row["source"]).lower(),
            "raw_state": str(row["raw_status"]),
            "requested_transition": requested_transition,
            "planning_attempt_count": int(row["planning_attempt_count"]),
            "attempt_count": int(row["attempt_count"]),
            "event_count": int(row["event_count"]),
            "plan_id": str(row["plan_id"]) if row["plan_id"] is not None else None,
            "planner_execution_id": (
                str(row["planner_execution_id"])
                if row["planner_execution_id"] is not None
                else None
            ),
            "started_at": str(row["started_at"]) if row["started_at"] else None,
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
            "paused_at": str(row["paused_at"]) if row["paused_at"] else None,
            "has_error": row["last_error"] is not None,
            "latest_operation_id": latest_operation_id,
            "operation_ids": operation_ids,
        }
        payload["resource_revision"] = self._revision(payload)
        return payload

    def get_objective(self, objective_id: str) -> dict[str, Any]:
        if not OBJECTIVE_ID_PATTERN.fullmatch(objective_id):
            raise ControllerError(
                400,
                "invalid_objective_id",
                "Invalid objective identifier",
            )
        sql = self._projection_sql() + " WHERE q.objective_id = ?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (objective_id,)).fetchone()
                if row is None:
                    raise ControllerError(
                        404,
                        "objective_not_found",
                        "Objective not found",
                        resource={"type": "objective", "id": objective_id},
                    )
                objective = self._objective(row)
                operations = [
                    str(item[0])
                    for item in connection.execute(
                        """
                        SELECT objective_attempt_id
                        FROM objective_attempts
                        WHERE objective_id = ?
                        ORDER BY attempt_number, objective_attempt_id
                        """,
                        (objective_id,),
                    )
                ]
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if any(not OPERATION_ID_PATTERN.fullmatch(item) for item in operations):
            raise ControllerError(
                503,
                "objective_projection_invalid",
                "Objective projection unavailable",
                "An objective references an invalid operation identifier.",
                resource={"type": "objective", "id": objective_id},
            )
        objective["operation_ids"] = operations
        objective["latest_operation_id"] = operations[-1] if operations else None
        return objective

    def list_objectives(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: str | None,
        state: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not 1 <= limit <= 200:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )
        if project_id is not None and not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ControllerError(400, "invalid_project_id", "Invalid project identifier")
        if state is not None and state not in OBJECTIVE_STATES:
            raise ControllerError(
                400,
                "invalid_objective_state",
                "Invalid objective state",
            )
        decoded = (
            self._decode_cursor(
                cursor,
                project_id=project_id,
                state=state,
                secret=cursor_secret,
            )
            if cursor is not None
            else None
        )

        conditions: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            conditions.append(
                "EXISTS (SELECT 1 FROM json_each("
                "CASE WHEN json_valid(q.project_scope_json) "
                "THEN q.project_scope_json ELSE '[]' END"
                ") AS scope WHERE scope.type = 'text' AND scope.value = ?)"
            )
            parameters.append(project_id)
        if state is not None:
            state_sql = {
                "draft": "q.status = 'QUEUED' AND p.plan_id IS NULL",
                "planned": "q.status = 'QUEUED' AND p.plan_id IS NOT NULL",
                "planning": "q.status = 'PLANNING'",
                "running": "(q.status IN ('PAUSE_REQUESTED','CANCEL_REQUESTED') "
                "OR (q.status = 'RUNNING' AND COALESCE(p.status, '') <> 'BLOCKED'))",
                "paused": "q.status = 'PAUSED'",
                "blocked": "q.status = 'RUNNING' AND p.status = 'BLOCKED'",
                "succeeded": "q.status = 'COMPLETED'",
                "failed": "q.status = 'FAILED'",
                "cancelled": "q.status = 'CANCELLED'",
                "archived": "0",
            }[state]
            conditions.append(f"({state_sql})")
        if decoded is not None:
            conditions.append(
                "(q.created_at < ? OR (q.created_at = ? AND q.objective_id < ?))"
            )
            parameters.extend(
                [decoded.created_at, decoded.created_at, decoded.objective_id]
            )

        sql = self._projection_sql()
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY q.created_at DESC, q.objective_id DESC LIMIT ?"
        parameters.append(limit + 1)

        try:
            with closing(self.database.connect()) as connection:
                if project_id is not None:
                    project = connection.execute(
                        "SELECT 1 FROM projects WHERE project_id = ?",
                        (project_id,),
                    ).fetchone()
                    if project is None:
                        raise ControllerError(
                            404,
                            "project_not_found",
                            "Project not found",
                            resource={"type": "project", "id": project_id},
                        )
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error

        has_more = len(rows) > limit
        selected = rows[:limit]
        objectives = [self._objective(row) for row in selected]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                ObjectiveCursor(
                    created_at=str(last["created_at"]),
                    objective_id=str(last["objective_id"]),
                    project_id=project_id,
                    state=state,
                ),
                secret=cursor_secret,
            )
        return objectives, next_cursor

    def get_operation(self, operation_id: str) -> dict[str, Any]:
        if not OPERATION_ID_PATTERN.fullmatch(operation_id):
            raise ControllerError(
                400,
                "invalid_operation_id",
                "Invalid operation identifier",
            )
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(
                    """
                    SELECT
                        a.objective_attempt_id,
                        a.objective_id,
                        a.attempt_number,
                        a.status,
                        a.executor_instance_id,
                        a.planner_execution_id,
                        a.plan_id,
                        a.result_json,
                        a.failure_reason,
                        a.started_at,
                        a.heartbeat_at,
                        a.finished_at,
                        a.next_attempt_at
                    FROM objective_attempts AS a
                    WHERE a.objective_attempt_id = ?
                    """,
                    (operation_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self.database._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "operation_not_found",
                "Operation not found",
                resource={"type": "operation", "id": operation_id},
            )
        state = self._operation_state(str(row["status"]))
        has_result = str(row["result_json"]) not in {"", "{}", "null"}
        has_error = row["failure_reason"] is not None
        objective_id = str(row["objective_id"])
        if not OBJECTIVE_ID_PATTERN.fullmatch(objective_id):
            raise ControllerError(
                503,
                "operation_projection_invalid",
                "Operation projection unavailable",
                "An objective attempt references an invalid objective identifier.",
                resource={"type": "operation", "id": operation_id},
            )
        payload: dict[str, Any] = {
            "id": operation_id,
            "kind": "objective.planning_attempt",
            "state": state,
            "created_at": str(row["started_at"]),
            "updated_at": str(row["heartbeat_at"]),
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
            "target": {
                "type": "objective",
                "id": objective_id,
                "plan_id": str(row["plan_id"]) if row["plan_id"] else None,
            },
            "result": (
                {"available": True, "legacy_payload_redacted": True}
                if has_result else None
            ),
            "error": (
                {"available": True, "legacy_payload_redacted": True}
                if has_error else None
            ),
            "attempt_number": int(row["attempt_number"]),
            "raw_state": str(row["status"]),
            "executor_instance_id": (
                str(row["executor_instance_id"])
                if row["executor_instance_id"] else None
            ),
            "planner_execution_id": (
                str(row["planner_execution_id"])
                if row["planner_execution_id"] else None
            ),
            "next_attempt_at": (
                str(row["next_attempt_at"]) if row["next_attempt_at"] else None
            ),
            "legacy_projection": True,
        }
        payload["resource_revision"] = self._revision(payload)
        return payload
