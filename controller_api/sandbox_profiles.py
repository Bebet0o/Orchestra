from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import ControllerError, Settings
from .blueprint import MAX_SOURCE_BYTES, BlueprintReport, validate_source


SANDBOX_ID_PATTERN = re.compile(r"^sandbox-[0-9a-f]{32}$")
REVISION_ID_PATTERN = re.compile(r"^sandbox-revision-[0-9a-f]{32}$")
PROFILE_NAME_PATTERN = re.compile(
    r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)
LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
IMAGE_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")
BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
CURSOR_FIELDS = {"v", "profile_name", "sandbox_id", "state"}
TIMESTAMP_PATTERN = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T"
    r"[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z$"
)
PROFILE_STATES = {"draft", "ready", "active", "inactive", "archived"}
MAX_LIST_LIMIT = 200

_HIGH_CONFIDENCE_SECRET_PATTERNS = (
    re.compile(rb"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    re.compile(rb"\$\{"),
    re.compile(rb"(?i)https?://[^/\s:@]+:[^/\s@]+@"),
    re.compile(
        rb"(?i)\b(?:token|password|secret|api[_-]?key)"
        rb"\s*[:=]\s*(?!false\b|null\b|none\b)[^\s#]+"
    ),
    re.compile(rb"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])ghp_[A-Za-z0-9]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(rb"(?<![A-Za-z0-9])xox[baprs]-[A-Za-z0-9-]{20,}"),
    re.compile(rb"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(
        rb"(?<![A-Za-z0-9_-])"
        rb"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"
        rb"(?![A-Za-z0-9_-])"
    ),
)


@dataclass(frozen=True)
class ImportResult:
    profile: dict[str, Any]
    created: bool
    revision_created: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "created": self.created,
            "revision_created": self.revision_created,
        }


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def read_source_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ControllerError(
            400,
            "sandbox_source_unavailable",
            "Sandbox profile source unavailable",
            "The Blueprint must be a readable regular file and not a symlink.",
        ) from error
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise ControllerError(
                400,
                "sandbox_source_unavailable",
                "Sandbox profile source unavailable",
                "The Blueprint must be a regular file with one hard link.",
            )
        if metadata.st_size <= 0 or metadata.st_size > MAX_SOURCE_BYTES:
            raise ControllerError(
                400,
                "sandbox_source_size_invalid",
                "Sandbox profile source size invalid",
                "The Blueprint source must be between 1 byte and 256 KiB.",
            )
        chunks: list[bytes] = []
        remaining = MAX_SOURCE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        final_metadata = os.fstat(descriptor)
        if (
            len(payload) != metadata.st_size
            or len(payload) > MAX_SOURCE_BYTES
            or final_metadata.st_dev != metadata.st_dev
            or final_metadata.st_ino != metadata.st_ino
            or final_metadata.st_size != metadata.st_size
            or final_metadata.st_mtime_ns != metadata.st_mtime_ns
            or final_metadata.st_ctime_ns != metadata.st_ctime_ns
        ):
            raise ControllerError(
                400,
                "sandbox_source_changed",
                "Sandbox profile source changed while reading",
                "Retry with a stable regular Blueprint.",
            )
        return payload
    finally:
        os.close(descriptor)


class SandboxProfileStore:
    REQUIRED_TABLES = {
        "sandbox_profiles",
        "sandbox_profile_revisions",
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
                "sandbox_profiles_unavailable",
                "Sandbox profiles unavailable",
                "The sandbox profile store cannot be opened.",
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
                    return False, "sandbox profile tables are missing"
                version = int(
                    connection.execute("PRAGMA user_version").fetchone()[0]
                )
                if version < 24:
                    return False, "Blueprint API namespace migration is not installed"
                connection.execute(
                    """
                    SELECT sandbox_id, current_revision_id
                    FROM sandbox_profiles
                    LIMIT 1
                    """
                ).fetchone()
        except (sqlite3.Error, ControllerError, TypeError, ValueError):
            return False, "sandbox profile store cannot be read"
        return True, "ready"

    @staticmethod
    def _database_error(error: sqlite3.Error) -> ControllerError:
        return ControllerError(
            503,
            "sandbox_profiles_unavailable",
            "Sandbox profiles unavailable",
            "The sandbox profile store cannot serve this request.",
        )

    @staticmethod
    def _ensure_persistence_eligible(source: bytes) -> None:
        if any(pattern.search(source) for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS):
            raise ControllerError(
                400,
                "sandbox_source_secret_detected",
                "Sandbox profile source is not persistence-eligible",
                "The source contains credential-like material and was not stored.",
            )

    @staticmethod
    def _validated_result(source: bytes) -> tuple[BlueprintReport, Any]:
        SandboxProfileStore._ensure_persistence_eligible(source)
        report = validate_source(source)
        if not report.valid or report.result is None:
            raise ControllerError(
                400,
                "sandbox_source_invalid",
                "Sandbox profile source is invalid",
                "The Blueprint failed validation and was not stored.",
            )
        return report, report.result

    @staticmethod
    def _safe_json(value: Any) -> str:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    @classmethod
    def _load_strict_json(cls, value: Any) -> Any:
        if not isinstance(value, str):
            raise ValueError("invalid JSON storage")
        return json.loads(
            value,
            object_pairs_hook=cls._strict_json_object,
        )

    @staticmethod
    def _public_text(
        value: Any,
        *,
        minimum: int,
        maximum: int,
        allow_newline: bool,
    ) -> str:
        if not isinstance(value, str) or not minimum <= len(value) <= maximum:
            raise ValueError("invalid public text")
        allowed_controls = {"\n", "\t"} if allow_newline else set()
        if any(
            ord(character) < 32 and character not in allowed_controls
            for character in value
        ) or "\x7f" in value:
            raise ValueError("unsafe public text")
        if not allow_newline and ("\n" in value or "\r" in value):
            raise ValueError("unsafe public text")
        raw = value.encode("utf-8")
        if any(pattern.search(raw) for pattern in _HIGH_CONFIDENCE_SECRET_PATTERNS):
            raise ValueError("private public text")
        return value

    @classmethod
    def _profile_projection(cls, row: sqlite3.Row) -> dict[str, Any]:
        try:
            sandbox_id = str(row["sandbox_id"])
            profile_name = str(row["profile_name"])
            source_format = str(row["source_format"])
            state = str(row["state"])
            source_sha256 = str(row["source_sha256"])
            canonical_sha256 = str(row["canonical_sha256"])
            created_at = str(row["created_at"])
            updated_at = str(row["updated_at"])
            source_revision = int(row["current_source_revision"])
            resource_revision = int(row["resource_revision"])
            canonical_size = int(row["canonical_size"])
            active_digest = (
                None
                if row["active_image_digest"] is None
                else str(row["active_image_digest"])
            )
            if (
                SANDBOX_ID_PATTERN.fullmatch(sandbox_id) is None
                or PROFILE_NAME_PATTERN.fullmatch(profile_name) is None
                or source_format != "blueprint-v1"
                or state not in PROFILE_STATES
                or SHA256_PATTERN.fullmatch(source_sha256) is None
                or SHA256_PATTERN.fullmatch(canonical_sha256) is None
                or TIMESTAMP_PATTERN.fullmatch(created_at) is None
                or TIMESTAMP_PATTERN.fullmatch(updated_at) is None
                or updated_at < created_at
                or source_revision < 1
                or resource_revision < 1
                or not 2 <= canonical_size <= 524288
                or (
                    active_digest is not None
                    and IMAGE_DIGEST_PATTERN.fullmatch(active_digest) is None
                )
            ):
                raise ValueError("invalid persisted metadata")
            display_name = cls._public_text(
                row["display_name"],
                minimum=1,
                maximum=120,
                allow_newline=False,
            )
            description = cls._public_text(
                row["description"],
                minimum=0,
                maximum=1000,
                allow_newline=True,
            )
            diagnostics = cls._load_strict_json(row["diagnostics_json"])
            labels = cls._load_strict_json(row["labels_json"])
            if not isinstance(labels, dict) or len(labels) > 64:
                raise ValueError("invalid labels")
            public_labels: dict[str, str] = {}
            for key, value in labels.items():
                if (
                    not isinstance(key, str)
                    or LABEL_PATTERN.fullmatch(key) is None
                ):
                    raise ValueError("invalid label key")
                public_labels[key] = cls._public_text(
                    value,
                    minimum=0,
                    maximum=120,
                    allow_newline=False,
                )
            if not isinstance(diagnostics, list) or len(diagnostics) > 100:
                raise ValueError("invalid diagnostics")
            public_diagnostics: list[dict[str, str]] = []
            diagnostic_keys = {
                "severity",
                "code",
                "path",
                "message",
                "documentation",
            }
            for item in diagnostics:
                if not isinstance(item, dict) or set(item) != diagnostic_keys:
                    raise ValueError("invalid diagnostic")
                severity = item.get("severity")
                if severity not in {"info", "warning", "error"}:
                    raise ValueError("invalid diagnostic severity")
                public_diagnostics.append(
                    {
                        "severity": severity,
                        "code": cls._public_text(
                            item.get("code"),
                            minimum=1,
                            maximum=128,
                            allow_newline=False,
                        ),
                        "path": cls._public_text(
                            item.get("path"),
                            minimum=1,
                            maximum=4096,
                            allow_newline=False,
                        ),
                        "message": cls._public_text(
                            item.get("message"),
                            minimum=1,
                            maximum=1000,
                            allow_newline=False,
                        ),
                        "documentation": cls._public_text(
                            item.get("documentation"),
                            minimum=1,
                            maximum=4096,
                            allow_newline=False,
                        ),
                    }
                )
        except (
            json.JSONDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise ControllerError(
                503,
                "sandbox_profile_projection_failed",
                "Sandbox profile projection failed",
                "Persisted sandbox profile metadata is malformed.",
            ) from error
        return {
            "id": sandbox_id,
            "name": display_name,
            "profile_name": profile_name,
            "description": description,
            "labels": public_labels,
            "source_format": source_format,
            "source_revision": source_revision,
            "state": state,
            "active_image_digest": active_digest,
            "source_sha256": source_sha256,
            "canonical_sha256": canonical_sha256,
            "canonical_size": canonical_size,
            "diagnostics": public_diagnostics,
            "created_at": created_at,
            "updated_at": updated_at,
            "resource_revision": resource_revision,
        }

    @staticmethod
    def _select_current_sql() -> str:
        return """
            SELECT
                p.sandbox_id,
                p.profile_name,
                p.display_name,
                p.description,
                p.labels_json,
                p.source_format,
                p.state,
                p.current_source_revision,
                p.active_image_digest,
                p.resource_revision,
                p.created_at,
                p.updated_at,
                r.source_sha256,
                r.canonical_sha256,
                r.canonical_size,
                r.diagnostics_json
            FROM sandbox_profiles AS p
            JOIN sandbox_profile_revisions AS r
              ON r.sandbox_id = p.sandbox_id
             AND r.revision_id = p.current_revision_id
             AND r.source_revision = p.current_source_revision
        """

    @staticmethod
    def _cursor_key(cursor_secret: str) -> bytes:
        return hmac.new(
            cursor_secret.encode("ascii"),
            b"orchestra-sandbox-profile-cursor-v1",
            hashlib.sha256,
        ).digest()

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
        canonical = (
            base64.urlsafe_b64encode(decoded)
            .rstrip(b"=")
            .decode("ascii")
        )
        if not hmac.compare_digest(segment, canonical):
            raise ValueError("noncanonical base64url")
        return decoded

    @classmethod
    def _encode_cursor(
        cls,
        *,
        profile_name: str,
        sandbox_id: str,
        state: str | None,
        cursor_secret: str,
    ) -> str:
        payload = cls._safe_json(
            {
                "v": 1,
                "profile_name": profile_name,
                "sandbox_id": sandbox_id,
                "state": state,
            }
        ).encode("utf-8")
        signature = hmac.new(
            cls._cursor_key(cursor_secret),
            payload,
            hashlib.sha256,
        ).digest()
        return (
            base64.urlsafe_b64encode(payload).rstrip(b"=").decode("ascii")
            + "."
            + base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
        )

    @classmethod
    def _decode_cursor(
        cls,
        cursor: str,
        *,
        state: str | None,
        cursor_secret: str,
    ) -> tuple[str, str]:
        try:
            if not 16 <= len(cursor) <= 2048 or cursor.count(".") != 1:
                raise ValueError("length")
            left, right = cursor.split(".", 1)
            payload = cls._decode_base64url(left)
            signature = cls._decode_base64url(right)
            if len(signature) != hashlib.sha256().digest_size:
                raise ValueError("signature length")
            expected = hmac.new(
                cls._cursor_key(cursor_secret),
                payload,
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(signature, expected):
                raise ValueError("signature")
            parsed = cls._load_strict_json(payload.decode("utf-8"))
            if (
                not isinstance(parsed, dict)
                or set(parsed) != CURSOR_FIELDS
                or type(parsed.get("v")) is not int
                or parsed.get("v") != 1
                or parsed.get("state") != state
                or not isinstance(parsed.get("profile_name"), str)
                or PROFILE_NAME_PATTERN.fullmatch(parsed["profile_name"]) is None
                or not isinstance(parsed.get("sandbox_id"), str)
                or not SANDBOX_ID_PATTERN.fullmatch(parsed["sandbox_id"])
                or cls._safe_json(parsed).encode("utf-8") != payload
            ):
                raise ValueError("shape")
            return parsed["profile_name"], parsed["sandbox_id"]
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeError) as error:
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
                "The sandbox profile cursor is invalid or no longer applicable.",
            ) from error

    @staticmethod
    def _validate_state(state: str | None) -> str | None:
        if state is not None and state not in PROFILE_STATES:
            raise ControllerError(
                400,
                "invalid_state",
                "Invalid sandbox profile state",
                "The requested sandbox profile state is not supported.",
            )
        return state

    def list_profiles(
        self,
        *,
        limit: int,
        cursor: str | None,
        state: str | None,
        cursor_secret: str,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )
        state = self._validate_state(state)
        after_name: str | None = None
        after_id: str | None = None
        if cursor is not None:
            after_name, after_id = self._decode_cursor(
                cursor,
                state=state,
                cursor_secret=cursor_secret,
            )
        clauses: list[str] = []
        parameters: list[Any] = []
        if state is not None:
            clauses.append("p.state = ?")
            parameters.append(state)
        if after_name is not None and after_id is not None:
            clauses.append(
                "(p.profile_name > ? OR "
                "(p.profile_name = ? AND p.sandbox_id > ?))"
            )
            parameters.extend((after_name, after_name, after_id))
        sql = self._select_current_sql()
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY p.profile_name, p.sandbox_id LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self.connect()) as connection:
                rows = connection.execute(sql, parameters).fetchall()
        except sqlite3.Error as error:
            raise self._database_error(error) from error
        has_more = len(rows) > limit
        selected = rows[:limit]
        items = [self._profile_projection(row) for row in selected]
        next_cursor = None
        if has_more and selected:
            last = selected[-1]
            next_cursor = self._encode_cursor(
                profile_name=str(last["profile_name"]),
                sandbox_id=str(last["sandbox_id"]),
                state=state,
                cursor_secret=cursor_secret,
            )
        return items, next_cursor

    def get_profile(self, sandbox_id: str) -> dict[str, Any]:
        if not SANDBOX_ID_PATTERN.fullmatch(sandbox_id):
            raise ControllerError(
                404,
                "sandbox_profile_not_found",
                "Sandbox profile not found",
                "No sandbox profile exists with that identifier.",
                resource={"type": "sandbox_profile", "id": sandbox_id},
            )
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    self._select_current_sql() + " WHERE p.sandbox_id = ?",
                    (sandbox_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self._database_error(error) from error
        if row is None:
            raise ControllerError(
                404,
                "sandbox_profile_not_found",
                "Sandbox profile not found",
                "No sandbox profile exists with that identifier.",
                resource={"type": "sandbox_profile", "id": sandbox_id},
            )
        return self._profile_projection(row)

    def import_source(self, source: bytes) -> ImportResult:
        report, result = self._validated_result(source)
        canonical = result.canonical
        metadata = canonical["metadata"]
        profile_name = str(metadata["name"])
        display_name = str(metadata.get("displayName") or profile_name)
        description = str(metadata.get("description") or "")
        labels = metadata.get("labels") or {}
        diagnostics = [item.as_dict() for item in report.diagnostics]
        source_text = source.decode("utf-8")
        now = utc_now()
        try:
            with closing(self.connect(write=True)) as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT
                        p.sandbox_id,
                        p.current_revision_id,
                        p.current_source_revision,
                        p.resource_revision,
                        r.source_sha256
                    FROM sandbox_profiles AS p
                    JOIN sandbox_profile_revisions AS r
                      ON r.sandbox_id = p.sandbox_id
                     AND r.revision_id = p.current_revision_id
                    WHERE p.profile_name = ?
                    """,
                    (profile_name,),
                ).fetchone()
                created = row is None
                if row is None:
                    sandbox_id = "sandbox-" + secrets.token_hex(16)
                    source_revision = 1
                    resource_revision = 1
                else:
                    sandbox_id = str(row["sandbox_id"])
                    if hmac.compare_digest(
                        str(row["source_sha256"]),
                        result.source_sha256,
                    ):
                        connection.rollback()
                        return ImportResult(
                            profile=self.get_profile(sandbox_id),
                            created=False,
                            revision_created=False,
                        )
                    source_revision = int(row["current_source_revision"]) + 1
                    resource_revision = int(row["resource_revision"]) + 1
                revision_id = "sandbox-revision-" + secrets.token_hex(16)
                if created:
                    connection.execute(
                        """
                        INSERT INTO sandbox_profiles (
                            sandbox_id, profile_name, display_name, description,
                            labels_json, source_format, state,
                            current_revision_id, current_source_revision,
                            active_image_digest, resource_revision,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?, NULL, ?, ?, ?)
                        """,
                        (
                            sandbox_id,
                            profile_name,
                            display_name,
                            description,
                            self._safe_json(labels),
                            result.source_format,
                            revision_id,
                            source_revision,
                            resource_revision,
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
                        self._safe_json(diagnostics),
                        now,
                    ),
                )
                if not created:
                    connection.execute(
                        """
                        UPDATE sandbox_profiles
                        SET
                            display_name = ?,
                            description = ?,
                            labels_json = ?,
                            current_revision_id = ?,
                            current_source_revision = ?,
                            resource_revision = ?,
                            updated_at = ?
                        WHERE sandbox_id = ?
                        """,
                        (
                            display_name,
                            description,
                            self._safe_json(labels),
                            revision_id,
                            source_revision,
                            resource_revision,
                            now,
                            sandbox_id,
                        ),
                    )
                profile_row = connection.execute(
                    self._select_current_sql() + " WHERE p.sandbox_id = ?",
                    (sandbox_id,),
                ).fetchone()
                if profile_row is None:
                    raise sqlite3.IntegrityError(
                        "committed sandbox profile projection is missing"
                    )
                profile = self._profile_projection(profile_row)
                connection.commit()
        except ControllerError:
            raise
        except sqlite3.IntegrityError as error:
            raise ControllerError(
                503,
                "sandbox_profile_persistence_failed",
                "Sandbox profile persistence failed",
                "The sandbox profile could not be persisted safely.",
            ) from error
        except sqlite3.Error as error:
            raise self._database_error(error) from error
        return ImportResult(
            profile=profile,
            created=created,
            revision_created=True,
        )

    def import_path(self, path: Path) -> ImportResult:
        return self.import_source(read_source_file(path))
