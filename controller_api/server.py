from __future__ import annotations

import json
import logging
import re
import signal
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from . import SERVICE_NAME
from .core import ControllerError, ControllerService, Settings
from .websocket_transport import (
    WEBSOCKET_PATH,
    WebSocketSession,
    header_has_token,
    parse_last_event_sequence,
    websocket_accept_value,
)

LOGGER = logging.getLogger(SERVICE_NAME)
MAX_REQUEST_TARGET_BYTES = 4096
MAX_JSON_BODY_BYTES = 384 * 1024
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 4096
MAX_QUERY_FIELDS = 8
ALLOWED_PROJECT_QUERY_FIELDS = {"cursor", "limit"}
ALLOWED_OBJECTIVE_QUERY_FIELDS = {"cursor", "limit", "project_id", "state"}
ALLOWED_EXECUTION_LIST_QUERY_FIELDS = {"cursor", "limit"}
ALLOWED_REVIEW_RECOVERY_QUERY_FIELDS = {"cursor", "limit", "project_id", "state"}
ALLOWED_PLAN_QUERY_FIELDS = {"cursor", "limit", "project_id", "state"}
ALLOWED_ASSIGNMENT_QUERY_FIELDS = {"cursor", "limit", "project_id", "state", "run_id"}
ALLOWED_SANDBOX_QUERY_FIELDS = {"cursor", "limit", "state"}
ALLOWED_BLUEPRINT_LIST_QUERY_FIELDS = {"cursor", "limit", "state"}
ALLOWED_LOG_QUERY_FIELDS = {"after_sequence", "limit"}


class ControllerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(
        self,
        server_address: tuple[str, int],
        service: ControllerService,
    ) -> None:
        self.service = service
        self._request_slots = threading.BoundedSemaphore(
            service.settings.max_concurrent_requests
        )
        self._websocket_slots = threading.BoundedSemaphore(
            service.settings.max_websocket_connections
        )
        self._request_slot_state = threading.local()
        super().__init__(server_address, ControllerRequestHandler)

    def acquire_websocket_slot(self) -> bool:
        return self._websocket_slots.acquire(blocking=False)

    def release_websocket_slot(self) -> None:
        self._websocket_slots.release()

    def release_request_slot_for_websocket(self) -> None:
        if getattr(self._request_slot_state, "released", False):
            raise RuntimeError("request slot already released")
        self._request_slots.release()
        self._request_slot_state.released = True

    def get_request(self) -> tuple[socket.socket, Any]:
        request, client_address = super().get_request()
        request.settimeout(self.service.settings.socket_timeout_seconds)
        return request, client_address

    def process_request(
        self,
        request: socket.socket,
        client_address: Any,
    ) -> None:
        if not self._request_slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 0\r\n"
                    b"Cache-Control: no-store\r\n"
                    b"\r\n"
                )
            except OSError:
                pass
            finally:
                request.close()
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._request_slots.release()
            raise

    def process_request_thread(
        self,
        request: socket.socket,
        client_address: Any,
    ) -> None:
        self._request_slot_state.released = False
        try:
            super().process_request_thread(request, client_address)
        finally:
            if not self._request_slot_state.released:
                self._request_slots.release()
            self._request_slot_state.__dict__.clear()


class ControllerRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = SERVICE_NAME
    sys_version = ""

    @property
    def controller(self) -> ControllerHTTPServer:
        return self.server  # type: ignore[return-value]

    @staticmethod
    def _safe_log(value: str) -> str:
        sanitized = "".join(
            character
            if character.isprintable() and character not in "\r\n"
            else "?"
            for character in value
        )
        request_line = re.match(
            r'^(\"[A-Z]+ [^ ?\"]+)\?[^ \"]* (HTTP/[0-9.]+\".*)$',
            sanitized,
        )
        if request_line is not None:
            return (
                request_line.group(1)
                + "?[query-redacted] "
                + request_line.group(2)
            )
        return sanitized

    def log_message(self, format_string: str, *args: object) -> None:
        LOGGER.info(
            "%s %s",
            self.client_address[0],
            self._safe_log(format_string % args),
        )

    def _request_id(self) -> str:
        return self.controller.service.request_id(
            self.headers.get("X-Request-ID")
        )

    def _security_headers(self) -> dict[str, str]:
        return {
            "Cross-Origin-Resource-Policy": "same-origin",
            "Content-Security-Policy": (
                "default-src 'none'; frame-ancestors 'none'"
            ),
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
        }

    def _cors_headers(self) -> dict[str, str]:
        values = self.headers.get_all("Origin", failobj=[])
        if (
            len(values) == 1
            and values[0] == self.controller.service.settings.console_origin
        ):
            return {
                "Access-Control-Allow-Origin": values[0],
                "Access-Control-Allow-Credentials": "true",
                "Vary": "Origin",
            }
        return {}

    def _validate_browser_origin(self) -> None:
        origin = self._single_header("Origin", required=True)
        if origin != self.controller.service.settings.console_origin:
            raise ControllerError(403, "origin_forbidden", "Forbidden request origin")

    @staticmethod
    def _session_cookie(token: str, max_age: int) -> str:
        return (
            f"hermesops_session={token}; Path=/; Max-Age={max_age}; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    @staticmethod
    def _clear_session_cookie() -> str:
        return (
            "hermesops_session=; Path=/; Max-Age=0; "
            "HttpOnly; Secure; SameSite=Strict"
        )

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        request_id: str,
        *,
        content_type: str = "application/json",
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
        close_connection: bool = False,
    ) -> None:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")

        if close_connection:
            self.close_connection = True

        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Request-ID", request_id)
        for name, value in self._security_headers().items():
            self.send_header(name, value)
        for name, value in self._cors_headers().items():
            self.send_header(name, value)
        if close_connection:
            self.send_header("Connection", "close")
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()

        if not head_only:
            try:
                self.wfile.write(body)
            except (
                BrokenPipeError,
                ConnectionResetError,
                TimeoutError,
                socket.timeout,
            ):
                self.close_connection = True

    def _problem(
        self,
        error: ControllerError,
        request_id: str,
        *,
        head_only: bool = False,
        close_connection: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send_json(
            error.status,
            self.controller.service.problem(error, request_id),
            request_id,
            content_type="application/problem+json",
            head_only=head_only,
            close_connection=close_connection,
            extra_headers=extra_headers,
        )

    def _single_header(
        self,
        name: str,
        *,
        required: bool = False,
    ) -> str | None:
        values = self.headers.get_all(name, failobj=[])
        if len(values) > 1:
            raise ControllerError(
                400,
                "ambiguous_header",
                "Ambiguous request header",
                f"The {name} header must appear at most once.",
            )
        value = values[0] if values else None
        if required and (value is None or not value.strip()):
            raise ControllerError(
                400,
                "header_required",
                "Required request header missing",
                f"The {name} header is required.",
            )
        return value

    def _validate_host(self) -> None:
        host = self._single_header("Host")
        if not host:
            raise ControllerError(
                400,
                "host_header_required",
                "Host header required",
            )

        accepted = {
            "localhost",
            f"localhost:{self.server.server_address[1]}",
            "127.0.0.1",
            f"127.0.0.1:{self.server.server_address[1]}",
            "[::1]",
            f"[::1]:{self.server.server_address[1]}",
        }
        if host.lower() not in accepted:
            raise ControllerError(
                421,
                "misdirected_request",
                "Misdirected request",
                "The Host header is not a permitted loopback authority.",
            )

    def _validate_request_target(self) -> None:
        if len(self.path.encode("utf-8", errors="replace")) > (
            MAX_REQUEST_TARGET_BYTES
        ):
            raise ControllerError(
                414,
                "request_target_too_long",
                "Request target too long",
            )

    def _reject_request_body(self) -> None:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        content_length = self._single_header("Content-Length")

        if transfer_encoding:
            raise ControllerError(
                400,
                "request_body_not_allowed",
                "Request body not allowed",
                "GET and HEAD requests must not use Transfer-Encoding.",
            )

        if content_length is None:
            return

        try:
            length = int(content_length)
        except ValueError as error:
            raise ControllerError(
                400,
                "invalid_content_length",
                "Invalid Content-Length",
            ) from error

        if length < 0:
            raise ControllerError(
                400,
                "invalid_content_length",
                "Invalid Content-Length",
            )
        if length > 0:
            raise ControllerError(
                400,
                "request_body_not_allowed",
                "Request body not allowed",
                "GET and HEAD requests must not include a request body.",
            )

    def _technical_probe(
        self,
        path: str,
        request_id: str,
    ) -> tuple[int, dict[str, Any]]:
        service = self.controller.service
        if path == "/health":
            return 200, {
                "status": "ok",
                "service": SERVICE_NAME,
                "version": service.version(),
                "request_id": request_id,
            }
        if path == "/version":
            return 200, {
                "service": SERVICE_NAME,
                "version": service.version(),
                "api_version": "v1",
                "request_id": request_id,
            }
        if path == "/ready":
            ready, reasons = service.readiness()
            return (200 if ready else 503), {
                "status": "ready" if ready else "not_ready",
                "service": SERVICE_NAME,
                "reasons": reasons,
                "request_id": request_id,
            }
        raise ControllerError(
            404,
            "route_not_found",
            "Route not found",
            "The requested Controller API route does not exist.",
        )

    def _parse_query(self, raw_query: str) -> dict[str, list[str]]:
        try:
            return parse_qs(
                raw_query,
                keep_blank_values=True,
                strict_parsing=False,
                max_num_fields=MAX_QUERY_FIELDS,
            )
        except ValueError as error:
            raise ControllerError(
                400,
                "invalid_query",
                "Invalid query string",
                "The query string contains too many fields.",
            ) from error

    def _protected_get(
        self,
        path: str,
        query: dict[str, list[str]],
        request_id: str,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        service = self.controller.service
        authenticated = service.authenticate_context(self._single_header("Cookie"))
        session_token = authenticated.secret

        if path == "/api/v1/auth/session":
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            return 200, {
                "data": service.browser_auth.session_payload(authenticated),
                "meta": service.meta(request_id),
            }, {}

        if path == "/api/v1/system/health":
            payload = {
                "data": {
                    "status": (
                        "ok"
                        if service.database.readiness()[0]
                        else "unavailable"
                    )
                },
                "meta": service.meta(request_id),
            }
            return 200, payload, {}

        if path == "/api/v1/system/capabilities":
            return 200, {
                "data": service.capabilities(),
                "meta": service.meta(request_id),
            }, {}

        if path == "/api/v1/system/status":
            return 200, {
                "data": service.system_status(),
                "meta": service.meta(request_id),
            }, {}

        if path == "/api/v1/sandboxes":
            unknown = set(query) - ALLOWED_SANDBOX_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor, limit and state are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            raw_state = query.get("state", [])
            if (
                len(raw_limit) != 1
                or len(raw_cursor) > 1
                or len(raw_state) > 1
            ):
                raise ControllerError(
                    400,
                    "invalid_query",
                    "Invalid query string",
                )
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                    "limit must be an integer.",
                ) from error
            profiles, next_cursor = service.sandbox_profiles.list_profiles(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                state=raw_state[0] if raw_state else None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": profiles,
                "meta": service.meta(
                    request_id,
                    next_cursor=next_cursor,
                ),
            }, {}

        sandbox_prefix = "/api/v1/sandboxes/"
        if path.startswith(sandbox_prefix):
            if query:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                )
            sandbox_id = unquote(path[len(sandbox_prefix):])
            if not sandbox_id or "/" in sandbox_id:
                raise ControllerError(
                    404,
                    "route_not_found",
                    "Route not found",
                )
            profile = service.sandbox_profiles.get_profile(sandbox_id)
            revision = int(profile["resource_revision"])
            return 200, {
                "data": profile,
                "meta": service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            }, {"ETag": f'"{revision}"'}

        if path == "/api/v1/blueprints/template":
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            return 200, {
                "data": service.blueprints.template(),
                "meta": service.meta(request_id),
            }, {}

        if path == "/api/v1/blueprints":
            unknown = set(query) - ALLOWED_BLUEPRINT_LIST_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor, limit and state are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            raw_state = query.get("state", [])
            if len(raw_limit) != 1 or len(raw_cursor) > 1 or len(raw_state) > 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(400, "invalid_limit", "Invalid pagination limit") from error
            profiles, next_cursor = service.sandbox_profiles.list_profiles(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                state=raw_state[0] if raw_state else None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": profiles,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        blueprint_prefix = "/api/v1/blueprints/"
        if path.startswith(blueprint_prefix):
            suffix = path[len(blueprint_prefix):]
            parts = [unquote(part) for part in suffix.split("/")]
            if not parts or not parts[0] or any(not part for part in parts):
                raise ControllerError(404, "route_not_found", "Route not found")
            sandbox_id = parts[0]
            if len(parts) == 1:
                if query:
                    raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
                current = service.blueprints.current(sandbox_id)
                revision = int(current["profile"]["resource_revision"])
                return 200, {
                    "data": current,
                    "meta": service.meta(request_id, resource_revision=revision),
                }, {"ETag": f'"{revision}"'}
            if len(parts) == 2 and parts[1] == "revisions":
                unknown = set(query) - {"limit"}
                if unknown or len(query.get("limit", ["50"])) != 1:
                    raise ControllerError(400, "invalid_query", "Invalid query string")
                try:
                    limit = int(query.get("limit", ["50"])[0])
                except ValueError as error:
                    raise ControllerError(400, "invalid_limit", "Invalid pagination limit") from error
                revisions = service.blueprints.list_revisions(sandbox_id, limit=limit)
                return 200, {
                    "data": revisions,
                    "meta": service.meta(request_id),
                }, {}
            if len(parts) == 3 and parts[1] == "revisions":
                if query:
                    raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
                try:
                    source_revision = int(parts[2])
                except ValueError as error:
                    raise ControllerError(404, "blueprint_revision_not_found", "Blueprint revision not found") from error
                revision_data = service.blueprints.get_revision(sandbox_id, source_revision)
                return 200, {
                    "data": revision_data,
                    "meta": service.meta(request_id),
                }, {}
            if len(parts) == 2 and parts[1] == "diff":
                if set(query) != {"from", "to"} or len(query["from"]) != 1 or len(query["to"]) != 1:
                    raise ControllerError(400, "invalid_revision_comparison", "Invalid revision comparison")
                try:
                    from_revision = int(query["from"][0])
                    to_revision = int(query["to"][0])
                except ValueError as error:
                    raise ControllerError(400, "invalid_revision_comparison", "Invalid revision comparison") from error
                comparison = service.blueprints.compare(sandbox_id, from_revision, to_revision)
                return 200, {
                    "data": comparison,
                    "meta": service.meta(request_id),
                }, {}
            raise ControllerError(404, "route_not_found", "Route not found")

        if path == "/api/v1/projects":
            unknown = set(query) - ALLOWED_PROJECT_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor and limit are supported.",
                )

            raw_limit = query.get("limit", ["50"])
            if len(raw_limit) != 1:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                )
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                    "limit must be an integer.",
                ) from error

            raw_cursor = query.get("cursor", [])
            if len(raw_cursor) > 1:
                raise ControllerError(
                    400,
                    "invalid_cursor",
                    "Invalid pagination cursor",
                )
            cursor = raw_cursor[0] if raw_cursor else None
            projects, next_cursor = service.database.list_projects(
                limit=limit,
                cursor=cursor,
            )
            return 200, {
                "data": projects,
                "meta": service.meta(
                    request_id,
                    next_cursor=next_cursor,
                ),
            }, {}

        if path == "/api/v1/plans":
            unknown = set(query) - ALLOWED_PLAN_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor, limit, project_id and state are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            raw_project = query.get("project_id", [])
            raw_state = query.get("state", [])
            if (
                len(raw_limit) != 1
                or len(raw_cursor) > 1
                or len(raw_project) > 1
                or len(raw_state) > 1
            ):
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                    "limit must be an integer.",
                ) from error
            plans, next_cursor = service.orchestration.list_plans(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                project_id=raw_project[0] if raw_project else None,
                state=raw_state[0] if raw_state else None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": plans,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        plan_prefix = "/api/v1/plans/"
        if path.startswith(plan_prefix):
            nested_routes = (
                ("/tasks", service.orchestration.list_plan_tasks),
                ("/dependencies", service.orchestration.list_plan_dependencies),
                ("/attempts", service.orchestration.list_plan_attempts),
            )
            for suffix, reader in nested_routes:
                if not path.endswith(suffix):
                    continue
                plan_id = unquote(path[len(plan_prefix):-len(suffix)])
                if not plan_id or "/" in plan_id:
                    raise ControllerError(404, "route_not_found", "Route not found")
                unknown = set(query) - ALLOWED_EXECUTION_LIST_QUERY_FIELDS
                if unknown:
                    raise ControllerError(
                        400,
                        "unknown_query_parameter",
                        "Unknown query parameter",
                        "Only cursor and limit are supported.",
                    )
                raw_limit = query.get("limit", ["50"])
                raw_cursor = query.get("cursor", [])
                if len(raw_limit) != 1 or len(raw_cursor) > 1:
                    raise ControllerError(400, "invalid_query", "Invalid query string")
                try:
                    limit = int(raw_limit[0])
                except ValueError as error:
                    raise ControllerError(
                        400,
                        "invalid_limit",
                        "Invalid pagination limit",
                    ) from error
                items, next_cursor = reader(
                    plan_id,
                    limit=limit,
                    cursor=raw_cursor[0] if raw_cursor else None,
                    cursor_secret=session_token,
                )
                return 200, {
                    "data": items,
                    "meta": service.meta(request_id, next_cursor=next_cursor),
                }, {}

            if query:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                )
            plan_id = unquote(path[len(plan_prefix):])
            if not plan_id or "/" in plan_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            plan = service.orchestration.get_plan(plan_id)
            revision = int(plan["resource_revision"])
            return 200, {
                "data": plan,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        if path == "/api/v1/reviewer-assignments":
            unknown = set(query) - ALLOWED_ASSIGNMENT_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor, limit, project_id, state and run_id are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            raw_project = query.get("project_id", [])
            raw_state = query.get("state", [])
            raw_run = query.get("run_id", [])
            if (
                len(raw_limit) != 1
                or len(raw_cursor) > 1
                or len(raw_project) > 1
                or len(raw_state) > 1
                or len(raw_run) > 1
            ):
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                    "limit must be an integer.",
                ) from error
            assignments, next_cursor = service.orchestration.list_assignments(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                project_id=raw_project[0] if raw_project else None,
                state=raw_state[0] if raw_state else None,
                run_id=raw_run[0] if raw_run else None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": assignments,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        assignment_prefix = "/api/v1/reviewer-assignments/"
        if path.startswith(assignment_prefix):
            if query:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                )
            assignment_id = unquote(path[len(assignment_prefix):])
            if not assignment_id or "/" in assignment_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            assignment = service.orchestration.get_assignment(assignment_id)
            revision = int(assignment["resource_revision"])
            return 200, {
                "data": assignment,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        if path == "/api/v1/objectives":
            unknown = set(query) - ALLOWED_OBJECTIVE_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor, limit, project_id and state are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            if len(raw_limit) != 1:
                raise ControllerError(400, "invalid_limit", "Invalid pagination limit")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                    "limit must be an integer.",
                ) from error
            raw_cursor = query.get("cursor", [])
            raw_project = query.get("project_id", [])
            raw_state = query.get("state", [])
            if len(raw_cursor) > 1 or len(raw_project) > 1 or len(raw_state) > 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            project_id = raw_project[0] if raw_project else None
            state = raw_state[0] if raw_state else None
            objectives, next_cursor = service.objectives.list_objectives(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                project_id=project_id,
                state=state,
                cursor_secret=session_token,
            )
            return 200, {
                "data": objectives,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        nested_prefix = "/api/v1/projects/"
        nested_suffix = "/objectives"
        if path.startswith(nested_prefix) and path.endswith(nested_suffix):
            project_id = unquote(path[len(nested_prefix):-len(nested_suffix)])
            if not project_id or "/" in project_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            unknown = set(query) - {"cursor", "limit"}
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor and limit are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            if len(raw_limit) != 1 or len(raw_cursor) > 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(400, "invalid_limit", "Invalid pagination limit") from error
            objectives, next_cursor = service.objectives.list_objectives(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                project_id=project_id,
                state=None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": objectives,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        if path in {"/api/v1/reviews", "/api/v1/recoveries"}:
            unknown = set(query) - ALLOWED_REVIEW_RECOVERY_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor, limit, project_id and state are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            raw_project = query.get("project_id", [])
            raw_state = query.get("state", [])
            if (
                len(raw_limit) != 1
                or len(raw_cursor) > 1
                or len(raw_project) > 1
                or len(raw_state) > 1
            ):
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                    "limit must be an integer.",
                ) from error
            project_id = raw_project[0] if raw_project else None
            state = raw_state[0] if raw_state else None
            if path == "/api/v1/reviews":
                items, next_cursor = service.review_recovery.list_reviews(
                    limit=limit,
                    cursor=raw_cursor[0] if raw_cursor else None,
                    project_id=project_id,
                    state=state,
                    cursor_secret=session_token,
                )
            else:
                items, next_cursor = service.review_recovery.list_recoveries(
                    limit=limit,
                    cursor=raw_cursor[0] if raw_cursor else None,
                    project_id=project_id,
                    state=state,
                    cursor_secret=session_token,
                )
            return 200, {
                "data": items,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        review_prefix = "/api/v1/reviews/"
        review_evidence_suffix = "/evidence"
        if path.startswith(review_prefix) and path.endswith(review_evidence_suffix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            review_id = unquote(path[len(review_prefix):-len(review_evidence_suffix)])
            if not review_id or "/" in review_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            evidence = service.review_recovery.get_review_evidence(review_id)
            return 200, {
                "data": evidence,
                "meta": service.meta(request_id),
            }, {}

        if path.startswith(review_prefix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            review_id = unquote(path[len(review_prefix):])
            if not review_id or "/" in review_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            review = service.review_recovery.get_review(review_id)
            revision = int(review["resource_revision"])
            return 200, {
                "data": review,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        recovery_prefix = "/api/v1/recoveries/"
        if path.startswith(recovery_prefix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            recovery_id = unquote(path[len(recovery_prefix):])
            if not recovery_id or "/" in recovery_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            recovery = service.review_recovery.get_recovery(recovery_id)
            revision = int(recovery["resource_revision"])
            return 200, {
                "data": recovery,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        objective_prefix = "/api/v1/objectives/"
        objective_tasks_suffix = "/tasks"
        if path.startswith(objective_prefix) and path.endswith(objective_tasks_suffix):
            objective_id = unquote(
                path[len(objective_prefix):-len(objective_tasks_suffix)]
            )
            if not objective_id or "/" in objective_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            unknown = set(query) - ALLOWED_EXECUTION_LIST_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor and limit are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            if len(raw_limit) != 1 or len(raw_cursor) > 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(400, "invalid_limit", "Invalid pagination limit") from error
            tasks, next_cursor = service.executions.list_objective_tasks(
                objective_id,
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": tasks,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        task_prefix = "/api/v1/tasks/"
        task_runs_suffix = "/runs"
        if path.startswith(task_prefix) and path.endswith(task_runs_suffix):
            task_id = unquote(path[len(task_prefix):-len(task_runs_suffix)])
            if not task_id or "/" in task_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            unknown = set(query) - ALLOWED_EXECUTION_LIST_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor and limit are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            if len(raw_limit) != 1 or len(raw_cursor) > 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(400, "invalid_limit", "Invalid pagination limit") from error
            runs, next_cursor = service.executions.list_task_runs(
                task_id,
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                cursor_secret=session_token,
            )
            return 200, {
                "data": runs,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        run_prefix = "/api/v1/runs/"
        nested_assignment_suffix = "/reviewer-assignments"
        if path.startswith(run_prefix) and path.endswith(nested_assignment_suffix):
            run_id = unquote(path[len(run_prefix):-len(nested_assignment_suffix)])
            if not run_id or "/" in run_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            unknown = set(query) - ALLOWED_EXECUTION_LIST_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only cursor and limit are supported.",
                )
            raw_limit = query.get("limit", ["50"])
            raw_cursor = query.get("cursor", [])
            if len(raw_limit) != 1 or len(raw_cursor) > 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(
                    400,
                    "invalid_limit",
                    "Invalid pagination limit",
                ) from error
            service.executions.get_run(run_id)
            assignments, next_cursor = service.orchestration.list_assignments(
                limit=limit,
                cursor=raw_cursor[0] if raw_cursor else None,
                project_id=None,
                state=None,
                run_id=run_id,
                cursor_secret=session_token,
            )
            return 200, {
                "data": assignments,
                "meta": service.meta(request_id, next_cursor=next_cursor),
            }, {}

        run_logs_suffix = "/logs"
        if path.startswith(run_prefix) and path.endswith(run_logs_suffix):
            run_id = unquote(path[len(run_prefix):-len(run_logs_suffix)])
            if not run_id or "/" in run_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            unknown = set(query) - ALLOWED_LOG_QUERY_FIELDS
            if unknown:
                raise ControllerError(
                    400,
                    "unknown_query_parameter",
                    "Unknown query parameter",
                    "Only after_sequence and limit are supported.",
                )
            raw_after = query.get("after_sequence", ["0"])
            raw_limit = query.get("limit", ["200"])
            if len(raw_after) != 1 or len(raw_limit) != 1:
                raise ControllerError(400, "invalid_query", "Invalid query string")
            try:
                after_sequence = int(raw_after[0])
                limit = int(raw_limit[0])
            except ValueError as error:
                raise ControllerError(400, "invalid_log_query", "Invalid log query") from error
            chunk, snapshot_sequence = service.executions.get_run_logs(
                run_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            return 200, {
                "data": chunk,
                "meta": service.meta(
                    request_id,
                    snapshot_sequence=snapshot_sequence,
                ),
            }, {}

        if path.startswith(task_prefix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            task_id = unquote(path[len(task_prefix):])
            if not task_id or "/" in task_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            task = service.executions.get_task(task_id)
            revision = int(task["resource_revision"])
            return 200, {
                "data": task,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        if path.startswith(run_prefix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            run_id = unquote(path[len(run_prefix):])
            if not run_id or "/" in run_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            run = service.executions.get_run(run_id)
            revision = int(run["resource_revision"])
            return 200, {
                "data": run,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        objective_prefix = "/api/v1/objectives/"
        if path.startswith(objective_prefix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            objective_id = unquote(path[len(objective_prefix):])
            if not objective_id or "/" in objective_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            objective = service.objectives.get_objective(objective_id)
            revision = int(objective["resource_revision"])
            return 200, {
                "data": objective,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        operation_prefix = "/api/v1/operations/"
        if path.startswith(operation_prefix):
            if query:
                raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
            operation_id = unquote(path[len(operation_prefix):])
            if not operation_id or "/" in operation_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            operation = service.get_operation(operation_id)
            revision = int(operation["resource_revision"])
            return 200, {
                "data": operation,
                "meta": service.meta(request_id, resource_revision=revision),
            }, {"ETag": f'"{revision}"'}

        if query:
            raise ControllerError(
                400,
                "unknown_query_parameter",
                "Unknown query parameter",
                "This route does not accept query parameters.",
            )

        prefix = "/api/v1/projects/"
        if path.startswith(prefix):
            project_id = unquote(path[len(prefix):])
            if not project_id or "/" in project_id:
                raise ControllerError(
                    404,
                    "route_not_found",
                    "Route not found",
                )
            project = service.database.get_project(project_id)
            revision = int(project["resource_revision"])
            return 200, {
                "data": project,
                "meta": service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            }, {"ETag": f'"{revision}"'}

        raise ControllerError(
            404,
            "route_not_found",
            "Route not found",
            "The requested Controller API route does not exist.",
        )

    def _validate_origin(self) -> None:
        origin = self._single_header("Origin")
        if origin is None:
            return
        if origin == self.controller.service.settings.console_origin:
            return
        host = self._single_header("Host") or ""
        if origin not in {f"http://{host}", f"https://{host}"}:
            raise ControllerError(403, "origin_forbidden", "Forbidden request origin")

    def _read_json_body(self) -> dict[str, Any]:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding:
            raise ControllerError(
                400,
                "transfer_encoding_forbidden",
                "Transfer-Encoding is not supported",
            )
        content_type = self._single_header("Content-Type") or ""
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ControllerError(
                415,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        raw_length = self._single_header("Content-Length")
        if raw_length is None:
            raise ControllerError(411, "content_length_required", "Content-Length required")
        normalized_length = raw_length.strip()
        if not re.fullmatch(r"[0-9]+", normalized_length):
            raise ControllerError(
                400,
                "invalid_content_length",
                "Invalid Content-Length",
            )
        length = int(normalized_length)
        if length < 2 or length > MAX_JSON_BODY_BYTES:
            raise ControllerError(413, "request_body_too_large", "Invalid request body size")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise ControllerError(400, "incomplete_request_body", "Incomplete request body")
        def strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise ValueError("duplicate JSON object member")
                result[key] = item
            return result

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON number: {value}")

        try:
            value = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=strict_object,
                parse_constant=reject_constant,
            )
            stack: list[tuple[Any, int]] = [(value, 0)]
            nodes = 0
            while stack:
                current, depth = stack.pop()
                nodes += 1
                if depth > MAX_JSON_DEPTH or nodes > MAX_JSON_NODES:
                    raise ValueError("JSON structure exceeds safety bounds")
                if isinstance(current, dict):
                    stack.extend((item, depth + 1) for item in current.values())
                elif isinstance(current, list):
                    stack.extend((item, depth + 1) for item in current)
            json.dumps(value, ensure_ascii=False).encode("utf-8")
        except (
            UnicodeDecodeError,
            UnicodeEncodeError,
            json.JSONDecodeError,
            RecursionError,
            ValueError,
        ) as error:
            raise ControllerError(400, "invalid_json", "Invalid JSON request body") from error
        if not isinstance(value, dict):
            raise ControllerError(400, "invalid_json_object", "JSON body must be an object")
        return value

    def _protected_post(
        self,
        path: str,
        query: dict[str, list[str]],
        request_id: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if query:
            raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
        service = self.controller.service
        idempotency_key = self._single_header("Idempotency-Key")

        if path == "/api/v1/auth/login":
            self._validate_browser_origin()
            if set(body) != {"username", "password"}:
                raise ControllerError(400, "invalid_login", "Invalid login request")
            result = service.browser_auth.login(
                username=body.get("username"),
                password=body.get("password"),
                idempotency_key=service.browser_auth.validate_idempotency_key(idempotency_key),
                bootstrap_secret=service.session_token(),
                source=str(self.client_address[0]),
                user_agent=self._single_header("User-Agent") or "",
                request_id=request_id,
            )
            return 200, {
                "data": result.payload,
                "meta": service.meta(request_id),
            }, {"Set-Cookie": self._session_cookie(result.token, result.max_age)}

        if path == "/api/v1/auth/logout":
            self._validate_browser_origin()
            if body:
                raise ControllerError(400, "invalid_logout", "Invalid logout request")
            bootstrap_secret = service.session_token()
            authenticated = service.browser_auth.logout_context(
                self._single_header("Cookie"),
                bootstrap_secret,
            )
            service.commands.verify_csrf_token(
                authenticated.secret,
                self._single_header("X-CSRF-Token"),
            )
            payload = service.browser_auth.logout(
                session=authenticated,
                idempotency_key=service.browser_auth.validate_idempotency_key(idempotency_key),
                bootstrap_secret=bootstrap_secret,
                request_id=request_id,
            )
            return 200, {
                "data": payload,
                "meta": service.meta(request_id),
            }, {"Set-Cookie": self._clear_session_cookie()}

        self._validate_origin()
        session_token = service.authenticate(self._single_header("Cookie"))

        if path == "/api/v1/auth/csrf":
            status, payload = service.commands.issue_csrf(
                session_token=session_token,
                idempotency_key=service.commands.validate_idempotency_key(idempotency_key),
                route=path,
                body=body,
                meta_factory=lambda: service.meta(request_id),
            )
            return status, payload, {}

        service.commands.verify_csrf_token(
            session_token,
            self._single_header("X-CSRF-Token"),
        )
        key = service.commands.validate_idempotency_key(idempotency_key)
        if path == "/api/v1/blueprints/validate":
            service.blueprints.validate_idempotency_key(key)
            source = service.blueprints._source_bytes(body)
            preview = service.blueprints.preview_source(source)
            return 200, {
                "data": preview,
                "meta": service.meta(request_id),
            }, {}

        if path == "/api/v1/blueprints":
            status, payload = service.blueprints.create(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        if path == "/api/v1/projects":
            status, payload = service.project_commands.create_project(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        project_prefix = "/api/v1/projects/"
        marker = "/commands/"
        if path.startswith(project_prefix) and marker in path[len(project_prefix):]:
            project_part, command = path[len(project_prefix):].split(marker, 1)
            project_id = unquote(project_part)
            command = unquote(command)
            if not project_id or "/" in project_id or not command or "/" in command:
                raise ControllerError(404, "route_not_found", "Route not found")
            status, payload = service.project_commands.command_project(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                project_id=project_id,
                command=command,
                if_match=self._single_header("If-Match"),
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        if path == "/api/v1/objectives":
            status, payload = service.commands.create_objective(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        prefix = "/api/v1/objectives/"
        if path.startswith(prefix) and marker in path[len(prefix):]:
            objective_part, command = path[len(prefix):].split(marker, 1)
            objective_id = unquote(objective_part)
            command = unquote(command)
            if not objective_id or "/" in objective_id or not command or "/" in command:
                raise ControllerError(404, "route_not_found", "Route not found")
            status, payload = service.commands.command_objective(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                objective_id=objective_id,
                command=command,
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        review_prefix = "/api/v1/reviews/"
        if path.startswith(review_prefix) and marker in path[len(review_prefix):]:
            review_part, command = path[len(review_prefix):].split(marker, 1)
            review_id = unquote(review_part)
            command = unquote(command)
            if not review_id or "/" in review_id or not command or "/" in command:
                raise ControllerError(404, "route_not_found", "Route not found")
            if self._single_header("If-Match") is not None:
                raise ControllerError(
                    409,
                    "review_precondition_unavailable",
                    "Review precondition is unavailable",
                    "Milestone 2H does not yet implement review If-Match semantics.",
                )
            status, payload = service.review_commands.command_review(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                review_id=review_id,
                command=command,
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        raise ControllerError(404, "route_not_found", "Route not found")

    def _handle_post(self) -> None:
        request_id = self._request_id()
        try:
            self._validate_host()
            self._validate_request_target()
            parsed = urlsplit(self.path)
            path = parsed.path
            is_project_command = (
                path.startswith("/api/v1/projects/")
                and "/commands/" in path[len("/api/v1/projects/"):]
            )
            is_objective_command = (
                path.startswith("/api/v1/objectives/")
                and "/commands/" in path[len("/api/v1/objectives/"):]
            )
            is_review_command = (
                path.startswith("/api/v1/reviews/")
                and "/commands/" in path[len("/api/v1/reviews/"):]
            )
            if (
                path not in {
                    "/api/v1/auth/login",
                    "/api/v1/auth/logout",
                    "/api/v1/auth/csrf",
                    "/api/v1/projects",
                    "/api/v1/objectives",
                    "/api/v1/blueprints",
                    "/api/v1/blueprints/validate",
                }
                and not is_project_command
                and not is_objective_command
                and not is_review_command
            ):
                self._method_not_allowed()
                return
            body = self._read_json_body()
            status, payload, headers = self._protected_post(
                parsed.path,
                self._parse_query(parsed.query),
                request_id,
                body,
            )
            self._send_json(
                status,
                payload,
                request_id,
                extra_headers=headers,
            )
        except ControllerError as error:
            self._problem(error, request_id, close_connection=True)
        except Exception:
            LOGGER.exception("Unhandled Controller API mutation failure")
            self._problem(
                ControllerError(500, "internal_error", "Internal server error"),
                request_id,
                close_connection=True,
            )

    def _protected_patch(
        self,
        path: str,
        query: dict[str, list[str]],
        request_id: str,
        body: dict[str, Any],
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        if query:
            raise ControllerError(400, "unknown_query_parameter", "Unknown query parameter")
        service = self.controller.service
        self._validate_origin()
        session_token = service.authenticate(self._single_header("Cookie"))
        service.commands.verify_csrf_token(
            session_token,
            self._single_header("X-CSRF-Token"),
        )
        key = service.commands.validate_idempotency_key(
            self._single_header("Idempotency-Key")
        )

        project_prefix = "/api/v1/projects/"
        if path.startswith(project_prefix):
            project_id = unquote(path[len(project_prefix):])
            if not project_id or "/" in project_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            status, payload = service.project_commands.update_project(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                project_id=project_id,
                if_match=self._single_header("If-Match"),
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        blueprint_prefix = "/api/v1/blueprints/"
        if path.startswith(blueprint_prefix):
            sandbox_id = unquote(path[len(blueprint_prefix):])
            if not sandbox_id or "/" in sandbox_id:
                raise ControllerError(404, "route_not_found", "Route not found")
            status, payload = service.blueprints.update(
                session_token=session_token,
                idempotency_key=key,
                route=path,
                sandbox_id=sandbox_id,
                if_match=self._single_header("If-Match"),
                body=body,
                meta_factory=lambda revision: service.meta(
                    request_id,
                    resource_revision=revision,
                ),
            )
            return status, payload, {}

        raise ControllerError(404, "route_not_found", "Route not found")

    def _handle_patch(self) -> None:
        request_id = self._request_id()
        try:
            self._validate_host()
            self._validate_request_target()
            parsed = urlsplit(self.path)
            path = parsed.path
            prefixes = ("/api/v1/projects/", "/api/v1/blueprints/")
            matched = next((prefix for prefix in prefixes if path.startswith(prefix)), None)
            if matched is None or "/" in path[len(matched):]:
                self._method_not_allowed()
                return
            body = self._read_json_body()
            status, payload, headers = self._protected_patch(
                path,
                self._parse_query(parsed.query),
                request_id,
                body,
            )
            self._send_json(
                status,
                payload,
                request_id,
                extra_headers=headers,
            )
        except ControllerError as error:
            self._problem(error, request_id, close_connection=True)
        except Exception:
            LOGGER.exception("Unhandled Controller API patch failure")
            self._problem(
                ControllerError(500, "internal_error", "Internal server error"),
                request_id,
                close_connection=True,
            )

    def _handle_websocket(self) -> None:
        request_id = self._request_id()
        slot_acquired = False
        try:
            self._validate_host()
            self._validate_request_target()
            self._reject_request_body()
            if self.request_version != "HTTP/1.1":
                raise ControllerError(
                    400,
                    "invalid_websocket_http_version",
                    "WebSocket upgrade requires HTTP/1.1",
                )
            parsed = urlsplit(self.path)
            if parsed.path != WEBSOCKET_PATH:
                raise ControllerError(404, "route_not_found", "Route not found")
            if parsed.query or parsed.fragment:
                raise ControllerError(
                    400,
                    "websocket_query_forbidden",
                    "WebSocket query parameters are forbidden",
                    "Credentials and replay cursors must not be sent in the WebSocket URL.",
                )
            if not header_has_token(self._single_header("Upgrade"), "websocket"):
                raise ControllerError(
                    426,
                    "websocket_upgrade_required",
                    "WebSocket upgrade required",
                )
            if not header_has_token(self._single_header("Connection"), "upgrade"):
                raise ControllerError(
                    400,
                    "invalid_websocket_connection",
                    "Invalid WebSocket Connection header",
                )
            version = self._single_header("Sec-WebSocket-Version", required=True)
            if version is None or version.strip() != "13":
                raise ControllerError(
                    426,
                    "websocket_version_unsupported",
                    "Unsupported WebSocket version",
                )
            if self._single_header("Sec-WebSocket-Extensions") is not None:
                raise ControllerError(
                    400,
                    "websocket_extensions_unsupported",
                    "WebSocket extensions are unsupported",
                )
            if self._single_header("Sec-WebSocket-Protocol") is not None:
                raise ControllerError(
                    400,
                    "websocket_subprotocol_unsupported",
                    "WebSocket subprotocols are unsupported",
                )
            origin = self._single_header("Origin", required=True)
            if origin != self.controller.service.settings.console_origin:
                raise ControllerError(
                    403,
                    "websocket_origin_forbidden",
                    "Forbidden WebSocket origin",
                )
            authenticated_session = self.controller.service.authenticate(
                self._single_header("Cookie")
            )
            key = self._single_header("Sec-WebSocket-Key", required=True)
            if key is None:
                raise ControllerError(400, "invalid_websocket_key", "Invalid WebSocket key")
            accept = websocket_accept_value(key.strip())
            initial_after_sequence = parse_last_event_sequence(
                self._single_header("Last-Event-Sequence")
            )
            if not self.controller.acquire_websocket_slot():
                raise ControllerError(
                    503,
                    "websocket_capacity_exhausted",
                    "WebSocket capacity exhausted",
                )
            slot_acquired = True
            self.controller.release_request_slot_for_websocket()
            self.close_connection = True
            self.send_response(101, "Switching Protocols")
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", accept)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Request-ID", request_id)
            for name, value in self._security_headers().items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.flush()
            WebSocketSession(
                connection=self.connection,
                stream=self.rfile,
                service=self.controller.service,
                authenticated_session=authenticated_session,
                initial_after_sequence=initial_after_sequence,
            ).run()
        except ControllerError as error:
            extra_headers = (
                {"Sec-WebSocket-Version": "13"}
                if error.status == 426
                else None
            )
            self._problem(
                error,
                request_id,
                close_connection=True,
                extra_headers=extra_headers,
            )
        except Exception:
            LOGGER.exception("Unhandled Controller WebSocket handshake failure")
            self._problem(
                ControllerError(500, "internal_error", "Internal server error"),
                request_id,
                close_connection=True,
            )
        finally:
            if slot_acquired:
                self.controller.release_websocket_slot()

    def _handle_get(self, *, head_only: bool = False) -> None:
        request_id = self._request_id()
        try:
            self._validate_host()
            self._validate_request_target()
            self._reject_request_body()

            parsed = urlsplit(self.path)
            path = parsed.path
            if path in {"/health", "/ready", "/version"}:
                if parsed.query:
                    raise ControllerError(
                        400,
                        "unknown_query_parameter",
                        "Unknown query parameter",
                        "Technical probes do not accept query parameters.",
                    )
                status, payload = self._technical_probe(path, request_id)
                self._send_json(
                    status,
                    payload,
                    request_id,
                    head_only=head_only,
                )
                return

            status, payload, headers = self._protected_get(
                path,
                self._parse_query(parsed.query),
                request_id,
            )
            self._send_json(
                status,
                payload,
                request_id,
                head_only=head_only,
                extra_headers=headers,
            )
        except ControllerError as error:
            self._problem(
                error,
                request_id,
                head_only=head_only,
                close_connection=(
                    error.code
                    in {
                        "invalid_content_length",
                        "request_body_not_allowed",
                    }
                ),
            )
        except Exception:
            LOGGER.exception("Unhandled Controller API request failure")
            self._problem(
                ControllerError(
                    500,
                    "internal_error",
                    "Internal server error",
                ),
                request_id,
                head_only=head_only,
                close_connection=True,
            )

    def _handle_options(self) -> None:
        request_id = self._request_id()
        try:
            self._validate_host()
            self._validate_request_target()
            self._reject_request_body()
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment or not parsed.path.startswith("/api/v1/"):
                raise ControllerError(404, "route_not_found", "Route not found")
            self._validate_browser_origin()
            method = self._single_header(
                "Access-Control-Request-Method",
                required=True,
            )
            if method not in {"GET", "HEAD", "POST", "PATCH"}:
                raise ControllerError(405, "method_not_allowed", "Method not allowed")
            raw_headers = self._single_header("Access-Control-Request-Headers") or ""
            requested = {
                value.strip().lower()
                for value in raw_headers.split(",")
                if value.strip()
            }
            allowed = {
                "content-type",
                "idempotency-key",
                "x-csrf-token",
                "x-request-id",
                "if-match",
            }
            if not requested.issubset(allowed):
                raise ControllerError(
                    400,
                    "cors_headers_forbidden",
                    "CORS request headers are forbidden",
                )
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", self.controller.service.settings.console_origin)
            self.send_header("Access-Control-Allow-Credentials", "true")
            self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, PATCH, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Idempotency-Key, If-Match, X-CSRF-Token, X-Request-ID",
            )
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
            for name, value in self._security_headers().items():
                self.send_header(name, value)
            self.end_headers()
        except ControllerError as error:
            self._problem(error, request_id, close_connection=True)
        except Exception:
            LOGGER.exception("Unhandled Controller CORS preflight failure")
            self._problem(
                ControllerError(500, "internal_error", "Internal server error"),
                request_id,
                close_connection=True,
            )

    def do_GET(self) -> None:
        if self.path.split("?", 1)[0] == WEBSOCKET_PATH:
            self._handle_websocket()
            return
        self._handle_get()

    def do_HEAD(self) -> None:
        self._handle_get(head_only=True)

    def _method_not_allowed(self) -> None:
        request_id = self._request_id()
        error = ControllerError(
            405,
            "method_not_allowed",
            "Method not allowed",
            "The requested HTTP method is not supported on this Controller route.",
        )
        self._send_json(
            405,
            self.controller.service.problem(error, request_id),
            request_id,
            content_type="application/problem+json",
            extra_headers={"Allow": "GET, HEAD"},
            close_connection=True,
        )

    def do_POST(self) -> None:
        self._handle_post()

    def do_PATCH(self) -> None:
        self._handle_patch()

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _handle_options
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed


def build_server(settings: Settings) -> ControllerHTTPServer:
    service = ControllerService(settings)
    return ControllerHTTPServer((settings.host, settings.port), service)


def serve(settings: Settings) -> None:
    server = build_server(settings)
    stop = threading.Event()

    def request_stop(
        signum: int,
        frame: object,
    ) -> None:
        del signum, frame
        if stop.is_set():
            return
        stop.set()
        threading.Thread(
            target=server.shutdown,
            name="controller-api-shutdown",
            daemon=True,
        ).start()

    previous: dict[int, Any] = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous[signum] = signal.signal(signum, request_stop)

    try:
        host, port = server.server_address[:2]
        LOGGER.info("Controller API listening on %s:%s", host, port)
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
        for signum, handler in previous.items():
            signal.signal(signum, handler)
