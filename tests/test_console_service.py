from __future__ import annotations

import http.client
import importlib.util
import json
import shutil
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
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


build_module = load_module("orchestra_console_build", REPO / "scripts/orchestra-console-build.py")
service_module = load_module("orchestra_console_service", REPO / "scripts/orchestra-console.py")


class ConsoleBuildTest(unittest.TestCase):
    def test_committed_distribution_is_reproducible(self) -> None:
        source = REPO / "console/src"
        expected = build_module.tree_bytes(REPO / "console/dist")
        for _ in range(16):
            with tempfile.TemporaryDirectory() as temporary:
                output = Path(temporary) / "dist"
                build_module.build(source, output)
                self.assertEqual(build_module.tree_bytes(output), expected)

    def test_source_file_set_and_link_guards_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src"
            shutil.copytree(REPO / "console/src", source)
            (source / "unexpected.txt").write_text("unexpected", encoding="utf-8")
            with self.assertRaises(build_module.ConsoleBuildError):
                build_module.build(source, Path(temporary) / "dist")

        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "src"
            shutil.copytree(REPO / "console/src", source)
            (source / "app.js").unlink()
            (source / "app.js").symlink_to(source / "index.html")
            with self.assertRaises(build_module.ConsoleBuildError):
                build_module.build(source, Path(temporary) / "dist")

    def test_manifest_matches_all_public_assets(self) -> None:
        root = REPO / "console/dist"
        manifest = json.loads((root / "asset-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["entrypoint"], "index.html")
        self.assertEqual(
            set(manifest["files"]),
            {"index.html", "assets/app.js", "assets/controller-client.js", "assets/styles.css"},
        )
        for relative, metadata in manifest["files"].items():
            data = (root / relative).read_bytes()
            self.assertEqual(metadata["size"], len(data))
            self.assertEqual(metadata["sha256"], __import__("hashlib").sha256(data).hexdigest())


class ConsoleHTTPTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.settings = service_module.Settings.from_root(
            REPO / "console/dist",
            host="127.0.0.1",
            port=0,
            max_connections=8,
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

    def request(self, path: str, *, method: str = "GET", host: str | None = None, body: bytes | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Host": host or f"127.0.0.1:{self.port}"}
        if body is not None:
            headers["Content-Length"] = str(len(body))
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            payload = response.read(600_000)
            return response.status, {name.lower(): value for name, value in response.getheaders()}, payload
        finally:
            connection.close()

    def assert_security_headers(self, headers: dict[str, str]) -> None:
        self.assertIn("default-src 'none'", headers["content-security-policy"])
        self.assertIn("connect-src 'self'", headers["content-security-policy"])
        self.assertEqual(headers["cross-origin-resource-policy"], "same-origin")
        self.assertEqual(headers["referrer-policy"], "no-referrer")
        self.assertEqual(headers["x-content-type-options"], "nosniff")
        self.assertEqual(headers["x-frame-options"], "DENY")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertTrue(headers["x-request-id"].startswith("req_"))
        self.assertNotIn("Python", headers.get("server", ""))

    def test_health_version_routes_assets_and_head(self) -> None:
        status, headers, body = self.request("/health")
        self.assertEqual(status, 200)
        self.assertEqual(
            json.loads(body),
            {"service": "orchestra-console", "status": "ok"},
        )
        self.assert_security_headers(headers)

        status, _, body = self.request("/version")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["version"], "v1")

        for route in service_module.ROUTES:
            status, headers, body = self.request(route)
            self.assertEqual(status, 200, route)
            self.assertIn(b"Orchestra Console", body)
            self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
            self.assert_security_headers(headers)

        status, headers, body = self.request("/assets/app.js")
        self.assertEqual(status, 200)
        self.assertIn(b"const routes", body)
        self.assertIn(b"createControllerClient", body)
        self.assertNotIn(b"fetch(", body)
        self.assertNotIn(b"WebSocket(", body)
        self.assertNotIn(b"localStorage", body)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")

        status, headers, body = self.request("/assets/controller-client.js")
        self.assertEqual(status, 200)
        self.assertIn(b"fetch(", body)
        self.assertIn(b'credentials: "same-origin"', body)
        self.assertNotIn(b"WebSocket(", body)
        self.assertNotIn(b"localStorage", body)
        self.assertNotIn(b"127.0.0.1:8765", body)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")

        status, headers, body = self.request("/", method="HEAD")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertGreater(int(headers["content-length"]), 0)

    def test_unknown_encoded_query_host_and_methods_fail_closed(self) -> None:
        cases = (
            ("/missing", 404),
            ("/asset-manifest.json", 404),
            ("/../VERSION", 404),
            ("/%2e%2e/VERSION", 400),
            ("/dashboard?next=/projects", 400),
            ("/assets\\app.js", 400),
        )
        for path, expected in cases:
            status, headers, body = self.request(path)
            self.assertEqual(status, expected, path)
            self.assertLess(len(body), 500)
            self.assertNotIn(str(REPO).encode(), body)
            self.assert_security_headers(headers)

        status, _, _ = self.request("/", host="evil.example")
        self.assertEqual(status, 400)

        for method in ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"):
            status, headers, body = self.request("/", method=method, body=b"ignored")
            self.assertEqual(status, 405, method)
            self.assertIn(headers["allow"], {"GET, HEAD", "GET, HEAD, POST, PATCH"})
            self.assertLess(len(body), 500)

    def test_capacity_exhaustion_is_bounded_and_hardened(self) -> None:
        acquired = 0
        try:
            for _ in range(self.settings.max_connections):
                self.assertTrue(self.server._slots.acquire(blocking=False))
                acquired += 1
            status, headers, body = self.request("/health")
            self.assertEqual(status, 503)
            self.assertLess(len(body), 256)
            self.assert_security_headers(headers)
        finally:
            for _ in range(acquired):
                self.server._slots.release()

    def test_concurrent_reads_are_bounded_and_consistent(self) -> None:
        paths = list(service_module.ROUTES) * 2
        with ThreadPoolExecutor(max_workers=4) as executor:
            results = list(executor.map(lambda path: self.request(path), paths))
        for status, headers, body in results:
            self.assertEqual(status, 200)
            self.assertIn(b"Orchestra Console", body)
            self.assert_security_headers(headers)


class ConsoleSettingsTest(unittest.TestCase):
    def test_explicit_appliance_public_bind_accepts_unspecified_listener(self) -> None:
        settings = service_module.Settings.from_root(
            REPO / "console/dist",
            host="0.0.0.0",
            port=8080,
            controller_origin="https://orchestra.example:443",
            public_bind=True,
        )
        self.assertTrue(settings.public_bind)

    def test_non_loopback_and_unsafe_distribution_are_rejected(self) -> None:
        with self.assertRaises(service_module.ConsoleServiceError):
            service_module.Settings.from_root(REPO / "console/dist", host="0.0.0.0")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dist"
            shutil.copytree(REPO / "console/dist", root)
            (root / "assets/app.js").unlink()
            (root / "assets/app.js").symlink_to(root / "index.html")
            with self.assertRaises(service_module.ConsoleServiceError):
                service_module.Settings.from_root(root)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "dist"
            shutil.copytree(REPO / "console/dist", root)
            manifest = json.loads((root / "asset-manifest.json").read_text(encoding="utf-8"))
            manifest["files"]["assets/app.js"]["sha256"] = "0" * 64
            (root / "asset-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaises(service_module.ConsoleServiceError):
                service_module.Settings.from_root(root)

        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "target"
            shutil.copytree(REPO / "console/dist", target)
            link = Path(temporary) / "dist"
            link.symlink_to(target, target_is_directory=True)
            with self.assertRaises(service_module.ConsoleServiceError):
                service_module.Settings.from_root(link)


if __name__ == "__main__":
    unittest.main(verbosity=2)
