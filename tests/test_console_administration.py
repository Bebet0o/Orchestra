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

service = load_module("orchestra_console_administration_service", REPO / "scripts/orchestra-console.py")

class AdministrationConsoleSourceTest(unittest.TestCase):
    def test_proxy_exposes_only_exact_system_diagnostic_reads(self) -> None:
        for path in ("/api/v1/system/health", "/api/v1/system/status"):
            self.assertTrue(service._controller_route_exposed("GET", path))
            self.assertFalse(service._controller_route_exposed("POST", path))
            self.assertFalse(service._controller_route_exposed("GET", path + "?verbose=1"))
        for path in ("/api/v1/system/logs", "/api/v1/system/config", "/api/v1/system/restart"):
            self.assertFalse(service._controller_route_exposed("GET", path))

    def test_administration_route_is_controller_backed_and_read_only(self) -> None:
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in ('id="administration-panel"', 'id="administration-health-state"', 'id="administration-component-list"', 'id="administration-capability-list"'):
            self.assertIn(marker, html)
        for marker in ("refreshAdministration", "renderAdministration", "client.systemHealth", "client.systemStatus", "administrationPanel.hidden"):
            self.assertIn(marker, app)
        self.assertIn('path: "/api/v1/system/health"', client)
        self.assertIn('path: "/api/v1/system/status"', client)
        for forbidden in ("systemRestart", "systemConfig", "systemLogs", "/api/v1/system/restart", "/api/v1/system/config", "/api/v1/system/logs"):
            self.assertNotIn(forbidden, client)

    def test_browser_has_no_privileged_administration_surface(self) -> None:
        source = "\n".join((REPO / "console/src" / name).read_text(encoding="utf-8") for name in ("app.js", "controller-client.js", "index.html"))
        for forbidden in ("orchestra.db", "/run/orchestra-docker", "/var/run/docker.sock", "journalctl", "docker.sock", "innerHTML", "localStorage", "sessionStorage", "indexedDB", "eval(", "new Function"):
            self.assertNotIn(forbidden, source)

if __name__ == "__main__":
    unittest.main(verbosity=2)
