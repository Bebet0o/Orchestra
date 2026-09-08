from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from controller_api.core import ControllerError, Settings
from controller_api.event_journal import EventJournal, canonical_json
from controller_api.memory_commands import MemoryCommandStore
from controller_api.project_memory import ProjectMemoryStore
from controller_api.server import build_server
from controller_api.websocket_transport import aggregates_for_topics
from tests.sqlite_test_support import sqlite_connect

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "m" * 64
NOW = "2026-09-08T08:00:00.000Z"


class ControllerMemoryFixture:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "state/controller").mkdir(parents=True)
        (self.root / "secrets").mkdir(parents=True)
        (self.root / "secrets/controller-session").write_text(TOKEN + "\n", encoding="ascii")
        os.chmod(self.root / "secrets/controller-session", 0o600)
        self.database = self.root / "state/controller/orchestra.db"
        self.migrate(self.database)
        self.insert_project(self.database, "alpha")
        self.settings = Settings.from_root(self.root, port=0)
        self.memory = ProjectMemoryStore(self.settings)
        self.store = MemoryCommandStore(self.settings, self.memory)

    @staticmethod
    def migrations() -> list[Path]:
        return sorted((ROOT / "migrations").glob("[0-9][0-9][0-9]_*.sql"))

    @classmethod
    def migrate(cls, database: Path, *, through: int | None = None) -> None:
        with sqlite_connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for migration in cls.migrations():
                version = int(migration.name.split("_", 1)[0])
                if through is not None and version > through:
                    break
                connection.executescript(migration.read_text(encoding="utf-8"))

    @staticmethod
    def insert_project(database: Path, project_id: str) -> None:
        with sqlite_connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                INSERT INTO projects (
                    project_id, display_name, repo_path, data_path, policy_id,
                    enabled, config_source, config_hash, registered_at, updated_at
                ) VALUES (?, ?, ?, ?, 'default', 1, 'test', ?, ?, ?)
                """,
                (
                    project_id,
                    project_id.title(),
                    f"/srv/{project_id}/repo",
                    f"/srv/{project_id}/data",
                    project_id + "-config-hash",
                    NOW,
                    NOW,
                ),
            )

    @staticmethod
    def meta(revision: int | None) -> dict[str, object]:
        return {"request_id": "request-memory-test", "resource_revision": revision}

    def create_body(self, **overrides: object) -> dict[str, object]:
        body: dict[str, object] = {
            "scope": "PROJECT",
            "kind": "FACT",
            "memory_key": "runtime.python",
            "title": "Python runtime",
            "content": "Python 3.13 is the supported runtime.",
        }
        body.update(overrides)
        return body

    def create(self, *, key: str = "memory-create-0001", **overrides: object):
        return self.store.create_memory(
            session_token=TOKEN,
            idempotency_key=key,
            route="/api/v1/projects/alpha/memories",
            project_id="alpha",
            body=self.create_body(**overrides),
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
            encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
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

    def csrf(self) -> str:
        status, _, payload = self.request(
            "POST",
            "/api/v1/auth/csrf",
            body={},
            key="memory-csrf-0001",
        )
        assert status == 200 and payload is not None
        return str(payload["data"]["token"])

    def close(self) -> None:
        self.temporary.cleanup()


class ControllerMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = ControllerMemoryFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def test_schema_readiness_and_memory_event_topic(self) -> None:
        self.assertEqual(self.fixture.store.readiness(), (True, "ready"))
        self.assertEqual(aggregates_for_topics(("memories",)), frozenset({"memory"}))
        with sqlite_connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 33)
            self.assertEqual(connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0], 33)
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_migration_33_preserves_event_sequences_and_hardening(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "events.db"
            self.fixture.migrate(database, through=32)
            self.fixture.insert_project(database, "alpha")
            with sqlite_connect(database) as connection:
                connection.execute("BEGIN IMMEDIATE")
                EventJournal.emit(
                    connection,
                    event_type="project.updated",
                    actor_type="operator",
                    actor_id="operator",
                    aggregate_type="project",
                    aggregate_id="alpha",
                    correlation_id="corr_" + "a" * 32,
                    project_id="alpha",
                    data={"state": "enabled"},
                    event_id="evt_" + "b" * 32,
                    occurred_at=NOW,
                )
                connection.commit()
            migration = next(path for path in self.fixture.migrations() if path.name.startswith("033_"))
            with sqlite_connect(database) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(migration.read_text(encoding="utf-8"))
                row = connection.execute(
                    "SELECT sequence,event_id,aggregate_type FROM controller_event_journal"
                ).fetchone()
                self.assertEqual(row, (1, "evt_" + "b" * 32, "project"))
                connection.execute("BEGIN IMMEDIATE")
                event = EventJournal.emit(
                    connection,
                    event_type="memory.created",
                    actor_type="operator",
                    actor_id="operator",
                    aggregate_type="memory",
                    aggregate_id="memory-test",
                    correlation_id="corr_" + "c" * 32,
                    project_id="alpha",
                    data={"state": "ACTIVE"},
                    occurred_at=NOW,
                )
                connection.commit()
                self.assertEqual(event["sequence"], 2)
                with self.assertRaises(sqlite3.IntegrityError):
                    connection.execute(
                        "UPDATE controller_event_journal SET event_type='memory.revised' WHERE sequence=1"
                    )

    def test_create_replay_exact_dedup_audit_and_event_are_safe(self) -> None:
        sentinel = "Python 3.13 is the supported runtime."
        status, first = self.fixture.create()
        replay_status, replay = self.fixture.create()
        self.assertEqual((status, replay_status), (202, 202))
        self.assertEqual(first, replay)
        operation = first["data"]
        memory_id = operation["result"]["memory_id"]
        self.assertFalse(operation["result"]["deduplicated"])
        self.assertEqual(operation["result"]["resource_revision"], 1)

        status, second = self.fixture.create(key="memory-create-0002")
        self.assertEqual(status, 202)
        self.assertEqual(second["data"]["result"]["memory_id"], memory_id)
        self.assertTrue(second["data"]["result"]["deduplicated"])
        self.assertEqual(second["data"]["result"]["resource_revision"], 2)

        with sqlite_connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_memories").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_memory_revisions").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_memory_operations").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_memory_idempotency").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_memory_command_audit").fetchone()[0], 2)
            event_types = [row[0] for row in connection.execute(
                "SELECT event_type FROM controller_event_journal ORDER BY sequence"
            )]
            self.assertEqual(event_types, ["memory.created", "memory.attested"])
            side_tables = "\n".join(
                str(value)
                for table in (
                    "controller_memory_operations",
                    "controller_memory_idempotency",
                    "controller_memory_command_audit",
                    "controller_event_journal",
                )
                for row in connection.execute(f"SELECT * FROM {table}").fetchall()
                for value in row
            )
        self.assertNotIn(sentinel, side_tables)

    def test_public_commands_derive_operator_authority_and_reject_secret(self) -> None:
        with self.assertRaises(ControllerError) as context:
            self.fixture.create(authority="AGENT_PROPOSED")
        self.assertEqual(context.exception.code, "invalid_memory_create")

        safe_status, safe = self.fixture.create(
            key="memory-template-safe",
            content="Configuration may reference ${HOME}/.config/orchestra.",
        )
        self.assertEqual(safe_status, 202)
        memory_id = safe["data"]["result"]["memory_id"]
        current = self.fixture.store.get_memory(memory_id)
        self.assertEqual(current["authority"], "OPERATOR_DECLARED")
        self.assertIn("${HOME}", current["content"])

        secret = "password=memory-api-private-sentinel"
        with self.assertRaises(ControllerError) as context:
            self.fixture.create(key="memory-secret-rejected", content=secret)
        self.assertEqual(context.exception.code, "memory_secret_detected")
        with sqlite_connect(self.fixture.database) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(secret, dump)

    def test_revision_requires_if_match_preserves_history_and_stale_loses(self) -> None:
        _, created = self.fixture.create()
        memory_id = created["data"]["result"]["memory_id"]
        body = {
            "kind": "CONSTRAINT",
            "memory_key": "runtime.python",
            "title": "Python runtime",
            "content": "Python 3.13 or newer is required.",
        }
        with self.assertRaises(ControllerError) as missing:
            self.fixture.store.revise_memory(
                session_token=TOKEN,
                idempotency_key="memory-revise-missing",
                route=f"/api/v1/memories/{memory_id}",
                memory_id=memory_id,
                if_match=None,
                body=body,
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(missing.exception.code, "precondition_required")

        status, revised = self.fixture.store.revise_memory(
            session_token=TOKEN,
            idempotency_key="memory-revise-0001",
            route=f"/api/v1/memories/{memory_id}",
            memory_id=memory_id,
            if_match='"1"',
            body=body,
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        self.assertEqual(revised["data"]["result"]["revision"], 2)
        revisions = self.fixture.store.list_revisions(memory_id, limit=50)
        self.assertEqual([item["revision"] for item in revisions], [2, 1])
        old = self.fixture.store.get_revision(memory_id, 1)
        self.assertEqual(old["content"], "Python 3.13 is the supported runtime.")
        with self.assertRaises(ControllerError) as stale:
            self.fixture.store.revise_memory(
                session_token=TOKEN,
                idempotency_key="memory-revise-stale",
                route=f"/api/v1/memories/{memory_id}",
                memory_id=memory_id,
                if_match='"1"',
                body={**body, "content": "stale writer"},
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(stale.exception.code, "resource_revision_conflict")

    def test_retract_then_security_redact_scrubs_all_revisions(self) -> None:
        _, created = self.fixture.create(content="First operator value.")
        memory_id = created["data"]["result"]["memory_id"]
        self.fixture.store.revise_memory(
            session_token=TOKEN,
            idempotency_key="memory-redact-revise",
            route=f"/api/v1/memories/{memory_id}",
            memory_id=memory_id,
            if_match='"1"',
            body={
                "kind": "FACT",
                "memory_key": "runtime.python",
                "title": "Python runtime",
                "content": "Second operator value.",
            },
            meta_factory=self.fixture.meta,
        )
        status, retracted = self.fixture.store.command_memory(
            session_token=TOKEN,
            idempotency_key="memory-retract-0001",
            route=f"/api/v1/memories/{memory_id}/commands/retract",
            memory_id=memory_id,
            command="retract",
            if_match='"2"',
            body={"reason": "Superseded by a newer project decision."},
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        self.assertEqual(retracted["data"]["result"]["state"], "RETRACTED")
        status, redacted = self.fixture.store.command_memory(
            session_token=TOKEN,
            idempotency_key="memory-redact-0001",
            route=f"/api/v1/memories/{memory_id}/commands/redact",
            memory_id=memory_id,
            command="redact",
            if_match='"3"',
            body={},
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        self.assertEqual(redacted["data"]["result"]["state"], "REDACTED")
        for revision in (1, 2):
            historical = self.fixture.store.get_revision(memory_id, revision)
            self.assertIsNone(historical["content"])
            self.assertIsNone(historical["memory_key"])
            self.assertIsNone(historical["title"])
        with sqlite_connect(self.fixture.database) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM project_memory_payloads WHERE state='AVAILABLE'"
                ).fetchone()[0],
                0,
            )

    def test_cross_project_link_rejected_atomically(self) -> None:
        self.fixture.insert_project(self.fixture.database, "beta")
        with sqlite_connect(self.fixture.database) as connection:
            connection.execute(
                """
                INSERT INTO runs (run_id, project_id, status, metadata_json, created_at)
                VALUES ('run-beta', 'beta', 'QUEUED', '{}', ?)
                """,
                (NOW,),
            )
        with self.assertRaises(ControllerError) as context:
            self.fixture.create(
                key="memory-cross-project",
                links=[{"kind": "RUN", "id": "run-beta"}],
            )
        self.assertEqual(context.exception.code, "invalid_memory_link")
        with sqlite_connect(self.fixture.database) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM project_memories").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_memory_operations").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_memory_idempotency").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM controller_memory_command_audit").fetchone()[0], 0)

    def test_list_is_bounded_content_redacted_and_cursor_is_bound(self) -> None:
        first = self.fixture.create(key="memory-list-one", memory_key="one", content="First list value.")[1]
        second = self.fixture.create(key="memory-list-two", memory_key="two", content="Second list value.")[1]
        first_id = first["data"]["result"]["memory_id"]
        second_id = second["data"]["result"]["memory_id"]
        items, cursor = self.fixture.store.list_memories(
            project_id="alpha",
            limit=1,
            cursor=None,
            state="ACTIVE",
            kind=None,
            scope=None,
            objective_id=None,
            cursor_secret=TOKEN,
        )
        self.assertEqual(len(items), 1)
        self.assertNotIn("content", items[0])
        self.assertIsNotNone(cursor)
        remaining, final_cursor = self.fixture.store.list_memories(
            project_id="alpha",
            limit=1,
            cursor=cursor,
            state="ACTIVE",
            kind=None,
            scope=None,
            objective_id=None,
            cursor_secret=TOKEN,
        )
        self.assertEqual({items[0]["id"], remaining[0]["id"]}, {first_id, second_id})
        self.assertIsNone(final_cursor)
        with self.assertRaises(ControllerError) as tampered:
            self.fixture.store.list_memories(
                project_id="alpha",
                limit=1,
                cursor=cursor[:-1] + ("A" if cursor[-1] != "A" else "B"),
                state="ACTIVE",
                kind=None,
                scope=None,
                objective_id=None,
                cursor_secret=TOKEN,
            )
        self.assertEqual(tampered.exception.code, "invalid_cursor")
        with self.assertRaises(ControllerError) as rebound:
            self.fixture.store.list_memories(
                project_id="alpha",
                limit=1,
                cursor=cursor,
                state=None,
                kind=None,
                scope=None,
                objective_id=None,
                cursor_secret=TOKEN,
            )
        self.assertEqual(rebound.exception.code, "invalid_cursor")

    def test_http_lifecycle_and_unified_operation_projection(self) -> None:
        csrf = self.fixture.csrf()
        status, _, created = self.fixture.request(
            "POST",
            "/api/v1/projects/alpha/memories",
            body=self.fixture.create_body(),
            key="memory-http-create",
            csrf=csrf,
        )
        self.assertEqual(status, 202)
        operation_id = created["data"]["id"]
        memory_id = created["data"]["result"]["memory_id"]

        status, _, collection = self.fixture.request(
            "GET", "/api/v1/projects/alpha/memories?limit=50&state=ACTIVE"
        )
        self.assertEqual(status, 200)
        self.assertEqual(collection["data"][0]["id"], memory_id)
        self.assertNotIn("content", collection["data"][0])

        status, headers, current = self.fixture.request("GET", f"/api/v1/memories/{memory_id}")
        self.assertEqual(status, 200)
        self.assertEqual(headers["etag"], '"1"')
        self.assertEqual(current["data"]["content"], "Python 3.13 is the supported runtime.")
        self.assertEqual(current["data"]["provenance"], [{"kind": "OPERATOR", "id": "operator"}])

        status, _, revised = self.fixture.request(
            "PATCH",
            f"/api/v1/memories/{memory_id}",
            body={
                "kind": "CONSTRAINT",
                "memory_key": "runtime.python",
                "title": "Python runtime",
                "content": "Python 3.13 or newer is required.",
            },
            key="memory-http-revise",
            csrf=csrf,
            if_match='"1"',
        )
        self.assertEqual(status, 202)
        self.assertEqual(revised["data"]["result"]["revision"], 2)

        status, _, history = self.fixture.request(
            "GET", f"/api/v1/memories/{memory_id}/revisions?limit=50"
        )
        self.assertEqual(status, 200)
        self.assertEqual([item["revision"] for item in history["data"]], [2, 1])
        status, _, old = self.fixture.request(
            "GET", f"/api/v1/memories/{memory_id}/revisions/1"
        )
        self.assertEqual(status, 200)
        self.assertEqual(old["data"]["content"], "Python 3.13 is the supported runtime.")

        status, _, operation = self.fixture.request("GET", f"/api/v1/operations/{operation_id}")
        self.assertEqual(status, 200)
        self.assertEqual(operation["data"]["target"], {"type": "memory", "id": memory_id})

        status, _, retracted = self.fixture.request(
            "POST",
            f"/api/v1/memories/{memory_id}/commands/retract",
            body={},
            key="memory-http-retract",
            csrf=csrf,
            if_match='"2"',
        )
        self.assertEqual(status, 202)
        self.assertEqual(retracted["data"]["result"]["state"], "RETRACTED")

        status, _, unavailable = self.fixture.request(
            "POST",
            f"/api/v1/memories/{memory_id}/commands/reactivate",
            body={},
            key="memory-http-reactivate",
            csrf=csrf,
            if_match='"3"',
        )
        self.assertEqual(status, 409)
        self.assertEqual(unavailable["code"], "memory_command_unavailable")
        status, _, deleted = self.fixture.request("DELETE", f"/api/v1/memories/{memory_id}")
        self.assertEqual(status, 405)
        self.assertEqual(deleted["code"], "method_not_allowed")

    def test_memory_audit_and_completed_idempotency_are_immutable(self) -> None:
        _, created = self.fixture.create()
        operation_id = created["data"]["id"]
        with sqlite_connect(self.fixture.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_memory_operations SET result_json='{}' WHERE operation_id=?",
                    (operation_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_memory_command_audit SET outcome='FAILED' WHERE operation_id=?",
                    (operation_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute("DELETE FROM controller_memory_command_audit WHERE operation_id=?", (operation_id,))
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_memory_idempotency SET completed_at=? WHERE operation_id=?",
                    ("2026-09-08T09:00:00.000Z", operation_id),
                )


    def test_corrupt_persisted_provenance_fails_closed_without_echo(self) -> None:
        _, created = self.fixture.create()
        memory_id = created["data"]["result"]["memory_id"]
        sentinel = "password=corrupt-provenance-sentinel"
        with sqlite_connect(self.fixture.database) as connection:
            connection.execute("DROP TRIGGER project_memory_provenance_update_guard")
            connection.execute(
                """
                UPDATE project_memory_provenance
                SET source_actor_id=?
                WHERE revision_id=(
                    SELECT current_revision_id FROM project_memories WHERE memory_id=?
                )
                """,
                (sentinel, memory_id),
            )
        with self.assertRaises(ControllerError) as context:
            self.fixture.store.get_memory(memory_id)
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(context.exception.code, "project_memory_projection_failed")
        rendered = json.dumps(
            {"title": context.exception.title, "detail": context.exception.detail}
        )
        self.assertNotIn(sentinel, rendered)

    def test_corrupt_idempotency_replay_fails_closed_without_echo(self) -> None:
        _, created = self.fixture.create()
        operation_id = created["data"]["id"]
        sentinel = "password=corrupt-idempotency-sentinel"
        with sqlite_connect(self.fixture.database) as connection:
            connection.execute("DROP TRIGGER controller_memory_idempotency_update_guard")
            row = connection.execute(
                "SELECT session_fingerprint,key_hash,response_json FROM controller_memory_idempotency WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            corrupted = json.loads(row[2])
            corrupted["leak"] = sentinel
            connection.execute(
                "UPDATE controller_memory_idempotency SET response_json=? WHERE session_fingerprint=? AND key_hash=?",
                (json.dumps(corrupted), row[0], row[1]),
            )
        with self.assertRaises(ControllerError) as context:
            self.fixture.create()
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(context.exception.code, "idempotency_projection_invalid")
        rendered = json.dumps(
            {"title": context.exception.title, "detail": context.exception.detail}
        )
        self.assertNotIn(sentinel, rendered)

    def test_pathological_legacy_memory_id_fails_closed_in_collection(self) -> None:
        legacy_id = "legacy-memory-record:" + "x" * 220
        with sqlite_connect(self.fixture.database) as connection:
            connection.execute(
                """
                INSERT INTO project_memories (
                    memory_id, project_id, scope, objective_id, state,
                    current_revision_id, current_revision_number, resource_revision,
                    created_at, updated_at
                ) VALUES (?, 'alpha', 'PROJECT', NULL, 'ACTIVE',
                    'legacy-long-revision', 1, 1, ?, ?)
                """,
                (legacy_id, NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO project_memory_payloads (
                    payload_id, kind, memory_key, title, content, state,
                    redacted_at, redaction_code
                ) VALUES ('legacy-long-payload', 'NOTE', NULL, NULL,
                    'legacy content', 'AVAILABLE', NULL, NULL)
                """
            )
            connection.execute(
                """
                INSERT INTO project_memory_revisions (
                    revision_id, memory_id, revision_number, payload_id,
                    payload_sha256, authority, authority_review_id,
                    authority_decision_id, supersedes_revision_id,
                    created_by_actor_type, created_by_actor_id, created_at
                ) VALUES ('legacy-long-revision', ?, 1, 'legacy-long-payload', NULL,
                    'LEGACY_UNVERIFIED', NULL, NULL, NULL,
                    'MIGRATION', 'migration-test', ?)
                """,
                (legacy_id, NOW),
            )
        with self.assertRaises(ControllerError) as context:
            self.fixture.store.list_memories(
                project_id="alpha",
                limit=50,
                cursor=None,
                state=None,
                kind=None,
                scope=None,
                objective_id=None,
                cursor_secret=TOKEN,
            )
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(context.exception.code, "project_memory_projection_failed")
        self.assertNotIn(legacy_id, context.exception.detail)

    def test_reused_key_conflicts_before_semantic_validation(self) -> None:
        self.fixture.create(key="memory-conflict-before-validation")
        with self.assertRaises(ControllerError) as context:
            self.fixture.create(
                key="memory-conflict-before-validation",
                authority="AGENT_PROPOSED",
            )
        self.assertEqual(context.exception.status, 409)
        self.assertEqual(context.exception.code, "idempotency_conflict")

    def test_request_hash_is_keyed_and_binds_if_match(self) -> None:
        _, created = self.fixture.create(key="memory-keyed-hash-create")
        memory_id = created["data"]["result"]["memory_id"]
        route = f"/api/v1/memories/{memory_id}/commands/retract"
        body = {"reason": "private operator explanation"}
        status, result = self.fixture.store.command_memory(
            session_token=TOKEN,
            idempotency_key="memory-keyed-hash-command",
            route=route,
            memory_id=memory_id,
            command="retract",
            if_match='"1"',
            body=body,
            meta_factory=self.fixture.meta,
        )
        self.assertEqual(status, 202)
        operation_id = result["data"]["id"]
        with sqlite_connect(self.fixture.database) as connection:
            stored = connection.execute(
                "SELECT request_hash FROM controller_memory_idempotency WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0]
        encoded = canonical_json(
            {
                "method": "POST",
                "route": route,
                "if_match": '"1"',
                "body": body,
            }
        ).encode("utf-8")
        plain = hashlib.sha256(encoded).hexdigest()
        expected = hmac.new(
            TOKEN.encode("ascii"),
            b"orchestra-memory-request-v1\0" + encoded,
            hashlib.sha256,
        ).hexdigest()
        self.assertEqual(stored, expected)
        self.assertNotEqual(stored, plain)

        with self.assertRaises(ControllerError) as conflict:
            self.fixture.store.command_memory(
                session_token=TOKEN,
                idempotency_key="memory-keyed-hash-command",
                route=route,
                memory_id=memory_id,
                command="retract",
                if_match='"2"',
                body=body,
                meta_factory=self.fixture.meta,
            )
        self.assertEqual(conflict.exception.status, 409)
        self.assertEqual(conflict.exception.code, "idempotency_conflict")

    def test_corrupt_idempotency_meta_revision_fails_closed(self) -> None:
        _, created = self.fixture.create()
        operation_id = created["data"]["id"]
        with sqlite_connect(self.fixture.database) as connection:
            connection.execute("DROP TRIGGER controller_memory_idempotency_update_guard")
            row = connection.execute(
                "SELECT session_fingerprint,key_hash,response_json FROM controller_memory_idempotency WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            corrupted = json.loads(row[2])
            corrupted["meta"]["resource_revision"] += 1
            connection.execute(
                "UPDATE controller_memory_idempotency SET response_json=? WHERE session_fingerprint=? AND key_hash=?",
                (json.dumps(corrupted), row[0], row[1]),
            )
        with self.assertRaises(ControllerError) as context:
            self.fixture.create()
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(context.exception.code, "idempotency_projection_invalid")

    def test_openapi_memory_response_codes_match_runtime(self) -> None:
        spec = json.loads(
            (ROOT / "specs/controller-api-v1.openapi.json").read_text(encoding="utf-8")
        )
        expected = {
            ("/projects/{project_id}/memories", "get"): {"200", "400", "401", "403", "404", "503"},
            ("/projects/{project_id}/memories", "post"): {"202", "400", "401", "403", "404", "409", "503"},
            ("/memories/{memory_id}", "get"): {"200", "400", "401", "403", "404", "503"},
            ("/memories/{memory_id}", "patch"): {"202", "400", "401", "403", "404", "409", "428", "503"},
            ("/memories/{memory_id}/revisions", "get"): {"200", "400", "401", "403", "404", "503"},
            ("/memories/{memory_id}/revisions/{revision}", "get"): {"200", "400", "401", "403", "404", "503"},
            ("/memories/{memory_id}/commands/{command}", "post"): {"202", "400", "401", "403", "404", "409", "428", "503"},
        }
        for (route, method), codes in expected.items():
            with self.subTest(route=route, method=method):
                self.assertEqual(set(spec["paths"][route][method]["responses"]), codes)

    def test_openapi_memory_ids_accept_legacy_migrated_aggregates(self) -> None:
        spec = json.loads(
            (ROOT / "specs/controller-api-v1.openapi.json").read_text(encoding="utf-8")
        )
        for path in (
            "/memories/{memory_id}",
            "/memories/{memory_id}/revisions",
            "/memories/{memory_id}/revisions/{revision}",
            "/memories/{memory_id}/commands/{command}",
        ):
            operation = next(iter(spec["paths"][path].values()))
            parameter = next(
                item for item in operation["parameters"] if item.get("name") == "memory_id"
            )
            schema = parameter["schema"]
            self.assertNotIn("pattern", schema)
            self.assertEqual(schema.get("minLength"), 1)
            self.assertEqual(schema.get("maxLength"), 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
