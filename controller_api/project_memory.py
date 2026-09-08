from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from .core import ControllerError, Settings
from .persistence_secrets import contains_credential_like


MEMORY_KINDS = {
    "FACT", "CONSTRAINT", "DECISION", "ASSUMPTION",
    "FINDING", "RESULT", "REFERENCE", "NOTE",
}
MEMORY_AUTHORITIES = {
    "OPERATOR_DECLARED", "CONTROL_PLANE_OBSERVED", "REVIEW_ACCEPTED",
    "AGENT_PROPOSED", "LEGACY_UNVERIFIED",
}
MEMORY_STATES = {"ACTIVE", "RETRACTED", "REDACTED"}
ACTOR_TYPES = {"OPERATOR", "CONTROL_PLANE", "AGENT", "MIGRATION"}
MAX_KEY_BYTES = 512
MAX_TITLE_BYTES = 1024
MAX_CONTENT_BYTES = 16384


@dataclass(frozen=True)
class MemoryMutationResult:
    memory: dict[str, Any]
    deduplicated: bool
    revision_created: bool


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


class ProjectMemoryStore:
    REQUIRED_TABLES = {
        "project_memories",
        "project_memory_payloads",
        "project_memory_revisions",
        "project_memory_provenance",
        "project_memory_revision_links",
        "schema_migrations",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def connect(self, *, write: bool = False) -> sqlite3.Connection:
        try:
            if write:
                connection = sqlite3.connect(
                    self.settings.database,
                    timeout=10,
                    isolation_level=None,
                    check_same_thread=False,
                )
            else:
                uri = f"{self.settings.database.as_uri()}?mode=ro"
                connection = sqlite3.connect(
                    uri,
                    uri=True,
                    timeout=5,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA query_only = ON")
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            return connection
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "project_memory_unavailable",
                "Project memory unavailable",
                "The project memory store cannot be opened.",
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
                    return False, "project memory tables are missing"
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 32:
                    return False, "structured project memory migration is not installed"
                connection.execute(
                    "SELECT memory_id, current_revision_id FROM project_memories LIMIT 1"
                ).fetchone()
        except (sqlite3.Error, ControllerError, TypeError, ValueError):
            return False, "project memory store cannot be read"
        return True, "ready"

    @staticmethod
    def _database_error(error: sqlite3.Error) -> ControllerError:
        return ControllerError(
            503,
            "project_memory_unavailable",
            "Project memory unavailable",
            "The project memory store cannot serve this request.",
        )

    @staticmethod
    def _normalize(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @classmethod
    def _safe_text(
        cls,
        value: Any,
        *,
        field: str,
        maximum_bytes: int,
        required: bool,
        allow_newline: bool,
    ) -> str | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise ControllerError(
                400,
                "invalid_memory_payload",
                "Invalid project memory payload",
                f"{field} must be text.",
            )
        value = cls._normalize(value)
        encoded = value.encode("utf-8")
        if (required and not value) or len(encoded) > maximum_bytes:
            raise ControllerError(
                400,
                "invalid_memory_payload",
                "Invalid project memory payload",
                f"{field} is outside the supported size bounds.",
            )
        allowed_controls = {"\n", "\t"} if allow_newline else set()
        if any(
            (ord(character) < 32 and character not in allowed_controls)
            or ord(character) == 127
            for character in value
        ):
            raise ControllerError(
                400,
                "invalid_memory_payload",
                "Invalid project memory payload",
                f"{field} contains unsupported control characters.",
            )
        if not allow_newline and "\n" in value:
            raise ControllerError(
                400,
                "invalid_memory_payload",
                "Invalid project memory payload",
                f"{field} must be single-line text.",
            )
        if contains_credential_like(encoded):
            raise ControllerError(
                400,
                "memory_secret_detected",
                "Project memory is not persistence-eligible",
                "Credential-like material was detected and was not stored.",
            )
        return value

    @classmethod
    def _payload(
        cls,
        *,
        kind: str,
        memory_key: str | None,
        title: str | None,
        content: str,
    ) -> tuple[str, str | None, str | None, str, str]:
        if kind not in MEMORY_KINDS:
            raise ControllerError(
                400,
                "invalid_memory_kind",
                "Invalid project memory kind",
                "The requested memory kind is not supported.",
            )
        key = cls._safe_text(
            memory_key,
            field="memory_key",
            maximum_bytes=MAX_KEY_BYTES,
            required=False,
            allow_newline=False,
        )
        safe_title = cls._safe_text(
            title,
            field="title",
            maximum_bytes=MAX_TITLE_BYTES,
            required=False,
            allow_newline=False,
        )
        safe_content = cls._safe_text(
            content,
            field="content",
            maximum_bytes=MAX_CONTENT_BYTES,
            required=True,
            allow_newline=True,
        )
        assert safe_content is not None
        canonical = json.dumps(
            {
                "content": safe_content,
                "kind": kind,
                "memory_key": key,
                "title": safe_title,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        return (
            kind,
            key,
            safe_title,
            safe_content,
            hashlib.sha256(canonical).hexdigest(),
        )

    @staticmethod
    def _validate_actor(*, authority: str, actor_type: str, actor_id: str) -> None:
        expected = {
            "OPERATOR_DECLARED": "OPERATOR",
            "CONTROL_PLANE_OBSERVED": "CONTROL_PLANE",
            "REVIEW_ACCEPTED": "CONTROL_PLANE",
            "AGENT_PROPOSED": "AGENT",
            "LEGACY_UNVERIFIED": "MIGRATION",
        }
        if (
            authority not in MEMORY_AUTHORITIES
            or actor_type not in ACTOR_TYPES
            or expected.get(authority) != actor_type
            or not isinstance(actor_id, str)
            or not 1 <= len(actor_id) <= 256
            or any(ord(character) < 32 or ord(character) == 127 for character in actor_id)
        ):
            raise ControllerError(
                400,
                "invalid_memory_authority",
                "Invalid project memory authority",
                "The authority is not valid for the writing actor.",
            )
        if contains_credential_like(actor_id.encode("utf-8")):
            raise ControllerError(
                400,
                "memory_secret_detected",
                "Project memory is not persistence-eligible",
                "Credential-like material was detected and was not stored.",
            )

    @staticmethod
    def _scope_exists(
        connection: sqlite3.Connection,
        *,
        project_id: str,
        scope: str,
        objective_id: str | None,
    ) -> None:
        if connection.execute(
            "SELECT 1 FROM projects WHERE project_id=?",
            (project_id,),
        ).fetchone() is None:
            raise ControllerError(
                404,
                "project_not_found",
                "Project not found",
                "The target project does not exist.",
                resource={"type": "project", "id": project_id},
            )
        if scope == "PROJECT" and objective_id is None:
            return
        if scope != "OBJECTIVE" or objective_id is None:
            raise ControllerError(
                400,
                "invalid_memory_scope",
                "Invalid project memory scope",
                "Memory scope must be PROJECT or a project-owned OBJECTIVE.",
            )
        objective = connection.execute(
            """
            SELECT 1
            FROM objective_queue AS objective
            JOIN json_each(objective.project_scope_json) AS scope
              ON scope.value = ?
            WHERE objective.objective_id = ?
            LIMIT 1
            """,
            (project_id, objective_id),
        ).fetchone()
        if objective is None:
            raise ControllerError(
                400,
                "invalid_memory_scope",
                "Invalid project memory scope",
                "The objective is not scoped to the target project.",
            )

    @staticmethod
    def _validate_review_authority(
        connection: sqlite3.Connection,
        *,
        authority: str,
        project_id: str,
        review_id: str | None,
        decision_id: str | None,
    ) -> None:
        if authority != "REVIEW_ACCEPTED":
            if review_id is not None or decision_id is not None:
                raise ControllerError(
                    400,
                    "invalid_memory_authority",
                    "Invalid project memory authority",
                    "Review evidence is only valid for REVIEW_ACCEPTED memory.",
                )
            return
        if review_id is None or decision_id is None:
            raise ControllerError(
                400,
                "invalid_memory_authority",
                "Invalid project memory authority",
                "REVIEW_ACCEPTED memory requires review and Judge evidence.",
            )
        accepted = connection.execute(
            """
            SELECT 1
            FROM task_reviews AS review
            JOIN judge_decisions AS decision
              ON decision.review_id = review.review_id
            LEFT JOIN approvals AS approval
              ON approval.approval_id = decision.approval_id
            WHERE review.review_id = ?
              AND decision.decision_id = ?
              AND review.project_id = ?
              AND review.status = 'COMPLETED'
              AND (
                  decision.disposition = 'PASS'
                  OR (
                      decision.disposition = 'HUMAN_REVIEW'
                      AND approval.status = 'APPROVED'
                  )
              )
            """,
            (review_id, decision_id, project_id),
        ).fetchone()
        if accepted is None:
            raise ControllerError(
                400,
                "invalid_memory_authority",
                "Invalid project memory authority",
                "The supplied review evidence did not accept this project result.",
            )

    @staticmethod
    def _select_current_sql() -> str:
        return """
            SELECT
                memory.memory_id,
                memory.project_id,
                memory.scope,
                memory.objective_id,
                memory.state,
                memory.current_revision_number,
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
              ON revision.revision_id = memory.current_revision_id
             AND revision.memory_id = memory.memory_id
             AND revision.revision_number = memory.current_revision_number
            JOIN project_memory_payloads AS payload
              ON payload.payload_id = revision.payload_id
        """

    @staticmethod
    def _persisted_text(
        value: Any,
        *,
        maximum_bytes: int,
        required: bool,
        allow_newline: bool,
    ) -> str | None:
        if value is None:
            if required:
                raise ValueError("missing persisted text")
            return None
        if not isinstance(value, str):
            raise ValueError("persisted text is not text")
        encoded = value.encode("utf-8")
        if (required and not value) or len(encoded) > maximum_bytes:
            raise ValueError("persisted text is outside supported bounds")
        allowed_controls = {"\n", "\r", "\t"} if allow_newline else set()
        if any(
            (ord(character) < 32 and character not in allowed_controls)
            or ord(character) == 127
            for character in value
        ):
            raise ValueError("persisted text contains unsupported controls")
        if not allow_newline and ("\n" in value or "\r" in value):
            raise ValueError("persisted single-line text contains a newline")
        if contains_credential_like(encoded):
            raise ValueError("unsafe persisted payload")
        return value

    @classmethod
    def _projection(cls, row: sqlite3.Row) -> dict[str, Any]:
        try:
            state = str(row["state"])
            payload_state = str(row["payload_state"])
            kind = str(row["kind"])
            authority = str(row["authority"])
            revision_number = int(row["current_revision_number"])
            resource_revision = int(row["resource_revision"])
            stored_hash = row["payload_sha256"]
            if (
                state not in MEMORY_STATES
                or kind not in MEMORY_KINDS
                or authority not in MEMORY_AUTHORITIES
                or revision_number < 1
                or resource_revision < 1
            ):
                raise ValueError("invalid persisted metadata")
            if stored_hash is not None and (
                not isinstance(stored_hash, str)
                or len(stored_hash) != 64
                or any(character not in "0123456789abcdef" for character in stored_hash)
            ):
                raise ValueError("invalid persisted payload hash")
            if state == "REDACTED":
                if (
                    payload_state != "REDACTED"
                    or row["memory_key"] is not None
                    or row["title"] is not None
                    or row["content"] != ""
                ):
                    raise ValueError("redaction mismatch")
                key = title = content = None
            else:
                if payload_state != "AVAILABLE":
                    raise ValueError("payload availability mismatch")
                key = cls._persisted_text(
                    row["memory_key"],
                    maximum_bytes=MAX_KEY_BYTES,
                    required=False,
                    allow_newline=False,
                )
                title = cls._persisted_text(
                    row["title"],
                    maximum_bytes=MAX_TITLE_BYTES,
                    required=False,
                    allow_newline=False,
                )
                content = cls._persisted_text(
                    row["content"],
                    maximum_bytes=MAX_CONTENT_BYTES,
                    required=stored_hash is not None,
                    allow_newline=True,
                )
                if stored_hash is not None:
                    _, _, _, _, computed_hash = cls._payload(
                        kind=kind,
                        memory_key=key,
                        title=title,
                        content=content,
                    )
                    if computed_hash != stored_hash:
                        raise ValueError("persisted payload hash mismatch")
        except (ControllerError, KeyError, TypeError, ValueError) as error:
            raise ControllerError(
                503,
                "project_memory_projection_failed",
                "Project memory projection failed",
                "Persisted project memory is malformed or unsafe to expose.",
            ) from error
        return {
            "id": str(row["memory_id"]),
            "project_id": str(row["project_id"]),
            "scope": str(row["scope"]),
            "objective_id": (
                None if row["objective_id"] is None else str(row["objective_id"])
            ),
            "state": state,
            "kind": kind,
            "memory_key": None if key is None else str(key),
            "title": None if title is None else str(title),
            "content": None if content is None else str(content),
            "authority": authority,
            "revision": revision_number,
            "resource_revision": resource_revision,
            "payload_sha256": (
                None
                if row["payload_sha256"] is None
                else str(row["payload_sha256"])
            ),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    @staticmethod
    def _expected_revision(value: int) -> int:
        if type(value) is not int or value < 1:
            raise ControllerError(
                400,
                "invalid_resource_revision",
                "Invalid resource revision",
                "The expected resource revision must be a positive integer.",
            )
        return value

    def _current_row(
        self,
        connection: sqlite3.Connection,
        memory_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            self._select_current_sql() + " WHERE memory.memory_id=?",
            (memory_id,),
        ).fetchone()
        if row is None:
            header_exists = connection.execute(
                "SELECT 1 FROM project_memories WHERE memory_id=?",
                (memory_id,),
            ).fetchone()
            if header_exists is not None:
                raise ControllerError(
                    503,
                    "project_memory_projection_failed",
                    "Project memory projection failed",
                    "Persisted project memory is malformed or unsafe to expose.",
                )
            raise ControllerError(
                404,
                "project_memory_not_found",
                "Project memory not found",
                "No project memory exists with that identifier.",
                resource={"type": "memory", "id": memory_id},
            )
        return row

    def get_memory(self, memory_id: str) -> dict[str, Any]:
        try:
            with closing(self.connect()) as connection:
                return self._projection(self._current_row(connection, memory_id))
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise self._database_error(error) from error

    @staticmethod
    def _entity_column(kind: str) -> str | None:
        return {
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

    def _insert_provenance(
        self,
        connection: sqlite3.Connection,
        *,
        revision_id: str,
        items: Iterable[dict[str, str]],
        created_at: str,
    ) -> int:
        count = 0
        for item in items:
            kind = item.get("kind")
            source_id = item.get("id")
            if not isinstance(kind, str) or not isinstance(source_id, str) or not source_id:
                raise ControllerError(
                    400,
                    "invalid_memory_provenance",
                    "Invalid project memory provenance",
                    "Each provenance item requires a supported kind and identifier.",
                )
            values: dict[str, str | None] = {
                "source_actor_id": None,
                "objective_id": None,
                "task_id": None,
                "attempt_id": None,
                "run_id": None,
                "review_id": None,
                "judge_decision_id": None,
                "recovery_action_id": None,
                "legacy_context_id": None,
                "legacy_memory_id": None,
            }
            if kind in {"OPERATOR", "CONTROL_PLANE"}:
                if (
                    len(source_id) > 256
                    or any(
                        ord(character) < 32 or ord(character) == 127
                        for character in source_id
                    )
                ):
                    raise ControllerError(
                        400,
                        "invalid_memory_provenance",
                        "Invalid project memory provenance",
                        "Free-form provenance identifiers must be bounded printable text.",
                    )
                if contains_credential_like(source_id.encode("utf-8")):
                    raise ControllerError(
                        400,
                        "memory_secret_detected",
                        "Project memory is not persistence-eligible",
                        "Credential-like material was detected and was not stored.",
                    )
                values["source_actor_id"] = source_id
            else:
                column = self._entity_column(kind)
                if column is None:
                    raise ControllerError(
                        400,
                        "invalid_memory_provenance",
                        "Invalid project memory provenance",
                        "The provenance kind is not supported.",
                    )
                values[column] = source_id
            connection.execute(
                """
                INSERT INTO project_memory_provenance (
                    provenance_id, revision_id, source_kind, source_actor_id,
                    objective_id, task_id, attempt_id, run_id, review_id,
                    judge_decision_id, recovery_action_id,
                    legacy_context_id, legacy_memory_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "memory-provenance-" + secrets.token_hex(16),
                    revision_id,
                    kind,
                    values["source_actor_id"],
                    values["objective_id"],
                    values["task_id"],
                    values["attempt_id"],
                    values["run_id"],
                    values["review_id"],
                    values["judge_decision_id"],
                    values["recovery_action_id"],
                    values["legacy_context_id"],
                    values["legacy_memory_id"],
                    created_at,
                ),
            )
            count += 1
        return count

    def _insert_links(
        self,
        connection: sqlite3.Connection,
        *,
        revision_id: str,
        items: Iterable[dict[str, str]],
        created_at: str,
    ) -> int:
        count = 0
        allowed = {
            "OBJECTIVE", "TASK", "ATTEMPT", "RUN",
            "REVIEW", "JUDGE_DECISION", "RECOVERY",
        }
        for item in items:
            kind = item.get("kind")
            source_id = item.get("id")
            if (
                not isinstance(kind, str)
                or kind not in allowed
                or not isinstance(source_id, str)
                or not source_id
            ):
                raise ControllerError(
                    400,
                    "invalid_memory_link",
                    "Invalid project memory link",
                    "Each link requires a supported kind and identifier.",
                )
            column = self._entity_column(kind)
            assert column is not None
            values: dict[str, str | None] = {
                "objective_id": None,
                "task_id": None,
                "attempt_id": None,
                "run_id": None,
                "review_id": None,
                "judge_decision_id": None,
                "recovery_action_id": None,
            }
            values[column] = source_id
            connection.execute(
                """
                INSERT INTO project_memory_revision_links (
                    link_id, revision_id, link_kind, objective_id, task_id,
                    attempt_id, run_id, review_id, judge_decision_id,
                    recovery_action_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "memory-link-" + secrets.token_hex(16),
                    revision_id,
                    kind,
                    values["objective_id"],
                    values["task_id"],
                    values["attempt_id"],
                    values["run_id"],
                    values["review_id"],
                    values["judge_decision_id"],
                    values["recovery_action_id"],
                    created_at,
                ),
            )
            count += 1
        return count

    def create_memory(
        self,
        *,
        project_id: str,
        scope: str,
        objective_id: str | None,
        kind: str,
        memory_key: str | None,
        title: str | None,
        content: str,
        authority: str,
        actor_type: str,
        actor_id: str,
        authority_review_id: str | None = None,
        authority_decision_id: str | None = None,
        provenance: Iterable[dict[str, str]] = (),
        links: Iterable[dict[str, str]] = (),
    ) -> MemoryMutationResult:
        kind, key, title, content, payload_sha256 = self._payload(
            kind=kind,
            memory_key=memory_key,
            title=title,
            content=content,
        )
        self._validate_actor(
            authority=authority,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        now = utc_now()
        provenance = tuple(provenance)
        links = tuple(links)
        try:
            with closing(self.connect(write=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                self._scope_exists(
                    connection,
                    project_id=project_id,
                    scope=scope,
                    objective_id=objective_id,
                )
                self._validate_review_authority(
                    connection,
                    authority=authority,
                    project_id=project_id,
                    review_id=authority_review_id,
                    decision_id=authority_decision_id,
                )
                existing = connection.execute(
                    self._select_current_sql()
                    + """
                      WHERE memory.project_id=?
                        AND memory.scope=?
                        AND memory.objective_id IS ?
                        AND memory.state='ACTIVE'
                        AND revision.payload_sha256=?
                        AND revision.authority=?
                        AND payload.state='AVAILABLE'
                      ORDER BY memory.memory_id
                      LIMIT 1
                    """,
                    (project_id, scope, objective_id, payload_sha256, authority),
                ).fetchone()
                if existing is not None:
                    additions = self._insert_provenance(
                        connection,
                        revision_id=str(existing["revision_id"]),
                        items=provenance,
                        created_at=now,
                    )
                    additions += self._insert_links(
                        connection,
                        revision_id=str(existing["revision_id"]),
                        items=links,
                        created_at=now,
                    )
                    if additions:
                        connection.execute(
                            """
                            UPDATE project_memories
                            SET resource_revision=resource_revision+1, updated_at=?
                            WHERE memory_id=?
                            """,
                            (now, str(existing["memory_id"])),
                        )
                        existing = self._current_row(
                            connection, str(existing["memory_id"])
                        )
                    result = MemoryMutationResult(
                        memory=self._projection(existing),
                        deduplicated=True,
                        revision_created=False,
                    )
                    connection.commit()
                    return result

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
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, NULL, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        memory_id,
                        payload_id,
                        payload_sha256,
                        authority,
                        authority_review_id,
                        authority_decision_id,
                        actor_type,
                        actor_id,
                        now,
                    ),
                )
                self._insert_provenance(
                    connection,
                    revision_id=revision_id,
                    items=provenance,
                    created_at=now,
                )
                self._insert_links(
                    connection,
                    revision_id=revision_id,
                    items=links,
                    created_at=now,
                )
                result = MemoryMutationResult(
                    memory=self._projection(self._current_row(connection, memory_id)),
                    deduplicated=False,
                    revision_created=True,
                )
                connection.commit()
                return result
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise ControllerError(
                503,
                "project_memory_persistence_failed",
                "Project memory persistence failed",
                "The project memory could not be persisted safely.",
            ) from error
        except sqlite3.Error as error:
            raise self._database_error(error) from error

    def revise_memory(
        self,
        memory_id: str,
        *,
        expected_resource_revision: int,
        kind: str,
        memory_key: str | None,
        title: str | None,
        content: str,
        authority: str,
        actor_type: str,
        actor_id: str,
        authority_review_id: str | None = None,
        authority_decision_id: str | None = None,
        provenance: Iterable[dict[str, str]] = (),
        links: Iterable[dict[str, str]] = (),
    ) -> MemoryMutationResult:
        expected = self._expected_revision(expected_resource_revision)
        kind, key, title, content, payload_sha256 = self._payload(
            kind=kind,
            memory_key=memory_key,
            title=title,
            content=content,
        )
        self._validate_actor(
            authority=authority,
            actor_type=actor_type,
            actor_id=actor_id,
        )
        now = utc_now()
        try:
            with closing(self.connect(write=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._current_row(connection, memory_id)
                if int(current["resource_revision"]) != expected:
                    raise ControllerError(
                        409,
                        "memory_revision_conflict",
                        "Project memory revision conflict",
                        "The project memory changed since the supplied revision.",
                    )
                if str(current["state"]) != "ACTIVE":
                    raise ControllerError(
                        409,
                        "memory_terminal",
                        "Project memory is terminal",
                        "Retracted or redacted memory cannot be revised.",
                    )
                self._validate_review_authority(
                    connection,
                    authority=authority,
                    project_id=str(current["project_id"]),
                    review_id=authority_review_id,
                    decision_id=authority_decision_id,
                )
                next_revision = int(current["current_revision_number"]) + 1
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
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        memory_id,
                        next_revision,
                        payload_id,
                        payload_sha256,
                        authority,
                        authority_review_id,
                        authority_decision_id,
                        str(current["revision_id"]),
                        actor_type,
                        actor_id,
                        now,
                    ),
                )
                self._insert_provenance(
                    connection,
                    revision_id=revision_id,
                    items=tuple(provenance),
                    created_at=now,
                )
                self._insert_links(
                    connection,
                    revision_id=revision_id,
                    items=tuple(links),
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
                result = MemoryMutationResult(
                    memory=self._projection(self._current_row(connection, memory_id)),
                    deduplicated=False,
                    revision_created=True,
                )
                connection.commit()
                return result
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise ControllerError(
                503,
                "project_memory_persistence_failed",
                "Project memory persistence failed",
                "The project memory could not be revised safely.",
            ) from error
        except sqlite3.Error as error:
            raise self._database_error(error) from error

    def retract_memory(
        self,
        memory_id: str,
        *,
        expected_resource_revision: int,
    ) -> dict[str, Any]:
        expected = self._expected_revision(expected_resource_revision)
        now = utc_now()
        try:
            with closing(self.connect(write=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._current_row(connection, memory_id)
                if int(current["resource_revision"]) != expected:
                    raise ControllerError(
                        409,
                        "memory_revision_conflict",
                        "Project memory revision conflict",
                        "The project memory changed since the supplied revision.",
                    )
                if str(current["state"]) != "ACTIVE":
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
                result = self._projection(self._current_row(connection, memory_id))
                connection.commit()
                return result
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise ControllerError(
                503,
                "project_memory_persistence_failed",
                "Project memory persistence failed",
                "The project memory could not be retracted safely.",
            ) from error
        except sqlite3.Error as error:
            raise self._database_error(error) from error

    def redact_memory(
        self,
        memory_id: str,
        *,
        expected_resource_revision: int,
        redaction_code: str = "credential_or_sensitive_content",
    ) -> dict[str, Any]:
        expected = self._expected_revision(expected_resource_revision)
        code = self._safe_text(
            redaction_code,
            field="redaction_code",
            maximum_bytes=128,
            required=True,
            allow_newline=False,
        )
        assert code is not None
        now = utc_now()
        try:
            with closing(self.connect(write=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._current_row(connection, memory_id)
                if int(current["resource_revision"]) != expected:
                    raise ControllerError(
                        409,
                        "memory_revision_conflict",
                        "Project memory revision conflict",
                        "The project memory changed since the supplied revision.",
                    )
                if str(current["state"]) == "REDACTED":
                    raise ControllerError(
                        409,
                        "memory_terminal",
                        "Project memory is terminal",
                        "Redacted memory cannot be mutated.",
                    )
                connection.execute(
                    """
                    UPDATE project_memory_payloads
                    SET memory_key=NULL, title=NULL, content='',
                        state='REDACTED', redacted_at=?, redaction_code=?
                    WHERE payload_id IN (
                        SELECT payload_id FROM project_memory_revisions
                        WHERE memory_id=?
                    )
                      AND state='AVAILABLE'
                    """,
                    (now, code, memory_id),
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
                result = self._projection(self._current_row(connection, memory_id))
                connection.commit()
                return result
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise ControllerError(
                503,
                "project_memory_persistence_failed",
                "Project memory persistence failed",
                "The project memory could not be redacted safely.",
            ) from error
        except sqlite3.Error as error:
            raise self._database_error(error) from error
