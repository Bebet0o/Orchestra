from __future__ import annotations

import http.client
import http.server
import importlib.util
import json
import socket
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    __import__("sys").modules[name] = module
    spec.loader.exec_module(module)
    return module


service_module = load_module("orchestra_console_client_service", REPO / "scripts/orchestra-console.py")


class FakeControllerHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeController"
    sys_version = ""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        return

    def _record(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.records.append(  # type: ignore[attr-defined]
            {
                "method": self.command,
                "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body,
            }
        )
        return body

    def _send_json(
        self,
        status: int,
        payload: dict[str, object],
        *,
        cookie: str | None = None,
        etag: str | None = None,
    ) -> None:
        body = (json.dumps(payload, separators=(",", ":")) + "\n").encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "http://127.0.0.1:8787")
        self.send_header("Access-Control-Allow-Credentials", "true")
        self.send_header("X-Request-ID", "controller-request-0001")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        if etag is not None:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._record()
        if self.path == "/api/v1/auth/session":
            self._send_json(401, {"code": "authentication_required", "title": "Authentication required"})
        elif self.path == "/api/v1/system/capabilities":
            self._send_json(200, {"data": {"features": {
                "browser_session_lifecycle": True,
                "blueprint_versions": ["v1"],
            }}})
        elif self.path == "/api/v1/projects/alpha":
            self._send_json(200, {"data": {"id": "alpha", "name": "Alpha", "state": "disabled", "resource_revision": 1}}, etag='"1"')
        elif self.path == "/api/v1/blueprints/template":
            self._send_json(200, {"data": {"source": "apiVersion: hermesops.dev/v1\n", "source_format": "blueprint-v1", "canonical_sha256": "a" * 64}})
        elif self.path == "/api/v1/blueprints/sandbox-" + "a" * 32:
            self._send_json(200, {"data": {"profile": {"id": "sandbox-" + "a" * 32, "profile_name": "python-project", "resource_revision": 1}, "revision": {"source_revision": 1, "source": "apiVersion: hermesops.dev/v1\n"}}}, etag='"1"')
        elif self.path == "/api/v1/blueprints/sandbox-" + "a" * 32 + "/revisions":
            self._send_json(200, {"data": [{"source_revision": 1}], "meta": {"next_cursor": None}})
        elif self.path == "/api/v1/blueprints/sandbox-" + "a" * 32 + "/revisions/1":
            self._send_json(200, {"data": {"source_revision": 1, "source": "apiVersion: hermesops.dev/v1\n"}})
        elif self.path == "/api/v1/blueprints/sandbox-" + "a" * 32 + "/diff?from=1&to=2":
            self._send_json(200, {"data": {"changed": True, "changes": [{"path": "/spec/runtime/cpu", "kind": "modified"}]}})
        elif self.path == "/api/v1/objectives/objective-" + "a" * 32:
            self._send_json(200, {"data": {"id": "objective-" + "a" * 32, "title": "Objective Alpha", "description": "Bounded objective", "state": "paused", "raw_state": "PAUSED", "project_ids": ["alpha"], "priority": 100, "resource_revision": 3, "requested_transition": None, "planning_attempt_count": 1, "attempt_count": 1, "event_count": 2, "plan_id": None, "not_before": "2026-07-29T00:00:00.000Z", "max_parallel_tasks": 1, "has_error": False, "latest_operation_id": "operation-" + "a" * 32}})
        elif self.path == "/api/v1/operations/operation-" + "a" * 32:
            self._send_json(200, {"data": {"id": "operation-" + "a" * 32, "kind": "objective.pause", "state": "succeeded", "created_at": "2026-07-29T00:00:00.000Z", "finished_at": "2026-07-29T00:00:00.000Z", "target": {"type": "objective", "id": "objective-" + "a" * 32}, "result": {"state": "paused"}}})
        elif self.path in {
            "/api/v1/projects",
            "/api/v1/blueprints",
            "/api/v1/objectives",
            "/api/v1/reviews",
            "/api/v1/recoveries",
            "/api/v1/plans",
            "/api/v1/reviewer-assignments",
        }:
            self._send_json(200, {"data": [], "meta": {"next_cursor": None}})
        else:
            self._send_json(404, {"code": "not_found", "title": "Not found"})

    def do_POST(self) -> None:
        body = self._record()
        if self.path == "/api/v1/auth/login":
            payload = json.loads(body)
            if payload != {"username": "operator", "password": "correct horse"}:
                self._send_json(400, {"code": "invalid_body", "title": "Invalid body"})
                return
            self._send_json(
                200,
                {"data": {"authenticated": True, "actor_id": "operator"}},
                cookie="orchestra_session=" + "a" * 64 + "; HttpOnly; Secure; SameSite=Strict; Path=/",
            )
        elif self.path == "/api/v1/auth/csrf":
            self._send_json(200, {"data": {"token": "csrf1.example"}})
        elif self.path == "/api/v1/auth/logout":
            self._send_json(
                200,
                {"data": {"authenticated": False}},
                cookie="orchestra_session=; Max-Age=0; HttpOnly; Secure; SameSite=Strict; Path=/",
            )
        elif self.path == "/api/v1/projects" or self.path.startswith("/api/v1/projects/alpha/commands/"):
            self._send_json(202, {"data": {"operation_id": "operation-" + "a" * 32, "state": "succeeded"}})
        elif self.path == "/api/v1/objectives":
            self._send_json(202, {"data": {"id": "operation-" + "a" * 32, "kind": "objective.create", "state": "succeeded", "target": {"type": "objective", "id": "objective-" + "a" * 32}, "result": {"objective_id": "objective-" + "a" * 32, "state": "queued"}}})
        elif self.path.startswith("/api/v1/objectives/objective-" + "a" * 32 + "/commands/"):
            command = self.path.rsplit("/", 1)[-1]
            self._send_json(202, {"data": {"id": "operation-" + "a" * 32, "kind": "objective." + command, "state": "succeeded", "target": {"type": "objective", "id": "objective-" + "a" * 32}, "result": {"state": command}}})
        elif self.path == "/api/v1/blueprints/validate":
            self._send_json(200, {"data": {"valid": True, "diagnostics": [], "canonical": {}, "runtime_config": {}}})
        elif self.path == "/api/v1/blueprints":
            self._send_json(202, {"data": {"id": "operation-" + "c" * 32, "state": "succeeded", "result": {"sandbox_id": "sandbox-" + "a" * 32}}})
        else:
            self._send_json(404, {"code": "not_found", "title": "Not found"})

    def do_PATCH(self) -> None:
        self._record()
        if self.path == "/api/v1/projects/alpha":
            self._send_json(202, {"data": {"operation_id": "operation-" + "b" * 32, "state": "succeeded"}})
        elif self.path == "/api/v1/blueprints/sandbox-" + "a" * 32:
            self._send_json(202, {"data": {"id": "operation-" + "d" * 32, "state": "succeeded", "result": {"sandbox_id": "sandbox-" + "a" * 32, "resource_revision": 2}}})
        else:
            self._send_json(404, {"code": "not_found", "title": "Not found"})


class FakeController(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        self.records: list[dict[str, object]] = []
        super().__init__(("127.0.0.1", 0), FakeControllerHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


class ConsoleControllerProxyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.controller = FakeController()
        cls.settings = service_module.Settings.from_root(
            REPO / "console/dist",
            host="127.0.0.1",
            port=0,
            max_connections=8,
            controller_host="127.0.0.1",
            controller_port=cls.controller.server_port,
            controller_origin="http://127.0.0.1:8787",
            controller_timeout=2.0,
        )
        cls.server = service_module.create_server(cls.settings)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.controller.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        request_headers.update(headers or {})
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            payload = response.read(2_000_000)
            return response.status, response.getheaders(), payload
        finally:
            connection.close()

    @staticmethod
    def headers_map(headers: list[tuple[str, str]]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {}
        for name, value in headers:
            result.setdefault(name.lower(), []).append(value)
        return result

    def test_login_is_same_origin_and_response_headers_are_bounded(self) -> None:
        body = b'{"username":"operator","password":"correct horse"}'
        status, raw_headers, payload = self.request(
            "POST",
            "/api/v1/auth/login",
            body=body,
            headers={
                "Origin": f"http://127.0.0.1:{self.port}",
                "Content-Type": "application/json",
                "Idempotency-Key": "console-login-0001",
                "Referer": "https://sensitive.invalid/path",
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["data"]["authenticated"])
        headers = self.headers_map(raw_headers)
        self.assertIn("orchestra_session=", headers["set-cookie"][0])
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertNotIn("access-control-allow-credentials", headers)
        self.assertIn("connect-src 'self'", headers["content-security-policy"][0])
        self.assertEqual(headers["x-orchestra-controller-request-id"], ["controller-request-0001"])
        self.assertTrue(headers["x-request-id"][0].startswith("req_"))

        record = self.controller.records[-1]
        request_headers = record["headers"]
        self.assertEqual(request_headers["host"], f"127.0.0.1:{self.controller.server_port}")
        self.assertEqual(request_headers["origin"], "http://127.0.0.1:8787")
        self.assertEqual(request_headers["idempotency-key"], "console-login-0001")
        self.assertNotIn("referer", request_headers)
        self.assertEqual(record["body"], body)

    def test_session_and_capabilities_gets_are_forwarded(self) -> None:
        status, raw_headers, _ = self.request("GET", "/api/v1/auth/session")
        self.assertEqual(status, 401)
        headers = self.headers_map(raw_headers)
        self.assertNotIn("access-control-allow-origin", headers)
        self.assertEqual(self.controller.records[-1]["path"], "/api/v1/auth/session")

        status, _, payload = self.request("GET", "/api/v1/system/capabilities")
        self.assertEqual(status, 200)
        features = json.loads(payload)["data"]["features"]
        self.assertTrue(features["browser_session_lifecycle"])
        self.assertEqual(features["blueprint_versions"], ["v1"])
        self.assertNotIn("hermesfile_versions", features)

    def test_operational_dashboard_gets_are_forwarded(self) -> None:
        for path in (
            "/api/v1/projects",
            "/api/v1/blueprints",
            "/api/v1/objectives",
            "/api/v1/reviews",
            "/api/v1/recoveries",
            "/api/v1/plans",
            "/api/v1/reviewer-assignments",
        ):
            with self.subTest(path=path):
                status, _, payload = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(payload)["data"], [])
                self.assertEqual(self.controller.records[-1]["path"], path)

    def test_cross_origin_unsupported_routes_and_large_bodies_fail_before_upstream(self) -> None:
        before = len(self.controller.records)
        status, _, payload = self.request(
            "POST",
            "/api/v1/auth/login",
            body=b"{}",
            headers={
                "Origin": "https://evil.invalid",
                "Content-Type": "application/json",
                "Idempotency-Key": "cross-origin-0001",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(payload)["type"], "urn:orchestra:console:origin_forbidden")
        self.assertEqual(len(self.controller.records), before)

        status, _, payload = self.request("GET", "/api/v1/tasks")
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(payload)["type"], "urn:orchestra:console:controller_route_not_exposed")
        self.assertEqual(len(self.controller.records), before)

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.putrequest("POST", "/api/v1/auth/login", skip_host=True)
            connection.putheader("Host", f"127.0.0.1:{self.port}")
            connection.putheader("Origin", f"http://127.0.0.1:{self.port}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Idempotency-Key", "large-body-0001")
            connection.putheader("Content-Length", str(service_module.MAX_PROXY_REQUEST_BODY + 1))
            connection.endheaders()
            response = connection.getresponse()
            payload = response.read(1000)
            self.assertEqual(response.status, 413)
            self.assertEqual(json.loads(payload)["type"], "urn:orchestra:console:request_too_large")
        finally:
            connection.close()
        self.assertEqual(len(self.controller.records), before)

    def test_duplicate_origin_is_rejected(self) -> None:
        before = len(self.controller.records)
        with socket.create_connection(("127.0.0.1", self.port), timeout=5) as connection:
            request = (
                "POST /api/v1/auth/login HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{self.port}\r\n"
                f"Origin: http://127.0.0.1:{self.port}\r\n"
                f"Origin: http://127.0.0.1:{self.port}\r\n"
                "Content-Type: application/json\r\n"
                "Idempotency-Key: duplicate-origin-0001\r\n"
                "Content-Length: 2\r\n"
                "Connection: close\r\n\r\n{}"
            ).encode("ascii")
            connection.sendall(request)
            response = connection.recv(4096)
        self.assertIn(b" 400 ", response.split(b"\r\n", 1)[0])
        self.assertEqual(len(self.controller.records), before)

    def test_unavailable_upstream_is_a_redacted_503(self) -> None:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            unused_port = probe.getsockname()[1]
        settings = service_module.Settings.from_root(
            REPO / "console/dist",
            host="127.0.0.1",
            port=0,
            controller_port=unused_port,
            controller_timeout=0.25,
        )
        server = service_module.create_server(settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            connection.request("GET", "/api/v1/auth/session", headers={"Host": f"127.0.0.1:{server.server_port}"})
            response = connection.getresponse()
            payload = response.read(1000)
            connection.close()
            self.assertEqual(response.status, 503)
            self.assertEqual(json.loads(payload)["type"], "urn:orchestra:console:controller_unavailable")
            self.assertNotIn(str(REPO).encode(), payload)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


class ControllerProxySettingsTest(unittest.TestCase):
    def test_controller_target_and_origin_are_loopback_and_canonical(self) -> None:
        with self.assertRaises(service_module.ConsoleServiceError):
            service_module.Settings.from_root(REPO / "console/dist", controller_host="192.0.2.10")
        with self.assertRaises(service_module.ConsoleServiceError):
            service_module.Settings.from_root(REPO / "console/dist", controller_origin="https://127.0.0.1:8787")
        with self.assertRaises(service_module.ConsoleServiceError):
            service_module.Settings.from_root(REPO / "console/dist", controller_origin="http://localhost:8787")
        with self.assertRaises(service_module.ConsoleServiceError):
            service_module.Settings.from_root(REPO / "console/dist", controller_origin="http://127.0.0.1:8787/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
