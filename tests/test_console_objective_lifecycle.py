from __future__ import annotations

import json
import unittest
from pathlib import Path

from tests.test_console_controller_client import ConsoleControllerProxyTest, service_module

REPO = Path(__file__).resolve().parents[1]
OBJECTIVE_ID = "objective-" + "a" * 32
OPERATION_ID = "operation-" + "a" * 32


class ConsoleObjectiveLifecycleSourceTest(unittest.TestCase):
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
        for marker in (
            'href="/objectives"',
            'id="objective-panel"',
            'id="objective-create-form"',
            'id="objective-detail-card"',
            'data-objective-command="pause"',
            'data-objective-command="resume"',
            'data-objective-command="cancel"',
        ):
            self.assertIn(marker, html)
        for marker in (
            "client.objectives",
            "client.objective",
            "client.createObjective",
            "client.commandObjective",
            "client.operation",
            "globalThis.confirm",
            "Promise.allSettled",
        ):
            self.assertIn(marker, app)
        for marker in (
            "async objective",
            "async createObjective",
            "async commandObjective",
            "async operation",
            'new Set(["pause", "resume", "cancel"])',
        ):
            self.assertIn(marker, client)
        self.assertIn(".objective-grid", styles)
        self.assertNotIn("innerHTML", app)
        self.assertNotIn("localStorage", app)
        self.assertNotIn("sessionStorage", app)
        for forbidden in (
            "startObjective",
            "replanObjective",
            "archiveObjective",
            "deleteObjective",
        ):
            self.assertNotIn(forbidden, client)

    def test_console_proxy_objective_boundary_is_exact(self) -> None:
        allowed = (
            ("GET", "/api/v1/objectives"),
            ("POST", "/api/v1/objectives"),
            ("GET", f"/api/v1/objectives/{OBJECTIVE_ID}"),
            ("GET", f"/api/v1/operations/{OPERATION_ID}"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/pause"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/resume"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/cancel"),
        )
        for method, path in allowed:
            with self.subTest(method=method, path=path):
                self.assertTrue(service_module._controller_route_exposed(method, path))
        denied = (
            ("DELETE", f"/api/v1/objectives/{OBJECTIVE_ID}"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/start"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/replan"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/archive"),
            ("GET", f"/api/v1/objectives/{OBJECTIVE_ID}/tasks"),
            ("GET", f"/api/v1/objectives/{OBJECTIVE_ID}?x=1"),
            ("GET", "/api/v1/objectives/objective-invalid"),
            ("GET", f"/api/v1/operations/{OPERATION_ID}/logs"),
            ("GET", f"/api/v1/objectives/{OBJECTIVE_ID}/../secrets"),
        )
        for method, path in denied:
            with self.subTest(method=method, path=path):
                self.assertFalse(service_module._controller_route_exposed(method, path))


class ConsoleObjectiveLifecycleProxyHTTPTest(ConsoleControllerProxyTest):
    def test_reads_create_commands_and_operation_are_forwarded(self) -> None:
        status, _, payload = self.request("GET", "/api/v1/objectives")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"], [])

        status, _, payload = self.request("GET", f"/api/v1/objectives/{OBJECTIVE_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"]["id"], OBJECTIVE_ID)

        status, _, payload = self.request("GET", f"/api/v1/operations/{OPERATION_ID}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["data"]["id"], OPERATION_ID)

        common = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "Cookie": "orchestra_session=" + "a" * 64,
            "Idempotency-Key": "objective-console-0001",
            "X-CSRF-Token": "csrf1.example",
        }
        body = (
            b'{"project_ids":["alpha"],"title":"Objective Alpha",'
            b'"description":"Bounded objective","constraints":[],"priority":100,'
            b'"not_before":null,"max_parallel_tasks":1,"planning_max_attempts":3}'
        )
        status, _, payload = self.request("POST", "/api/v1/objectives", body=body, headers=common)
        self.assertEqual(status, 202)
        self.assertEqual(json.loads(payload)["data"]["target"]["id"], OBJECTIVE_ID)
        record = self.controller.records[-1]
        self.assertEqual(record["method"], "POST")
        self.assertEqual(record["body"], body)
        self.assertEqual(record["headers"]["origin"], "http://127.0.0.1:8787")

        for index, command in enumerate(("pause", "resume", "cancel"), 2):
            headers = dict(common)
            headers["Idempotency-Key"] = f"objective-console-000{index}"
            status, _, _ = self.request(
                "POST",
                f"/api/v1/objectives/{OBJECTIVE_ID}/commands/{command}",
                body=b'{"reason":null}',
                headers=headers,
            )
            self.assertEqual(status, 202)
            self.assertEqual(
                self.controller.records[-1]["path"],
                f"/api/v1/objectives/{OBJECTIVE_ID}/commands/{command}",
            )

    def test_unsafe_objective_routes_fail_before_upstream(self) -> None:
        before = len(self.controller.records)
        headers = {
            "Origin": f"http://127.0.0.1:{self.port}",
            "Content-Type": "application/json",
            "Idempotency-Key": "objective-console-bad1",
            "X-CSRF-Token": "csrf1.example",
        }
        for method, path in (
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/start"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/replan"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/archive"),
            ("POST", f"/api/v1/objectives/{OBJECTIVE_ID}/commands/delete"),
            ("PATCH", f"/api/v1/objectives/{OBJECTIVE_ID}"),
        ):
            status, _, payload = self.request(method, path, body=b"{}", headers=headers)
            self.assertEqual(status, 404)
            self.assertEqual(
                json.loads(payload)["type"],
                "urn:orchestra:console:controller_route_not_exposed",
            )
        self.assertEqual(len(self.controller.records), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
