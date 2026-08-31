from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from controller_api.blueprint import validate_source
from controller_api.blueprint_lifecycle import BlueprintLifecycleStore
from controller_api.sandbox_profiles import SandboxProfileStore


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "migration-session-" + "a" * 48
SANDBOX_ID = "sandbox-" + "b" * 32
REVISION_ID = "sandbox-revision-" + "c" * 32
OPERATION_ID = "operation-" + "d" * 32
SECOND_OPERATION_ID = "operation-" + "f" * 32
AUDIT_ID = "audit-" + "e" * 32
CREATED_AT = "2026-08-30T12:34:56.000Z"
HISTORICAL_ROUTE = "/api/v1/hermesfiles"
IDEMPOTENCY_KEY = "historical-create-0001"


class BlueprintMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.database = self.root / "controller.db"
        self.settings = SimpleNamespace(root=self.root, database=self.database)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def apply_through(self, version: int) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
                if int(migration.name[:3]) > version:
                    break
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + migration.read_text(encoding="utf-8")
                    + "\nCOMMIT;"
                )

    @staticmethod
    def key_hash() -> str:
        return hmac.new(
            TOKEN.encode("ascii"),
            b"hermesops-hermesfile-idempotency-v1\0"
            + IDEMPOTENCY_KEY.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def request_hash(source: str) -> str:
        payload = json.dumps(
            {
                "body": {"source": source},
                "method": "POST",
                "route": HISTORICAL_ROUTE,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def seed_real_v22(self) -> dict[str, object]:
        self.apply_through(22)
        source = (ROOT / "config/examples/Blueprint").read_text(encoding="utf-8")
        report = validate_source(source)
        self.assertTrue(report.valid)
        self.assertIsNotNone(report.result)
        result = report.result
        assert result is not None
        request_hash = self.request_hash(source)
        key_hash = self.key_hash()
        response = {
            "data": {
                "id": OPERATION_ID,
                "kind": "hermesfile.create",
                "state": "succeeded",
                "target": {"type": "sandbox_profile", "id": SANDBOX_ID},
                "result": {"sandbox_id": SANDBOX_ID},
                "created_at": CREATED_AT,
                "updated_at": CREATED_AT,
                "finished_at": CREATED_AT,
            },
            "meta": {"request_id": "request-historical", "resource_revision": 1},
        }
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                INSERT INTO sandbox_profile_revisions (
                    revision_id, sandbox_id, source_revision, source_format,
                    api_version, source_text, source_sha256, canonical_json,
                    canonical_sha256, canonical_size, diagnostics_json, created_at
                ) VALUES (?, ?, 1, 'hermesfile-v1', ?, ?, ?, ?, ?, ?, '[]', ?)
                """,
                (
                    REVISION_ID,
                    SANDBOX_ID,
                    result.api_version,
                    source,
                    result.source_sha256,
                    result.canonical_bytes.decode("utf-8"),
                    result.canonical_sha256,
                    len(result.canonical_bytes),
                    CREATED_AT,
                ),
            )
            connection.execute(
                """
                INSERT INTO sandbox_profiles (
                    sandbox_id, profile_name, display_name, description,
                    labels_json, source_format, state, current_revision_id,
                    current_source_revision, active_image_digest,
                    resource_revision, created_at, updated_at
                ) VALUES (?, 'python-project', 'Python Project Worker',
                    'Historical source', '{}', 'hermesfile-v1', 'draft', ?,
                    1, NULL, 1, ?, ?)
                """,
                (SANDBOX_ID, REVISION_ID, CREATED_AT, CREATED_AT),
            )
            connection.execute(
                """
                INSERT INTO controller_hermesfile_operations (
                    operation_id, command_kind, state, target_id, result_json,
                    error_code, created_at, updated_at, finished_at
                ) VALUES (?, 'hermesfile.create', 'SUCCEEDED', ?, ?,
                    'hermesfile_source_invalid', ?, ?, ?)
                """,
                (
                    OPERATION_ID,
                    SANDBOX_ID,
                    json.dumps({"sandbox_id": SANDBOX_ID}, separators=(",", ":")),
                    CREATED_AT,
                    CREATED_AT,
                    CREATED_AT,
                ),
            )
            connection.execute(
                """
                INSERT INTO controller_hermesfile_operations (
                    operation_id, command_kind, state, target_id, result_json,
                    error_code, created_at, updated_at, finished_at
                ) VALUES (?, 'hermesfile.update', 'FAILED', ?, '{}',
                    'upstream_hermesfile_bridge_failed', ?, ?, ?)
                """,
                (
                    SECOND_OPERATION_ID,
                    SANDBOX_ID,
                    CREATED_AT,
                    CREATED_AT,
                    CREATED_AT,
                ),
            )
            connection.execute(
                """
                INSERT INTO controller_hermesfile_idempotency (
                    session_fingerprint, key_hash, method, route, request_hash,
                    response_status, response_json, operation_id,
                    created_at, completed_at
                ) VALUES (?, ?, 'POST', ?, ?, 202, ?, ?, ?, ?)
                """,
                (
                    hashlib.sha256(TOKEN.encode("ascii")).hexdigest()[:32],
                    key_hash,
                    HISTORICAL_ROUTE,
                    request_hash,
                    json.dumps(response, sort_keys=True, separators=(",", ":")),
                    OPERATION_ID,
                    CREATED_AT,
                    CREATED_AT,
                ),
            )
            connection.execute(
                """
                INSERT INTO controller_hermesfile_command_audit (
                    audit_id, operation_id, actor_type, actor_id, action,
                    resource_type, resource_id, session_fingerprint,
                    idempotency_key_hash, request_hash, outcome, created_at
                ) VALUES (?, ?, 'session', 'historical-actor',
                    'hermesfile.create', 'sandbox_profile', ?, ?, ?, ?,
                    'SUCCEEDED', ?)
                """,
                (
                    AUDIT_ID,
                    OPERATION_ID,
                    SANDBOX_ID,
                    hashlib.sha256(TOKEN.encode("ascii")).hexdigest()[:32],
                    key_hash,
                    request_hash,
                    CREATED_AT,
                ),
            )
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, repo_path, data_path, policy_id,
                    enabled, config_source, config_hash, registered_at,
                    updated_at, default_branch, sandbox_profile_id, archived,
                    repository_mode, resource_revision
                ) VALUES (
                    'alpha', 'Alpha', '/srv/alpha', '/var/lib/alpha', 'default',
                    1, 'controller', ?, ?, ?, 'main', 'python-project', 0,
                    'existing', 1
                )
                """,
                ("f" * 64, CREATED_AT, CREATED_AT),
            )
            connection.execute(
                """
                INSERT INTO objective_queue (
                    objective_id, objective, source, status, priority,
                    not_before, project_scope_json, max_parallel_tasks,
                    planning_max_attempts, planning_attempt_count, created_at,
                    heartbeat_at
                ) VALUES (
                    'objective-migration', 'Preserve Blueprint linkage',
                    'TEST', 'QUEUED', 100, ?, '["alpha"]', 1, 3, 0, ?, ?
                )
                """,
                (CREATED_AT, CREATED_AT, CREATED_AT),
            )
            connection.commit()
        return {
            "source": source,
            "source_sha256": result.source_sha256,
            "canonical": result.canonical_bytes.decode("utf-8"),
            "canonical_sha256": result.canonical_sha256,
            "request_hash": request_hash,
            "key_hash": key_hash,
        }

    def apply_v23(self) -> None:
        migration = (ROOT / "migrations/023_blueprint_migration.sql").read_text(
            encoding="utf-8"
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript("BEGIN IMMEDIATE;\n" + migration + "\nCOMMIT;")

    def assert_v23_rejected_atomically(self) -> None:
        with self.assertRaises(sqlite3.Error):
            self.apply_v23()
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 22)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            self.assertIn("sandbox_profiles", tables)
            self.assertIn("controller_hermesfile_operations", tables)
            self.assertNotIn("controller_blueprint_operations", tables)
            self.assertEqual(
                connection.execute(
                    "SELECT route FROM controller_hermesfile_idempotency"
                ).fetchone()[0],
                HISTORICAL_ROUTE,
            )

    def test_fresh_database_reaches_schema_23(self) -> None:
        self.apply_through(23)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
            self.assertEqual(
                connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0],
                23,
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_real_v22_upgrade_preserves_integrity_and_reopens(self) -> None:
        expected = self.seed_real_v22()
        self.apply_v23()
        with sqlite3.connect(self.database) as connection:
            connection.row_factory = sqlite3.Row
            revision = connection.execute(
                "SELECT * FROM sandbox_profile_revisions WHERE revision_id=?",
                (REVISION_ID,),
            ).fetchone()
            profile = connection.execute(
                "SELECT * FROM sandbox_profiles WHERE sandbox_id=?", (SANDBOX_ID,)
            ).fetchone()
            idem = connection.execute(
                "SELECT * FROM controller_blueprint_idempotency WHERE operation_id=?",
                (OPERATION_ID,),
            ).fetchone()
            operation = connection.execute(
                "SELECT * FROM controller_blueprint_operations WHERE operation_id=?",
                (OPERATION_ID,),
            ).fetchone()
            audit = connection.execute(
                "SELECT * FROM controller_blueprint_command_audit WHERE operation_id=?",
                (OPERATION_ID,),
            ).fetchone()
            assert revision is not None and profile is not None and idem is not None
            assert operation is not None and audit is not None
            second_operation = connection.execute(
                "SELECT * FROM controller_blueprint_operations WHERE operation_id=?",
                (SECOND_OPERATION_ID,),
            ).fetchone()
            assert second_operation is not None
            self.assertEqual(revision["source_format"], "blueprint-v1")
            self.assertEqual(profile["source_format"], "blueprint-v1")
            self.assertEqual(revision["source_text"], expected["source"])
            self.assertEqual(revision["source_sha256"], expected["source_sha256"])
            self.assertEqual(revision["canonical_json"], expected["canonical"])
            self.assertEqual(revision["canonical_sha256"], expected["canonical_sha256"])
            self.assertEqual(revision["revision_id"], REVISION_ID)
            self.assertEqual(revision["sandbox_id"], SANDBOX_ID)
            self.assertEqual(revision["source_revision"], 1)
            self.assertEqual(revision["created_at"], CREATED_AT)
            self.assertEqual(profile["current_revision_id"], REVISION_ID)
            self.assertEqual(profile["created_at"], CREATED_AT)
            self.assertEqual(profile["updated_at"], CREATED_AT)
            project_link = connection.execute(
                "SELECT sandbox_profile_id FROM projects WHERE project_id='alpha'"
            ).fetchone()[0]
            objective_link = connection.execute(
                "SELECT project_scope_json FROM objective_queue "
                "WHERE objective_id='objective-migration'"
            ).fetchone()[0]
            self.assertEqual(project_link, "python-project")
            self.assertEqual(objective_link, '["alpha"]')
            self.assertEqual(idem["route"], HISTORICAL_ROUTE)
            self.assertEqual(idem["request_hash"], expected["request_hash"])
            self.assertEqual(idem["key_hash"], expected["key_hash"])
            self.assertEqual(operation["command_kind"], "blueprint.create")
            self.assertEqual(operation["error_code"], "blueprint_source_invalid")
            self.assertEqual(
                second_operation["error_code"],
                "upstream_hermesfile_bridge_failed",
            )
            self.assertEqual(audit["action"], "blueprint.create")
            self.assertEqual(
                json.loads(idem["response_json"])["data"]["kind"],
                "blueprint.create",
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            schema_objects = {
                (row[0], row[1])
                for row in connection.execute(
                    "SELECT type, name FROM sqlite_master "
                    "WHERE type IN ('index', 'trigger') AND sql IS NOT NULL"
                )
            }
            for expected_object in {
                ("index", "idx_sandbox_profiles_state_name"),
                ("index", "idx_sandbox_profile_revisions_profile"),
                ("index", "idx_controller_blueprint_operations_target"),
                ("index", "idx_controller_blueprint_audit_resource"),
                ("trigger", "sandbox_profile_revision_update_guard"),
                ("trigger", "sandbox_profile_revision_delete_guard"),
                ("trigger", "sandbox_profile_identity_guard"),
                ("trigger", "sandbox_profile_resource_revision_guard"),
                ("trigger", "sandbox_profile_source_revision_guard"),
                ("trigger", "controller_blueprint_audit_update_guard"),
                ("trigger", "controller_blueprint_audit_delete_guard"),
                ("trigger", "controller_blueprint_idempotency_delete_guard"),
            }:
                self.assertIn(expected_object, schema_objects)

            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "UPDATE sandbox_profile_revisions SET created_at=created_at "
                    "WHERE revision_id=?",
                    (REVISION_ID,),
                )
            with self.assertRaisesRegex(sqlite3.IntegrityError, "immutable"):
                connection.execute(
                    "DELETE FROM controller_blueprint_command_audit WHERE audit_id=?",
                    (AUDIT_ID,),
                )

        store = BlueprintLifecycleStore(
            self.settings, SandboxProfileStore(self.settings)
        )
        self.assertEqual(store.readiness(), (True, "ready"))
        self.assertEqual(store.get_operation(OPERATION_ID)["kind"], "blueprint.create")
        current = store.current(SANDBOX_ID)
        self.assertEqual(current["revision"]["source_format"], "blueprint-v1")
        reopened = BlueprintLifecycleStore(
            self.settings, SandboxProfileStore(self.settings)
        )
        self.assertEqual(reopened.get_operation(OPERATION_ID)["id"], OPERATION_ID)

    def test_migration_23_rerun_fails_without_changing_database(self) -> None:
        expected = self.seed_real_v22()
        self.apply_v23()
        before = self.database.read_bytes()
        migration = (ROOT / "migrations/023_blueprint_migration.sql").read_text(
            encoding="utf-8"
        )
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            with self.assertRaises(sqlite3.Error):
                connection.executescript("BEGIN IMMEDIATE;\n" + migration + "\nCOMMIT;")
            if connection.in_transaction:
                connection.rollback()
        self.assertEqual(self.database.read_bytes(), before)
        with sqlite3.connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 23)
            self.assertEqual(
                connection.execute(
                    "SELECT source_sha256 FROM sandbox_profile_revisions"
                ).fetchone()[0],
                expected["source_sha256"],
            )

    def test_unexpected_idempotency_kind_fails_atomically(self) -> None:
        self.seed_real_v22()
        with sqlite3.connect(self.database) as connection:
            response = json.loads(
                connection.execute(
                    "SELECT response_json FROM controller_hermesfile_idempotency"
                ).fetchone()[0]
            )
            response["data"]["kind"] = "hermesfile.delete"
            connection.execute(
                "UPDATE controller_hermesfile_idempotency SET response_json=?",
                (json.dumps(response, separators=(",", ":")),),
            )
        self.assert_v23_rejected_atomically()

    def test_unexpected_legacy_operation_kind_fails_atomically(self) -> None:
        self.seed_real_v22()
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE controller_hermesfile_operations "
                "SET command_kind='hermesfile.delete' WHERE operation_id=?",
                (OPERATION_ID,),
            )
        self.assert_v23_rejected_atomically()

    def test_malformed_persisted_json_fails_atomically(self) -> None:
        self.seed_real_v22()
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE controller_hermesfile_idempotency SET response_json='{'"
            )
        self.assert_v23_rejected_atomically()

    def test_unexpected_legacy_source_format_fails_atomically(self) -> None:
        self.seed_real_v22()
        with sqlite3.connect(self.database) as connection:
            trigger_sql = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='sandbox_profile_revision_update_guard'"
            ).fetchone()[0]
            connection.execute("DROP TRIGGER sandbox_profile_revision_update_guard")
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(
                "UPDATE sandbox_profile_revisions SET source_format='unknown-v1'"
            )
            connection.execute(trigger_sql)
        self.assert_v23_rejected_atomically()

    def test_preexisting_foreign_key_violation_fails_atomically(self) -> None:
        self.seed_real_v22()
        with sqlite3.connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute(
                "UPDATE controller_hermesfile_idempotency SET operation_id=?",
                ("operation-" + "0" * 32,),
            )
        self.assert_v23_rejected_atomically()


if __name__ == "__main__":
    unittest.main()
