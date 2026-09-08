from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import uuid
from contextlib import closing
from typing import Any, Callable, Iterable

from .core import ControllerError, PROJECT_ID_PATTERN, Settings
from .event_journal import EventJournal, canonical_json, utc_now
from .project_memory import MEMORY_KINDS, MEMORY_STATES, ProjectMemoryStore

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~:-]{8,200}$")
OPERATION_ID_PATTERN = re.compile(r"^operation-[0-9a-f]{32}$")
BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
MAX_LIST_LIMIT = 200
MAX_LINKS = 64
MAX_REASON_LENGTH = 1000
MAX_CURSOR_BYTES = 2048


class MemoryCommandStore:
    """Controller-owned HTTP command and read model for structured memory."""

    REQUIRED_TABLES = {
        "project_memories",
        "project_memory_payloads",
        "project_memory_revisions",
        "project_memory_provenance",
        "project_memory_revision_links",
        "controller_memory_operations",
        "controller_memory_idempotency",
        "controller_memory_command_audit",
        "controller_event_journal",
        "schema_migrations",
    }

    def __init__(self, settings: Settings, memory: ProjectMemoryStore) -> None:
        self.settings = settings
        self.memory = memory

    def connect(self) -> sqlite3.Connection:
        try:
            connection = sqlite3.connect(
                self.settings.database,
                timeout=10,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA synchronous = FULL")
            return connection
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "project_memory_unavailable",
                "Project memory unavailable",
                "The project memory Controller store cannot be opened.",
            ) from error

    def readiness(self) -> tuple[bool, str]:
        try:
            with closing(self.connect()) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if self.REQUIRED_TABLES - tables:
                    return False, "project memory Controller tables are missing"
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 33:
                    return False, "project memory Controller migration is not installed"
                connection.execute(
                    "SELECT operation_id FROM controller_memory_operations LIMIT 1"
                ).fetchone()
        except (sqlite3.Error, ControllerError, TypeError, ValueError):
            return False, "project memory Controller persistence cannot be read"
        return True, "ready"

    @staticmethod
    def validate_idempotency_key(value: str | None) -> str:
        if value is None or IDEMPOTENCY_KEY_PATTERN.fullmatch(value) is None:
            raise ControllerError(
                400,
                "invalid_idempotency_key",
                "Invalid Idempotency-Key",
                "Idempotency-Key must contain 8..200 safe ASCII characters.",
            )
        return value

    @staticmethod
    def _session_fingerprint(session_token: str) -> str:
        return hashlib.sha256(session_token.encode("ascii")).hexdigest()[:32]

    @staticmethod
    def _key_hash(session_token: str, key: str) -> str:
        return hmac.new(
            session_token.encode("ascii"),
            b"orchestra-memory-idempotency-v1\0" + key.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _request_hash(
        session_token: str,
        method: str,
        route: str,
        body: dict[str, Any],
        if_match: str | None,
    ) -> str:
        encoded = canonical_json(
            {
                "method": method,
                "route": route,
                "if_match": if_match,
                "body": body,
            }
        ).encode("utf-8")
        return hmac.new(
            session_token.encode("ascii"),
            b"orchestra-memory-request-v1\0" + encoded,
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _parse_if_match(value: str | None) -> int:
        if value is None:
            raise ControllerError(
                428,
                "precondition_required",
                "If-Match is required",
                "The current quoted project memory resource revision is required.",
            )
        if re.fullmatch(r'"[1-9][0-9]*"', value) is None:
            raise ControllerError(400, "invalid_if_match", "Invalid If-Match")
        revision = int(value[1:-1])
        if revision > 2**63 - 1:
            raise ControllerError(400, "invalid_if_match", "Invalid If-Match")
        return revision

    @staticmethod
    def _memory_id(value: str) -> str:
        if (
            not isinstance(value, str)
            or not 1 <= len(value) <= 200
            or "/" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
        ):
            raise ControllerError(404, "project_memory_not_found", "Project memory not found")
        return value

    @staticmethod
    def _revision_number(value: int) -> int:
        if type(value) is not int or not 1 <= value <= 2**63 - 1:
            raise ControllerError(
                404,
                "project_memory_revision_not_found",
                "Project memory revision not found",
            )
        return value

    @staticmethod
    def _normalize_links(value: Any) -> tuple[dict[str, str], ...]:
        if value is None:
            return ()
        if not isinstance(value, list) or len(value) > MAX_LINKS:
            raise ControllerError(
                400,
                "invalid_memory_link",
                "Invalid project memory link",
                "links must be a bounded array of resource references.",
            )
        normalized: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"kind", "id"}:
                raise ControllerError(
                    400,
                    "invalid_memory_link",
                    "Invalid project memory link",
                    "Each link requires exactly kind and id.",
                )
            kind = item.get("kind")
            source_id = item.get("id")
            if (
                not isinstance(kind, str)
                or not isinstance(source_id, str)
                or not 1 <= len(source_id) <= 200
                or "/" in source_id
                or any(ord(character) < 32 or ord(character) == 127 for character in source_id)
            ):
                raise ControllerError(
                    400,
                    "invalid_memory_link",
                    "Invalid project memory link",
                )
            pair = (kind, source_id)
            if pair in seen:
                raise ControllerError(
                    400,
                    "invalid_memory_link",
                    "Invalid project memory link",
                    "Duplicate links are not accepted in one request.",
                )
            seen.add(pair)
            normalized.append({"kind": kind, "id": source_id})
        return tuple(normalized)

    @staticmethod
    def _reason_body(body: dict[str, Any]) -> str | None:
        if set(body) - {"reason"}:
            raise ControllerError(400, "unknown_field", "Unknown request field")
        reason = body.get("reason")
        if reason is None:
            return None
        if (
            not isinstance(reason, str)
            or not reason.strip()
            or len(reason) > MAX_REASON_LENGTH
            or any(ord(character) < 32 and character not in "\t\n\r" for character in reason)
            or any(ord(character) == 127 for character in reason)
        ):
            raise ControllerError(400, "invalid_reason", "Invalid command reason")
        return reason.strip()

    def _create_payload(
        self,
        body: dict[str, Any],
    ) -> tuple[str, str | None, str, str | None, str | None, str, str, tuple[dict[str, str], ...]]:
        allowed = {
            "scope",
            "objective_id",
            "kind",
            "memory_key",
            "title",
            "content",
            "links",
        }
        if set(body) - allowed or not {"scope", "kind", "content"}.issubset(body):
            raise ControllerError(
                400,
                "invalid_memory_create",
                "Invalid project memory creation request",
            )
        scope = body.get("scope")
        objective_id = body.get("objective_id")
        if scope not in {"PROJECT", "OBJECTIVE"}:
            raise ControllerError(400, "invalid_memory_scope", "Invalid project memory scope")
        if objective_id is not None and (
            not isinstance(objective_id, str)
            or not 1 <= len(objective_id) <= 200
            or "/" in objective_id
            or any(ord(character) < 32 or ord(character) == 127 for character in objective_id)
        ):
            raise ControllerError(400, "invalid_memory_scope", "Invalid project memory scope")
        kind, key, title, content, payload_hash = self.memory._payload(
            kind=body.get("kind"),
            memory_key=body.get("memory_key"),
            title=body.get("title"),
            content=body.get("content"),
        )
        return (
            scope,
            objective_id,
            kind,
            key,
            title,
            content,
            payload_hash,
            self._normalize_links(body.get("links")),
        )

    def _revision_payload(
        self,
        body: dict[str, Any],
    ) -> tuple[str, str | None, str | None, str, str, tuple[dict[str, str], ...]]:
        allowed = {"kind", "memory_key", "title", "content", "links"}
        if set(body) - allowed or not {"kind", "content"}.issubset(body):
            raise ControllerError(
                400,
                "invalid_memory_revision",
                "Invalid project memory revision request",
            )
        kind, key, title, content, payload_hash = self.memory._payload(
            kind=body.get("kind"),
            memory_key=body.get("memory_key"),
            title=body.get("title"),
            content=body.get("content"),
        )
        return kind, key, title, content, payload_hash, self._normalize_links(body.get("links"))

    def _replay_or_reserve(
        self,
        connection: sqlite3.Connection,
        *,
        session_token: str,
        idempotency_key: str,
        method: str,
        route: str,
        body: dict[str, Any],
        if_match: str | None,
    ) -> tuple[dict[str, Any] | None, str, str, str]:
        session_fp = self._session_fingerprint(session_token)
        key_hash = self._key_hash(session_token, idempotency_key)
        request_hash = self._request_hash(
            session_token,
            method,
            route,
            body,
            if_match,
        )
        row = connection.execute(
            """
            SELECT method, route, request_hash, response_json, operation_id
            FROM controller_memory_idempotency
            WHERE session_fingerprint=? AND key_hash=?
            """,
            (session_fp, key_hash),
        ).fetchone()
        if row is not None:
            if (
                str(row["method"]) != method
                or str(row["route"]) != route
                or str(row["request_hash"]) != request_hash
            ):
                raise ControllerError(409, "idempotency_conflict", "Idempotency key conflict")
            if row["response_json"] is None:
                raise ControllerError(
                    409,
                    "idempotency_reservation_invalid",
                    "Idempotency reservation is incomplete",
                )
            try:
                replay = json.loads(str(row["response_json"]))
                operation_id = str(row["operation_id"])
                operation_row = connection.execute(
                    "SELECT * FROM controller_memory_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if operation_row is None:
                    raise ValueError("missing idempotency operation")
                operation = self._operation_payload(operation_row)
                self._validate_replay_payload(replay, operation)
            except (ControllerError, json.JSONDecodeError, TypeError, ValueError) as error:
                if isinstance(error, ControllerError) and error.code == "idempotency_projection_invalid":
                    raise
                raise ControllerError(
                    503,
                    "idempotency_projection_invalid",
                    "Idempotency projection unavailable",
                ) from error
            return replay, session_fp, key_hash, request_hash
        connection.execute(
            """
            INSERT INTO controller_memory_idempotency (
                session_fingerprint, key_hash, method, route, request_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_fp, key_hash, method, route, request_hash, utc_now()),
        )
        return None, session_fp, key_hash, request_hash

    @classmethod
    def _operation_payload(cls, row: sqlite3.Row) -> dict[str, Any]:
        try:
            result = json.loads(str(row["result_json"]))
            if not isinstance(result, dict) or set(result) != {
                "memory_id", "project_id", "state", "scope", "kind",
                "revision", "resource_revision", "deduplicated", "revision_created",
            }:
                raise ValueError("invalid memory operation result shape")
            memory_id = cls._memory_id(result["memory_id"])
            project_id = result["project_id"]
            if not isinstance(project_id, str) or PROJECT_ID_PATTERN.fullmatch(project_id) is None:
                raise ValueError("invalid operation project")
            if result["state"] not in MEMORY_STATES:
                raise ValueError("invalid operation memory state")
            if result["scope"] not in {"PROJECT", "OBJECTIVE"}:
                raise ValueError("invalid operation scope")
            if result["kind"] not in MEMORY_KINDS:
                raise ValueError("invalid operation kind")
            if (
                type(result["revision"]) is not int
                or result["revision"] < 1
                or type(result["resource_revision"]) is not int
                or result["resource_revision"] < 1
                or type(result["deduplicated"]) is not bool
                or type(result["revision_created"]) is not bool
            ):
                raise ValueError("invalid operation revision metadata")
            command_kind = str(row["command_kind"])
            if command_kind not in {
                "memory.create", "memory.revise", "memory.retract", "memory.redact"
            }:
                raise ValueError("invalid memory operation kind")
            if str(row["state"]) != "SUCCEEDED" or row["error_code"] is not None:
                raise ValueError("invalid completed memory operation state")
            target_id = cls._memory_id(str(row["target_id"]))
            if target_id != memory_id:
                raise ValueError("memory operation target mismatch")
            created_at = cls._persisted_reference(row["created_at"], maximum_bytes=64)
            updated_at = cls._persisted_reference(row["updated_at"], maximum_bytes=64)
            finished_at = cls._persisted_reference(row["finished_at"], maximum_bytes=64)
        except (ControllerError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ControllerError(
                503,
                "operation_projection_invalid",
                "Operation projection unavailable",
            ) from error
        payload: dict[str, Any] = {
            "id": str(row["operation_id"]),
            "kind": command_kind,
            "state": "succeeded",
            "created_at": created_at,
            "updated_at": updated_at,
            "finished_at": finished_at,
            "target": {"type": "memory", "id": target_id},
            "result": result,
            "error": None,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        payload["resource_revision"] = int(digest[:15], 16)
        return payload

    @classmethod
    def _validate_replay_payload(
        cls,
        replay: Any,
        operation: dict[str, Any],
    ) -> None:
        if not isinstance(replay, dict) or set(replay) != {"data", "meta"}:
            raise ControllerError(
                503,
                "idempotency_projection_invalid",
                "Idempotency projection unavailable",
            )
        if replay.get("data") != operation:
            raise ControllerError(
                503,
                "idempotency_projection_invalid",
                "Idempotency projection unavailable",
            )
        meta = replay.get("meta")
        allowed = {"request_id", "resource_revision", "next_cursor", "snapshot_sequence"}
        if (
            not isinstance(meta, dict)
            or not {"request_id", "resource_revision"}.issubset(meta)
            or set(meta) - allowed
            or type(meta.get("resource_revision")) is not int
            or int(meta["resource_revision"]) < 1
            or int(meta["resource_revision"])
            != int(operation["result"]["resource_revision"])
            or ("next_cursor" in meta and meta["next_cursor"] is not None)
            or ("snapshot_sequence" in meta and meta["snapshot_sequence"] is not None)
        ):
            raise ControllerError(
                503,
                "idempotency_projection_invalid",
                "Idempotency projection unavailable",
            )
        cls._persisted_reference(meta.get("request_id"), maximum_bytes=128)

    @classmethod
    def _record_operation(
        cls,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        kind: str,
        memory_id: str,
        result: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        connection.execute(
            """
            INSERT INTO controller_memory_operations (
                operation_id, command_kind, state, target_id, result_json,
                error_code, created_at, updated_at, finished_at
            ) VALUES (?, ?, 'SUCCEEDED', ?, ?, NULL, ?, ?, ?)
            """,
            (operation_id, kind, memory_id, canonical_json(result), now, now, now),
        )
        row = connection.execute(
            "SELECT * FROM controller_memory_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        assert row is not None
        return cls._operation_payload(row)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        action: str,
        memory_id: str,
        project_id: str,
        session_fp: str,
        key_hash: str,
        request_hash: str,
        reason_present: bool,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO controller_memory_command_audit (
                audit_id, operation_id, actor_type, actor_id, action,
                resource_type, resource_id, project_id, session_fingerprint,
                idempotency_key_hash, request_hash, outcome,
                reason_present, created_at
            ) VALUES (?, ?, 'session', 'operator', ?, 'memory', ?, ?, ?, ?, ?,
                      'SUCCEEDED', ?, ?)
            """,
            (
                "audit-" + uuid.uuid4().hex,
                operation_id,
                action,
                memory_id,
                project_id,
                session_fp,
                key_hash,
                request_hash,
                int(reason_present),
                now,
            ),
        )

    @staticmethod
    def _emit(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        memory: dict[str, Any],
        operation_id: str,
        data: dict[str, Any],
        now: str,
    ) -> None:
        EventJournal.emit(
            connection,
            event_type=event_type,
            actor_type="operator",
            actor_id="operator",
            aggregate_type="memory",
            aggregate_id=str(memory["id"]),
            correlation_id=EventJournal.correlation_for_causation(operation_id),
            causation_id=operation_id,
            project_id=str(memory["project_id"]),
            objective_id=(
                None if memory["objective_id"] is None else str(memory["objective_id"])
            ),
            data=data,
            occurred_at=now,
        )

    @staticmethod
    def _complete_idempotency(
        connection: sqlite3.Connection,
        *,
        session_fp: str,
        key_hash: str,
        status: int,
        payload: dict[str, Any],
        operation_id: str,
        now: str,
    ) -> None:
        connection.execute(
            """
            UPDATE controller_memory_idempotency
            SET response_status=?, response_json=?, operation_id=?, completed_at=?
            WHERE session_fingerprint=? AND key_hash=?
            """,
            (
                status,
                canonical_json(payload),
                operation_id,
                now,
                session_fp,
                key_hash,
            ),
        )

    @staticmethod
    def _result_metadata(
        memory: dict[str, Any],
        *,
        deduplicated: bool,
        revision_created: bool,
    ) -> dict[str, Any]:
        return {
            "memory_id": str(memory["id"]),
            "project_id": str(memory["project_id"]),
            "state": str(memory["state"]),
            "scope": str(memory["scope"]),
            "kind": str(memory["kind"]),
            "revision": int(memory["revision"]),
            "resource_revision": int(memory["resource_revision"]),
            "deduplicated": deduplicated,
            "revision_created": revision_created,
        }

    @staticmethod
    def _integrity_error(error: sqlite3.IntegrityError) -> ControllerError:
        message = str(error).lower()
        if (
            "project memory link crosses project boundary" in message
            or "foreign key constraint failed" in message
        ):
            return ControllerError(
                400,
                "invalid_memory_link",
                "Invalid project memory link",
                "A linked resource does not exist in the target project.",
            )
        return ControllerError(
            503,
            "project_memory_persistence_failed",
            "Project memory persistence failed",
            "The Controller could not persist project memory safely.",
        )

    def create_memory(
        self,
        *,
        session_token: str,
        idempotency_key: str,
        route: str,
        project_id: str,
        body: dict[str, Any],
        meta_factory: Callable[[int | None], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        self.validate_idempotency_key(idempotency_key)
        if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
            raise ControllerError(400, "invalid_project_id", "Invalid project identifier")
        now = utc_now()
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    replay, session_fp, key_hash, request_hash = self._replay_or_reserve(
                        connection,
                        session_token=session_token,
                        idempotency_key=idempotency_key,
                        method="POST",
                        route=route,
                        body=body,
                        if_match=None,
                    )
                    if replay is not None:
                        connection.commit()
                        return 202, replay
                    scope, objective_id, kind, key, title, content, payload_hash, links = (
                        self._create_payload(body)
                    )
                    self.memory._validate_actor(
                        authority="OPERATOR_DECLARED",
                        actor_type="OPERATOR",
                        actor_id="operator",
                    )
                    self.memory._scope_exists(
                        connection,
                        project_id=project_id,
                        scope=scope,
                        objective_id=objective_id,
                    )
                    existing = connection.execute(
                        self.memory._select_current_sql()
                        + """
                          WHERE memory.project_id=?
                            AND memory.scope=?
                            AND memory.objective_id IS ?
                            AND memory.state='ACTIVE'
                            AND revision.payload_sha256=?
                            AND revision.authority='OPERATOR_DECLARED'
                            AND payload.state='AVAILABLE'
                          ORDER BY memory.memory_id
                          LIMIT 1
                        """,
                        (project_id, scope, objective_id, payload_hash),
                    ).fetchone()
                    if existing is not None:
                        revision_id = str(existing["revision_id"])
                        self.memory._insert_provenance(
                            connection,
                            revision_id=revision_id,
                            items=({"kind": "OPERATOR", "id": "operator"},),
                            created_at=now,
                        )
                        self.memory._insert_links(
                            connection,
                            revision_id=revision_id,
                            items=links,
                            created_at=now,
                        )
                        connection.execute(
                            """
                            UPDATE project_memories
                            SET resource_revision=resource_revision+1, updated_at=?
                            WHERE memory_id=?
                            """,
                            (now, str(existing["memory_id"])),
                        )
                        current = self.memory._projection(
                            self.memory._current_row(connection, str(existing["memory_id"]))
                        )
                        deduplicated = True
                        revision_created = False
                        event_type = "memory.attested"
                    else:
                        memory_id = "memory-" + secrets.token_hex(16)
                        revision_id = "memory-revision-" + secrets.token_hex(16)
                        payload_id = "memory-payload-" + secrets.token_hex(16)
                        connection.execute(
                            """
                            INSERT INTO project_memories (
                                memory_id, project_id, scope, objective_id, state,
                                current_revision_id, current_revision_number,
                                resource_revision, created_at, updated_at
                            ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, 1, 1, ?, ?)
                            """,
                            (memory_id, project_id, scope, objective_id, revision_id, now, now),
                        )
                        connection.execute(
                            """
                            INSERT INTO project_memory_payloads (
                                payload_id, kind, memory_key, title, content, state,
                                redacted_at, redaction_code
                            ) VALUES (?, ?, ?, ?, ?, 'AVAILABLE', NULL, NULL)
                            """,
                            (payload_id, kind, key, title, content),
                        )
                        connection.execute(
                            """
                            INSERT INTO project_memory_revisions (
                                revision_id, memory_id, revision_number, payload_id,
                                payload_sha256, authority, authority_review_id,
                                authority_decision_id, supersedes_revision_id,
                                created_by_actor_type, created_by_actor_id, created_at
                            ) VALUES (?, ?, 1, ?, ?, 'OPERATOR_DECLARED', NULL, NULL,
                                      NULL, 'OPERATOR', 'operator', ?)
                            """,
                            (revision_id, memory_id, payload_id, payload_hash, now),
                        )
                        self.memory._insert_provenance(
                            connection,
                            revision_id=revision_id,
                            items=({"kind": "OPERATOR", "id": "operator"},),
                            created_at=now,
                        )
                        self.memory._insert_links(
                            connection,
                            revision_id=revision_id,
                            items=links,
                            created_at=now,
                        )
                        current = self.memory._projection(
                            self.memory._current_row(connection, memory_id)
                        )
                        deduplicated = False
                        revision_created = True
                        event_type = "memory.created"

                    operation_id = "operation-" + uuid.uuid4().hex
                    result = self._result_metadata(
                        current,
                        deduplicated=deduplicated,
                        revision_created=revision_created,
                    )
                    operation = self._record_operation(
                        connection,
                        operation_id=operation_id,
                        kind="memory.create",
                        memory_id=str(current["id"]),
                        result=result,
                        now=now,
                    )
                    self._audit(
                        connection,
                        operation_id=operation_id,
                        action="memory.create",
                        memory_id=str(current["id"]),
                        project_id=str(current["project_id"]),
                        session_fp=session_fp,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        reason_present=False,
                        now=now,
                    )
                    self._emit(
                        connection,
                        event_type=event_type,
                        memory=current,
                        operation_id=operation_id,
                        data=result,
                        now=now,
                    )
                    payload = {
                        "data": operation,
                        "meta": meta_factory(int(current["resource_revision"])),
                    }
                    self._complete_idempotency(
                        connection,
                        session_fp=session_fp,
                        key_hash=key_hash,
                        status=202,
                        payload=payload,
                        operation_id=operation_id,
                        now=now,
                    )
                    connection.commit()
                    return 202, payload
                except Exception:
                    connection.rollback()
                    raise
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise self._integrity_error(error) from error
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error

    def revise_memory(
        self,
        *,
        session_token: str,
        idempotency_key: str,
        route: str,
        memory_id: str,
        if_match: str | None,
        body: dict[str, Any],
        meta_factory: Callable[[int | None], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        self.validate_idempotency_key(idempotency_key)
        memory_id = self._memory_id(memory_id)
        now = utc_now()
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    replay, session_fp, key_hash, request_hash = self._replay_or_reserve(
                        connection,
                        session_token=session_token,
                        idempotency_key=idempotency_key,
                        method="PATCH",
                        route=route,
                        body=body,
                        if_match=if_match,
                    )
                    if replay is not None:
                        connection.commit()
                        return 202, replay
                    expected = self._parse_if_match(if_match)
                    kind, key, title, content, payload_hash, links = self._revision_payload(body)
                    self.memory._validate_actor(
                        authority="OPERATOR_DECLARED",
                        actor_type="OPERATOR",
                        actor_id="operator",
                    )
                    current_row = self.memory._current_row(connection, memory_id)
                    if int(current_row["resource_revision"]) != expected:
                        raise ControllerError(
                            409,
                            "resource_revision_conflict",
                            "Project memory revision conflict",
                            "The project memory changed since the supplied revision.",
                            resource={"type": "memory", "id": memory_id},
                        )
                    if str(current_row["state"]) != "ACTIVE":
                        raise ControllerError(
                            409,
                            "memory_terminal",
                            "Project memory is terminal",
                            "Retracted or redacted memory cannot be revised.",
                            resource={"type": "memory", "id": memory_id},
                        )
                    next_revision = int(current_row["current_revision_number"]) + 1
                    revision_id = "memory-revision-" + secrets.token_hex(16)
                    payload_id = "memory-payload-" + secrets.token_hex(16)
                    connection.execute(
                        """
                        INSERT INTO project_memory_payloads (
                            payload_id, kind, memory_key, title, content, state,
                            redacted_at, redaction_code
                        ) VALUES (?, ?, ?, ?, ?, 'AVAILABLE', NULL, NULL)
                        """,
                        (payload_id, kind, key, title, content),
                    )
                    connection.execute(
                        """
                        INSERT INTO project_memory_revisions (
                            revision_id, memory_id, revision_number, payload_id,
                            payload_sha256, authority, authority_review_id,
                            authority_decision_id, supersedes_revision_id,
                            created_by_actor_type, created_by_actor_id, created_at
                        ) VALUES (?, ?, ?, ?, ?, 'OPERATOR_DECLARED', NULL, NULL,
                                  ?, 'OPERATOR', 'operator', ?)
                        """,
                        (
                            revision_id,
                            memory_id,
                            next_revision,
                            payload_id,
                            payload_hash,
                            str(current_row["revision_id"]),
                            now,
                        ),
                    )
                    self.memory._insert_provenance(
                        connection,
                        revision_id=revision_id,
                        items=({"kind": "OPERATOR", "id": "operator"},),
                        created_at=now,
                    )
                    self.memory._insert_links(
                        connection,
                        revision_id=revision_id,
                        items=links,
                        created_at=now,
                    )
                    connection.execute(
                        """
                        UPDATE project_memories
                        SET current_revision_id=?, current_revision_number=?,
                            resource_revision=resource_revision+1, updated_at=?
                        WHERE memory_id=?
                        """,
                        (revision_id, next_revision, now, memory_id),
                    )
                    current = self.memory._projection(
                        self.memory._current_row(connection, memory_id)
                    )
                    operation_id = "operation-" + uuid.uuid4().hex
                    result = self._result_metadata(
                        current,
                        deduplicated=False,
                        revision_created=True,
                    )
                    operation = self._record_operation(
                        connection,
                        operation_id=operation_id,
                        kind="memory.revise",
                        memory_id=memory_id,
                        result=result,
                        now=now,
                    )
                    self._audit(
                        connection,
                        operation_id=operation_id,
                        action="memory.revise",
                        memory_id=memory_id,
                        project_id=str(current["project_id"]),
                        session_fp=session_fp,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        reason_present=False,
                        now=now,
                    )
                    self._emit(
                        connection,
                        event_type="memory.revised",
                        memory=current,
                        operation_id=operation_id,
                        data=result,
                        now=now,
                    )
                    payload = {
                        "data": operation,
                        "meta": meta_factory(int(current["resource_revision"])),
                    }
                    self._complete_idempotency(
                        connection,
                        session_fp=session_fp,
                        key_hash=key_hash,
                        status=202,
                        payload=payload,
                        operation_id=operation_id,
                        now=now,
                    )
                    connection.commit()
                    return 202, payload
                except Exception:
                    connection.rollback()
                    raise
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise self._integrity_error(error) from error
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error

    def command_memory(
        self,
        *,
        session_token: str,
        idempotency_key: str,
        route: str,
        memory_id: str,
        command: str,
        if_match: str | None,
        body: dict[str, Any],
        meta_factory: Callable[[int | None], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        self.validate_idempotency_key(idempotency_key)
        memory_id = self._memory_id(memory_id)
        now = utc_now()
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    replay, session_fp, key_hash, request_hash = self._replay_or_reserve(
                        connection,
                        session_token=session_token,
                        idempotency_key=idempotency_key,
                        method="POST",
                        route=route,
                        body=body,
                        if_match=if_match,
                    )
                    if replay is not None:
                        connection.commit()
                        return 202, replay
                    expected = self._parse_if_match(if_match)
                    if command not in {"retract", "redact"}:
                        raise ControllerError(
                            409,
                            "memory_command_unavailable",
                            "Project memory command unavailable",
                            "Only retract and redact are supported; delete and reactivation are unavailable.",
                        )
                    reason = self._reason_body(body)
                    current_row = self.memory._current_row(connection, memory_id)
                    if int(current_row["resource_revision"]) != expected:
                        raise ControllerError(
                            409,
                            "resource_revision_conflict",
                            "Project memory revision conflict",
                            "The project memory changed since the supplied revision.",
                            resource={"type": "memory", "id": memory_id},
                        )
                    current_state = str(current_row["state"])
                    if command == "retract":
                        if current_state != "ACTIVE":
                            raise ControllerError(
                                409,
                                "memory_terminal",
                                "Project memory is terminal",
                                "Only active memory can be retracted.",
                            )
                        connection.execute(
                            """
                            UPDATE project_memories
                            SET state='RETRACTED', resource_revision=resource_revision+1,
                                updated_at=?
                            WHERE memory_id=?
                            """,
                            (now, memory_id),
                        )
                        event_type = "memory.retracted"
                    else:
                        if current_state == "REDACTED":
                            raise ControllerError(
                                409,
                                "memory_terminal",
                                "Project memory is terminal",
                                "Redacted memory cannot be mutated.",
                            )
                        connection.execute(
                            """
                            UPDATE project_memory_payloads
                            SET memory_key=NULL, title=NULL, content='', state='REDACTED',
                                redacted_at=?, redaction_code='operator_security_redaction'
                            WHERE payload_id IN (
                                SELECT payload_id FROM project_memory_revisions
                                WHERE memory_id=?
                            ) AND state='AVAILABLE'
                            """,
                            (now, memory_id),
                        )
                        connection.execute(
                            """
                            UPDATE project_memories
                            SET state='REDACTED', resource_revision=resource_revision+1,
                                updated_at=?
                            WHERE memory_id=?
                            """,
                            (now, memory_id),
                        )
                        event_type = "memory.redacted"
                    current = self.memory._projection(
                        self.memory._current_row(connection, memory_id)
                    )
                    operation_id = "operation-" + uuid.uuid4().hex
                    result = self._result_metadata(
                        current,
                        deduplicated=False,
                        revision_created=False,
                    )
                    kind = f"memory.{command}"
                    operation = self._record_operation(
                        connection,
                        operation_id=operation_id,
                        kind=kind,
                        memory_id=memory_id,
                        result=result,
                        now=now,
                    )
                    self._audit(
                        connection,
                        operation_id=operation_id,
                        action=kind,
                        memory_id=memory_id,
                        project_id=str(current["project_id"]),
                        session_fp=session_fp,
                        key_hash=key_hash,
                        request_hash=request_hash,
                        reason_present=reason is not None,
                        now=now,
                    )
                    self._emit(
                        connection,
                        event_type=event_type,
                        memory=current,
                        operation_id=operation_id,
                        data={**result, "reason_present": reason is not None},
                        now=now,
                    )
                    payload = {
                        "data": operation,
                        "meta": meta_factory(int(current["resource_revision"])),
                    }
                    self._complete_idempotency(
                        connection,
                        session_fp=session_fp,
                        key_hash=key_hash,
                        status=202,
                        payload=payload,
                        operation_id=operation_id,
                        now=now,
                    )
                    connection.commit()
                    return 202, payload
                except Exception:
                    connection.rollback()
                    raise
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise self._integrity_error(error) from error
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error

    @staticmethod
    def _decode_base64url(segment: str) -> bytes:
        if (
            not isinstance(segment, str)
            or not segment
            or len(segment) % 4 == 1
            or BASE64URL_PATTERN.fullmatch(segment) is None
        ):
            raise ValueError("base64url shape")
        decoded = base64.b64decode(
            segment + "=" * (-len(segment) % 4),
            altchars=b"-_",
            validate=True,
        )
        canonical = base64.urlsafe_b64encode(decoded).rstrip(b"=").decode("ascii")
        if not hmac.compare_digest(segment, canonical):
            raise ValueError("noncanonical base64url")
        return decoded

    @staticmethod
    def _cursor_key(cursor_secret: str) -> bytes:
        return hmac.new(
            cursor_secret.encode("ascii"),
            b"orchestra-project-memory-cursor-v1",
            hashlib.sha256,
        ).digest()

    @classmethod
    def _encode_cursor(
        cls,
        *,
        memory_id: str,
        project_id: str,
        state: str | None,
        kind: str | None,
        scope: str | None,
        objective_id: str | None,
        cursor_secret: str,
    ) -> str:
        encoded = canonical_json(
            {
                "v": 1,
                "memory_id": memory_id,
                "project_id": project_id,
                "state": state,
                "kind": kind,
                "scope": scope,
                "objective_id": objective_id,
            }
        ).encode("utf-8")
        signature = hmac.new(cls._cursor_key(cursor_secret), encoded, hashlib.sha256).digest()
        return (
            base64.urlsafe_b64encode(encoded).rstrip(b"=").decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    @classmethod
    def _decode_cursor(
        cls,
        cursor: str,
        *,
        project_id: str,
        state: str | None,
        kind: str | None,
        scope: str | None,
        objective_id: str | None,
        cursor_secret: str,
    ) -> str:
        try:
            if not 16 <= len(cursor) <= MAX_CURSOR_BYTES or cursor.count(".") != 1:
                raise ValueError("cursor length")
            left, right = cursor.split(".", 1)
            payload = cls._decode_base64url(left)
            signature = cls._decode_base64url(right)
            expected = hmac.new(cls._cursor_key(cursor_secret), payload, hashlib.sha256).digest()
            if len(signature) != hashlib.sha256().digest_size or not hmac.compare_digest(
                signature, expected
            ):
                raise ValueError("cursor signature")
            parsed = json.loads(payload.decode("utf-8"))
            if (
                not isinstance(parsed, dict)
                or set(parsed)
                != {"v", "memory_id", "project_id", "state", "kind", "scope", "objective_id"}
                or parsed.get("v") != 1
                or parsed.get("project_id") != project_id
                or parsed.get("state") != state
                or parsed.get("kind") != kind
                or parsed.get("scope") != scope
                or parsed.get("objective_id") != objective_id
                or canonical_json(parsed).encode("utf-8") != payload
            ):
                raise ValueError("cursor shape")
            return cls._memory_id(parsed.get("memory_id"))
        except (ControllerError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
                "The project memory cursor is invalid or no longer applicable.",
            ) from error

    @staticmethod
    def _validate_filters(
        *,
        state: str | None,
        kind: str | None,
        scope: str | None,
        objective_id: str | None,
    ) -> None:
        if state is not None and state not in MEMORY_STATES:
            raise ControllerError(400, "invalid_memory_state", "Invalid project memory state")
        if kind is not None and kind not in MEMORY_KINDS:
            raise ControllerError(400, "invalid_memory_kind", "Invalid project memory kind")
        if scope is not None and scope not in {"PROJECT", "OBJECTIVE"}:
            raise ControllerError(400, "invalid_memory_scope", "Invalid project memory scope")
        if objective_id is not None:
            if scope != "OBJECTIVE" or not isinstance(objective_id, str) or not 1 <= len(objective_id) <= 200:
                raise ControllerError(400, "invalid_memory_scope", "Invalid project memory scope")

    @classmethod
    def _public_projection(cls, projected: dict[str, Any], *, include_content: bool) -> dict[str, Any]:
        try:
            memory_id = cls._memory_id(projected.get("id"))
        except ControllerError as error:
            raise ControllerError(
                503,
                "project_memory_projection_failed",
                "Project memory projection failed",
                "Persisted project memory identity is outside the public API bounds.",
            ) from error
        result = dict(projected)
        result["id"] = memory_id
        result.pop("payload_sha256", None)
        if not include_content:
            result.pop("content", None)
        return result

    @staticmethod
    def _persisted_reference(value: Any, *, maximum_bytes: int) -> str:
        try:
            safe = ProjectMemoryStore._persisted_text(
                value,
                maximum_bytes=maximum_bytes,
                required=True,
                allow_newline=False,
            )
            if safe is None:
                raise ValueError("missing persisted reference")
            return safe
        except (ControllerError, TypeError, ValueError) as error:
            raise ControllerError(
                503,
                "project_memory_projection_failed",
                "Project memory projection failed",
                "Persisted project memory references are malformed or unsafe to expose.",
            ) from error

    @staticmethod
    def _source_identifier(row: sqlite3.Row, kind: str) -> str:
        column = {
            "OPERATOR": "source_actor_id",
            "CONTROL_PLANE": "source_actor_id",
            "OBJECTIVE": "objective_id",
            "TASK": "task_id",
            "ATTEMPT": "attempt_id",
            "RUN": "run_id",
            "REVIEW": "review_id",
            "JUDGE_DECISION": "judge_decision_id",
            "RECOVERY": "recovery_action_id",
            "LEGACY_CONTEXT": "legacy_context_id",
            "LEGACY_MEMORY": "legacy_memory_id",
        }.get(kind)
        if column is None or row[column] is None:
            raise ControllerError(
                503,
                "project_memory_projection_failed",
                "Project memory projection failed",
                "Persisted project memory provenance is malformed.",
            )
        return MemoryCommandStore._persisted_reference(
            row[column], maximum_bytes=256
        )

    @classmethod
    def _provenance(
        cls, connection: sqlite3.Connection, revision_id: str
    ) -> list[dict[str, str]]:
        rows = connection.execute(
            """
            SELECT source_kind, source_actor_id, objective_id, task_id, attempt_id,
                   run_id, review_id, judge_decision_id, recovery_action_id,
                   legacy_context_id, legacy_memory_id
            FROM project_memory_provenance
            WHERE revision_id=?
            ORDER BY created_at, provenance_id
            """,
            (revision_id,),
        ).fetchall()
        return [
            {"kind": str(row["source_kind"]), "id": cls._source_identifier(row, str(row["source_kind"]))}
            for row in rows
        ]

    @staticmethod
    def _link_identifier(row: sqlite3.Row, kind: str) -> str:
        column = {
            "OBJECTIVE": "objective_id",
            "TASK": "task_id",
            "ATTEMPT": "attempt_id",
            "RUN": "run_id",
            "REVIEW": "review_id",
            "JUDGE_DECISION": "judge_decision_id",
            "RECOVERY": "recovery_action_id",
        }.get(kind)
        if column is None or row[column] is None:
            raise ControllerError(
                503,
                "project_memory_projection_failed",
                "Project memory projection failed",
                "Persisted project memory links are malformed.",
            )
        return MemoryCommandStore._persisted_reference(
            row[column], maximum_bytes=200
        )

    @classmethod
    def _links(
        cls, connection: sqlite3.Connection, revision_id: str
    ) -> list[dict[str, str]]:
        rows = connection.execute(
            """
            SELECT link_kind, objective_id, task_id, attempt_id, run_id,
                   review_id, judge_decision_id, recovery_action_id
            FROM project_memory_revision_links
            WHERE revision_id=?
            ORDER BY created_at, link_id
            """,
            (revision_id,),
        ).fetchall()
        return [
            {"kind": str(row["link_kind"]), "id": cls._link_identifier(row, str(row["link_kind"]))}
            for row in rows
        ]

    @staticmethod
    def _select_revision_sql() -> str:
        return """
            SELECT
                memory.memory_id,
                memory.project_id,
                memory.scope,
                memory.objective_id,
                memory.state,
                revision.revision_number AS current_revision_number,
                memory.resource_revision,
                memory.created_at,
                memory.updated_at,
                revision.revision_id,
                revision.payload_sha256,
                revision.authority,
                revision.created_by_actor_type,
                revision.created_by_actor_id,
                revision.authority_review_id,
                revision.authority_decision_id,
                revision.created_at AS revision_created_at,
                payload.payload_id,
                payload.kind,
                payload.memory_key,
                payload.title,
                payload.content,
                payload.state AS payload_state
            FROM project_memories AS memory
            JOIN project_memory_revisions AS revision
              ON revision.memory_id=memory.memory_id
            JOIN project_memory_payloads AS payload
              ON payload.payload_id=revision.payload_id
        """

    def _decorate(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_content: bool,
        include_references: bool,
    ) -> dict[str, Any]:
        projected = self._public_projection(
            self.memory._projection(row),
            include_content=include_content,
        )
        projected["revision_created_at"] = str(row["revision_created_at"])
        if include_references:
            revision_id = str(row["revision_id"])
            projected["provenance"] = self._provenance(connection, revision_id)
            projected["links"] = self._links(connection, revision_id)
        return projected

    def list_memories(
        self,
        *,
        project_id: str,
        limit: int,
        cursor: str | None,
        state: str | None,
        kind: str | None,
        scope: str | None,
        objective_id: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if PROJECT_ID_PATTERN.fullmatch(project_id) is None:
            raise ControllerError(400, "invalid_project_id", "Invalid project identifier")
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ControllerError(400, "invalid_limit", "Invalid pagination limit")
        self._validate_filters(
            state=state,
            kind=kind,
            scope=scope,
            objective_id=objective_id,
        )
        after_id = None
        if cursor is not None:
            after_id = self._decode_cursor(
                cursor,
                project_id=project_id,
                state=state,
                kind=kind,
                scope=scope,
                objective_id=objective_id,
                cursor_secret=cursor_secret,
            )
        try:
            with closing(self.memory.connect()) as connection:
                if connection.execute(
                    "SELECT 1 FROM projects WHERE project_id=?", (project_id,)
                ).fetchone() is None:
                    raise ControllerError(
                        404,
                        "project_not_found",
                        "Project not found",
                        resource={"type": "project", "id": project_id},
                    )
                clauses = ["memory.project_id=?"]
                parameters: list[Any] = [project_id]
                if state is not None:
                    clauses.append("memory.state=?")
                    parameters.append(state)
                if kind is not None:
                    clauses.append("payload.kind=?")
                    parameters.append(kind)
                if scope is not None:
                    clauses.append("memory.scope=?")
                    parameters.append(scope)
                if objective_id is not None:
                    clauses.append("memory.objective_id=?")
                    parameters.append(objective_id)
                if after_id is not None:
                    clauses.append("memory.memory_id>?")
                    parameters.append(after_id)
                sql = self.memory._select_current_sql()
                sql += " WHERE " + " AND ".join(clauses)
                sql += " ORDER BY memory.memory_id LIMIT ?"
                parameters.append(limit + 1)
                rows = connection.execute(sql, parameters).fetchall()
                selected = rows[:limit]
                items = [
                    self._decorate(
                        connection,
                        row,
                        include_content=False,
                        include_references=False,
                    )
                    for row in selected
                ]
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error
        next_cursor = None
        if len(rows) > limit and selected:
            next_cursor = self._encode_cursor(
                memory_id=str(selected[-1]["memory_id"]),
                project_id=project_id,
                state=state,
                kind=kind,
                scope=scope,
                objective_id=objective_id,
                cursor_secret=cursor_secret,
            )
        return items, next_cursor

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        memory_id = self._memory_id(memory_id)
        try:
            with closing(self.memory.connect()) as connection:
                row = self.memory._current_row(connection, memory_id)
                return self._decorate(
                    connection,
                    row,
                    include_content=True,
                    include_references=True,
                )
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error

    def list_revisions(self, memory_id: str, *, limit: int) -> list[dict[str, Any]]:
        memory_id = self._memory_id(memory_id)
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ControllerError(400, "invalid_limit", "Invalid pagination limit")
        try:
            with closing(self.memory.connect()) as connection:
                if connection.execute(
                    "SELECT 1 FROM project_memories WHERE memory_id=?", (memory_id,)
                ).fetchone() is None:
                    raise ControllerError(
                        404,
                        "project_memory_not_found",
                        "Project memory not found",
                        resource={"type": "memory", "id": memory_id},
                    )
                rows = connection.execute(
                    self._select_revision_sql()
                    + " WHERE memory.memory_id=? ORDER BY revision.revision_number DESC LIMIT ?",
                    (memory_id, limit),
                ).fetchall()
                return [
                    self._decorate(
                        connection,
                        row,
                        include_content=False,
                        include_references=False,
                    )
                    for row in rows
                ]
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error

    def get_revision(self, memory_id: str, revision: int) -> dict[str, Any]:
        memory_id = self._memory_id(memory_id)
        revision = self._revision_number(revision)
        try:
            with closing(self.memory.connect()) as connection:
                row = connection.execute(
                    self._select_revision_sql()
                    + " WHERE memory.memory_id=? AND revision.revision_number=?",
                    (memory_id, revision),
                ).fetchone()
                if row is None:
                    if connection.execute(
                        "SELECT 1 FROM project_memories WHERE memory_id=?", (memory_id,)
                    ).fetchone() is None:
                        raise ControllerError(
                            404,
                            "project_memory_not_found",
                            "Project memory not found",
                            resource={"type": "memory", "id": memory_id},
                        )
                    raise ControllerError(
                        404,
                        "project_memory_revision_not_found",
                        "Project memory revision not found",
                        resource={"type": "memory", "id": memory_id},
                    )
                return self._decorate(
                    connection,
                    row,
                    include_content=True,
                    include_references=True,
                )
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            return None
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM controller_memory_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self.memory._database_error(error) from error
        if row is None:
            return None
        return self._operation_payload(row)
