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

from .core import (
    ControllerError,
    PROJECT_ID_PATTERN,
    ReadOnlyDatabase,
    Settings,
)

REVIEW_ID_PATTERN = re.compile(r"^review-[a-f0-9]{32}$")
RECOVERY_ID_PATTERN = re.compile(r"^recovery-[a-f0-9]{32}$")
REVIEW_EXECUTION_ID_PATTERN = re.compile(r"^review-execution-[a-f0-9]{32}$")
INTEGRATION_ID_PATTERN = re.compile(r"^integration-[a-f0-9]{32}$")
ROLE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
PROFILE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,127}$")
POLICY_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
COMMIT_PATTERN = re.compile(r"^[a-f0-9]{40}(?:[a-f0-9]{24})?$")
SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
PUBLIC_TRANSACTION_PATTERN = re.compile(r"^transaction-[a-f0-9]{32}$")
PUBLIC_EVIDENCE_PATTERN = re.compile(r"^review-evidence-[a-f0-9]{32}$")
MAX_INTERNAL_KEY_BYTES = 512
MAX_CURSOR_BYTES = 1024
MAX_JSON_BYTES = 64 * 1024
MAX_JSON_ITEMS = 256
MAX_SUMMARY_BYTES = 4096

REVIEW_VERDICTS = {
    "PASS",
    "PASS_WITH_DEBT",
    "FIX",
    "SECURITY",
    "PERFORMANCE",
    "ARCHITECTURE",
    "HUMAN",
}
REVIEW_DECISIONS = {"APPROVE", "REJECT", "BLOCK_HUMAN"}
INTEGRATION_STATUSES = {
    "PREPARED",
    "COMPLETED",
    "REJECTED",
    "BLOCKED",
    "FAILED",
}
REVIEW_STATES = {"completed", "approved", "rejected", "blocked", "failed"}
RECOVERY_OBSERVED_STATUSES = {
    "SNAPSHOTTING",
    "RUNNING",
    "REVIEWING",
    "WAITING_HUMAN",
    "COMMITTING",
    "RECOVERING",
    "FAILED",
}
RECOVERY_DECISIONS = {"RESUME_SAFE", "ROLLBACK_SAFE", "BLOCK_HUMAN"}
RECOVERY_OUTCOMES = {"ASSESSED", "RESUMED", "ROLLED_BACK", "BLOCKED", "FAILED"}
RECOVERY_STATES = {"assessed", "resumed", "rolled_back", "blocked", "failed"}
RUN_STATUSES = {
    "QUEUED",
    "SNAPSHOTTING",
    "RUNNING",
    "REVIEWING",
    "WAITING_HUMAN",
    "COMMITTING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "RECOVERING",
}

_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization:",
    "bearer ",
    "password",
    "private_key",
    "secret",
    "token=",
)
_UNSAFE_FIELD_KEY_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "password",
    "private_key",
    "secret",
    "token",
    "credential",
    "path",
    "worktree",
    "container",
    "prompt",
    "output",
    "owner",
)
_PATH_MARKERS = (
    "/home/",
    "/opt/",
    "/root/",
    "/tmp/",
    "file://",
    "\\\\",
)


@dataclass(frozen=True)
class ReviewCursor:
    project_id: str | None
    state: str | None
    created_at: str
    review_id: str


@dataclass(frozen=True)
class RecoveryCursor:
    project_id: str | None
    state: str | None
    created_at: str
    recovery_id: str


class ReviewRecoveryReadStore:
    """Read-only, redacted review/integration/recovery projections."""

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
    def _transaction_reference(internal_key: str) -> str:
        digest = hashlib.sha256(
            b"hermesops-transaction-reference-v1\0"
            + internal_key.encode("utf-8")
        ).hexdigest()
        return "transaction-" + digest[:32]

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
    ) -> str:
        candidate = value if isinstance(value, str) else ""
        if not pattern.fullmatch(candidate):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid public identifier.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return candidate

    @classmethod
    def _internal_key(
        cls,
        value: Any,
        *,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> str:
        if not isinstance(value, str):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid internal reference.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        encoded = value.encode("utf-8")
        if (
            not value
            or len(encoded) > MAX_INTERNAL_KEY_BYTES
            or "/" in value
            or "\\" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid internal reference.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return value

    @classmethod
    def _integer(
        cls,
        value: Any,
        *,
        minimum: int,
        maximum: int,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> int:
        if type(value) is not int or not minimum <= value <= maximum:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid numeric value.",
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
        return bool(
            cls._integer(
                value,
                minimum=0,
                maximum=1,
                code=code,
                title=title,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        )

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
            if required:
                raise cls._projection_error(
                    code=code,
                    title=title,
                    detail="Stored controller data contains a missing timestamp.",
                    resource_type=resource_type,
                    resource_id=resource_id,
                )
            return None
        if not isinstance(value, str):
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid timestamp.",
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
                detail="Stored controller data contains an invalid timestamp.",
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
                detail="Stored controller data contains an invalid timestamp.",
                resource_type=resource_type,
                resource_id=resource_id,
            ) from error
        if parsed.tzinfo is None:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller timestamps must include a timezone.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return value

    @classmethod
    def _json(
        cls,
        value: Any,
        *,
        expected: type,
        code: str,
        title: str,
        resource_type: str,
        resource_id: str,
    ) -> dict[str, Any] | list[Any]:
        if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_JSON_BYTES:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid structured payload.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        try:
            payload = json.loads(value)
        except (json.JSONDecodeError, UnicodeError, RecursionError) as error:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid structured payload.",
                resource_type=resource_type,
                resource_id=resource_id,
            ) from error
        if type(payload) is not expected or len(payload) > MAX_JSON_ITEMS:
            raise cls._projection_error(
                code=code,
                title=title,
                detail="Stored controller data contains an invalid structured payload.",
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return payload

    @staticmethod
    def _safe_keys(payload: dict[str, Any]) -> list[str]:
        return sorted(
            key
            for key in payload
            if isinstance(key, str)
            and re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,63}", key)
            and not any(marker in key.lower() for marker in _UNSAFE_FIELD_KEY_MARKERS)
        )[:32]

    @staticmethod
    def _safe_summary(
        value: Any,
        *,
        forbidden_values: tuple[str, ...] = (),
    ) -> str:
        if not isinstance(value, str):
            return "Review summary unavailable."
        encoded = value.encode("utf-8")
        if (
            not value.strip()
            or len(encoded) > MAX_SUMMARY_BYTES
            or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
        ):
            return "Review summary unavailable."
        normalized = " ".join(value.split())
        lowered = normalized.lower()
        if (
            any(marker in lowered for marker in _SECRET_MARKERS + _PATH_MARKERS)
            or any(forbidden and forbidden in normalized for forbidden in forbidden_values)
            or re.search(r"(?:^|\s)/(?:[^/\s]+/)+[^/\s]*", normalized)
            or re.search(r"\b[A-Za-z]:[\\/]", normalized)
            or re.search(r"\b(?:file|https?|ssh)://", lowered)
        ):
            return "Review summary redacted."
        return normalized[:2000]

    @staticmethod
    def _encode_cursor(payload: dict[str, Any], secret: str) -> str:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
        return base64.urlsafe_b64encode(body + signature).decode("ascii").rstrip("=")

    @classmethod
    def _decode_cursor(
        cls,
        cursor: str | None,
        secret: str,
        *,
        kind: str,
    ) -> dict[str, Any] | None:
        if cursor is None:
            return None
        if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_BYTES:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        try:
            padding = "=" * (-len(cursor) % 4)
            combined = base64.urlsafe_b64decode(cursor + padding)
        except Exception as error:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor") from error
        if len(combined) <= hashlib.sha256().digest_size:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        body = combined[:-hashlib.sha256().digest_size]
        signature = combined[-hashlib.sha256().digest_size:]
        expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as error:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor") from error
        if type(payload) is not dict or payload.get("kind") != kind:
            raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        return payload

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )

    @staticmethod
    def _validate_project_filter(project_id: str | None) -> None:
        if project_id is not None and not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ControllerError(400, "invalid_project_id", "Invalid project identifier")

    @staticmethod
    def _database_failure(error: sqlite3.Error) -> ControllerError:
        return ControllerError(
            503,
            "database_unavailable",
            "Controller database unavailable",
            "The Orchestra control database cannot serve this request.",
        )

    @staticmethod
    def _review_state(
        *,
        verdict: str,
        decision: str | None,
        integration_status: str | None,
        failure: bool,
    ) -> str:
        if failure or integration_status == "FAILED":
            return "failed"
        if decision == "BLOCK_HUMAN" or verdict == "HUMAN" or integration_status == "BLOCKED":
            return "blocked"
        if (
            decision == "REJECT"
            or verdict in {"FIX", "SECURITY", "PERFORMANCE", "ARCHITECTURE"}
            or integration_status == "REJECTED"
        ):
            return "rejected"
        if decision == "APPROVE" and verdict in {"PASS", "PASS_WITH_DEBT"}:
            return "approved"
        return "completed"

    @classmethod
    def _review(cls, row: sqlite3.Row) -> dict[str, Any]:
        review_id = cls._identifier(
            row["review_id"],
            REVIEW_ID_PATTERN,
            code="review_projection_invalid",
            title="Review projection unavailable",
            resource_type="review",
            resource_id=str(row["review_id"] or "unknown"),
        )
        project_id = cls._identifier(
            row["project_id"],
            PROJECT_ID_PATTERN,
            code="review_projection_invalid",
            title="Review projection unavailable",
            resource_type="review",
            resource_id=review_id,
        )
        internal_run = cls._internal_key(
            row["run_id"],
            code="review_projection_invalid",
            title="Review projection unavailable",
            resource_type="review",
            resource_id=review_id,
        )
        if row["run_status"] not in RUN_STATUSES:
            raise cls._projection_error(
                code="review_projection_invalid",
                title="Review projection unavailable",
                detail="Stored review data references an invalid run state.",
                resource_type="review",
                resource_id=review_id,
            )
        verdict = str(row["verdict"] or "")
        if verdict not in REVIEW_VERDICTS:
            raise cls._projection_error(
                code="review_projection_invalid",
                title="Review projection unavailable",
                detail="Stored review data contains an invalid verdict.",
                resource_type="review",
                resource_id=review_id,
            )
        created_at = cls._timestamp(
            row["review_created_at"],
            required=True,
            code="review_projection_invalid",
            title="Review projection unavailable",
            resource_type="review",
            resource_id=review_id,
        )
        details = cls._json(
            row["details_json"],
            expected=dict,
            code="review_projection_invalid",
            title="Review projection unavailable",
            resource_type="review",
            resource_id=review_id,
        )

        reviewer: dict[str, Any] | None = None
        decision: str | None = None
        reviewer_failure = False
        if row["review_execution_id"] is not None:
            execution_id = cls._identifier(
                row["review_execution_id"],
                REVIEW_EXECUTION_ID_PATTERN,
                code="review_projection_invalid",
                title="Review projection unavailable",
                resource_type="review",
                resource_id=review_id,
            )
            if row["execution_review_id"] != review_id or row["execution_run_id"] != internal_run:
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored review data contains an inconsistent execution link.",
                    resource_type="review",
                    resource_id=review_id,
                )
            role_id = cls._identifier(
                row["role_id"],
                ROLE_ID_PATTERN,
                code="review_projection_invalid",
                title="Review projection unavailable",
                resource_type="review",
                resource_id=review_id,
            )
            profile = cls._identifier(
                row["source_profile"],
                PROFILE_ID_PATTERN,
                code="review_projection_invalid",
                title="Review projection unavailable",
                resource_type="review",
                resource_id=review_id,
            )
            runtime_profile = cls._identifier(
                row["runtime_profile"],
                PROFILE_ID_PATTERN,
                code="review_projection_invalid",
                title="Review projection unavailable",
                resource_type="review",
                resource_id=review_id,
            )
            if (
                row["registered_role_id"] != role_id
                or row["registered_profile"] != profile
                or row["registered_role_kind"] != "reviewer"
                or row["registered_workspace_mode"] != "read_only"
                or row["workspace_mode"] != "read_only"
                or row["registered_network_enabled"] != 0
                or row["role_enabled"] != 1
            ):
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored reviewer execution does not match the registered reviewer policy.",
                    resource_type="review",
                    resource_id=review_id,
                )
            network_enabled = cls._flag(
                row["network_enabled"],
                code="review_projection_invalid",
                title="Review projection unavailable",
                resource_type="review",
                resource_id=review_id,
            )
            if network_enabled:
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored reviewer execution violates the no-network policy.",
                    resource_type="review",
                    resource_id=review_id,
                )
            cpu_limit = cls._integer(
                row["cpu_limit"], minimum=1, maximum=64,
                code="review_projection_invalid", title="Review projection unavailable",
                resource_type="review", resource_id=review_id,
            )
            memory_mb = cls._integer(
                row["memory_mb"], minimum=512, maximum=131072,
                code="review_projection_invalid", title="Review projection unavailable",
                resource_type="review", resource_id=review_id,
            )
            verification = {
                "mount_verified": cls._flag(
                    row["mount_verified"], code="review_projection_invalid",
                    title="Review projection unavailable", resource_type="review",
                    resource_id=review_id,
                ),
                "isolation_verified": cls._flag(
                    row["isolation_verified"], code="review_projection_invalid",
                    title="Review projection unavailable", resource_type="review",
                    resource_id=review_id,
                ),
                "repository_unchanged": cls._flag(
                    row["repository_unchanged"], code="review_projection_invalid",
                    title="Review projection unavailable", resource_type="review",
                    resource_id=review_id,
                ),
            }
            execution_verdict = row["execution_verdict"]
            if execution_verdict is not None and execution_verdict != verdict:
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored reviewer verdicts are inconsistent.",
                    resource_type="review",
                    resource_id=review_id,
                )
            decision = row["review_decision"]
            if decision is not None and decision not in REVIEW_DECISIONS:
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored review data contains an invalid decision.",
                    resource_type="review",
                    resource_id=review_id,
                )
            exit_code = row["review_exit_code"]
            if exit_code is not None:
                exit_code = cls._integer(
                    exit_code, minimum=-255, maximum=255,
                    code="review_projection_invalid", title="Review projection unavailable",
                    resource_type="review", resource_id=review_id,
                )
            cls._json(
                row["review_result_json"], expected=dict,
                code="review_projection_invalid", title="Review projection unavailable",
                resource_type="review", resource_id=review_id,
            )
            started_at = cls._timestamp(
                row["review_started_at"], required=False,
                code="review_projection_invalid", title="Review projection unavailable",
                resource_type="review", resource_id=review_id,
            )
            finished_at = cls._timestamp(
                row["review_finished_at"], required=False,
                code="review_projection_invalid", title="Review projection unavailable",
                resource_type="review", resource_id=review_id,
            )
            reviewer_failure = row["review_failure_reason"] is not None or (
                exit_code is not None and exit_code != 0
            )
            reviewer = {
                "id": execution_id,
                "role_id": role_id,
                "source_profile": profile,
                "runtime_profile": runtime_profile,
                "workspace_mode": "read_only",
                "network_enabled": False,
                "cpu_limit": cpu_limit,
                "memory_mb": memory_mb,
                "verification": verification,
                "decision": decision,
                "verdict": verdict,
                "exit_code": exit_code,
                "failure_present": row["review_failure_reason"] is not None,
                "started_at": started_at,
                "finished_at": finished_at,
            }

        integration: dict[str, Any] | None = None
        integration_status: str | None = None
        integration_failure = False
        if row["integration_id"] is not None:
            integration_id = cls._identifier(
                row["integration_id"],
                INTEGRATION_ID_PATTERN,
                code="review_projection_invalid",
                title="Review projection unavailable",
                resource_type="review",
                resource_id=review_id,
            )
            if (
                row["integration_run_id"] != internal_run
                or row["integration_review_id"] != review_id
                or row["integration_review_execution_id"] != row["review_execution_id"]
            ):
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored review data contains an inconsistent integration link.",
                    resource_type="review",
                    resource_id=review_id,
                )
            integration_decision = str(row["integration_decision"] or "")
            integration_verdict = str(row["integration_verdict"] or "")
            integration_status = str(row["integration_status"] or "")
            if (
                integration_decision not in REVIEW_DECISIONS
                or integration_verdict != verdict
                or integration_status not in INTEGRATION_STATUSES
            ):
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored integration data is inconsistent with the review.",
                    resource_type="review",
                    resource_id=review_id,
                )
            if decision is not None and decision != integration_decision:
                raise cls._projection_error(
                    code="review_projection_invalid",
                    title="Review projection unavailable",
                    detail="Stored reviewer and integration decisions are inconsistent.",
                    resource_type="review",
                    resource_id=review_id,
                )
            decision = integration_decision
            commits: dict[str, str] = {}
            for field in ("base_commit", "reviewed_commit", "main_before", "main_after"):
                commit = str(row[field] or "")
                if not COMMIT_PATTERN.fullmatch(commit):
                    raise cls._projection_error(
                        code="review_projection_invalid",
                        title="Review projection unavailable",
                        detail="Stored integration data contains an invalid commit identifier.",
                        resource_type="review",
                        resource_id=review_id,
                    )
                commits[field] = commit
            snapshot_verified = cls._flag(
                row["snapshot_verified"], code="review_projection_invalid",
                title="Review projection unavailable", resource_type="review",
                resource_id=review_id,
            )
            review_current = cls._flag(
                row["review_current"], code="review_projection_invalid",
                title="Review projection unavailable", resource_type="review",
                resource_id=review_id,
            )
            cls._json(
                row["integration_details_json"], expected=dict,
                code="review_projection_invalid", title="Review projection unavailable",
                resource_type="review", resource_id=review_id,
            )
            integration_failure = row["integration_failure_reason"] is not None
            integration = {
                "id": integration_id,
                "decision": integration_decision,
                "verdict": integration_verdict,
                "status": integration_status.lower(),
                "commits": commits,
                "snapshot_verified": snapshot_verified,
                "review_current": review_current,
                "approval_present": row["approval_id"] is not None,
                "failure_present": integration_failure,
                "created_at": cls._timestamp(
                    row["integration_created_at"], required=True,
                    code="review_projection_invalid", title="Review projection unavailable",
                    resource_type="review", resource_id=review_id,
                ),
                "started_at": cls._timestamp(
                    row["integration_started_at"], required=False,
                    code="review_projection_invalid", title="Review projection unavailable",
                    resource_type="review", resource_id=review_id,
                ),
                "finished_at": cls._timestamp(
                    row["integration_finished_at"], required=False,
                    code="review_projection_invalid", title="Review projection unavailable",
                    resource_type="review", resource_id=review_id,
                ),
            }

        state = cls._review_state(
            verdict=verdict,
            decision=decision,
            integration_status=integration_status,
            failure=reviewer_failure or integration_failure,
        )
        payload: dict[str, Any] = {
            "id": review_id,
            "project_id": project_id,
            "run_id": cls._transaction_reference(internal_run),
            "state": state,
            "verdict": verdict,
            "decision": decision,
            "summary": cls._safe_summary(
                row["summary"], forbidden_values=(internal_run,)
            ),
            "details": {
                "fields": cls._safe_keys(details),
                "redacted": True,
            },
            "reviewer": reviewer,
            "integration": integration,
            "created_at": created_at,
        }
        payload["resource_revision"] = cls._revision(payload)
        return payload

    @staticmethod
    def _review_sql() -> str:
        return """
            SELECT
                rr.review_id,
                rr.run_id,
                rr.verdict,
                rr.summary,
                rr.details_json,
                rr.created_at AS review_created_at,
                r.project_id,
                r.status AS run_status,
                re.execution_id AS review_execution_id,
                re.review_id AS execution_review_id,
                re.run_id AS execution_run_id,
                re.role_id,
                re.source_profile,
                re.runtime_profile,
                re.workspace_mode,
                re.network_enabled,
                re.cpu_limit,
                re.memory_mb,
                re.mount_verified,
                re.isolation_verified,
                re.repository_unchanged,
                re.decision AS review_decision,
                re.verdict AS execution_verdict,
                re.exit_code AS review_exit_code,
                re.result_json AS review_result_json,
                re.failure_reason AS review_failure_reason,
                re.started_at AS review_started_at,
                re.finished_at AS review_finished_at,
                role.role_id AS registered_role_id,
                role.profile_name AS registered_profile,
                role.role_kind AS registered_role_kind,
                role.workspace_mode AS registered_workspace_mode,
                role.network_enabled AS registered_network_enabled,
                role.enabled AS role_enabled,
                integ.integration_id,
                integ.run_id AS integration_run_id,
                integ.review_id AS integration_review_id,
                integ.review_execution_id AS integration_review_execution_id,
                integ.decision AS integration_decision,
                integ.verdict AS integration_verdict,
                integ.status AS integration_status,
                integ.base_commit,
                integ.reviewed_commit,
                integ.main_before,
                integ.main_after,
                integ.snapshot_verified,
                integ.review_current,
                integ.approval_id,
                integ.details_json AS integration_details_json,
                integ.failure_reason AS integration_failure_reason,
                integ.created_at AS integration_created_at,
                integ.started_at AS integration_started_at,
                integ.finished_at AS integration_finished_at
            FROM review_results AS rr
            JOIN runs AS r ON r.run_id = rr.run_id
            LEFT JOIN reviewer_executions AS re ON re.review_id = rr.review_id
            LEFT JOIN roles AS role ON role.role_id = re.role_id
            LEFT JOIN integration_executions AS integ
              ON integ.integration_id = (
                SELECT i2.integration_id
                FROM integration_executions AS i2
                WHERE i2.review_id = rr.review_id
                ORDER BY i2.created_at DESC, i2.integration_id DESC
                LIMIT 1
              )
        """

    def list_reviews(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: str | None,
        state: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        self._validate_project_filter(project_id)
        if state is not None and state not in REVIEW_STATES:
            raise ControllerError(400, "invalid_state", "Invalid review state")
        decoded = self._decode_cursor(cursor, cursor_secret, kind="review")
        if decoded is not None:
            if decoded.get("project_id") != project_id or decoded.get("state") != state:
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            cursor_created = decoded.get("created_at")
            cursor_id = decoded.get("id")
            if not isinstance(cursor_created, str) or not REVIEW_ID_PATTERN.fullmatch(str(cursor_id or "")):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        else:
            cursor_created = None
            cursor_id = None

        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("r.project_id = ?")
            parameters.append(project_id)
        if cursor_created is not None:
            clauses.append("(rr.created_at < ? OR (rr.created_at = ? AND rr.review_id < ?))")
            parameters.extend([cursor_created, cursor_created, cursor_id])
        sql = self._review_sql()
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY rr.created_at DESC, rr.review_id DESC LIMIT 1001"
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self._database_failure(error) from error

        projected: list[dict[str, Any]] = []
        last_scanned: dict[str, Any] | None = None
        for row in rows:
            item = self._review(row)
            last_scanned = item
            if state is not None and item["state"] != state:
                continue
            projected.append(item)
            if len(projected) > limit:
                break
        next_cursor: str | None = None
        cursor_item: dict[str, Any] | None = None
        if len(projected) > limit:
            projected = projected[:limit]
            cursor_item = projected[-1]
        elif state is not None and len(rows) == 1001 and last_scanned is not None:
            cursor_item = last_scanned
        if cursor_item is not None:
            next_cursor = self._encode_cursor(
                {
                    "kind": "review",
                    "project_id": project_id,
                    "state": state,
                    "created_at": cursor_item["created_at"],
                    "id": cursor_item["id"],
                },
                cursor_secret,
            )
        return projected, next_cursor

    def get_review(self, review_id: str) -> dict[str, Any]:
        if not REVIEW_ID_PATTERN.fullmatch(review_id):
            raise ControllerError(404, "review_not_found", "Review not found")
        sql = self._review_sql() + " WHERE rr.review_id = ?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (review_id,)).fetchone()
        except sqlite3.Error as error:
            raise self._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "review_not_found",
                "Review not found",
                resource={"type": "review", "id": review_id},
            )
        return self._review(row)

    def get_review_evidence(self, review_id: str) -> list[dict[str, Any]]:
        review = self.get_review(review_id)
        evidence: list[dict[str, Any]] = []

        def append(kind: str, name: str, created_at: str, body: dict[str, Any]) -> None:
            safe = {
                "review_id": review_id,
                "kind": kind,
                "name": name,
                "created_at": created_at,
                "body": body,
            }
            canonical = json.dumps(safe, sort_keys=True, separators=(",", ":")).encode("utf-8")
            digest = hashlib.sha256(canonical).hexdigest()
            evidence.append(
                {
                    "id": f"review-evidence-{digest[:32]}",
                    "review_id": review_id,
                    "kind": kind,
                    "name": name,
                    "media_type": "application/json",
                    "sha256": digest,
                    "created_at": created_at,
                    "available": False,
                    "raw_content_available": False,
                }
            )

        append(
            "review-result-metadata",
            "review-result",
            str(review["created_at"]),
            {
                "state": review["state"],
                "verdict": review["verdict"],
                "decision": review["decision"],
                "details_fields": review["details"]["fields"],
            },
        )
        if review["reviewer"] is not None:
            reviewer = review["reviewer"]
            append(
                "reviewer-verification-metadata",
                "reviewer-verification",
                str(reviewer["finished_at"] or review["created_at"]),
                {
                    "verification": reviewer["verification"],
                    "workspace_mode": reviewer["workspace_mode"],
                    "network_enabled": reviewer["network_enabled"],
                    "failure_present": reviewer["failure_present"],
                },
            )
        if review["integration"] is not None:
            integration = review["integration"]
            append(
                "integration-decision-metadata",
                "integration-decision",
                str(integration["finished_at"] or integration["created_at"]),
                {
                    "status": integration["status"],
                    "decision": integration["decision"],
                    "verdict": integration["verdict"],
                    "snapshot_verified": integration["snapshot_verified"],
                    "review_current": integration["review_current"],
                },
            )
        for item in evidence:
            if not PUBLIC_EVIDENCE_PATTERN.fullmatch(str(item["id"])):
                raise AssertionError("invalid evidence projection identifier")
        return evidence

    @staticmethod
    def _recovery_state(outcome: str) -> str:
        return {
            "ASSESSED": "assessed",
            "RESUMED": "resumed",
            "ROLLED_BACK": "rolled_back",
            "BLOCKED": "blocked",
            "FAILED": "failed",
        }[outcome]

    @classmethod
    def _recovery(cls, row: sqlite3.Row) -> dict[str, Any]:
        recovery_id = cls._identifier(
            row["recovery_id"], RECOVERY_ID_PATTERN,
            code="recovery_projection_invalid", title="Recovery projection unavailable",
            resource_type="recovery", resource_id=str(row["recovery_id"] or "unknown"),
        )
        project_id = cls._identifier(
            row["project_id"], PROJECT_ID_PATTERN,
            code="recovery_projection_invalid", title="Recovery projection unavailable",
            resource_type="recovery", resource_id=recovery_id,
        )
        internal_run = cls._internal_key(
            row["run_id"], code="recovery_projection_invalid",
            title="Recovery projection unavailable", resource_type="recovery",
            resource_id=recovery_id,
        )
        if row["run_status"] not in RUN_STATUSES:
            raise cls._projection_error(
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                detail="Stored recovery data references an invalid run state.",
                resource_type="recovery", resource_id=recovery_id,
            )
        role_id = cls._identifier(
            row["role_id"], ROLE_ID_PATTERN,
            code="recovery_projection_invalid", title="Recovery projection unavailable",
            resource_type="recovery", resource_id=recovery_id,
        )
        source_profile = cls._identifier(
            row["source_profile"], PROFILE_ID_PATTERN,
            code="recovery_projection_invalid", title="Recovery projection unavailable",
            resource_type="recovery", resource_id=recovery_id,
        )
        if (
            row["registered_role_id"] != role_id
            or row["registered_profile"] != source_profile
            or row["registered_role_kind"] != "recovery"
            or row["registered_workspace_mode"] != "controller_only"
            or row["role_enabled"] != 1
        ):
            raise cls._projection_error(
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                detail="Stored recovery execution does not match the registered recovery policy.",
                resource_type="recovery", resource_id=recovery_id,
            )
        policy_version = str(row["policy_version"] or "")
        if not POLICY_VERSION_PATTERN.fullmatch(policy_version):
            raise cls._projection_error(
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                detail="Stored recovery data contains an invalid policy version.",
                resource_type="recovery", resource_id=recovery_id,
            )
        observed_status = str(row["observed_status"] or "")
        decision = str(row["decision"] or "")
        outcome = str(row["outcome"] or "")
        expected_outcome = {
            "RESUME_SAFE": "RESUMED",
            "ROLLBACK_SAFE": "ROLLED_BACK",
            "BLOCK_HUMAN": "BLOCKED",
        }.get(decision)
        if (
            observed_status not in RECOVERY_OBSERVED_STATUSES
            or decision not in RECOVERY_DECISIONS
            or outcome not in RECOVERY_OUTCOMES
            or (
                expected_outcome is not None
                and outcome not in {"ASSESSED", "FAILED", expected_outcome}
            )
        ):
            raise cls._projection_error(
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                detail="Stored recovery data contains an invalid decision state.",
                resource_type="recovery", resource_id=recovery_id,
            )
        evidence_sha256 = str(row["evidence_sha256"] or "")
        if not SHA256_PATTERN.fullmatch(evidence_sha256):
            raise cls._projection_error(
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                detail="Stored recovery data contains an invalid evidence digest.",
                resource_type="recovery", resource_id=recovery_id,
            )
        evidence_payload = cls._json(
            row["evidence_json"], expected=dict,
            code="recovery_projection_invalid", title="Recovery projection unavailable",
            resource_type="recovery", resource_id=recovery_id,
        )
        actions_payload = cls._json(
            row["actions_json"], expected=list,
            code="recovery_projection_invalid", title="Recovery projection unavailable",
            resource_type="recovery", resource_id=recovery_id,
        )
        action_types: list[str] = []
        for action in actions_payload:
            if type(action) is not dict:
                continue
            candidate = action.get("action")
            if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", candidate):
                action_types.append(candidate)
        action_types = sorted(set(action_types))[:32]
        payload: dict[str, Any] = {
            "id": recovery_id,
            "project_id": project_id,
            "run_id": cls._transaction_reference(internal_run),
            "state": cls._recovery_state(outcome),
            "observed_state": observed_status.lower(),
            "decision": decision,
            "outcome": outcome,
            "role_id": role_id,
            "source_profile": source_profile,
            "policy_version": policy_version,
            "evidence": {
                "sha256": evidence_sha256,
                "fields": cls._safe_keys(evidence_payload),
                "redacted": True,
            },
            "actions": {
                "count": len(actions_payload),
                "types": action_types,
                "redacted": True,
            },
            "failure_present": row["failure_reason"] is not None,
            "created_at": cls._timestamp(
                row["created_at"], required=True,
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                resource_type="recovery", resource_id=recovery_id,
            ),
            "started_at": cls._timestamp(
                row["started_at"], required=True,
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                resource_type="recovery", resource_id=recovery_id,
            ),
            "finished_at": cls._timestamp(
                row["finished_at"], required=False,
                code="recovery_projection_invalid", title="Recovery projection unavailable",
                resource_type="recovery", resource_id=recovery_id,
            ),
        }
        payload["resource_revision"] = cls._revision(payload)
        return payload

    @staticmethod
    def _recovery_sql() -> str:
        return """
            SELECT
                recovery.recovery_id,
                recovery.run_id,
                recovery.role_id,
                recovery.source_profile,
                recovery.policy_version,
                recovery.observed_status,
                recovery.decision,
                recovery.outcome,
                recovery.evidence_sha256,
                recovery.evidence_json,
                recovery.actions_json,
                recovery.failure_reason,
                recovery.created_at,
                recovery.started_at,
                recovery.finished_at,
                run.project_id,
                run.status AS run_status,
                role.role_id AS registered_role_id,
                role.profile_name AS registered_profile,
                role.role_kind AS registered_role_kind,
                role.workspace_mode AS registered_workspace_mode,
                role.enabled AS role_enabled
            FROM recovery_executions AS recovery
            JOIN runs AS run ON run.run_id = recovery.run_id
            LEFT JOIN roles AS role ON role.role_id = recovery.role_id
        """

    def list_recoveries(
        self,
        *,
        limit: int,
        cursor: str | None,
        project_id: str | None,
        state: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        self._validate_limit(limit)
        self._validate_project_filter(project_id)
        if state is not None and state not in RECOVERY_STATES:
            raise ControllerError(400, "invalid_state", "Invalid recovery state")
        decoded = self._decode_cursor(cursor, cursor_secret, kind="recovery")
        if decoded is not None:
            if decoded.get("project_id") != project_id or decoded.get("state") != state:
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
            cursor_created = decoded.get("created_at")
            cursor_id = decoded.get("id")
            if not isinstance(cursor_created, str) or not RECOVERY_ID_PATTERN.fullmatch(str(cursor_id or "")):
                raise ControllerError(400, "invalid_cursor", "Invalid pagination cursor")
        else:
            cursor_created = None
            cursor_id = None
        clauses: list[str] = []
        parameters: list[Any] = []
        if project_id is not None:
            clauses.append("run.project_id = ?")
            parameters.append(project_id)
        if cursor_created is not None:
            clauses.append(
                "(recovery.created_at < ? OR "
                "(recovery.created_at = ? AND recovery.recovery_id < ?))"
            )
            parameters.extend([cursor_created, cursor_created, cursor_id])
        sql = self._recovery_sql()
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY recovery.created_at DESC, recovery.recovery_id DESC LIMIT 1001"
        try:
            with closing(self.database.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self._database_failure(error) from error
        projected: list[dict[str, Any]] = []
        last_scanned: dict[str, Any] | None = None
        for row in rows:
            item = self._recovery(row)
            last_scanned = item
            if state is not None and item["state"] != state:
                continue
            projected.append(item)
            if len(projected) > limit:
                break
        next_cursor: str | None = None
        cursor_item: dict[str, Any] | None = None
        if len(projected) > limit:
            projected = projected[:limit]
            cursor_item = projected[-1]
        elif state is not None and len(rows) == 1001 and last_scanned is not None:
            cursor_item = last_scanned
        if cursor_item is not None:
            next_cursor = self._encode_cursor(
                {
                    "kind": "recovery",
                    "project_id": project_id,
                    "state": state,
                    "created_at": cursor_item["created_at"],
                    "id": cursor_item["id"],
                },
                cursor_secret,
            )
        return projected, next_cursor

    def get_recovery(self, recovery_id: str) -> dict[str, Any]:
        if not RECOVERY_ID_PATTERN.fullmatch(recovery_id):
            raise ControllerError(404, "recovery_not_found", "Recovery not found")
        sql = self._recovery_sql() + " WHERE recovery.recovery_id = ?"
        try:
            with closing(self.database.connect()) as connection:
                row = connection.execute(sql, (recovery_id,)).fetchone()
        except sqlite3.Error as error:
            raise self._database_failure(error) from error
        if row is None:
            raise ControllerError(
                404,
                "recovery_not_found",
                "Recovery not found",
                resource={"type": "recovery", "id": recovery_id},
            )
        return self._recovery(row)
