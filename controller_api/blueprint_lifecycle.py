from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
from contextlib import closing
from typing import Any, Callable

from .core import ControllerError, Settings
from .event_journal import EventJournal
from .blueprint import MAX_SOURCE_BYTES, validate_source
from .objective_commands import canonical_json, utc_now
from .sandbox_profiles import (
    PROFILE_NAME_PATTERN,
    REVISION_ID_PATTERN,
    SANDBOX_ID_PATTERN,
    SandboxProfileStore,
)

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~:-]{8,200}$")
OPERATION_ID_PATTERN = re.compile(r"^operation-[0-9a-f]{32}$")
MAX_REVISION_LIMIT = 200
MAX_DIFF_CHANGES = 500


class BlueprintLifecycleStore:
    REQUIRED_TABLES = {
        "sandbox_profiles",
        "sandbox_profile_revisions",
        "controller_blueprint_operations",
        "controller_blueprint_idempotency",
        "controller_blueprint_command_audit",
        "controller_event_journal",
        "schema_migrations",
    }

    def __init__(self, settings: Settings, profiles: SandboxProfileStore) -> None:
        self.settings = settings
        self.profiles = profiles
        self.template_path = settings.root / "repo" / "config" / "examples" / "Blueprint"

    def connect(self, *, write: bool = False) -> sqlite3.Connection:
        try:
            if write:
                connection = sqlite3.connect(
                    self.settings.database,
                    timeout=10,
                    isolation_level=None,
                    check_same_thread=False,
                )
                connection.execute("PRAGMA synchronous = FULL")
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
                "blueprint_lifecycle_unavailable",
                "Blueprint lifecycle unavailable",
                "The Blueprint lifecycle store cannot be opened.",
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
                    return False, "Blueprint lifecycle tables are missing"
                version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                if version < 23:
                    return False, "Blueprint lifecycle migration is missing"
                connection.execute(
                    "SELECT operation_id FROM controller_blueprint_operations LIMIT 1"
                ).fetchone()
        except (sqlite3.Error, ControllerError, TypeError, ValueError):
            return False, "Blueprint lifecycle persistence cannot be read"
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
    def _source_bytes(body: dict[str, Any]) -> bytes:
        if set(body) != {"source"} or not isinstance(body.get("source"), str):
            raise ControllerError(
                400,
                "invalid_blueprint_request",
                "Invalid Blueprint request",
                "The JSON body must contain exactly one string field named source.",
            )
        try:
            source = body["source"].encode("utf-8")
        except UnicodeEncodeError as error:
            raise ControllerError(
                400,
                "invalid_blueprint_source",
                "Invalid Blueprint source",
            ) from error
        if not 1 <= len(source) <= MAX_SOURCE_BYTES:
            raise ControllerError(
                400,
                "blueprint_source_size_invalid",
                "Blueprint source size invalid",
                "The source must contain between 1 byte and 256 KiB.",
            )
        return source

    @staticmethod
    def _runtime_config(result: Any) -> dict[str, Any]:
        canonical = result.canonical
        spec = canonical["spec"]
        metadata = canonical["metadata"]
        base = spec["base"]
        registry = base.get("registry", "docker.io")
        return {
            "schema_version": 1,
            "source_format": result.source_format,
            "api_version": result.api_version,
            "profile_name": metadata["name"],
            "canonical_sha256": result.canonical_sha256,
            "base_image": f"{registry}/{base['image']}@{base['digest']}",
            "workspace": spec["workspace"],
            "runtime": spec["runtime"],
            "network": spec["network"]["runtime"],
            "security": spec["security"],
            "mounts": spec.get("mounts", []),
            "validation": spec["validation"],
        }

    @classmethod
    def preview_source(cls, source: bytes) -> dict[str, Any]:
        try:
            SandboxProfileStore._ensure_persistence_eligible(source)
        except ControllerError:
            return {
                "valid": False,
                "diagnostics": [
                    {
                        "severity": "error",
                        "code": "secret_material_detected",
                        "path": "/",
                        "message": (
                            "Credential-like material was detected. "
                            "The source was not persisted or echoed."
                        ),
                        "documentation": "specs/blueprint-v1.schema.json",
                    }
                ],
            }
        report = validate_source(source)
        payload = report.as_dict(include_canonical=True)
        if report.valid and report.result is not None:
            payload["runtime_config"] = cls._runtime_config(report.result)
        return payload

    def template(self) -> dict[str, Any]:
        source = self.template_path.read_bytes()
        if not 1 <= len(source) <= MAX_SOURCE_BYTES:
            raise ControllerError(
                503,
                "blueprint_template_unavailable",
                "Blueprint template unavailable",
            )
        preview = self.preview_source(source)
        if not preview.get("valid"):
            raise ControllerError(
                503,
                "blueprint_template_invalid",
                "Blueprint template is invalid",
            )
        return {
            "source": source.decode("utf-8"),
            "source_format": preview["source_format"],
            "canonical_sha256": preview["canonical_sha256"],
        }

    @staticmethod
    def _session_fingerprint(session_token: str) -> str:
        return hashlib.sha256(session_token.encode("ascii")).hexdigest()[:32]

    @staticmethod
    def _key_hash(session_token: str, key: str) -> str:
        return hmac.new(
            session_token.encode("ascii"),
            # Historical integrity boundary: persisted key hashes from the v22
            # lifecycle cannot be recomputed or renamed during migration.
            b"hermesops-hermesfile-idempotency-v1\0" + key.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _request_hash(method: str, route: str, body: dict[str, Any]) -> str:
        encoded = canonical_json({"method": method, "route": route, "body": body})
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_if_match(value: str | None) -> int:
        if value is None or re.fullmatch(r'"[1-9][0-9]*"', value) is None:
            raise ControllerError(428, "precondition_required", "If-Match is required")
        revision = int(value[1:-1])
        if revision > 2**63 - 1:
            raise ControllerError(400, "invalid_if_match", "Invalid If-Match")
        return revision

    @staticmethod
    def _profile_row(connection: sqlite3.Connection, sandbox_id: str) -> sqlite3.Row:
        if SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None:
            raise ControllerError(
                404,
                "sandbox_profile_not_found",
                "Sandbox profile not found",
                resource={"type": "sandbox_profile", "id": sandbox_id},
            )
        row = connection.execute(
            """
            SELECT p.*, r.source_sha256
            FROM sandbox_profiles AS p
            JOIN sandbox_profile_revisions AS r
              ON r.sandbox_id = p.sandbox_id
             AND r.revision_id = p.current_revision_id
             AND r.source_revision = p.current_source_revision
            WHERE p.sandbox_id = ?
            """,
            (sandbox_id,),
        ).fetchone()
        if row is None:
            raise ControllerError(
                404,
                "sandbox_profile_not_found",
                "Sandbox profile not found",
                resource={"type": "sandbox_profile", "id": sandbox_id},
            )
        return row

    @staticmethod
    def _operation_payload(row: sqlite3.Row) -> dict[str, Any]:
        try:
            result = json.loads(str(row["result_json"]))
        except json.JSONDecodeError as error:
            raise ControllerError(
                503,
                "operation_projection_invalid",
                "Operation projection unavailable",
            ) from error
        payload = {
            "id": str(row["operation_id"]),
            "kind": str(row["command_kind"]),
            "state": str(row["state"]).lower(),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
            "finished_at": str(row["finished_at"]) if row["finished_at"] else None,
            "target": {"type": "sandbox_profile", "id": str(row["target_id"])},
            "result": result,
            "error": {"code": str(row["error_code"])} if row["error_code"] else None,
        }
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        payload["resource_revision"] = int(digest[:15], 16)
        return payload

    def _replay_or_reserve(
        self,
        connection: sqlite3.Connection,
        *,
        session_token: str,
        idempotency_key: str,
        method: str,
        route: str,
        body: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str, str, str]:
        session_fp = self._session_fingerprint(session_token)
        key_hash = self._key_hash(session_token, idempotency_key)
        request_hash = self._request_hash(method, route, body)
        row = connection.execute(
            "SELECT method, route, request_hash, response_json "
            "FROM controller_blueprint_idempotency "
            "WHERE session_fingerprint=? AND key_hash=?",
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
            except json.JSONDecodeError as error:
                raise ControllerError(
                    503,
                    "idempotency_projection_invalid",
                    "Idempotency projection unavailable",
                ) from error
            if not isinstance(replay, dict):
                raise ControllerError(
                    503,
                    "idempotency_projection_invalid",
                    "Idempotency projection unavailable",
                )
            return replay, session_fp, key_hash, request_hash
        connection.execute(
            "INSERT INTO controller_blueprint_idempotency ("
            "session_fingerprint,key_hash,method,route,request_hash,created_at"
            ") VALUES (?,?,?,?,?,?)",
            (session_fp, key_hash, method, route, request_hash, utc_now()),
        )
        return None, session_fp, key_hash, request_hash

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
            "UPDATE controller_blueprint_idempotency SET response_status=?, "
            "response_json=?, operation_id=?, completed_at=? "
            "WHERE session_fingerprint=? AND key_hash=?",
            (
                status,
                canonical_json(payload),
                operation_id,
                now,
                session_fp,
                key_hash,
            ),
        )

    @classmethod
    def _record_operation(
        cls,
        connection: sqlite3.Connection,
        *,
        operation_id: str,
        kind: str,
        sandbox_id: str,
        result: dict[str, Any],
        now: str,
    ) -> dict[str, Any]:
        connection.execute(
            "INSERT INTO controller_blueprint_operations ("
            "operation_id,command_kind,state,target_id,result_json,created_at,updated_at,finished_at"
            ") VALUES (?,?, 'SUCCEEDED', ?, ?, ?, ?, ?)",
            (
                operation_id,
                kind,
                sandbox_id,
                canonical_json(result),
                now,
                now,
                now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM controller_blueprint_operations WHERE operation_id=?",
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
        sandbox_id: str,
        session_fp: str,
        key_hash: str,
        request_hash: str,
        now: str,
    ) -> None:
        connection.execute(
            "INSERT INTO controller_blueprint_command_audit ("
            "audit_id,operation_id,actor_type,actor_id,action,resource_type,resource_id,"
            "session_fingerprint,idempotency_key_hash,request_hash,outcome,created_at"
            ") VALUES (?,?,'session','operator',?,'sandbox_profile',?,?,?,?, 'SUCCEEDED',?)",
            (
                "audit-" + uuid.uuid4().hex,
                operation_id,
                action,
                sandbox_id,
                session_fp,
                key_hash,
                request_hash,
                now,
            ),
        )

    @staticmethod
    def _emit(
        connection: sqlite3.Connection,
        *,
        event_type: str,
        sandbox_id: str,
        operation_id: str,
        data: dict[str, Any],
        now: str,
    ) -> None:
        EventJournal.emit(
            connection,
            event_type=event_type,
            actor_type="operator",
            actor_id="operator",
            aggregate_type="sandbox",
            aggregate_id=sandbox_id,
            correlation_id="corr_" + uuid.uuid4().hex,
            causation_id=operation_id,
            data=data,
            occurred_at=now,
        )

    @staticmethod
    def _validated(source: bytes) -> tuple[Any, Any]:
        SandboxProfileStore._ensure_persistence_eligible(source)
        report = validate_source(source)
        if not report.valid or report.result is None:
            raise ControllerError(
                400,
                "blueprint_source_invalid",
                "Blueprint source is invalid",
                "Validate the source and correct all errors before persistence.",
            )
        return report, report.result

    def create(
        self,
        *,
        session_token: str,
        idempotency_key: str,
        route: str,
        body: dict[str, Any],
        meta_factory: Callable[[int | None], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        self.validate_idempotency_key(idempotency_key)
        source = self._source_bytes(body)
        report, result = self._validated(source)
        metadata = result.canonical["metadata"]
        profile_name = str(metadata["name"])
        display_name = str(metadata.get("displayName") or profile_name)
        description = str(metadata.get("description") or "")
        labels = metadata.get("labels") or {}
        diagnostics = [item.as_dict() for item in report.diagnostics]
        source_text = source.decode("utf-8")
        with closing(self.connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay, session_fp, key_hash, request_hash = self._replay_or_reserve(
                    connection,
                    session_token=session_token,
                    idempotency_key=idempotency_key,
                    method="POST",
                    route=route,
                    body=body,
                )
                if replay is not None:
                    connection.commit()
                    return 202, replay
                existing = connection.execute(
                    "SELECT sandbox_id FROM sandbox_profiles WHERE profile_name=?",
                    (profile_name,),
                ).fetchone()
                if existing is not None:
                    raise ControllerError(
                        409,
                        "blueprint_profile_conflict",
                        "Blueprint profile already exists",
                        resource={
                            "type": "sandbox_profile",
                            "id": str(existing["sandbox_id"]),
                        },
                    )
                sandbox_id = "sandbox-" + uuid.uuid4().hex
                revision_id = "sandbox-revision-" + uuid.uuid4().hex
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO sandbox_profiles (
                        sandbox_id, profile_name, display_name, description,
                        labels_json, source_format, state,
                        current_revision_id, current_source_revision,
                        active_image_digest, resource_revision,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, 1, NULL, 1, ?, ?)
                    """,
                    (
                        sandbox_id,
                        profile_name,
                        display_name,
                        description,
                        SandboxProfileStore._safe_json(labels),
                        result.source_format,
                        revision_id,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sandbox_profile_revisions (
                        revision_id, sandbox_id, source_revision,
                        source_format, api_version, source_text,
                        source_sha256, canonical_json, canonical_sha256,
                        canonical_size, diagnostics_json, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        sandbox_id,
                        result.source_format,
                        result.api_version,
                        source_text,
                        result.source_sha256,
                        result.canonical_bytes.decode("utf-8"),
                        result.canonical_sha256,
                        len(result.canonical_bytes),
                        SandboxProfileStore._safe_json(diagnostics),
                        now,
                    ),
                )
                operation_id = "operation-" + uuid.uuid4().hex
                operation = self._record_operation(
                    connection,
                    operation_id=operation_id,
                    kind="blueprint.create",
                    sandbox_id=sandbox_id,
                    result={
                        "sandbox_id": sandbox_id,
                        "profile_name": profile_name,
                        "source_revision": 1,
                        "resource_revision": 1,
                        "canonical_sha256": result.canonical_sha256,
                    },
                    now=now,
                )
                self._audit(
                    connection,
                    operation_id=operation_id,
                    action="blueprint.create",
                    sandbox_id=sandbox_id,
                    session_fp=session_fp,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    now=now,
                )
                self._emit(
                    connection,
                    event_type="sandbox.created",
                    sandbox_id=sandbox_id,
                    operation_id=operation_id,
                    data={
                        "profile_name": profile_name,
                        "source_revision": 1,
                        "resource_revision": 1,
                        "canonical_sha256": result.canonical_sha256,
                    },
                    now=now,
                )
                payload = {"data": operation, "meta": meta_factory(1)}
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

    def update(
        self,
        *,
        session_token: str,
        idempotency_key: str,
        route: str,
        sandbox_id: str,
        if_match: str | None,
        body: dict[str, Any],
        meta_factory: Callable[[int | None], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        self.validate_idempotency_key(idempotency_key)
        expected_revision = self._parse_if_match(if_match)
        source = self._source_bytes(body)
        report, result = self._validated(source)
        metadata = result.canonical["metadata"]
        profile_name = str(metadata["name"])
        display_name = str(metadata.get("displayName") or profile_name)
        description = str(metadata.get("description") or "")
        labels = metadata.get("labels") or {}
        diagnostics = [item.as_dict() for item in report.diagnostics]
        source_text = source.decode("utf-8")
        with closing(self.connect(write=True)) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                replay, session_fp, key_hash, request_hash = self._replay_or_reserve(
                    connection,
                    session_token=session_token,
                    idempotency_key=idempotency_key,
                    method="PATCH",
                    route=route,
                    body=body,
                )
                if replay is not None:
                    connection.commit()
                    return 202, replay
                row = self._profile_row(connection, sandbox_id)
                current_resource_revision = int(row["resource_revision"])
                if current_resource_revision != expected_revision:
                    raise ControllerError(
                        409,
                        "resource_revision_conflict",
                        "Blueprint revision conflict",
                    )
                if str(row["state"]) == "archived":
                    raise ControllerError(
                        409,
                        "sandbox_profile_archived",
                        "Sandbox profile is archived",
                    )
                if profile_name != str(row["profile_name"]):
                    raise ControllerError(
                        409,
                        "blueprint_identity_immutable",
                        "Blueprint profile identity is immutable",
                    )
                if hmac.compare_digest(str(row["source_sha256"]), result.source_sha256):
                    raise ControllerError(
                        409,
                        "blueprint_unchanged",
                        "Blueprint source is unchanged",
                    )
                source_revision = int(row["current_source_revision"]) + 1
                resource_revision = current_resource_revision + 1
                revision_id = "sandbox-revision-" + uuid.uuid4().hex
                now = utc_now()
                connection.execute(
                    """
                    INSERT INTO sandbox_profile_revisions (
                        revision_id, sandbox_id, source_revision,
                        source_format, api_version, source_text,
                        source_sha256, canonical_json, canonical_sha256,
                        canonical_size, diagnostics_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        revision_id,
                        sandbox_id,
                        source_revision,
                        result.source_format,
                        result.api_version,
                        source_text,
                        result.source_sha256,
                        result.canonical_bytes.decode("utf-8"),
                        result.canonical_sha256,
                        len(result.canonical_bytes),
                        SandboxProfileStore._safe_json(diagnostics),
                        now,
                    ),
                )
                connection.execute(
                    """
                    UPDATE sandbox_profiles
                    SET display_name=?, description=?, labels_json=?,
                        current_revision_id=?, current_source_revision=?,
                        resource_revision=?, updated_at=?
                    WHERE sandbox_id=?
                    """,
                    (
                        display_name,
                        description,
                        SandboxProfileStore._safe_json(labels),
                        revision_id,
                        source_revision,
                        resource_revision,
                        now,
                        sandbox_id,
                    ),
                )
                operation_id = "operation-" + uuid.uuid4().hex
                operation = self._record_operation(
                    connection,
                    operation_id=operation_id,
                    kind="blueprint.update",
                    sandbox_id=sandbox_id,
                    result={
                        "sandbox_id": sandbox_id,
                        "profile_name": profile_name,
                        "source_revision": source_revision,
                        "resource_revision": resource_revision,
                        "canonical_sha256": result.canonical_sha256,
                    },
                    now=now,
                )
                self._audit(
                    connection,
                    operation_id=operation_id,
                    action="blueprint.update",
                    sandbox_id=sandbox_id,
                    session_fp=session_fp,
                    key_hash=key_hash,
                    request_hash=request_hash,
                    now=now,
                )
                self._emit(
                    connection,
                    event_type="sandbox.updated",
                    sandbox_id=sandbox_id,
                    operation_id=operation_id,
                    data={
                        "profile_name": profile_name,
                        "source_revision": source_revision,
                        "resource_revision": resource_revision,
                        "canonical_sha256": result.canonical_sha256,
                    },
                    now=now,
                )
                payload = {"data": operation, "meta": meta_factory(resource_revision)}
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

    @staticmethod
    def _revision_metadata(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": str(row["revision_id"]),
            "sandbox_id": str(row["sandbox_id"]),
            "source_revision": int(row["source_revision"]),
            "source_format": str(row["source_format"]),
            "api_version": str(row["api_version"]),
            "source_sha256": str(row["source_sha256"]),
            "canonical_sha256": str(row["canonical_sha256"]),
            "canonical_size": int(row["canonical_size"]),
            "created_at": str(row["created_at"]),
        }

    def list_revisions(self, sandbox_id: str, *, limit: int) -> list[dict[str, Any]]:
        if SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None:
            raise ControllerError(404, "sandbox_profile_not_found", "Sandbox profile not found")
        if type(limit) is not int or not 1 <= limit <= MAX_REVISION_LIMIT:
            raise ControllerError(400, "invalid_limit", "Invalid pagination limit")
        with closing(self.connect()) as connection:
            exists = connection.execute(
                "SELECT 1 FROM sandbox_profiles WHERE sandbox_id=?",
                (sandbox_id,),
            ).fetchone()
            if exists is None:
                raise ControllerError(404, "sandbox_profile_not_found", "Sandbox profile not found")
            rows = connection.execute(
                """
                SELECT * FROM sandbox_profile_revisions
                WHERE sandbox_id=?
                ORDER BY source_revision DESC
                LIMIT ?
                """,
                (sandbox_id, limit),
            ).fetchall()
        return [self._revision_metadata(row) for row in rows]

    def get_revision(self, sandbox_id: str, source_revision: int) -> dict[str, Any]:
        if SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None or source_revision < 1:
            raise ControllerError(404, "blueprint_revision_not_found", "Blueprint revision not found")
        with closing(self.connect()) as connection:
            row = connection.execute(
                """
                SELECT * FROM sandbox_profile_revisions
                WHERE sandbox_id=? AND source_revision=?
                """,
                (sandbox_id, source_revision),
            ).fetchone()
        if row is None:
            raise ControllerError(404, "blueprint_revision_not_found", "Blueprint revision not found")
        try:
            canonical = json.loads(str(row["canonical_json"]))
            diagnostics = json.loads(str(row["diagnostics_json"]))
        except json.JSONDecodeError as error:
            raise ControllerError(
                503,
                "blueprint_revision_projection_failed",
                "Blueprint revision projection failed",
            ) from error
        result = self._revision_metadata(row)
        result.update(
            {
                "source": str(row["source_text"]),
                "canonical": canonical,
                "diagnostics": diagnostics,
                "runtime_config": self._runtime_config_from_persisted(result, canonical),
            }
        )
        return result

    @staticmethod
    def _runtime_config_from_persisted(metadata: dict[str, Any], canonical: dict[str, Any]) -> dict[str, Any]:
        spec = canonical["spec"]
        base = spec["base"]
        registry = base.get("registry", "docker.io")
        return {
            "schema_version": 1,
            "source_format": metadata["source_format"],
            "api_version": metadata["api_version"],
            "profile_name": canonical["metadata"]["name"],
            "canonical_sha256": metadata["canonical_sha256"],
            "base_image": f"{registry}/{base['image']}@{base['digest']}",
            "workspace": spec["workspace"],
            "runtime": spec["runtime"],
            "network": spec["network"]["runtime"],
            "security": spec["security"],
            "mounts": spec.get("mounts", []),
            "validation": spec["validation"],
        }

    def current(self, sandbox_id: str) -> dict[str, Any]:
        profile = self.profiles.get_profile(sandbox_id)
        revision = self.get_revision(sandbox_id, int(profile["source_revision"]))
        return {"profile": profile, "revision": revision}

    @classmethod
    def _diff_values(
        cls,
        before: Any,
        after: Any,
        *,
        path: str,
        changes: list[dict[str, str]],
    ) -> bool:
        if len(changes) >= MAX_DIFF_CHANGES:
            return True
        if type(before) is not type(after):
            changes.append({"path": path or "/", "kind": "modified"})
            return False
        if isinstance(before, dict):
            for key in sorted(set(before) | set(after)):
                escaped = key.replace("~", "~0").replace("/", "~1")
                child = f"{path}/{escaped}"
                if key not in before:
                    changes.append({"path": child, "kind": "added"})
                elif key not in after:
                    changes.append({"path": child, "kind": "removed"})
                elif cls._diff_values(before[key], after[key], path=child, changes=changes):
                    return True
                if len(changes) >= MAX_DIFF_CHANGES:
                    return True
            return False
        if isinstance(before, list):
            maximum = max(len(before), len(after))
            for index in range(maximum):
                child = f"{path}/{index}"
                if index >= len(before):
                    changes.append({"path": child, "kind": "added"})
                elif index >= len(after):
                    changes.append({"path": child, "kind": "removed"})
                elif cls._diff_values(before[index], after[index], path=child, changes=changes):
                    return True
                if len(changes) >= MAX_DIFF_CHANGES:
                    return True
            return False
        if before != after:
            changes.append({"path": path or "/", "kind": "modified"})
        return False

    def compare(self, sandbox_id: str, from_revision: int, to_revision: int) -> dict[str, Any]:
        if from_revision < 1 or to_revision < 1 or from_revision == to_revision:
            raise ControllerError(400, "invalid_revision_comparison", "Invalid revision comparison")
        before = self.get_revision(sandbox_id, from_revision)
        after = self.get_revision(sandbox_id, to_revision)
        changes: list[dict[str, str]] = []
        truncated = self._diff_values(
            before["canonical"],
            after["canonical"],
            path="",
            changes=changes,
        )
        return {
            "sandbox_id": sandbox_id,
            "from_revision": from_revision,
            "to_revision": to_revision,
            "from_canonical_sha256": before["canonical_sha256"],
            "to_canonical_sha256": after["canonical_sha256"],
            "changed": bool(changes),
            "changes": changes[:MAX_DIFF_CHANGES],
            "truncated": truncated,
        }

    def get_operation(self, operation_id: str) -> dict[str, Any] | None:
        if OPERATION_ID_PATTERN.fullmatch(operation_id) is None:
            return None
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    "SELECT * FROM controller_blueprint_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "database_unavailable",
                "Controller database unavailable",
            ) from error
        if row is None:
            return None
        return self._operation_payload(row)
