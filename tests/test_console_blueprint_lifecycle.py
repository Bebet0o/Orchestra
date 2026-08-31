from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.test_console_controller_client import ConsoleControllerProxyTest, service_module

REPO = Path(__file__).resolve().parents[1]
SANDBOX_ID = "sandbox-" + "a" * 32


class ConsoleBlueprintLifecycleSourceTest(unittest.TestCase):
    def test_source_distribution_and_bounded_ui_contract(self) -> None:
        for name in ("index.html", "assets/app.js", "assets/controller-client.js", "assets/styles.css"):
            source_name = name.removeprefix("assets/")
            self.assertEqual(
                (REPO / "console/src" / source_name).read_bytes(),
                (REPO / "console/dist" / name).read_bytes(),
            )
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        styles = (REPO / "console/src/styles.css").read_text(encoding="utf-8")
        for current_source in (html, app, client, styles):
            self.assertNotIn("hermesfile", current_source.lower())
        for marker in (
            'href="/blueprints"',
            'id="blueprint-panel"',
            'id="blueprint-source"',
            'id="blueprint-validate"',
            'id="blueprint-save"',
            'id="blueprint-revisions"',
            'id="blueprint-compare"',
        ):
            self.assertIn(marker, html)
        for marker in (
            "client.blueprints",
            "client.blueprintTemplate",
            "client.validateBlueprint",
            "client.createBlueprint",
            "client.updateBlueprint",
            "client.compareBlueprintRevisions",
            "globalThis.confirm",
        ):
            self.assertIn(marker, app)
        for marker in (
            "async blueprints",
            "async validateBlueprint",
            "async createBlueprint",
            "async updateBlueprint",
            "If-Match",
        ):
            self.assertIn(marker, client)
        self.assertIn('path: "/api/v1/blueprints"', client)
        self.assertNotIn("/api/v1/hermesfiles", client)
        self.assertIn(".blueprint-source", styles)
        self.assertNotIn("innerHTML", app)
        self.assertNotIn("localStorage", app)
        self.assertNotIn("sessionStorage", app)
        for forbidden in ("deleteBlueprint", "buildBlueprint", "activateBlueprint", "bindSecret"):
            self.assertNotIn(forbidden, client)

    def test_console_proxy_blueprint_boundary_is_exact(self) -> None:
        allowed = (
            ("GET", "/api/v1/blueprints"),
            ("POST", "/api/v1/blueprints"),
            ("POST", "/api/v1/blueprints/validate"),
            ("GET", "/api/v1/blueprints/template"),
            ("GET", f"/api/v1/blueprints/{SANDBOX_ID}"),
            ("PATCH", f"/api/v1/blueprints/{SANDBOX_ID}"),
            ("GET", f"/api/v1/blueprints/{SANDBOX_ID}/revisions"),
            ("GET", f"/api/v1/blueprints/{SANDBOX_ID}/revisions/1"),
            ("GET", f"/api/v1/blueprints/{SANDBOX_ID}/diff"),
        )
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(service_module._controller_route_exposed(method, path))
        denied = (
            ("GET", "/api/v1/hermesfiles"),
            ("POST", "/api/v1/hermesfiles"),
            ("GET", f"/api/v1/hermesfiles/{SANDBOX_ID}"),
            ("PATCH", f"/api/v1/hermesfiles/{SANDBOX_ID}"),
            ("DELETE", f"/api/v1/blueprints/{SANDBOX_ID}"),
            ("POST", f"/api/v1/blueprints/{SANDBOX_ID}/builds"),
            ("POST", f"/api/v1/blueprints/{SANDBOX_ID}/activate"),
            ("PATCH", "/api/v1/blueprints/sandbox-invalid"),
            ("GET", f"/api/v1/blueprints/{SANDBOX_ID}/revisions/0"),
            ("GET", f"/api/v1/blueprints/{SANDBOX_ID}/../secrets"),
        )
        for method, path in denied:
            with self.subTest(method=method, path=path):
                self.assertFalse(service_module._controller_route_exposed(method, path))


class ConsoleBlueprintLifecycleProxyHTTPTest(ConsoleControllerProxyTest):
    def test_console_blueprint_route_replaces_hermesfiles(self) -> None:
        status, _, payload = self.request("GET", "/blueprints")
        self.assertEqual(status, 200)
        self.assertIn(b"HermesOps Console", payload)

        before = len(self.controller.records)
        status, _, _ = self.request("GET", "/hermesfiles")
        self.assertEqual(status, 404)
        self.assertEqual(len(self.controller.records), before)

        status, _, _ = self.request("GET", "/api/v1/hermesfiles")
        self.assertEqual(status, 404)
        self.assertEqual(len(self.controller.records), before)

    def test_reads_validation_create_update_and_diff_are_forwarded(self) -> None:
        status, _, payload = self.request("GET", "/api/v1/blueprints")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"], [])

        status, raw_headers, payload = self.request("GET", f"/api/v1/blueprints/{SANDBOX_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(self.headers_map(raw_headers)["etag"], ['"1"'])
        self.assertEqual(json.loads(payload)["data"]["profile"]["id"], SANDBOX_ID)

        common = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "Cookie": "hermesops_session=" + "a" * 64,
            "X-CSRF-Token": "csrf1.example",
            "Idempotency-Key": "blueprint-console-0001",
        }
        source_body = b'{"source":"apiVersion: hermesops.dev/v1\\n"}'
        status, _, payload = self.request(
            "POST", "/api/v1/blueprints/validate", body=source_body, headers=common
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["data"]["valid"])
        self.assertEqual(self.controller.records[-1]["body"], source_body)
        self.assertEqual(self.controller.records[-1]["headers"]["origin"], "http://127.0.0.1:8787")

        create_headers = dict(common)
        create_headers["Idempotency-Key"] = "blueprint-console-0002"
        status, _, _ = self.request("POST", "/api/v1/blueprints", body=source_body, headers=create_headers)
        self.assertEqual(status, 202)

        patch_headers = dict(common)
        patch_headers["Idempotency-Key"] = "blueprint-console-0003"
        patch_headers["If-Match"] = '"1"'
        status, _, _ = self.request(
            "PATCH", f"/api/v1/blueprints/{SANDBOX_ID}", body=source_body, headers=patch_headers
        )
        self.assertEqual(status, 202)
        self.assertEqual(self.controller.records[-1]["headers"]["if-match"], '"1"')

        status, _, payload = self.request(
            "GET", f"/api/v1/blueprints/{SANDBOX_ID}/diff?from=1&to=2"
        )
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(payload)["data"]["changed"])

    def test_out_of_scope_mutations_fail_before_upstream(self) -> None:
        before = len(self.controller.records)
        headers = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "Idempotency-Key": "blueprint-console-bad1",
            "X-CSRF-Token": "csrf1.example",
            "If-Match": '"1"',
        }
        for method, path in (
            ("POST", f"/api/v1/blueprints/{SANDBOX_ID}/builds"),
            ("POST", f"/api/v1/blueprints/{SANDBOX_ID}/activate"),
            ("PATCH", f"/api/v1/blueprints/{SANDBOX_ID}/revisions/1"),
        ):
            with self.subTest(method=method, path=path):
                status, _, payload = self.request(method, path, body=b"{}", headers=headers)
                self.assertEqual(status, 404)
                self.assertEqual(
                    json.loads(payload)["type"],
                    "urn:hermesops:console:controller_route_not_exposed",
                )
        self.assertEqual(len(self.controller.records), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
