#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import http.client
import http.server
import ipaddress
import json
import logging
import os
import signal
import socket
import stat
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.parse import urlsplit

MAX_FILE_SIZE = 512 * 1024
MAX_PROXY_REQUEST_BODY = 384 * 1024
MAX_PROXY_RESPONSE_BODY = 1024 * 1024
PROXY_TIMEOUT_MIN = 0.25
PROXY_TIMEOUT_MAX = 30.0
ROUTES = frozenset(
    {
        "/",
        "/dashboard",
        "/projects",
        "/blueprints",
        "/objectives",
        "/executions",
        "/reviews",
        "/events",
        "/administration",
    }
)
ASSETS = {
    "/assets/app.js": (Path("assets/app.js"), "text/javascript"),
    "/assets/controller-client.js": (
        Path("assets/controller-client.js"),
        "text/javascript",
    ),
    "/assets/styles.css": (Path("assets/styles.css"), "text/css"),
}
CONTROLLER_ROUTES = frozenset(
    {
        ("GET", "/api/v1/auth/session"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/auth/csrf"),
        ("POST", "/api/v1/auth/logout"),
        ("GET", "/api/v1/system/capabilities"),
        ("GET", "/api/v1/projects"),
        ("POST", "/api/v1/projects"),
        ("GET", "/api/v1/blueprints"),
        ("POST", "/api/v1/blueprints"),
        ("POST", "/api/v1/blueprints/validate"),
        ("GET", "/api/v1/blueprints/template"),
        ("GET", "/api/v1/objectives"),
        ("POST", "/api/v1/objectives"),
        ("GET", "/api/v1/reviews"),
        ("GET", "/api/v1/recoveries"),
        ("GET", "/api/v1/plans"),
        ("GET", "/api/v1/reviewer-assignments"),
    }
)
PROJECT_ID_PATTERN = __import__("re").compile(r"^[a-z][a-z0-9-]{1,62}$")
PROJECT_COMMANDS = frozenset({"enable", "disable", "rescan", "archive"})
OBJECTIVE_COMMANDS = frozenset({"pause", "resume", "cancel"})
OBJECTIVE_ID_PATTERN = __import__("re").compile(r"^objective-[0-9a-f]{32}$")
OPERATION_ID_PATTERN = __import__("re").compile(r"^operation-[0-9a-f]{32}$")
SANDBOX_ID_PATTERN = __import__("re").compile(r"^sandbox-[0-9a-f]{32}$")
REVISION_PATTERN = __import__("re").compile(r"^[1-9][0-9]*$")


def _controller_route_exposed(method: str, path: str) -> bool:
    parsed = urlsplit(path)
    if parsed.fragment or "%" in parsed.path or "\\" in parsed.path:
        return False
    route_path = parsed.path
    query = parsed.query
    if query and not (
        method == "GET"
        and __import__("re").fullmatch(r"from=[1-9][0-9]*&to=[1-9][0-9]*", query)
        and route_path.startswith("/api/v1/blueprints/")
        and route_path.endswith("/diff")
    ):
        return False
    if (method, route_path) in CONTROLLER_ROUTES and not query:
        return True
    blueprint_prefix = "/api/v1/blueprints/"
    if route_path.startswith(blueprint_prefix):
        suffix = route_path[len(blueprint_prefix):]
        parts = suffix.split("/")
        if method in {"GET", "PATCH"} and len(parts) == 1:
            return SANDBOX_ID_PATTERN.fullmatch(parts[0]) is not None
        if method == "GET" and len(parts) == 2 and parts[1] in {"revisions", "diff"}:
            return SANDBOX_ID_PATTERN.fullmatch(parts[0]) is not None
        if method == "GET" and len(parts) == 3 and parts[1] == "revisions":
            return (
                SANDBOX_ID_PATTERN.fullmatch(parts[0]) is not None
                and REVISION_PATTERN.fullmatch(parts[2]) is not None
            )
        return False
    operation_prefix = "/api/v1/operations/"
    if route_path.startswith(operation_prefix):
        operation_id = route_path[len(operation_prefix):]
        return (
            method == "GET"
            and not query
            and OPERATION_ID_PATTERN.fullmatch(operation_id) is not None
        )
    objective_prefix = "/api/v1/objectives/"
    if route_path.startswith(objective_prefix):
        suffix = route_path[len(objective_prefix):]
        if method == "GET" and not query:
            return OBJECTIVE_ID_PATTERN.fullmatch(suffix) is not None
        marker = "/commands/"
        if method == "POST" and not query and marker in suffix:
            objective_id, command = suffix.split(marker, 1)
            return (
                OBJECTIVE_ID_PATTERN.fullmatch(objective_id) is not None
                and command in OBJECTIVE_COMMANDS
            )
        return False
    prefix = "/api/v1/projects/"
    if not route_path.startswith(prefix):
        return False
    suffix = route_path[len(prefix):]
    if method in {"GET", "PATCH"}:
        return PROJECT_ID_PATTERN.fullmatch(suffix) is not None
    marker = "/commands/"
    if method == "POST" and marker in suffix:
        project_id, command = suffix.split(marker, 1)
        return (
            PROJECT_ID_PATTERN.fullmatch(project_id) is not None
            and command in PROJECT_COMMANDS
        )
    return False


REQUEST_HEADER_ALLOWLIST = frozenset(
    {
        "accept",
        "content-type",
        "cookie",
        "idempotency-key",
        "if-match",
        "user-agent",
        "x-csrf-token",
    }
)
SINGLETON_REQUEST_HEADERS = frozenset(
    {
        "content-length",
        "content-type",
        "cookie",
        "expect",
        "idempotency-key",
        "if-match",
        "origin",
        "transfer-encoding",
        "x-csrf-token",
    }
)
RESPONSE_HEADER_ALLOWLIST = frozenset(
    {
        "allow",
        "etag",
        "retry-after",
        "set-cookie",
    }
)
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self'; font-src 'self'; connect-src 'self'; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    ),
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ConsoleServiceError(RuntimeError):
    pass


def read_safe_file(path: Path, maximum: int = MAX_FILE_SIZE) -> bytes:
    try:
        before = path.lstat()
    except OSError as error:
        raise ConsoleServiceError("Console file is unavailable") from error
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size <= 0
        or before.st_size > maximum
    ):
        raise ConsoleServiceError("Console file is unsafe")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ConsoleServiceError("Console file cannot be opened safely") from error
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise ConsoleServiceError("Console file changed type")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
    finally:
        os.close(descriptor)
    if (
        before.st_dev != opened.st_dev
        or before.st_ino != opened.st_ino
        or before.st_size != opened.st_size
        or len(data) != opened.st_size
        or len(data) > maximum
    ):
        raise ConsoleServiceError("Console file changed while reading")
    return data


def _canonical_loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "http"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.hostname is None
        or parsed.port is None
    ):
        raise ConsoleServiceError("Controller origin must be one canonical loopback HTTP origin")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as error:
        raise ConsoleServiceError("Controller origin host must be a loopback IP address") from error
    if not address.is_loopback:
        raise ConsoleServiceError("Controller origin must be loopback")
    host = f"[{address.compressed}]" if address.version == 6 else address.compressed
    canonical = f"http://{host}:{parsed.port}"
    if value != canonical:
        raise ConsoleServiceError("Controller origin is not canonical")
    return canonical


def _safe_header_value(value: str, maximum: int = 4096) -> bool:
    return 0 < len(value) <= maximum and "\r" not in value and "\n" not in value and "\0" not in value


@dataclass(frozen=True)
class Settings:
    root: Path
    host: str = "127.0.0.1"
    port: int = 8788
    max_connections: int = 16
    controller_host: str = "127.0.0.1"
    controller_port: int = 8765
    controller_origin: str = "http://127.0.0.1:8787"
    controller_timeout: float = 5.0

    @classmethod
    def from_root(
        cls,
        root: Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8788,
        max_connections: int = 16,
        controller_host: str = "127.0.0.1",
        controller_port: int = 8765,
        controller_origin: str = "http://127.0.0.1:8787",
        controller_timeout: float = 5.0,
    ) -> "Settings":
        if root.is_symlink():
            raise ConsoleServiceError("Console distribution root must not be a symlink")
        resolved = root.resolve(strict=True)
        settings = cls(
            root=resolved,
            host=host,
            port=port,
            max_connections=max_connections,
            controller_host=controller_host,
            controller_port=controller_port,
            controller_origin=controller_origin,
            controller_timeout=controller_timeout,
        )
        settings.validate()
        return settings

    def validate(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
            controller_address = ipaddress.ip_address(self.controller_host)
        except ValueError as error:
            raise ConsoleServiceError("Console and Controller hosts must be loopback IP addresses") from error
        if not address.is_loopback or not controller_address.is_loopback:
            raise ConsoleServiceError("Console and Controller hosts must be loopback")
        if not 0 <= self.port <= 65_535:
            raise ConsoleServiceError("Console port is invalid")
        if not 1 <= self.controller_port <= 65_535:
            raise ConsoleServiceError("Controller port is invalid")
        if not 1 <= self.max_connections <= 128:
            raise ConsoleServiceError("Console connection limit is invalid")
        if not PROXY_TIMEOUT_MIN <= self.controller_timeout <= PROXY_TIMEOUT_MAX:
            raise ConsoleServiceError("Controller timeout is invalid")
        _canonical_loopback_origin(self.controller_origin)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ConsoleServiceError("Console distribution root is invalid")
        expected = {
            Path("index.html"),
            Path("asset-manifest.json"),
            Path("assets/app.js"),
            Path("assets/controller-client.js"),
            Path("assets/styles.css"),
        }
        actual: set[Path] = set()
        for path in self.root.rglob("*"):
            if path.is_symlink():
                raise ConsoleServiceError("Console distribution contains a symlink")
            if path.is_file():
                actual.add(path.relative_to(self.root))
        if actual != expected:
            raise ConsoleServiceError("Console distribution file set is invalid")
        file_bytes = {relative: read_safe_file(self.root / relative) for relative in expected}
        try:
            manifest = json.loads(file_bytes[Path("asset-manifest.json")])
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ConsoleServiceError("Console asset manifest is invalid") from error
        if (
            set(manifest) != {"schema_version", "entrypoint", "files"}
            or manifest.get("schema_version") != 1
            or manifest.get("entrypoint") != "index.html"
        ):
            raise ConsoleServiceError("Console asset manifest contract is invalid")
        entries = manifest.get("files")
        expected_entries = {
            "index.html",
            "assets/app.js",
            "assets/controller-client.js",
            "assets/styles.css",
        }
        if not isinstance(entries, dict) or set(entries) != expected_entries:
            raise ConsoleServiceError("Console asset manifest file set is invalid")
        for name in sorted(expected_entries):
            metadata = entries[name]
            data = file_bytes[Path(name)]
            if not isinstance(metadata, dict) or set(metadata) != {"sha256", "size"}:
                raise ConsoleServiceError("Console asset manifest metadata is invalid")
            if (
                metadata.get("size") != len(data)
                or metadata.get("sha256") != hashlib.sha256(data).hexdigest()
            ):
                raise ConsoleServiceError("Console asset digest mismatch")


class BoundedHTTPServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler, settings: Settings):
        self.settings = settings
        self._slots = threading.BoundedSemaphore(settings.max_connections)
        super().__init__(server_address, handler)

    def process_request(self, request, client_address) -> None:
        if not self._slots.acquire(blocking=False):
            request_id = "req_" + uuid.uuid4().hex
            body = b'{"status":503,"title":"Console capacity exhausted","type":"urn:orchestra:console:capacity_exhausted"}\n'
            headers = [
                "HTTP/1.1 503 Service Unavailable",
                "Content-Type: application/problem+json; charset=utf-8",
                f"Content-Length: {len(body)}",
                "Cache-Control: no-store",
                f"X-Request-ID: {request_id}",
                "Connection: close",
            ]
            headers.extend(f"{name}: {value}" for name, value in SECURITY_HEADERS.items())
            response = ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body
            try:
                request.sendall(response)
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request, client_address) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()


class ConsoleHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "OrchestraConsole"
    sys_version = ""

    @property
    def settings(self) -> Settings:
        return self.server.settings  # type: ignore[attr-defined]

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(10.0)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _request_id(self) -> str:
        return "req_" + uuid.uuid4().hex

    def _valid_host(self) -> bool:
        values = self.headers.get_all("Host", failobj=[])
        if len(values) != 1:
            return False
        supplied = values[0].strip().lower()
        expected = {
            f"{self.settings.host}:{self.server.server_port}",
            f"[{self.settings.host}]:{self.server.server_port}",
        }
        return supplied in expected

    def _browser_origin(self) -> str:
        host = f"[{self.settings.host}]" if ":" in self.settings.host else self.settings.host
        return f"http://{host}:{self.server.server_port}"

    def _headers(
        self,
        *,
        status: int,
        length: int,
        content_type: str,
        request_id: str,
        allow: str | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Request-ID", request_id)
        if allow is not None:
            self.send_header("Allow", allow)
        for name, value in extra_headers:
            self.send_header(name, value)
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()

    def _send(
        self,
        *,
        status: int,
        body: bytes,
        content_type: str,
        request_id: str,
        head_only: bool = False,
        allow: str | None = None,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        self._headers(
            status=status,
            length=len(body),
            content_type=content_type,
            request_id=request_id,
            allow=allow,
            extra_headers=extra_headers,
        )
        if not head_only:
            self.wfile.write(body)

    def _problem(
        self,
        status: int,
        code: str,
        title: str,
        request_id: str,
        *,
        head_only: bool = False,
        allow: str | None = None,
    ) -> None:
        body = (
            json.dumps(
                {
                    "type": f"urn:orchestra:console:{code}",
                    "title": title,
                    "status": status,
                    "request_id": request_id,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        self._send(
            status=status,
            body=body,
            content_type="application/problem+json",
            request_id=request_id,
            head_only=head_only,
            allow=allow,
        )

    def _read_regular(self, relative: Path) -> bytes:
        return read_safe_file(self.settings.root / relative)

    def _parsed_path(
        self,
        request_id: str,
        *,
        method: str = "GET",
        head_only: bool = False,
    ) -> str | None:
        parsed = urlsplit(self.path)
        target = parsed.path + (("?" + parsed.query) if parsed.query else "")
        if (
            parsed.fragment
            or "%" in parsed.path
            or "\\" in parsed.path
            or (parsed.query and not _controller_route_exposed(method, target))
        ):
            self._problem(400, "invalid_path", "Invalid request path", request_id, head_only=head_only)
            return None
        return target

    def _serve_static(self, *, head_only: bool) -> None:
        request_id = self._request_id()
        if not self._valid_host():
            self._problem(400, "invalid_host", "Invalid Host header", request_id, head_only=head_only)
            return
        path = self._parsed_path(request_id, method="GET", head_only=head_only)
        if path is None:
            return
        if path.startswith("/api/"):
            if head_only:
                self._problem(405, "method_not_allowed", "Method not allowed", request_id, allow="GET, POST, PATCH")
            else:
                self._proxy_controller("GET", path, request_id)
            return
        try:
            if path == "/health":
                body = b'{"service":"orchestra-console","status":"ok"}\n'
                self._send(
                    status=200,
                    body=body,
                    content_type="application/json",
                    request_id=request_id,
                    head_only=head_only,
                )
                return
            if path == "/version":
                body = (
                    json.dumps(
                        {"service": "orchestra-console", "version": "v1"},
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    + "\n"
                ).encode("utf-8")
                self._send(
                    status=200,
                    body=body,
                    content_type="application/json",
                    request_id=request_id,
                    head_only=head_only,
                )
                return
            if path in ROUTES:
                body = self._read_regular(Path("index.html"))
                self._send(
                    status=200,
                    body=body,
                    content_type="text/html",
                    request_id=request_id,
                    head_only=head_only,
                )
                return
            asset = ASSETS.get(path)
            if asset is not None:
                relative, content_type = asset
                body = self._read_regular(relative)
                self._send(
                    status=200,
                    body=body,
                    content_type=content_type,
                    request_id=request_id,
                    head_only=head_only,
                )
                return
        except (OSError, ConsoleServiceError, UnicodeError):
            self._problem(503, "asset_unavailable", "Console asset unavailable", request_id, head_only=head_only)
            return
        self._problem(404, "route_not_found", "Route not found", request_id, head_only=head_only)

    def _singleton_headers_valid(self) -> bool:
        return all(len(self.headers.get_all(name, failobj=[])) <= 1 for name in SINGLETON_REQUEST_HEADERS)

    def _validated_proxy_body(self, method: str, request_id: str) -> bytes | None:
        if self.headers.get("Transfer-Encoding") is not None or self.headers.get("Expect") is not None:
            self._problem(400, "unsupported_request_framing", "Unsupported request framing", request_id)
            self.close_connection = True
            return None
        raw_length = self.headers.get("Content-Length")
        try:
            length = 0 if raw_length is None else int(raw_length, 10)
        except ValueError:
            self._problem(400, "invalid_content_length", "Invalid Content-Length", request_id)
            self.close_connection = True
            return None
        if length < 0 or length > MAX_PROXY_REQUEST_BODY:
            self._problem(413, "request_too_large", "Controller request body is too large", request_id)
            self.close_connection = True
            return None
        if method == "GET" and length != 0:
            self._problem(400, "unexpected_request_body", "GET request body is not allowed", request_id)
            self.close_connection = True
            return None
        if method in {"POST", "PATCH"}:
            content_type = self.headers.get("Content-Type", "")
            if content_type.split(";", 1)[0].strip().lower() != "application/json":
                self._problem(415, "unsupported_media_type", "Controller requests require JSON", request_id)
                self.close_connection = True
                return None
        if length == 0:
            return b""
        try:
            body = self.rfile.read(length)
        except OSError:
            self._problem(400, "request_body_unavailable", "Request body is unavailable", request_id)
            self.close_connection = True
            return None
        if len(body) != length:
            self._problem(400, "incomplete_request_body", "Request body is incomplete", request_id)
            self.close_connection = True
            return None
        return body

    def _validated_origin(self, method: str, request_id: str) -> bool:
        values = self.headers.get_all("Origin", failobj=[])
        if method in {"POST", "PATCH"}:
            if len(values) != 1 or values[0] != self._browser_origin():
                self._problem(403, "origin_forbidden", "Request origin is forbidden", request_id)
                return False
        elif values and (len(values) != 1 or values[0] != self._browser_origin()):
            self._problem(403, "origin_forbidden", "Request origin is forbidden", request_id)
            return False
        return True

    def _proxy_request_headers(self, method: str) -> dict[str, str]:
        result = {
            "Host": f"{self.settings.controller_host}:{self.settings.controller_port}",
            "Connection": "close",
            "Origin": self.settings.controller_origin,
        }
        for name in REQUEST_HEADER_ALLOWLIST:
            values = self.headers.get_all(name, failobj=[])
            if len(values) == 1 and _safe_header_value(values[0]):
                result[name] = values[0]
        if method == "GET":
            result.pop("content-type", None)
        return result

    def _proxy_controller(self, method: str, path: str, request_id: str) -> None:
        if not _controller_route_exposed(method, path):
            self._problem(404, "controller_route_not_exposed", "Controller route is not exposed", request_id)
            if method in {"POST", "PATCH"}:
                self.close_connection = True
            return
        if not self._singleton_headers_valid():
            self._problem(400, "duplicate_header", "Duplicate request header", request_id)
            if method in {"POST", "PATCH"}:
                self.close_connection = True
            return
        if not self._validated_origin(method, request_id):
            if method in {"POST", "PATCH"}:
                self.close_connection = True
            return
        body = self._validated_proxy_body(method, request_id)
        if body is None:
            return
        connection = http.client.HTTPConnection(
            self.settings.controller_host,
            self.settings.controller_port,
            timeout=self.settings.controller_timeout,
        )
        try:
            connection.request(
                method,
                path,
                body=body if method in {"POST", "PATCH"} else None,
                headers=self._proxy_request_headers(method),
            )
            response = connection.getresponse()
            if 100 <= response.status < 200 or 300 <= response.status < 400:
                raise ConsoleServiceError("Controller returned an unsupported status")
            raw_headers = response.getheaders()
            preliminary: dict[str, list[str]] = {}
            for name, value in raw_headers:
                preliminary.setdefault(name.lower(), []).append(value)
            if preliminary.get("transfer-encoding"):
                raise ConsoleServiceError("Controller response framing is unsupported")
            content_lengths = preliminary.get("content-length", [])
            if len(content_lengths) != 1:
                raise ConsoleServiceError("Controller response length is invalid")
            try:
                declared = int(content_lengths[0], 10)
            except ValueError as error:
                raise ConsoleServiceError("Controller response length is invalid") from error
            if declared < 0 or declared > MAX_PROXY_RESPONSE_BODY:
                raise ConsoleServiceError("Controller response is too large")
            response_body = response.read(MAX_PROXY_RESPONSE_BODY + 1)
            if len(response_body) != declared or len(response_body) > MAX_PROXY_RESPONSE_BODY:
                raise ConsoleServiceError("Controller response length is invalid")
        except (OSError, TimeoutError, http.client.HTTPException):
            self._problem(503, "controller_unavailable", "Controller is unavailable", request_id)
            return
        except ConsoleServiceError:
            self._problem(502, "invalid_controller_response", "Controller response is invalid", request_id)
            return
        finally:
            connection.close()

        header_map: dict[str, list[str]] = {}
        for name, value in raw_headers:
            header_map.setdefault(name.lower(), []).append(value)
        content_types = header_map.get("content-type", [])
        if len(content_types) != 1 or not _safe_header_value(content_types[0], 256):
            self._problem(502, "invalid_controller_response", "Controller response is invalid", request_id)
            return
        media_type = content_types[0].split(";", 1)[0].strip().lower()
        if media_type not in {"application/json", "application/problem+json"}:
            self._problem(502, "invalid_controller_response", "Controller response is invalid", request_id)
            return

        extra: list[tuple[str, str]] = []
        for name in RESPONSE_HEADER_ALLOWLIST:
            for value in header_map.get(name, []):
                if _safe_header_value(value):
                    extra.append(("-".join(part.capitalize() for part in name.split("-")), value))
        upstream_request_ids = header_map.get("x-request-id", [])
        if len(upstream_request_ids) == 1 and _safe_header_value(upstream_request_ids[0], 128):
            extra.append(("X-Orchestra-Controller-Request-ID", upstream_request_ids[0]))
        self._send(
            status=response.status,
            body=response_body,
            content_type=media_type,
            request_id=request_id,
            extra_headers=tuple(extra),
        )

    def do_GET(self) -> None:
        self._serve_static(head_only=False)

    def do_HEAD(self) -> None:
        self._serve_static(head_only=True)

    def _serve_mutation(self, method: str) -> None:
        request_id = self._request_id()
        if not self._valid_host():
            self._problem(400, "invalid_host", "Invalid Host header", request_id)
            return
        path = self._parsed_path(request_id, method=method)
        if path is None:
            return
        if path.startswith("/api/"):
            self._proxy_controller(method, path, request_id)
            return
        self._problem(405, "method_not_allowed", "Method not allowed", request_id, allow="GET, HEAD")
        self.close_connection = True

    def do_POST(self) -> None:
        self._serve_mutation("POST")

    def do_PATCH(self) -> None:
        self._serve_mutation("PATCH")

    def _method_not_allowed(self) -> None:
        request_id = self._request_id()
        self._problem(405, "method_not_allowed", "Method not allowed", request_id, allow="GET, HEAD, POST, PATCH")
        self.close_connection = True

    do_PUT = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_TRACE = _method_not_allowed
    do_CONNECT = _method_not_allowed


def create_server(settings: Settings) -> BoundedHTTPServer:
    return BoundedHTTPServer((settings.host, settings.port), ConsoleHandler, settings)


def check_bind(settings: Settings) -> None:
    with socket.socket(socket.AF_INET6 if ":" in settings.host else socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((settings.host, settings.port))


def settings_from_arguments(arguments: argparse.Namespace) -> Settings:
    return Settings.from_root(
        arguments.root,
        host=arguments.host,
        port=arguments.port,
        max_connections=arguments.max_connections,
        controller_host=arguments.controller_host,
        controller_port=arguments.controller_port,
        controller_origin=arguments.controller_origin,
        controller_timeout=arguments.controller_timeout,
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Serve the Orchestra Console browser client")
    subparsers = result.add_subparsers(dest="command", required=True)
    for name in ("check", "serve"):
        child = subparsers.add_parser(name)
        child.add_argument("--root", type=Path, required=True)
        child.add_argument("--host", default="127.0.0.1")
        child.add_argument("--port", type=int, default=8788)
        child.add_argument("--max-connections", type=int, default=16)
        child.add_argument("--controller-host", default="127.0.0.1")
        child.add_argument("--controller-port", type=int, default=8765)
        child.add_argument("--controller-origin", default="http://127.0.0.1:8787")
        child.add_argument("--controller-timeout", type=float, default=5.0)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        settings = settings_from_arguments(arguments)
        if arguments.command == "check":
            check_bind(settings)
            print("ORCHESTRA_CONSOLE_SERVICE_CHECK_PASS")
            return 0
        server = create_server(settings)
    except (ConsoleServiceError, OSError) as error:
        print(f"Console service failed: {error}", file=__import__("sys").stderr)
        return 1

    stop = threading.Event()

    def request_stop(signum, frame) -> None:
        del signum, frame
        if not stop.is_set():
            stop.set()
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logging.info("Orchestra Console listening on %s:%s", settings.host, server.server_port)
    try:
        server.serve_forever(poll_interval=0.25)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
