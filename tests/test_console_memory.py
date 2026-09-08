from __future__ import annotations

import unittest
from pathlib import Path

from tests.test_console_controller_client import service_module

REPO = Path(__file__).resolve().parents[1]


class ConsoleMemorySourceTest(unittest.TestCase):
    def test_source_distribution_and_closed_memory_ui_contract(self) -> None:
        for name in ("index.html", "assets/app.js", "assets/controller-client.js", "assets/styles.css"):
            source = REPO / "console/src" / name.removeprefix("assets/")
            distribution = REPO / "console/dist" / name
            self.assertEqual(source.read_bytes(), distribution.read_bytes())
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in (
            'href="/memory"', 'id="memory-panel"', 'id="memory-project-select"',
            'id="memory-editor-form"', 'value="DECISION"', 'data-memory-command="retract"',
            'data-memory-command="redact"', 'id="memory-revisions"',
        ):
            self.assertIn(marker, html)
        for marker in ("client.memories", "client.memoryRevisions", "client.createMemory", "client.reviseMemory", "client.commandMemory", "globalThis.confirm"):
            self.assertIn(marker, app)
        self.assertIn('"memories"', client)
        self.assertIn("function safeId(item, fallback, maximum = 96)", app)
        self.assertGreaterEqual(app.count('selectedMemoryId = "";'), 4)
        self.assertIn("L’autorité et la provenance seront dérivées par le Controller.", app)
        self.assertGreaterEqual(app.count("generation !== memoryGeneration"), 3)
        self.assertIn('memoryEditorObjective.disabled = busy || Boolean(selectedMemoryId) || memoryEditorScope.value !== "OBJECTIVE";', app)
        self.assertNotIn('name="authority"', html)
        self.assertNotIn('name="provenance"', html)
        self.assertNotIn("innerHTML", app)
        self.assertNotIn("localStorage", app)
        self.assertNotIn("sessionStorage", app)
        self.assertNotIn("deleteMemory", client)
        self.assertNotIn("reactivateMemory", client)

    def test_console_proxy_exposes_only_memory_a2_surface(self) -> None:
        memory = "memory-" + "a" * 32
        legacy = "legacy-memory-record%3Aold-id"
        for identifier in (memory, legacy):
            self.assertTrue(service_module._controller_route_exposed("GET", f"/api/v1/memories/{identifier}"))
        self.assertTrue(service_module._controller_route_exposed("GET", "/api/v1/projects/alpha/memories"))
        self.assertTrue(service_module._controller_route_exposed("POST", "/api/v1/projects/alpha/memories"))
        self.assertTrue(service_module._controller_route_exposed("PATCH", f"/api/v1/memories/{memory}"))
        self.assertTrue(service_module._controller_route_exposed("GET", f"/api/v1/memories/{memory}/revisions"))
        self.assertTrue(service_module._controller_route_exposed("GET", f"/api/v1/memories/{memory}/revisions/1"))
        for command in ("retract", "redact"):
            self.assertTrue(service_module._controller_route_exposed("POST", f"/api/v1/memories/{memory}/commands/{command}"))
        for method, path in (
            ("DELETE", f"/api/v1/memories/{memory}"),
            ("POST", f"/api/v1/memories/{memory}/commands/reactivate"),
            ("POST", f"/api/v1/memories/{memory}/commands/delete"),
            ("GET", f"/api/v1/memories/{memory}?verbose=1"),
            ("GET", "/api/v1/memories/legacy%2Fescape"),
            ("GET", "/api/v1/projects/alpha/memories?limit=200"),
        ):
            self.assertFalse(service_module._controller_route_exposed(method, path), (method, path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
