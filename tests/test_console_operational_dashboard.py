from __future__ import annotations

import importlib.util
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


service = load_module("orchestra_console_dashboard_service", REPO / "scripts/orchestra-console.py")


class ConsoleOperationalDashboardSourceTest(unittest.TestCase):
    def test_proxy_preserves_dashboard_routes_and_closed_boundaries(self) -> None:
        required = {
            ("GET", "/api/v1/auth/session"),
            ("POST", "/api/v1/auth/login"),
            ("POST", "/api/v1/auth/csrf"),
            ("POST", "/api/v1/auth/logout"),
            ("GET", "/api/v1/system/capabilities"),
            ("GET", "/api/v1/projects"),
            ("POST", "/api/v1/projects"),
            ("GET", "/api/v1/objectives"),
            ("GET", "/api/v1/reviews"),
            ("GET", "/api/v1/recoveries"),
            ("GET", "/api/v1/plans"),
            ("GET", "/api/v1/reviewer-assignments"),
        }
        self.assertTrue(required.issubset(service.CONTROLLER_ROUTES))
        self.assertNotIn(("GET", "/api/v1/tasks"), service.CONTROLLER_ROUTES)
        self.assertNotIn(("GET", "/api/v1/events"), service.CONTROLLER_ROUTES)

    def test_dashboard_dom_and_client_operations_are_closed(self) -> None:
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in (
            'id="dashboard-panel"',
            'id="dashboard-refresh"',
            'id="attention-list"',
            'id="active-work-list"',
            'id="project-list"',
            'id="dashboard-coverage"',
        ):
            self.assertIn(marker, html)
        for marker in (
            "Promise.allSettled",
            "client.projects()",
            "client.objectives()",
            "client.reviews()",
            "client.recoveries()",
            "client.plans()",
            "client.reviewerAssignments()",
            "replaceChildren",
            "textContent",
        ):
            self.assertIn(marker, app)
        for forbidden in (
            "fetch(",
            "innerHTML",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "WebSocket(",
            "eval(",
            "new Function",
        ):
            self.assertNotIn(forbidden, app)
        for path in (
            "/api/v1/projects",
            "/api/v1/objectives",
            "/api/v1/reviews",
            "/api/v1/recoveries",
            "/api/v1/plans",
            "/api/v1/reviewer-assignments",
        ):
            self.assertIn(f'path: "{path}"', client)
        for forbidden in (
            "127.0.0.1:8765",
            "localStorage",
            "sessionStorage",
            "indexedDB",
            "WebSocket(",
        ):
            self.assertNotIn(forbidden, client)

    def test_committed_distribution_contains_same_dashboard_sources(self) -> None:
        mapping = {
            "index.html": "index.html",
            "app.js": "assets/app.js",
            "controller-client.js": "assets/controller-client.js",
            "styles.css": "assets/styles.css",
        }
        for source_name, dist_name in mapping.items():
            with self.subTest(source=source_name):
                self.assertEqual(
                    (REPO / "console/src" / source_name).read_bytes(),
                    (REPO / "console/dist" / dist_name).read_bytes(),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
