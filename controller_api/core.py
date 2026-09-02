from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import sqlite3
import stat
import tomllib
import uuid
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import API_VERSION, SERVICE_NAME

PROJECT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,62}$")
SESSION_COOKIE = "orchestra_session"
SESSION_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9._~-]{32,256}$")
MAX_CONFIG_BYTES = 1024 * 1024


class ControllerError(RuntimeError):
    """Expected Controller API failure."""

    def __init__(
        self,
        status: int,
        code: str,
        title: str,
        detail: str = "",
        *,
        resource: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail
        self.resource = resource


@dataclass(frozen=True)
class Settings:
    root: Path
    database: Path
    session_file: Path
    host: str = "127.0.0.1"
    port: int = 8765
    socket_timeout_seconds: float = 10.0
    max_concurrent_requests: int = 32
    console_origin: str = "http://127.0.0.1:8787"
    max_websocket_connections: int = 8

    @classmethod
    def from_root(
        cls,
        root: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        database: Path | None = None,
        session_file: Path | None = None,
        socket_timeout_seconds: float = 10.0,
        max_concurrent_requests: int = 32,
        console_origin: str = "http://127.0.0.1:8787",
        max_websocket_connections: int | None = None,
    ) -> "Settings":
        resolved_root = root.resolve(strict=False)
        return cls(
            root=resolved_root,
            database=(
                database
                or resolved_root / "state" / "controller" / "orchestra.db"
            ).resolve(strict=False),
            session_file=(
                session_file
                or resolved_root / "secrets" / "controller-session"
            ).resolve(strict=False),
            host=host,
            port=port,
            socket_timeout_seconds=socket_timeout_seconds,
            max_concurrent_requests=max_concurrent_requests,
            console_origin=console_origin,
            max_websocket_connections=(
                min(8, max_concurrent_requests)
                if max_websocket_connections is None
                else max_websocket_connections
            ),
        )

    @classmethod
    def from_environment(
        cls,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
    ) -> "Settings":
        root = Path(
            os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
        )
        database = os.environ.get("ORCHESTRA_CONTROLLER_DATABASE")
        session_file = os.environ.get(
            "ORCHESTRA_CONTROLLER_SESSION_FILE"
        )
        console_origin = os.environ.get(
            "ORCHESTRA_CONTROLLER_CONSOLE_ORIGIN",
            "http://127.0.0.1:8787",
        )
        raw_websocket_limit = os.environ.get(
            "ORCHESTRA_CONTROLLER_MAX_WEBSOCKETS"
        )
        try:
            websocket_limit = (
                int(raw_websocket_limit)
                if raw_websocket_limit is not None
                else None
            )
        except ValueError:
            websocket_limit = 0
        return cls.from_root(
            root,
            host=host,
            port=port,
            database=Path(database) if database else None,
            session_file=Path(session_file) if session_file else None,
            console_origin=console_origin,
            max_websocket_connections=websocket_limit,
        )

    def validate_bind(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ControllerError(
                400,
                "invalid_bind_address",
                "Invalid bind address",
                "The Controller API accepts a literal loopback IP address only.",
            ) from error
        if not address.is_loopback:
            raise ControllerError(
                400,
                "non_loopback_bind_forbidden",
                "Non-loopback binding is forbidden",
                "The Controller API may listen only on a loopback address.",
            )
        if not 0 <= self.port <= 65535:
            raise ControllerError(
                400,
                "invalid_port",
                "Invalid port",
                "Port must be between 0 and 65535.",
            )
        if not 1.0 <= self.socket_timeout_seconds <= 120.0:
            raise ControllerError(
                400,
                "invalid_socket_timeout",
                "Invalid socket timeout",
                "Socket timeout must be between 1 and 120 seconds.",
            )
        if not 1 <= self.max_concurrent_requests <= 256:
            raise ControllerError(
                400,
                "invalid_concurrency_limit",
                "Invalid concurrency limit",
                "Maximum concurrent requests must be between 1 and 256.",
            )
        if (
            not 1 <= self.max_websocket_connections <= 64
            or self.max_websocket_connections > self.max_concurrent_requests
        ):
            raise ControllerError(
                400,
                "invalid_websocket_limit",
                "Invalid WebSocket connection limit",
                "WebSocket connections must be between 1 and 64 and no greater than the request limit.",
            )
        parsed_origin = urlsplit(self.console_origin)
        try:
            parsed_origin.port
            origin_port_valid = True
        except ValueError:
            origin_port_valid = False
        if (
            not origin_port_valid
            or parsed_origin.scheme not in {"http", "https"}
            or parsed_origin.hostname is None
            or parsed_origin.username is not None
            or parsed_origin.password is not None
            or parsed_origin.path
            or parsed_origin.query
            or parsed_origin.fragment
            or self.console_origin
            != f"{parsed_origin.scheme}://{parsed_origin.netloc}"
        ):
            raise ControllerError(
                400,
                "invalid_console_origin",
                "Invalid Console origin",
                "Console origin must be one canonical HTTP or HTTPS origin without credentials or a path.",
            )


class ReadOnlyDatabase:
    """Small SQLite read adapter that cannot open a writable connection."""

    REQUIRED_TABLES = {
        "projects",
        "schema_migrations",
        "roles",
        "runs",
        "events",
        "worker_executions",
        "objective_queue",
        "objective_attempts",
        "objective_events",
        "orchestration_plans",
        "orchestration_tasks",
        "orchestration_attempts",
        "orchestration_dependencies",
        "review_results",
        "reviewer_executions",
        "integration_executions",
        "recovery_executions",
    }

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def connect(self) -> sqlite3.Connection:
        if not self.settings.database.is_file():
            raise ControllerError(
                503,
                "database_unavailable",
                "Controller database unavailable",
                "The Orchestra control database does not exist.",
            )
        uri = f"{self.settings.database.as_uri()}?mode=ro"
        try:
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=5,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 3000")
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "database_unavailable",
                "Controller database unavailable",
                "The Orchestra control database cannot be opened read-only.",
            ) from error
        return connection

    @staticmethod
    def _database_failure(error: sqlite3.Error) -> ControllerError:
        return ControllerError(
            503,
            "database_unavailable",
            "Controller database unavailable",
            "The Orchestra control database cannot serve this request.",
        )

    def readiness(self) -> tuple[bool, str]:
        """Cheap liveness/readiness check; integrity scans stay offline."""
        try:
            with closing(self.connect()) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        """
                        SELECT name
                        FROM sqlite_master
                        WHERE type = 'table'
                        """
                    )
                }
                missing = sorted(self.REQUIRED_TABLES - tables)
                if missing:
                    return False, "required database tables are missing"
                connection.execute(
                    "SELECT project_id FROM projects LIMIT 1"
                ).fetchone()
                connection.execute(
                    "SELECT version FROM schema_migrations "
                    "ORDER BY version DESC LIMIT 1"
                ).fetchone()
        except (sqlite3.Error, ControllerError):
            return False, "database cannot be read"
        return True, "ready"

    @staticmethod
    def _read_default_branch(row: sqlite3.Row) -> str:
        value = str(row["default_branch"])
        return value if value else "unknown"

    @staticmethod
    def _revision(config_hash: str) -> int:
        if re.fullmatch(r"[0-9a-fA-F]{16,}", config_hash):
            return int(config_hash[:15], 16)
        digest = hashlib.sha256(config_hash.encode("utf-8")).hexdigest()
        return int(digest[:15], 16)

    def _project(self, row: sqlite3.Row) -> dict[str, Any]:
        project_id = str(row["project_id"])
        state = (
            "archived"
            if int(row["archived"])
            else ("enabled" if int(row["enabled"]) else "disabled")
        )
        return {
            "id": project_id,
            "slug": project_id,
            "name": str(row["display_name"]),
            "state": state,
            "default_branch": self._read_default_branch(row),
            "policy_id": str(row["policy_id"]),
            "sandbox_profile_id": (
                str(row["sandbox_profile_id"])
                if row["sandbox_profile_id"] is not None
                else None
            ),
            "repository": {
                "mode": str(row["repository_mode"]),
                "managed_path": True,
            },
            "resource_revision": int(row["resource_revision"]),
            "created_at": str(row["registered_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def list_projects(
        self,
        *,
        limit: int,
        cursor: str | None,
    ) -> tuple[list[dict[str, Any]], str | None]:
        if not 1 <= limit <= 200:
            raise ControllerError(
                400,
                "invalid_limit",
                "Invalid pagination limit",
                "limit must be between 1 and 200.",
            )
        if cursor is not None and not PROJECT_ID_PATTERN.fullmatch(cursor):
            raise ControllerError(
                400,
                "invalid_cursor",
                "Invalid pagination cursor",
                "cursor must be a valid project identifier.",
            )

        sql = """
            SELECT
                project_id,
                display_name,
                policy_id,
                enabled,
                config_source,
                config_hash,
                default_branch,
                sandbox_profile_id,
                archived,
                repository_mode,
                resource_revision,
                registered_at,
                updated_at
            FROM projects
        """
        parameters: list[Any] = []
        if cursor is not None:
            sql += " WHERE project_id > ?"
            parameters.append(cursor)
        sql += " ORDER BY project_id LIMIT ?"
        parameters.append(limit + 1)

        try:
            with closing(self.connect()) as connection:
                rows = list(connection.execute(sql, parameters))
        except sqlite3.Error as error:
            raise self._database_failure(error) from error

        has_more = len(rows) > limit
        selected = rows[:limit]
        projects = [self._project(row) for row in selected]
        next_cursor = (
            str(selected[-1]["project_id"])
            if has_more and selected
            else None
        )
        return projects, next_cursor

    def get_project(self, project_id: str) -> dict[str, Any]:
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise ControllerError(
                400,
                "invalid_project_id",
                "Invalid project identifier",
                "The project identifier does not match the public contract.",
            )

        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    """
                    SELECT
                        project_id,
                        display_name,
                        policy_id,
                        enabled,
                        config_source,
                        config_hash,
                        default_branch,
                        sandbox_profile_id,
                        archived,
                        repository_mode,
                        resource_revision,
                        registered_at,
                        updated_at
                    FROM projects
                    WHERE project_id = ?
                    """,
                    (project_id,),
                ).fetchone()
        except sqlite3.Error as error:
            raise self._database_failure(error) from error

        if row is None:
            raise ControllerError(
                404,
                "project_not_found",
                "Project not found",
                f"No project exists with identifier {project_id!r}.",
                resource={"type": "project", "id": project_id},
            )
        return self._project(row)


class ControllerService:
    def __init__(self, settings: Settings) -> None:
        settings.validate_bind()
        self.settings = settings
        self.database = ReadOnlyDatabase(settings)
        from .objective_reads import ObjectiveReadStore
        from .execution_reads import ExecutionReadStore
        from .review_recovery_reads import ReviewRecoveryReadStore
        from .orchestration_reads import OrchestrationReadStore
        from .sandbox_profiles import SandboxProfileStore
        from .objective_commands import ObjectiveCommandStore
        from .review_commands import ReviewCommandStore
        from .project_commands import ProjectCommandStore
        from .blueprint_lifecycle import BlueprintLifecycleStore
        self.objectives = ObjectiveReadStore(settings)
        self.executions = ExecutionReadStore(settings)
        self.review_recovery = ReviewRecoveryReadStore(settings)
        self.orchestration = OrchestrationReadStore(settings)
        self.sandbox_profiles = SandboxProfileStore(settings)
        self.commands = ObjectiveCommandStore(settings)
        self.project_commands = ProjectCommandStore(settings)
        self.blueprints = BlueprintLifecycleStore(settings, self.sandbox_profiles)
        self.review_commands = ReviewCommandStore(settings)
        from .browser_auth import BrowserAuthStore
        self.browser_auth = BrowserAuthStore(settings)

    def version(self) -> str:
        return API_VERSION

    def session_token(self) -> str:
        path = self.settings.session_file
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)

        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError as error:
            raise ControllerError(
                503,
                "controller_auth_not_configured",
                "Controller authentication is not configured",
                "Create the Controller session file before using protected endpoints.",
            ) from error
        except OSError as error:
            raise ControllerError(
                503,
                "controller_auth_unavailable",
                "Controller authentication is unavailable",
                "The Controller session file cannot be opened safely.",
            ) from error

        try:
            metadata = os.fstat(descriptor)
            mode = stat.S_IMODE(metadata.st_mode)
            if not stat.S_ISREG(metadata.st_mode):
                raise ControllerError(
                    503,
                    "controller_auth_file_invalid",
                    "Controller authentication is unavailable",
                    "The Controller session path must be a regular file.",
                )
            if metadata.st_uid != os.geteuid():
                raise ControllerError(
                    503,
                    "controller_auth_owner_invalid",
                    "Controller authentication is unavailable",
                    "The Controller session file must be owned by the service user.",
                )
            if mode != 0o600:
                raise ControllerError(
                    503,
                    "controller_auth_permissions_invalid",
                    "Controller authentication is unavailable",
                    "The Controller session file must have mode 0600.",
                )
            if metadata.st_nlink != 1:
                raise ControllerError(
                    503,
                    "controller_auth_links_invalid",
                    "Controller authentication is unavailable",
                    "The Controller session file must not have additional hard links.",
                )
            if metadata.st_size > 512:
                raise ControllerError(
                    503,
                    "controller_auth_invalid",
                    "Controller authentication is unavailable",
                    "The configured session value is too large.",
                )
            with os.fdopen(
                descriptor,
                "r",
                encoding="ascii",
                closefd=False,
            ) as stream:
                token = stream.read(513).strip()
        except UnicodeError as error:
            raise ControllerError(
                503,
                "controller_auth_invalid",
                "Controller authentication is unavailable",
                "The configured session value must be ASCII.",
            ) from error
        finally:
            os.close(descriptor)

        if not SESSION_VALUE_PATTERN.fullmatch(token):
            raise ControllerError(
                503,
                "controller_auth_invalid",
                "Controller authentication is unavailable",
                "The configured session value has an invalid format.",
            )
        return token

    def authenticate_context(self, cookie_header: str | None) -> Any:
        return self.browser_auth.authenticate_cookie(
            cookie_header,
            self.session_token(),
        )

    def authenticate(self, cookie_header: str | None) -> str:
        return self.authenticate_context(cookie_header).secret

    def session_is_current(self, session_secret: str) -> bool:
        try:
            bootstrap_secret = self.session_token()
        except ControllerError:
            return False
        return self.browser_auth.session_is_current(
            session_secret,
            bootstrap_secret,
        )

    @staticmethod
    def request_id(candidate: str | None = None) -> str:
        if candidate and re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", candidate):
            return candidate
        return str(uuid.uuid4())

    @staticmethod
    def meta(
        request_id: str,
        *,
        resource_revision: int | None = None,
        next_cursor: str | None = None,
        snapshot_sequence: int | None = None,
    ) -> dict[str, Any]:
        return {
            "request_id": request_id,
            "resource_revision": resource_revision,
            "next_cursor": next_cursor,
            "snapshot_sequence": snapshot_sequence,
        }

    def readiness(self) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        database_ready, database_reason = self.database.readiness()
        if not database_ready:
            reasons.append(database_reason)
        command_ready, command_reason = self.commands.readiness()
        if not command_ready:
            reasons.append(command_reason)
        project_command_ready, project_command_reason = self.project_commands.readiness()
        if not project_command_ready:
            reasons.append(project_command_reason)
        review_command_ready, review_command_reason = self.review_commands.readiness()
        if not review_command_ready:
            reasons.append(review_command_reason)
        sandbox_ready, sandbox_reason = self.sandbox_profiles.readiness()
        if not sandbox_ready:
            reasons.append(sandbox_reason)
        blueprint_ready, blueprint_reason = self.blueprints.readiness()
        if not blueprint_ready:
            reasons.append(blueprint_reason)
        try:
            self.session_token()
        except ControllerError as error:
            reasons.append(error.code)
        return not reasons, reasons

    def capabilities(self) -> dict[str, Any]:
        return {
            "api_versions": [API_VERSION],
            "event_schema_versions": [1],
            "blueprint_versions": ["v1"],
            "features": {
                "read_only_controller_api": False,
                "project_reads": True,
                "project_writes": True,
                "project_write_commands": [
                    "create", "update", "enable", "disable", "rescan", "archive"
                ],
                "project_delete": False,
                "objective_reads": True,
                "operation_reads": True,
                "legacy_operation_projection": True,
                "task_reads": True,
                "run_reads": True,
                "worker_execution_reads": True,
                "persisted_event_log_reads": True,
                "review_reads": True,
                "review_evidence_reads": True,
                "integration_summary_reads": True,
                "recovery_reads": True,
                "orchestration_plan_reads": True,
                "orchestration_graph_reads": True,
                "orchestration_attempt_reads": True,
                "reviewer_assignment_reads": True,
                "raw_orchestration_payload_reads": False,
                "raw_review_artifact_reads": False,
                "raw_worker_log_reads": False,
                "run_artifact_reads": False,
                "durable_controller_operations": True,
                "objective_writes": True,
                "objective_write_commands": ["create", "pause", "resume", "cancel"],
                "review_writes": True,
                "review_write_commands": ["acknowledge-debt", "request-human-review"],
                "review_rerun": False,
                "review_write_if_match": False,
                "csrf_challenges": True,
                "idempotent_mutations": True,
                "websocket_events": True,
                "browser_session_lifecycle": self.browser_auth.readiness()[0],
                "operator_login": self.browser_auth.readiness()[0],
                "sandbox_profile_reads": True,
                "sandbox_profile_operator_import": True,
                "sandbox_profile_http_writes": True,
                "sandbox_profile_http_validation": True,
                "blueprint_revision_history": True,
                "blueprint_revision_comparison": True,
                "blueprint_runtime_projection": True,
                "blueprint_builds": False,
                "console": False,
            },
        }


    def get_operation(self, operation_id: str) -> dict[str, Any]:
        blueprint_operation = self.blueprints.get_operation(operation_id)
        if blueprint_operation is not None:
            return blueprint_operation
        project_operation = self.project_commands.get_operation(operation_id)
        if project_operation is not None:
            return project_operation
        review_operation = self.review_commands.get_operation(operation_id)
        if review_operation is not None:
            return review_operation
        controller_operation = self.commands.get_operation(operation_id)
        if controller_operation is not None:
            return controller_operation
        return self.objectives.get_operation(operation_id)

    def system_status(self) -> dict[str, Any]:
        database_ready, _ = self.database.readiness()
        components = [
            {"name": SERVICE_NAME, "status": "healthy"},
            {
                "name": "sqlite",
                "status": "healthy" if database_ready else "unavailable",
            },
            {"name": "hermes-agent", "status": "unknown"},
            {"name": "sandbox-engine", "status": "unknown"},
        ]
        return {
            "status": "healthy" if database_ready else "unavailable",
            "components": components,
            "capabilities": {
                key: value
                for key, value in self.capabilities().items()
                if key != "features"
            },
        }

    @staticmethod
    def problem(
        error: ControllerError,
        request_id: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "type": f"urn:orchestra:problem:{error.code}",
            "title": error.title,
            "status": error.status,
            "code": error.code,
            "request_id": request_id,
        }
        if error.detail:
            payload["detail"] = error.detail
        if error.resource is not None:
            payload["resource"] = error.resource
        return payload
