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


service = load_module("orchestra_console_multi_agent_service", REPO / "scripts/orchestra-console.py")
PLAN = "plan-" + "a" * 32


class MultiAgentConsoleSourceTest(unittest.TestCase):
    def test_proxy_exposes_only_bounded_plan_detail_reads(self) -> None:
        for path in (
            f"/api/v1/plans/{PLAN}",
            f"/api/v1/plans/{PLAN}/tasks",
            f"/api/v1/plans/{PLAN}/dependencies",
            f"/api/v1/plans/{PLAN}/attempts",
        ):
            with self.subTest(path=path):
                self.assertTrue(service._controller_route_exposed("GET", path))
        for method, path in (
            ("POST", f"/api/v1/plans/{PLAN}"),
            ("GET", f"/api/v1/plans/{PLAN}/unknown"),
            ("GET", f"/api/v1/plans/{PLAN}/tasks?limit=200"),
            ("GET", "/api/v1/plans/not-a-plan/tasks"),
            ("GET", f"/api/v1/plans/{PLAN}/tasks/extra"),
        ):
            with self.subTest(method=method, path=path):
                self.assertFalse(service._controller_route_exposed(method, path))

    def test_execution_route_is_a_real_controller_backed_panel(self) -> None:
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in (
            'id="execution-panel"',
            'id="execution-plan-list"',
            'id="execution-detail-card"',
            'id="execution-task-list"',
            'id="execution-dependency-list"',
            'id="execution-attempt-list"',
        ):
            self.assertIn(marker, html)
        for marker in (
            "refreshExecutions",
            "selectExecutionPlan",
            "client.planTasks",
            "client.planDependencies",
            "client.planAttempts",
            "executionPanel.hidden",
            "textContent",
            "replaceChildren",
        ):
            self.assertIn(marker, app)
        for marker in (
            "async plan(identifier)",
            "async planTasks(identifier)",
            "async planDependencies(identifier)",
            "async planAttempts(identifier)",
            "PLAN_ID_PATTERN",
        ):
            self.assertIn(marker, client)
        for forbidden in ("innerHTML", "localStorage", "sessionStorage", "indexedDB", "eval(", "new Function"):
            self.assertNotIn(forbidden, app)

    def test_no_direct_sqlite_or_privileged_runtime_surface_is_added_to_browser(self) -> None:
        source = "\n".join(
            (REPO / "console/src" / name).read_text(encoding="utf-8")
            for name in ("app.js", "controller-client.js", "index.html")
        )
        for forbidden in (
            "orchestra.db",
            "/run/orchestra-docker",
            "/var/run/docker.sock",
            "prompt_path",
            "output_path",
            "result_json",
            "failure_reason",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
