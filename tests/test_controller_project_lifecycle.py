from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from controller_api.core import ControllerError, Settings
from controller_api.project_commands import ProjectCommandStore
from controller_api.server import build_server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 64


class ProjectLifecycleFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "repo/config/policies").mkdir(parents=True)
        (self.root / "repo/config/projects.d").mkdir(parents=True)
        (self.root / "state/controller").mkdir(parents=True)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "secrets/controller-session").write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.root / "secrets/controller-session", 0o600)
        shutil.copy2(ROOT / "config/policies/default.toml", self.root / "repo/config/policies/default.toml")
        shutil.copy2(ROOT / "config/controller.toml", self.root / "repo/config/controller.toml")
        self.database = self.root / "state/controller/orchestra.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
        self.settings = Settings.from_root(self.root)
        self.store = ProjectCommandStore(self.settings)

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        key: str | None = None,
        csrf: str | None = None,
        if_match: str | None = None,
    ):
        server = build_server(Settings.from_root(self.root, port=0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {"Cookie": f"orchestra_session={TOKEN}"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":")).encode()
            headers["Content-Type"] = "application/json"
        if key is not None:
            headers["Idempotency-Key"] = key
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if if_match is not None:
            headers["If-Match"] = if_match
        try:
            connection.request(method, path, body=encoded, headers=headers)
            response = connection.getresponse()
            raw = response.read()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, json.loads(raw) if raw else None
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def close(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def meta(revision: int | None) -> dict[str, object]:
        return {"request_id": "request-project-test", "resource_revision": revision}

    def create(self, *, key: str = "project-create-0001", slug: str = "alpha"):
        return self.store.create_project(
            session_token=TOKEN,
            idempotency_key=key,
            route="/api/v1/projects",
            body={
                "name": "Alpha",
                "slug": slug,
                "repository": {"mode": "initialize", "url": None, "default_branch": "main"},
                "policy_id": "default",
                "sandbox_profile_id": None,
            },
            meta_factory=self.meta,
        )

    def row(self, slug: str = "alpha") -> sqlite3.Row:
        connection = sqlite3.connect(self.database)
        connection.row_factory = sqlite3.Row
        try:
            row = connection.execute("SELECT * FROM projects WHERE project_id=?", (slug,)).fetchone()
            assert row is not None
            return row
        finally:
            connection.close()


class ProjectLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ProjectLifecycleFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_migration_readiness_and_constraints(self) -> None:
        ready, reason = self.fixture.store.readiness()
        self.assertTrue(ready, reason)
        with sqlite3.connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 24)
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 24)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(projects)")}
            self.assertTrue({"default_branch", "sandbox_profile_id", "archived", "repository_mode", "resource_revision"} <= columns)

    def test_create_is_atomic_audited_evented_and_idempotent(self) -> None:
        status, first = self.fixture.create()
        replay_status, replay = self.fixture.create()
        self.assertEqual(status, 202)
        self.assertEqual(replay_status, 202)
        self.assertEqual(first, replay)
        row = self.fixture.row()
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["default_branch"], "main")
        self.assertEqual(row["repository_mode"], "initialize")
        self.assertEqual(row["resource_revision"], 1)
        self.assertTrue((self.fixture.root / "workspaces/alpha/.git").is_dir())
        self.assertTrue((self.fixture.root / "project-data/alpha").is_dir())
        config = self.fixture.root / "repo/config/projects.d/alpha.toml"
        self.assertTrue(config.is_file())
        self.assertNotIn("session-token", config.read_text(encoding="utf-8"))
        with sqlite3.connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_project_operations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_project_command_audit").fetchone()[0], 1)
            event = connection.execute("SELECT event_type, redacted_data_json FROM controller_event_journal").fetchone()
            self.assertEqual(event[0], "project.created")
            self.assertNotIn("session", event[1])

    def test_changed_body_with_reused_key_conflicts(self) -> None:
        self.fixture.create(key="project-conflict-01")
        with self.assertRaises(ControllerError) as caught:
            self.fixture.store.create_project(
                session_token=TOKEN,
                idempotency_key="project-conflict-01",
                route="/api/v1/projects",
                body={
                    "name": "Changed",
                    "slug": "alpha",
                    "repository": {"mode": "initialize", "url": None, "default_branch": "main"},
                    "policy_id": "default",
                    "sandbox_profile_id": None,
                },
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(caught.exception.code, "idempotency_conflict")

    def test_update_enable_disable_rescan_archive_lifecycle(self) -> None:
        self.fixture.create()
        status, updated = self.fixture.store.update_project(
            session_token=TOKEN,
            idempotency_key="project-update-0001",
            route="/api/v1/projects/alpha",
            project_id="alpha",
            if_match='"1"',
            body={"name": "Alpha updated", "policy_id": "default", "sandbox_profile_id": None},
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        self.assertEqual(updated["meta"]["resource_revision"], 2)
        commands = [
            ("enable", 2, 3, "enabled"),
            ("rescan", 3, 3, "enabled"),
            ("disable", 3, 4, "disabled"),
            ("archive", 4, 5, "archived"),
        ]
        for index, (command, revision, expected_revision, state) in enumerate(commands):
            status, payload = self.fixture.store.command_project(
                session_token=TOKEN,
                idempotency_key=f"project-command-{index:04d}",
                route=f"/api/v1/projects/alpha/commands/{command}",
                project_id="alpha",
                command=command,
                if_match=f'"{revision}"',
                body={"reason": "operator intent"},
                meta_factory=self.fixture.meta,
            )
            self.assertEqual(status, 202)
            self.assertEqual(payload["data"]["result"]["state"], state)
            self.assertEqual(payload["data"]["result"]["resource_revision"], expected_revision)
        row = self.fixture.row()
        self.assertEqual(row["archived"], 1)
        self.assertEqual(row["enabled"], 0)
        self.assertEqual(row["resource_revision"], 5)

    def test_if_match_and_active_work_fail_closed(self) -> None:
        self.fixture.create()
        with self.assertRaises(ControllerError) as missing:
            self.fixture.store.command_project(
                session_token=TOKEN,
                idempotency_key="project-enable-no-match",
                route="/api/v1/projects/alpha/commands/enable",
                project_id="alpha",
                command="enable",
                if_match=None,
                body={},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(missing.exception.status, 428)
        self.fixture.store.command_project(
            session_token=TOKEN,
            idempotency_key="project-enable-active",
            route="/api/v1/projects/alpha/commands/enable",
            project_id="alpha",
            command="enable",
            if_match='"1"',
            body={},
            meta_factory=self.fixture.meta,
        )
        with sqlite3.connect(self.fixture.database) as connection:
            connection.execute(
                "INSERT INTO project_locks(project_id,run_id,holder,acquired_at,heartbeat_at) VALUES(?,?,?,?,?)",
                ("alpha", "run-active", "test", "2026-07-27T00:00:00.000Z", "2026-07-27T00:00:00.000Z"),
            )
        with self.assertRaises(ControllerError) as active:
            self.fixture.store.command_project(
                session_token=TOKEN,
                idempotency_key="project-disable-active",
                route="/api/v1/projects/alpha/commands/disable",
                project_id="alpha",
                command="disable",
                if_match='"2"',
                body={},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(active.exception.code, "project_has_active_work")

    def test_http_create_patch_command_and_operation_projection(self) -> None:
        status, _, csrf_payload = self.fixture.request(
            "POST", "/api/v1/auth/csrf", body={}, key="project-http-csrf-01"
        )
        self.assertEqual(status, 200)
        csrf = csrf_payload["data"]["token"]
        status, _, created = self.fixture.request(
            "POST",
            "/api/v1/projects",
            body={
                "name": "HTTP Project",
                "slug": "http-project",
                "repository": {"mode": "initialize", "url": None, "default_branch": "main"},
                "policy_id": "default",
                "sandbox_profile_id": None,
            },
            key="project-http-create",
            csrf=csrf,
        )
        self.assertEqual(status, 202)
        operation_id = created["data"]["id"]
        status, headers, detail = self.fixture.request("GET", "/api/v1/projects/http-project")
        self.assertEqual(status, 200)
        self.assertEqual(headers["etag"], '"1"')
        self.assertEqual(detail["data"]["repository"]["mode"], "initialize")
        status, _, updated = self.fixture.request(
            "PATCH",
            "/api/v1/projects/http-project",
            body={"name": "HTTP Updated"},
            key="project-http-update",
            csrf=csrf,
            if_match='"1"',
        )
        self.assertEqual(status, 202)
        self.assertEqual(updated["meta"]["resource_revision"], 2)
        status, _, operation = self.fixture.request("GET", f"/api/v1/operations/{operation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(operation["data"]["target"]["id"], "http-project")
        status, _, unavailable = self.fixture.request(
            "POST",
            "/api/v1/projects/http-project/commands/delete",
            body={},
            key="project-http-delete",
            csrf=csrf,
            if_match='"2"',
        )
        self.assertEqual(status, 409)
        self.assertEqual(unavailable["code"], "project_command_unavailable")

    def test_invalid_inputs_and_delete_are_unavailable(self) -> None:
        with self.assertRaises(ControllerError) as url:
            self.fixture.store.create_project(
                session_token=TOKEN,
                idempotency_key="project-url-invalid",
                route="/api/v1/projects",
                body={
                    "name": "Invalid",
                    "slug": "invalid",
                    "repository": {"mode": "clone", "url": "file:///etc", "default_branch": "main"},
                    "policy_id": "default",
                    "sandbox_profile_id": None,
                },
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(url.exception.code, "invalid_repository_url")
        self.fixture.create()
        with self.assertRaises(ControllerError) as unavailable:
            self.fixture.store.command_project(
                session_token=TOKEN,
                idempotency_key="project-delete-unavailable",
                route="/api/v1/projects/alpha/commands/delete",
                project_id="alpha",
                command="delete",
                if_match='"1"',
                body={},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(unavailable.exception.code, "project_command_unavailable")

    def test_registry_sync_preserves_lifecycle_and_backfills_branch(self) -> None:
        self.fixture.create()
        with sqlite3.connect(self.fixture.database) as connection:
            connection.execute(
                "UPDATE projects SET default_branch='unknown', resource_revision=resource_revision+1 WHERE project_id='alpha'"
            )
        environment = dict(os.environ)
        environment["ORCHESTRA_ROOT"] = str(self.fixture.root)
        result = subprocess.run(
            ["python3", str(ROOT / "scripts/orchestra-registry.py"), "sync"],
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        row = self.fixture.row()
        self.assertEqual(row["default_branch"], "main")
        self.assertEqual(row["repository_mode"], "initialize")
        self.assertEqual(row["archived"], 0)
        self.assertEqual(row["resource_revision"], 3)

    def test_existing_repository_mode_and_dirty_enable_guard(self) -> None:
        repository = self.fixture.root / "workspaces/existing"
        repository.mkdir(parents=True)
        subprocess.run(["git", "init", "--initial-branch", "main", str(repository)], check=True, stdout=subprocess.DEVNULL)
        status, _ = self.fixture.store.create_project(
            session_token=TOKEN,
            idempotency_key="project-existing-01",
            route="/api/v1/projects",
            body={
                "name": "Existing",
                "slug": "existing",
                "repository": {"mode": "existing", "url": None, "default_branch": "main"},
                "policy_id": "default",
                "sandbox_profile_id": None,
            },
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        (repository / "dirty.txt").write_text("dirty", encoding="utf-8")
        with self.assertRaises(ControllerError) as dirty:
            self.fixture.store.command_project(
                session_token=TOKEN,
                idempotency_key="project-enable-dirty",
                route="/api/v1/projects/existing/commands/enable",
                project_id="existing",
                command="enable",
                if_match='"1"',
                body={},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(dirty.exception.code, "repository_dirty")


if __name__ == "__main__":
    unittest.main()
