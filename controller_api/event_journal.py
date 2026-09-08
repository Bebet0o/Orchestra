from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable

from .core import ControllerError

EVENT_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
_TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z$"
)
EVENT_ID_PATTERN = re.compile(r"^evt_[0-9a-f]{32}$")
CORRELATION_ID_PATTERN = re.compile(r"^corr_[0-9a-f]{32}$")
ACTOR_TYPES = {"operator", "system", "agent", "worker"}
AGGREGATE_TYPES = {
    "system",
    "project",
    "objective",
    "task",
    "run",
    "review",
    "recovery",
    "sandbox",
    "sandbox_build",
    "backup",
    "notification",
    "confirmation",
    "audit",
    "memory",
}
MAX_EVENT_DATA_BYTES = 16_384
MAX_EVENT_STRING_LENGTH = 4_096
MAX_EVENT_DEPTH = 8
MAX_EVENT_NODES = 512
MAX_REPLAY_LIMIT = 500

_SENSITIVE_KEY_TOKENS = {
    "auth",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "csrf",
    "env",
    "environment",
    "idempotency",
    "passwd",
    "password",
    "secret",
    "session",
    "token",
}
_SENSITIVE_KEY_PAIRS = {
    ("api", "key"),
    ("auth", "json"),
    ("private", "key"),
}
_FORBIDDEN_VALUE = re.compile(
    r"(?:-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----|"
    r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+\-/=]{8,}|"
    r"\b(?:sk|rk|pk)-(?:live|test)-?[A-Za-z0-9_-]{12,}|"
    r"\bghp_[A-Za-z0-9]{20,}|"
    r"\bgithub_pat_[A-Za-z0-9_]{20,}|"
    r"\bglpat-[A-Za-z0-9_-]{16,}|"
    r"\bxox[baprs]-[A-Za-z0-9-]{16,}|"
    r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{35}\b|"
    r"\b[0-9]{6,12}:[A-Za-z0-9_-]{30,}\b|"
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b|"
    r"[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@)",
    re.IGNORECASE,
)


def _key_is_sensitive(key: str) -> bool:
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", separated).strip("_").lower()
    parts = tuple(part for part in normalized.split("_") if part)
    if any(part in _SENSITIVE_KEY_TOKENS for part in parts):
        return True
    return any(pair in zip(parts, parts[1:]) for pair in _SENSITIVE_KEY_PAIRS)



def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class EventJournal:
    """Transactional persistence and bounded replay for EVENTS_V1 envelopes."""

    @staticmethod
    def correlation_for_causation(causation_id: str) -> str:
        if not isinstance(causation_id, str) or not causation_id:
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        digest = hashlib.sha256(causation_id.encode("utf-8")).hexdigest()[:32]
        return "corr_" + digest

    @classmethod
    def _validate_text(
        cls,
        value: str | None,
        *,
        field: str,
        required: bool,
        maximum: int = 200,
    ) -> str | None:
        if value is None:
            if required:
                raise ControllerError(
                    503,
                    "event_journal_input_invalid",
                    "Event journal input is invalid",
                )
            return None
        if (
            not isinstance(value, str)
            or not value
            or len(value) > maximum
            or any(ord(character) < 32 for character in value)
        ):
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
                f"Invalid {field}.",
            )
        return value

    @staticmethod
    def _validate_timestamp(value: str) -> str:
        if not isinstance(value, str) or not _TIMESTAMP_PATTERN.fullmatch(value):
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as error:
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            ) from error
        if parsed.tzinfo is None:
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        return value

    @classmethod
    def _validate_data(cls, data: dict[str, Any]) -> str:
        if not isinstance(data, dict):
            raise ControllerError(
                503,
                "event_journal_redaction_failed",
                "Event data could not be safely persisted",
            )
        nodes = 0

        def visit(value: Any, depth: int) -> None:
            nonlocal nodes
            nodes += 1
            if nodes > MAX_EVENT_NODES or depth > MAX_EVENT_DEPTH:
                raise ControllerError(
                    503,
                    "event_journal_redaction_failed",
                    "Event data could not be safely persisted",
                )
            if value is None or isinstance(value, bool) or isinstance(value, int):
                return
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise ControllerError(
                        503,
                        "event_journal_redaction_failed",
                        "Event data could not be safely persisted",
                    )
                return
            if isinstance(value, str):
                if (
                    len(value) > MAX_EVENT_STRING_LENGTH
                    or any(ord(character) < 32 and character not in "\t\n\r" for character in value)
                    or _FORBIDDEN_VALUE.search(value)
                ):
                    raise ControllerError(
                        503,
                        "event_journal_redaction_failed",
                        "Event data could not be safely persisted",
                    )
                return
            if isinstance(value, list):
                for item in value:
                    visit(item, depth + 1)
                return
            if isinstance(value, dict):
                for key, item in value.items():
                    if (
                        not isinstance(key, str)
                        or not key
                        or len(key) > 128
                        or _key_is_sensitive(key)
                        or any(ord(character) < 32 for character in key)
                    ):
                        raise ControllerError(
                            503,
                            "event_journal_redaction_failed",
                            "Event data could not be safely persisted",
                        )
                    visit(item, depth + 1)
                return
            raise ControllerError(
                503,
                "event_journal_redaction_failed",
                "Event data could not be safely persisted",
            )

        visit(data, 0)
        encoded = canonical_json(data)
        if len(encoded.encode("utf-8")) > MAX_EVENT_DATA_BYTES:
            raise ControllerError(
                503,
                "event_journal_redaction_failed",
                "Event data could not be safely persisted",
            )
        return encoded

    @classmethod
    def emit(
        cls,
        connection: sqlite3.Connection,
        *,
        event_type: str,
        actor_type: str,
        actor_id: str,
        aggregate_type: str,
        aggregate_id: str,
        correlation_id: str,
        data: dict[str, Any],
        causation_id: str | None = None,
        project_id: str | None = None,
        objective_id: str | None = None,
        event_id: str | None = None,
        occurred_at: str | None = None,
    ) -> dict[str, Any]:
        if not connection.in_transaction:
            raise ControllerError(
                503,
                "event_journal_transaction_required",
                "Event journal transaction required",
            )
        if not isinstance(event_type, str) or not EVENT_TYPE_PATTERN.fullmatch(event_type):
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        if actor_type not in ACTOR_TYPES or aggregate_type not in AGGREGATE_TYPES:
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        actor_id = cls._validate_text(actor_id, field="actor_id", required=True)
        aggregate_id = cls._validate_text(
            aggregate_id, field="aggregate_id", required=True
        )
        project_id = cls._validate_text(project_id, field="project_id", required=False)
        objective_id = cls._validate_text(
            objective_id, field="objective_id", required=False
        )
        causation_id = cls._validate_text(
            causation_id, field="causation_id", required=False
        )
        if not isinstance(correlation_id, str) or not CORRELATION_ID_PATTERN.fullmatch(
            correlation_id
        ):
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        event_id = event_id or "evt_" + uuid.uuid4().hex
        if not EVENT_ID_PATTERN.fullmatch(event_id):
            raise ControllerError(
                503,
                "event_journal_input_invalid",
                "Event journal input is invalid",
            )
        occurred_at = cls._validate_timestamp(occurred_at or utc_now())
        encoded_data = cls._validate_data(data)
        try:
            revision = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(aggregate_revision), 0) + 1
                    FROM controller_event_journal
                    WHERE aggregate_type=? AND aggregate_id=?
                    """,
                    (aggregate_type, aggregate_id),
                ).fetchone()[0]
            )
            cursor = connection.execute(
                """
                INSERT INTO controller_event_journal (
                    event_id, schema_version, event_type, occurred_at,
                    actor_type, actor_id, aggregate_type, aggregate_id,
                    aggregate_revision, project_id, objective_id,
                    correlation_id, causation_id, redacted_data_json
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    event_type,
                    occurred_at,
                    actor_type,
                    actor_id,
                    aggregate_type,
                    aggregate_id,
                    revision,
                    project_id,
                    objective_id,
                    correlation_id,
                    causation_id,
                    encoded_data,
                ),
            )
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "event_journal_unavailable",
                "Event journal unavailable",
            ) from error
        return {
            "schema_version": 1,
            "sequence": int(cursor.lastrowid),
            "event_id": event_id,
            "type": event_type,
            "occurred_at": occurred_at,
            "actor": {"type": actor_type, "id": actor_id},
            "aggregate": {
                "type": aggregate_type,
                "id": aggregate_id,
                "revision": revision,
            },
            "correlation_id": correlation_id,
            "causation_id": causation_id,
            "project_id": project_id,
            "objective_id": objective_id,
            "data": data,
        }

    @classmethod
    def _row_to_envelope(cls, row: sqlite3.Row) -> dict[str, Any]:
        try:
            data = json.loads(str(row["redacted_data_json"]))
            encoded = cls._validate_data(data)
            occurred_at = cls._validate_timestamp(str(row["occurred_at"]))
            actor_id = cls._validate_text(
                str(row["actor_id"]), field="actor_id", required=True
            )
            aggregate_id = cls._validate_text(
                str(row["aggregate_id"]), field="aggregate_id", required=True
            )
            causation_id = cls._validate_text(
                str(row["causation_id"])
                if row["causation_id"] is not None
                else None,
                field="causation_id",
                required=False,
            )
            project_id = cls._validate_text(
                str(row["project_id"]) if row["project_id"] is not None else None,
                field="project_id",
                required=False,
            )
            objective_id = cls._validate_text(
                str(row["objective_id"])
                if row["objective_id"] is not None
                else None,
                field="objective_id",
                required=False,
            )
            envelope = {
                "schema_version": int(row["schema_version"]),
                "sequence": int(row["sequence"]),
                "event_id": str(row["event_id"]),
                "type": str(row["event_type"]),
                "occurred_at": occurred_at,
                "actor": {
                    "type": str(row["actor_type"]),
                    "id": actor_id,
                },
                "aggregate": {
                    "type": str(row["aggregate_type"]),
                    "id": aggregate_id,
                    "revision": int(row["aggregate_revision"]),
                },
                "correlation_id": str(row["correlation_id"]),
                "causation_id": causation_id,
                "project_id": project_id,
                "objective_id": objective_id,
                "data": data,
            }
            if (
                envelope["schema_version"] != 1
                or envelope["sequence"] < 1
                or not EVENT_ID_PATTERN.fullmatch(envelope["event_id"])
                or not EVENT_TYPE_PATTERN.fullmatch(envelope["type"])
                or envelope["actor"]["type"] not in ACTOR_TYPES
                or envelope["aggregate"]["type"] not in AGGREGATE_TYPES
                or envelope["aggregate"]["revision"] < 1
                or not CORRELATION_ID_PATTERN.fullmatch(envelope["correlation_id"])
                or encoded != str(row["redacted_data_json"])
            ):
                raise ValueError("invalid persisted event")
            return envelope
        except (
            ControllerError,
            KeyError,
            TypeError,
            ValueError,
            json.JSONDecodeError,
        ) as error:
            raise ControllerError(
                503,
                "event_journal_corrupt",
                "Event journal projection unavailable",
            ) from error

    @classmethod
    def read_after(
        cls,
        connection: sqlite3.Connection,
        *,
        after_sequence: int,
        limit: int = 100,
        project_id: str | None = None,
        event_types: Iterable[str] | None = None,
    ) -> list[dict[str, Any]]:
        if type(after_sequence) is not int or after_sequence < 0:
            raise ControllerError(400, "invalid_event_sequence", "Invalid event sequence")
        if type(limit) is not int or not 1 <= limit <= MAX_REPLAY_LIMIT:
            raise ControllerError(400, "invalid_event_limit", "Invalid event limit")
        project_id = cls._validate_text(
            project_id, field="project_id", required=False
        )
        normalized_types: tuple[str, ...] = ()
        if event_types is not None:
            normalized_types = tuple(event_types)
            if (
                not 1 <= len(normalized_types) <= 32
                or len(set(normalized_types)) != len(normalized_types)
                or any(
                    not isinstance(value, str)
                    or not EVENT_TYPE_PATTERN.fullmatch(value)
                    for value in normalized_types
                )
            ):
                raise ControllerError(
                    400, "invalid_event_types", "Invalid event types"
                )
        clauses = ["sequence > ?"]
        parameters: list[Any] = [after_sequence]
        if project_id is not None:
            clauses.append("project_id = ?")
            parameters.append(project_id)
        if normalized_types:
            placeholders = ",".join("?" for _ in normalized_types)
            clauses.append(f"event_type IN ({placeholders})")
            parameters.extend(normalized_types)
        parameters.append(limit)
        try:
            rows = list(
                connection.execute(
                    """
                    SELECT sequence, event_id, schema_version, event_type,
                           occurred_at, actor_type, actor_id, aggregate_type,
                           aggregate_id, aggregate_revision, project_id,
                           objective_id, correlation_id, causation_id,
                           redacted_data_json
                    FROM controller_event_journal
                    WHERE """
                    + " AND ".join(clauses)
                    + " ORDER BY sequence LIMIT ?",
                    tuple(parameters),
                )
            )
        except sqlite3.Error as error:
            raise ControllerError(
                503, "event_journal_unavailable", "Event journal unavailable"
            ) from error
        return [cls._row_to_envelope(row) for row in rows]

    @staticmethod
    def bounds(connection: sqlite3.Connection) -> tuple[int | None, int]:
        try:
            row = connection.execute(
                "SELECT MIN(sequence), COALESCE(MAX(sequence), 0) "
                "FROM controller_event_journal"
            ).fetchone()
        except sqlite3.Error as error:
            raise ControllerError(
                503, "event_journal_unavailable", "Event journal unavailable"
            ) from error
        return (int(row[0]) if row[0] is not None else None, int(row[1]))
