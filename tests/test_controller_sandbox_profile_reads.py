from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from urllib.parse import quote

from controller_api.core import Settings
from controller_api.sandbox_profiles import SandboxProfileStore
from controller_api.server import build_server


VALID = """
apiVersion: hermesops.dev/v1
kind: SandboxProfile
metadata:
  name: python-project
  displayName: Python Project Worker
  description: Reproducible Python sandbox.
  labels:
    language: python
spec:
  base:
    image: python
    digest: sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
  build:
    python:
      packages: [pytest==9.0.2]
  workspace:
    user: hermes
    group: hermes
    directory: /workspace
    sourceMode: worktree
  runtime:
    cpu: 4
    memory: 1GiB
    pids: 512
    timeout: 2h
    stopGracePeriod: 30s
  network:
    build:
      mode: allowlist
      allow: [pypi.org]
    runtime:
      mode: none
      allow: []
  security:
    privileged: false
    noNewPrivileges: true
    readOnlyRoot: false
    capabilities:
      drop: [ALL]
      add: []
    seccompProfile: default
    secrets: false
    allowDockerSocket: false
    allowDeviceAccess: false
  validation:
    commands:
      - name: python
        run: [python3, --version]
        timeout: 30s
        expectExitCode: 0
"""


class SandboxProfileHTTPReadTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        (root / "repo").mkdir(mode=0o700)
        secrets = root / "secrets"
        secrets.mkdir(mode=0o700)
        self.token = "sandbox-http-test-" + "a" * 48
        session = secrets / "controller-session"
        session.write_text(self.token + "\n", encoding="ascii")
        session.chmod(0o600)
        database = root / "state" / "controller" / "orchestra.db"
        database.parent.mkdir(parents=True, mode=0o700)
        self._apply_migrations(database)
        self.settings = Settings.from_root(
            root,
            database=database,
            session_file=session,
            host="127.0.0.1",
            port=0,
        )
        store = SandboxProfileStore(self.settings)
        first = store.import_source(textwrap.dedent(VALID).encode("utf-8"))
        second_source = textwrap.dedent(VALID).replace(
            "python-project",
            "python-project-two",
        )
        store.import_source(second_source.encode("utf-8"))
        self.first_id = first.profile["id"]
        self.database = database
        self.database_hash_before = self._logical_hash(database)
        self.server = build_server(self.settings)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.05},
            daemon=True,
        )
        self.thread.start()
        self.port = int(self.server.server_address[1])
        for _ in range(100):
            try:
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    self.port,
                    timeout=0.2,
                )
                connection.request(
                    "GET",
                    "/health",
                    headers={"Host": f"127.0.0.1:{self.port}"},
                )
                response = connection.getresponse()
                response.read()
                connection.close()
                if response.status == 200:
                    break
            except OSError:
                time.sleep(0.01)
        else:
            raise RuntimeError("temporary Controller did not start")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    @staticmethod
    def _apply_migrations(database: Path) -> None:
        repository = Path(__file__).resolve().parents[1]
        connection = sqlite3.connect(database)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        for migration in sorted((repository / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
            script = migration.read_text(encoding="utf-8")
            connection.executescript(
                "BEGIN IMMEDIATE;\n" + script + "\nCOMMIT;\n"
            )
        connection.close()

    @staticmethod
    def _logical_hash(database: Path) -> str:
        connection = sqlite3.connect(database)
        dump = "\n".join(connection.iterdump()).encode("utf-8")
        connection.close()
        return hashlib.sha256(dump).hexdigest()

    def request(
        self,
        method: str,
        path: str,
        *,
        authenticated: bool = True,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {
            "Accept": "application/json",
            "Host": f"127.0.0.1:{self.port}",
        }
        if authenticated:
            headers["Cookie"] = f"orchestra_session={self.token}"
        if body is not None:
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(body))
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.port,
            timeout=5,
        )
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result_headers = {
            key.lower(): value
            for key, value in response.getheaders()
        }
        status = response.status
        connection.close()
        return status, result_headers, payload

    @staticmethod
    def json_payload(body: bytes) -> dict:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise AssertionError("response is not an object")
        return value

    @staticmethod
    def assert_public(value) -> None:
        forbidden_keys = {
            "source",
            "source_text",
            "canonical",
            "canonical_json",
            "repo_path",
            "data_path",
            "host_path",
            "password",
            "secret",
            "secret_value",
            "token",
            "credential",
        }
        if isinstance(value, dict):
            for key, item in value.items():
                if str(key).lower() in forbidden_keys:
                    raise AssertionError(f"private key exposed: {key}")
                SandboxProfileHTTPReadTest.assert_public(item)
        elif isinstance(value, list):
            for item in value:
                SandboxProfileHTTPReadTest.assert_public(item)

    def test_list_detail_etag_pagination_and_redaction(self) -> None:
        status, _, body = self.request("GET", "/api/v1/sandboxes?limit=1")
        self.assertEqual(status, 200, body)
        payload = self.json_payload(body)
        self.assert_public(payload)
        self.assertEqual(len(payload["data"]), 1)
        cursor = payload["meta"]["next_cursor"]
        self.assertIsInstance(cursor, str)

        status, _, body = self.request(
            "GET",
            "/api/v1/sandboxes?limit=1&cursor="
            + quote(cursor, safe=""),
        )
        self.assertEqual(status, 200, body)
        second_page = self.json_payload(body)
        self.assertEqual(len(second_page["data"]), 1)
        self.assertNotEqual(
            payload["data"][0]["id"],
            second_page["data"][0]["id"],
        )

        status, headers, body = self.request(
            "GET",
            f"/api/v1/sandboxes/{self.first_id}",
        )
        self.assertEqual(status, 200, body)
        detail = self.json_payload(body)
        self.assert_public(detail)
        self.assertEqual(detail["data"]["id"], self.first_id)
        self.assertEqual(headers.get("etag"), '"1"')
        self.assertEqual(detail["meta"]["resource_revision"], 1)

        status, headers, body = self.request(
            "HEAD",
            f"/api/v1/sandboxes/{self.first_id}",
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(headers.get("etag"), '"1"')

    def test_authentication_queries_and_methods_fail_closed(self) -> None:
        status, _, body = self.request(
            "GET",
            "/api/v1/sandboxes",
            authenticated=False,
        )
        self.assertEqual(status, 401, body)
        for path, code in (
            ("/api/v1/sandboxes?limit=0", "invalid_limit"),
            ("/api/v1/sandboxes?limit=201", "invalid_limit"),
            ("/api/v1/sandboxes?state=unknown", "invalid_state"),
            ("/api/v1/sandboxes?unexpected=1", "unknown_query_parameter"),
        ):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 400, body)
                self.assertEqual(self.json_payload(body)["code"], code)
        status, _, body = self.request(
            "GET",
            "/api/v1/sandboxes/%2Fbad",
        )
        self.assertEqual(status, 404, body)
        status, _, body = self.request(
            "POST",
            "/api/v1/sandboxes",
            body=b"{}",
        )
        self.assertIn(status, {404, 405}, body)

    def test_authenticated_gets_do_not_modify_database(self) -> None:
        for _ in range(5):
            status, _, body = self.request(
                "GET",
                "/api/v1/sandboxes?limit=10&state=draft",
            )
            self.assertEqual(status, 200, body)
            status, _, body = self.request(
                "GET",
                f"/api/v1/sandboxes/{self.first_id}",
            )
            self.assertEqual(status, 200, body)
        self.assertEqual(
            self._logical_hash(self.database),
            self.database_hash_before,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
