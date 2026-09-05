from __future__ import annotations

import http.client
import json
import os
import shutil
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from controller_api.core import ControllerError, Settings
from controller_api.blueprint_lifecycle import BlueprintLifecycleStore
from controller_api.sandbox_profiles import SandboxProfileStore
from controller_api.server import build_server

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "b" * 64


class BlueprintLifecycleFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "repo/config/examples").mkdir(parents=True)
        (self.root / "repo/config/policies").mkdir(parents=True)
        (self.root / "repo/config/projects.d").mkdir(parents=True)
        (self.root / "state/controller").mkdir(parents=True)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "secrets/controller-session").write_text(TOKEN + "\n", encoding="utf-8")
        os.chmod(self.root / "secrets/controller-session", 0o600)
        shutil.copy2(ROOT / "config/examples/Blueprint", self.root / "repo/config/examples/Blueprint")
        shutil.copy2(ROOT / "config/policies/default.toml", self.root / "repo/config/policies/default.toml")
        shutil.copy2(ROOT / "config/controller.toml", self.root / "repo/config/controller.toml")
        self.database = self.root / "state/controller/orchestra.db"
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                connection.executescript(migration.read_text(encoding="utf-8"))
        self.settings = Settings.from_root(self.root)
        profiles = SandboxProfileStore(self.settings)
        self.store = BlueprintLifecycleStore(self.settings, profiles)
        self.source = (self.root / "repo/config/examples/Blueprint").read_text(encoding="utf-8")

    @staticmethod
    def meta(revision: int | None) -> dict[str, object]:
        return {"request_id": "request-blueprint-test", "resource_revision": revision}

    def changed_source(self) -> str:
        return self.source.replace(
            "description: Pinned Python sandbox with offline runtime networking.",
            "description: Updated declarative sandbox profile.",
        ).replace("cpu: 4", "cpu: 3")

    def renamed_source(self) -> str:
        return self.source.replace("name: python-project", "name: renamed-profile")

    def create(self, *, key: str = "blueprint-create-0001") -> tuple[int, dict[str, object]]:
        return self.store.create(
            session_token=TOKEN,
            idempotency_key=key,
            route="/api/v1/blueprints",
            body={"source": self.source},
            meta_factory=self.meta,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, object] | None = None,
        key: str | None = None,
        csrf: str | None = None,
        if_match: str | None = None,
    ) -> tuple[int, dict[str, str], dict[str, object] | None]:
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
            payload = json.loads(raw) if raw else None
            return response.status, {name.lower(): value for name, value in response.getheaders()}, payload
        finally:
            connection.close()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def close(self) -> None:
        self.temporary.cleanup()


class BlueprintLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = BlueprintLifecycleFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_schema_readiness_and_immutable_audit(self) -> None:
        self.assertEqual(self.fixture.store.readiness(), (True, "ready"))
        with sqlite3.connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 30)
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 30)
        _, payload = self.fixture.create()
        operation_id = payload["data"]["id"]
        with sqlite3.connect(self.fixture.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_blueprint_command_audit SET outcome='FAILED' WHERE operation_id=?",
                    (operation_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM controller_blueprint_command_audit WHERE operation_id=?",
                    (operation_id,),
                )

    def test_preview_is_bounded_and_secret_safe(self) -> None:
        preview = self.fixture.store.preview_source(self.fixture.source.encode())
        self.assertTrue(preview["valid"])
        self.assertEqual(preview["runtime_config"]["profile_name"], "python-project")
        self.assertEqual(preview["runtime_config"]["network"]["mode"], "none")
        secret = ("# password=private-sentinel\n" + self.fixture.source).encode()
        rejected = self.fixture.store.preview_source(secret)
        self.assertFalse(rejected["valid"])
        serialized = json.dumps(rejected, sort_keys=True)
        self.assertNotIn("private-sentinel", serialized)
        self.assertNotIn(self.fixture.source, serialized)

    def test_current_api_rejects_historical_hermesops_api_version(self) -> None:
        historical = self.fixture.source.replace(
            "orchestra.dev/v1", "hermesops.dev/v1"
        )
        report = self.fixture.store.preview_source(historical.encode("utf-8"))
        self.assertFalse(report["valid"])
        self.assertIn(
            "unsupported_api_version",
            {item["code"] for item in report["diagnostics"]},
        )
        with self.assertRaises(ControllerError) as context:
            self.fixture.store.create(
                session_token=TOKEN,
                idempotency_key="historical-api-version-0001",
                route="/api/v1/blueprints",
                body={"source": historical},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(context.exception.code, "blueprint_source_invalid")
        status, _, csrf_payload = self.fixture.request(
            "POST", "/api/v1/auth/csrf", body={}, key="historical-version-csrf"
        )
        self.assertEqual(status, 200)
        csrf = csrf_payload["data"]["token"]
        status, _, validation = self.fixture.request(
            "POST",
            "/api/v1/blueprints/validate",
            body={"source": historical},
            key="historical-version-validate",
            csrf=csrf,
        )
        self.assertEqual(status, 200)
        self.assertFalse(validation["data"]["valid"])
        status, _, rejected = self.fixture.request(
            "POST",
            "/api/v1/blueprints",
            body={"source": historical},
            key="historical-version-create",
            csrf=csrf,
        )
        self.assertEqual(status, 400)
        self.assertEqual(rejected["code"], "blueprint_source_invalid")

    def test_create_is_atomic_idempotent_audited_evented_and_source_safe(self) -> None:
        status, first = self.fixture.create()
        replay_status, replay = self.fixture.create()
        self.assertEqual(status, 202)
        self.assertEqual(replay_status, 202)
        self.assertEqual(first, replay)
        operation = first["data"]
        sandbox_id = operation["result"]["sandbox_id"]
        current = self.fixture.store.current(sandbox_id)
        self.assertEqual(current["profile"]["source_revision"], 1)
        self.assertEqual(current["profile"]["source_format"], "blueprint-v1")
        self.assertEqual(current["revision"]["source_format"], "blueprint-v1")
        self.assertEqual(current["revision"]["source"], self.fixture.source)
        self.assertEqual(current["revision"]["runtime_config"]["profile_name"], "python-project")
        self.assertEqual(self.fixture.store.get_operation(operation["id"])["target"]["id"], sandbox_id)
        with sqlite3.connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sandbox_profiles").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM sandbox_profile_revisions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_blueprint_operations").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_blueprint_idempotency").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_blueprint_command_audit").fetchone()[0], 1)
            event_type, event_data = connection.execute(
                "SELECT event_type, redacted_data_json FROM controller_event_journal"
            ).fetchone()
            self.assertEqual(event_type, "sandbox.created")
            event_payload = json.loads(event_data)
            self.assertNotIn("source", event_payload)
            self.assertNotIn("source_text", event_payload)
            self.assertNotIn("canonical", event_payload)
            sensitive_free = "\n".join(
                str(value)
                for table in (
                    "controller_blueprint_operations",
                    "controller_blueprint_idempotency",
                    "controller_blueprint_command_audit",
                    "controller_event_journal",
                )
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
                for value in row
            )
        self.assertNotIn("Pinned Python sandbox", event_data)
        self.assertNotIn("Pinned Python sandbox", sensitive_free)
        self.assertNotIn(self.fixture.source, sensitive_free)

    def test_idempotency_conflict_and_canonical_equivalent_revision(self) -> None:
        status, created = self.fixture.create(key="blueprint-create-conflict")
        self.assertEqual(status, 202)
        sandbox_id = created["data"]["result"]["sandbox_id"]

        with self.assertRaises(ControllerError) as conflict:
            self.fixture.store.create(
                session_token=TOKEN,
                idempotency_key="blueprint-create-conflict",
                route="/api/v1/blueprints",
                body={"source": self.fixture.changed_source()},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

        equivalent = "# formatting-only revision\n" + self.fixture.source
        status, updated = self.fixture.store.update(
            session_token=TOKEN,
            idempotency_key="blueprint-update-equivalent",
            route=f"/api/v1/blueprints/{sandbox_id}",
            sandbox_id=sandbox_id,
            if_match='"1"',
            body={"source": equivalent},
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        self.assertEqual(updated["meta"]["resource_revision"], 2)
        comparison = self.fixture.store.compare(sandbox_id, 1, 2)
        self.assertFalse(comparison["changed"])
        self.assertEqual(comparison["changes"], [])
        revision_one = self.fixture.store.get_revision(sandbox_id, 1)
        revision_two = self.fixture.store.get_revision(sandbox_id, 2)
        self.assertNotEqual(revision_one["source_sha256"], revision_two["source_sha256"])
        self.assertEqual(revision_one["canonical_sha256"], revision_two["canonical_sha256"])

    def test_update_requires_if_match_preserves_history_and_compares_canonical_paths(self) -> None:
        _, created = self.fixture.create()
        sandbox_id = created["data"]["result"]["sandbox_id"]
        with self.assertRaises(ControllerError) as missing:
            self.fixture.store.update(
                session_token=TOKEN,
                idempotency_key="blueprint-update-missing",
                route=f"/api/v1/blueprints/{sandbox_id}",
                sandbox_id=sandbox_id,
                if_match=None,
                body={"source": self.fixture.changed_source()},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(missing.exception.code, "precondition_required")
        status, updated = self.fixture.store.update(
            session_token=TOKEN,
            idempotency_key="blueprint-update-0001",
            route=f"/api/v1/blueprints/{sandbox_id}",
            sandbox_id=sandbox_id,
            if_match='"1"',
            body={"source": self.fixture.changed_source()},
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        self.assertEqual(updated["meta"]["resource_revision"], 2)
        revisions = self.fixture.store.list_revisions(sandbox_id, limit=50)
        self.assertEqual([item["source_revision"] for item in revisions], [2, 1])
        self.assertEqual(self.fixture.store.get_revision(sandbox_id, 1)["source"], self.fixture.source)
        comparison = self.fixture.store.compare(sandbox_id, 1, 2)
        self.assertTrue(comparison["changed"])
        changed_paths = {item["path"] for item in comparison["changes"]}
        self.assertIn("/metadata/description", changed_paths)
        self.assertIn("/spec/runtime/cpu", changed_paths)
        with self.assertRaises(ControllerError) as stale:
            self.fixture.store.update(
                session_token=TOKEN,
                idempotency_key="blueprint-update-stale",
                route=f"/api/v1/blueprints/{sandbox_id}",
                sandbox_id=sandbox_id,
                if_match='"1"',
                body={"source": self.fixture.source.replace("cpu: 4", "cpu: 2")},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(stale.exception.code, "resource_revision_conflict")

    def test_identity_noop_invalid_and_secret_updates_fail_without_new_revision(self) -> None:
        _, created = self.fixture.create()
        sandbox_id = created["data"]["result"]["sandbox_id"]
        cases = (
            ("identity", self.fixture.renamed_source(), "blueprint_identity_immutable"),
            ("noop", self.fixture.source, "blueprint_unchanged"),
            (
                "secret",
                "# password=private-sentinel\n" + self.fixture.changed_source(),
                "sandbox_source_secret_detected",
            ),
            (
                "invalid",
                self.fixture.source.replace("privileged: false", "privileged: true"),
                "blueprint_source_invalid",
            ),
        )
        for label, source, code in cases:
            with self.subTest(label=label):
                with self.assertRaises(ControllerError) as caught:
                    self.fixture.store.update(
                        session_token=TOKEN,
                        idempotency_key=f"blueprint-update-{label}-01",
                        route=f"/api/v1/blueprints/{sandbox_id}",
                        sandbox_id=sandbox_id,
                        if_match='"1"',
                        body={"source": source},
                        meta_factory=self.fixture.meta,
                    )
                self.assertEqual(caught.exception.code, code)
        self.assertEqual(len(self.fixture.store.list_revisions(sandbox_id, limit=50)), 1)
        with sqlite3.connect(self.fixture.database) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("private-sentinel", dump)
        self.assertNotIn("renamed-profile", dump)

    def test_http_lifecycle_validation_history_diff_and_closed_delete_boundary(self) -> None:
        status, _, csrf_payload = self.fixture.request(
            "POST", "/api/v1/auth/csrf", body={}, key="blueprint-http-csrf"
        )
        self.assertEqual(status, 200)
        csrf = csrf_payload["data"]["token"]
        status, _, validated = self.fixture.request(
            "POST",
            "/api/v1/blueprints/validate",
            body={"source": self.fixture.source},
            key="blueprint-http-validate",
            csrf=csrf,
        )
        self.assertEqual(status, 200)
        self.assertTrue(validated["data"]["valid"])
        status, _, created = self.fixture.request(
            "POST",
            "/api/v1/blueprints",
            body={"source": self.fixture.source},
            key="blueprint-http-create",
            csrf=csrf,
        )
        self.assertEqual(status, 202)
        operation_id = created["data"]["id"]
        sandbox_id = created["data"]["result"]["sandbox_id"]
        status, _, collection = self.fixture.request("GET", "/api/v1/blueprints")
        self.assertEqual(status, 200)
        self.assertEqual(collection["data"][0]["id"], sandbox_id)
        status, headers, current = self.fixture.request("GET", f"/api/v1/blueprints/{sandbox_id}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["etag"], '"1"')
        self.assertEqual(current["data"]["revision"]["source"], self.fixture.source)
        status, _, updated = self.fixture.request(
            "PATCH",
            f"/api/v1/blueprints/{sandbox_id}",
            body={"source": self.fixture.changed_source()},
            key="blueprint-http-update",
            csrf=csrf,
            if_match='"1"',
        )
        self.assertEqual(status, 202)
        self.assertEqual(updated["meta"]["resource_revision"], 2)
        status, _, history = self.fixture.request("GET", f"/api/v1/blueprints/{sandbox_id}/revisions")
        self.assertEqual(status, 200)
        self.assertEqual([item["source_revision"] for item in history["data"]], [2, 1])
        status, _, diff = self.fixture.request("GET", f"/api/v1/blueprints/{sandbox_id}/diff?from=1&to=2")
        self.assertEqual(status, 200)
        self.assertTrue(diff["data"]["changed"])
        status, _, operation = self.fixture.request("GET", f"/api/v1/operations/{operation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(operation["data"]["target"]["id"], sandbox_id)
        status, _, _ = self.fixture.request("DELETE", f"/api/v1/blueprints/{sandbox_id}")
        self.assertEqual(status, 405)

    def test_legacy_hermesfile_routes_are_unavailable(self) -> None:
        for method, path in (
            ("GET", "/api/v1/hermesfiles"),
            ("GET", "/api/v1/hermesfiles/template"),
            ("POST", "/api/v1/hermesfiles"),
            ("POST", "/api/v1/hermesfiles/validate"),
            ("PATCH", "/api/v1/hermesfiles/sandbox-" + "a" * 32),
        ):
            with self.subTest(method=method, path=path):
                status, _, payload = self.fixture.request(method, path)
                self.assertIn(status, {404, 405})
                self.assertIn(payload["code"], {"route_not_found", "method_not_allowed"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
