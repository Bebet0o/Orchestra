from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.test_console_controller_client import ConsoleControllerProxyTest, service_module

REPO = Path(__file__).resolve().parents[1]


class ConsoleProjectLifecycleSourceTest(unittest.TestCase):
    def test_source_and_distribution_match_and_expose_bounded_project_ui(self) -> None:
        for name in ("index.html", "assets/app.js", "assets/controller-client.js", "assets/styles.css"):
            source_name = name.removeprefix("assets/")
            source = REPO / "console/src" / source_name
            distribution = REPO / "console/dist" / name
            self.assertEqual(source.read_bytes(), distribution.read_bytes())
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in (
            'id="project-panel"',
            'id="project-create-form"',
            'id="project-update-form"',
            'data-project-command="enable"',
            'data-project-command="archive"',
        ):
            self.assertIn(marker, html)
        self.assertIn("client.createProject", app)
        self.assertIn("client.updateProject", app)
        self.assertIn("client.commandProject", app)
        self.assertIn("globalThis.confirm", app)
        self.assertNotIn("innerHTML", app)
        self.assertNotIn("localStorage", app)
        self.assertNotIn("sessionStorage", app)
        self.assertNotIn("deleteProject", client)
        self.assertNotIn('"delete"', client)

    def test_console_proxy_dynamic_project_boundary_is_closed(self) -> None:
        self.assertTrue(service_module._controller_route_exposed("GET", "/api/v1/projects/alpha"))
        self.assertTrue(service_module._controller_route_exposed("PATCH", "/api/v1/projects/alpha"))
        for command in ("enable", "disable", "rescan", "archive"):
            self.assertTrue(service_module._controller_route_exposed("POST", f"/api/v1/projects/alpha/commands/{command}"))
        for method, path in (
            ("DELETE", "/api/v1/projects/alpha"),
            ("POST", "/api/v1/projects/alpha/commands/delete"),
            ("POST", "/api/v1/projects/alpha/commands/start"),
            ("PATCH", "/api/v1/projects/../alpha"),
            ("GET", "/api/v1/projects/alpha/objectives"),
        ):
            self.assertFalse(service_module._controller_route_exposed(method, path))


class ConsoleProjectLifecycleProxyHTTPTest(ConsoleControllerProxyTest):
    def test_project_detail_patch_create_and_commands_are_forwarded(self) -> None:
        status, raw_headers, payload = self.request("GET", "/api/v1/projects/alpha")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"]["id"], "alpha")
        self.assertEqual(self.headers_map(raw_headers)["etag"], ['"1"'])

        common = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "Cookie": "orchestra_session=" + "a" * 64,
            "Idempotency-Key": "project-console-0001",
            "X-CSRF-Token": "csrf1.example",
        }
        body = b'{"name":"Alpha","slug":"alpha","repository":{"mode":"existing","url":null,"default_branch":"main"},"policy_id":"default","sandbox_profile_id":null}'
        status, _, _ = self.request("POST", "/api/v1/projects", body=body, headers=common)
        self.assertEqual(status, 202)
        record = self.controller.records[-1]
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["body"], body)
        self.assertEqual(record["headers"]["origin"], "http://127.0.0.1:8787")

        patch_headers = dict(common)
        patch_headers["Idempotency-Key"] = "project-console-0002"
        patch_headers["If-Match"] = '"1"'
        patch_body = b'{"name":"Alpha updated"}'
        status, _, _ = self.request("PATCH", "/api/v1/projects/alpha", body=patch_body, headers=patch_headers)
        self.assertEqual(status, 202)
        record = self.controller.records[-1]
        self.assertEqual(record["method"], "PATCH")
        self.assertEqual(record["headers"]["if-match"], '"1"')

        command_headers = dict(patch_headers)
        command_headers["Idempotency-Key"] = "project-console-0003"
        status, _, _ = self.request(
            "POST",
            "/api/v1/projects/alpha/commands/disable",
            body=b'{"reason":null}',
            headers=command_headers,
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.controller.records[-1]["path"], "/api/v1/projects/alpha/commands/disable")

    def test_unsafe_project_routes_fail_before_upstream(self) -> None:
        before = len(self.controller.records)
        headers = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "Idempotency-Key": "project-console-bad1",
            "X-CSRF-Token": "csrf1.example",
            "If-Match": '"1"',
        }
        for method, path in (
            ("POST", "/api/v1/projects/alpha/commands/delete"),
            ("POST", "/api/v1/projects/alpha/commands/start"),
            ("PATCH", "/api/v1/projects/alpha/objectives"),
        ):
            status, _, payload = self.request(method, path, body=b"{}", headers=headers)
            self.assertEqual(status, 404)
            self.assertEqual(json.loads(payload)["type"], "urn:orchestra:console:controller_route_not_exposed")
        self.assertEqual(len(self.controller.records), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
