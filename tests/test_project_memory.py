from __future__ import annotations

import concurrent.futures
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from controller_api.core import ControllerError
from controller_api.project_memory import ProjectMemoryStore
from tests.sqlite_test_support import sqlite_connect


NOW = "2026-09-07T16:00:00.000Z"


class ProjectMemoryStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "controller.db"
        self._migrate(self.database)
        self._insert_project(self.database, "alpha")
        self.store = ProjectMemoryStore(SimpleNamespace(database=self.database))

    def tearDown(self) -> None:
        self.temporary.cleanup()

    @staticmethod
    def _migrations() -> list[Path]:
        root = Path(__file__).resolve().parents[1]
        return sorted((root / "migrations").glob("[0-9][0-9][0-9]_*.sql"))

    @classmethod
    def _migrate(cls, database: Path, *, through: int | None = None) -> None:
        connection = sqlite_connect(database)
        connection.execute("PRAGMA foreign_keys=ON")
        for migration in cls._migrations():
            version = int(migration.name.split("_", 1)[0])
            if through is not None and version > through:
                break
            connection.executescript(migration.read_text(encoding="utf-8"))
        connection.close()

    @staticmethod
    def _insert_project(database: Path, project_id: str) -> None:
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

    def _create(self, **overrides):
        values = {
            "project_id": "alpha",
            "scope": "PROJECT",
            "objective_id": None,
            "kind": "FACT",
            "memory_key": "runtime.python",
            "title": "Python runtime",
            "content": "Python 3.13 is the supported runtime.",
            "authority": "OPERATOR_DECLARED",
            "actor_type": "OPERATOR",
            "actor_id": "operator:test",
        }
        values.update(overrides)
        return self.store.create_memory(**values)

    def test_schema_and_readiness(self) -> None:
        self.assertEqual(self.store.readiness(), (True, "ready"))
        with sqlite_connect(self.database) as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 33)
            self.assertEqual(
                connection.execute(
                    "SELECT version FROM schema_migrations ORDER BY version"
                ).fetchall(),
                [(version,) for version in range(1, 34)],
            )
            self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_create_exact_dedup_revise_and_context_separation(self) -> None:
        first = self._create()
        self.assertFalse(first.deduplicated)
        self.assertTrue(first.revision_created)
        self.assertEqual(first.memory["revision"], 1)
        self.assertEqual(first.memory["resource_revision"], 1)
        second = self._create()
        self.assertTrue(second.deduplicated)
        self.assertFalse(second.revision_created)
        self.assertEqual(second.memory["id"], first.memory["id"])
        self.assertEqual(second.memory["revision"], 1)
        revised = self.store.revise_memory(
            first.memory["id"],
            expected_resource_revision=1,
            kind="CONSTRAINT",
            memory_key="runtime.python",
            title="Python runtime",
            content="Python 3.13 or newer is required.",
            authority="OPERATOR_DECLARED",
            actor_type="OPERATOR",
            actor_id="operator:test",
        )
        self.assertEqual(revised.memory["revision"], 2)
        self.assertEqual(revised.memory["resource_revision"], 2)
        self.assertEqual(revised.memory["kind"], "CONSTRAINT")
        with sqlite_connect(self.database) as connection:
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM shared_context_entries").fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) FROM project_memory_revisions").fetchone()[0],
                2,
            )

    def test_semantically_similar_text_is_not_merged(self) -> None:
        first = self._create(content="Keep this exact fact.")
        second = self._create(content="Keep this exact fact. ")
        self.assertNotEqual(first.memory["id"], second.memory["id"])
        self.assertFalse(second.deduplicated)

    def test_secret_rejection_preserves_harmless_template_reference(self) -> None:
        safe = self._create(
            memory_key="runtime.home",
            title="Home path",
            content="The worker reads configuration below ${HOME}/.config.",
        )
        self.assertIn("${HOME}", safe.memory["content"])
        sentinel = "password=ultra-private-memory-sentinel"
        with self.assertRaises(ControllerError) as context:
            self._create(content=sentinel)
        self.assertEqual(context.exception.code, "memory_secret_detected")
        with sqlite_connect(self.database) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(sentinel, dump)

    def test_authority_cannot_be_forged_by_wrong_actor(self) -> None:
        with self.assertRaises(ControllerError) as context:
            self._create(
                authority="OPERATOR_DECLARED",
                actor_type="AGENT",
                actor_id="worker:one",
            )
        self.assertEqual(context.exception.code, "invalid_memory_authority")
        with self.assertRaises(ControllerError) as context:
            self._create(
                authority="REVIEW_ACCEPTED",
                actor_type="CONTROL_PLANE",
                actor_id="controller",
            )
        self.assertEqual(context.exception.code, "invalid_memory_authority")

    def test_actor_and_free_provenance_identifiers_reject_controls(self) -> None:
        with self.assertRaises(ControllerError) as context:
            self._create(actor_id="operator:test\nforged")
        self.assertEqual(context.exception.code, "invalid_memory_authority")

        with self.assertRaises(ControllerError) as context:
            self._create(
                provenance=({"kind": "OPERATOR", "id": "operator:test\nforged"},)
            )
        self.assertEqual(context.exception.code, "invalid_memory_provenance")

    def test_retract_then_redact_scrubs_every_revision(self) -> None:
        first = self._create(content="First durable value.")
        memory_id = first.memory["id"]
        revised = self.store.revise_memory(
            memory_id,
            expected_resource_revision=1,
            kind="FACT",
            memory_key="runtime.python",
            title="Python runtime",
            content="Second durable value.",
            authority="OPERATOR_DECLARED",
            actor_type="OPERATOR",
            actor_id="operator:test",
        )
        self.assertEqual(revised.memory["resource_revision"], 2)
        retracted = self.store.retract_memory(
            memory_id,
            expected_resource_revision=2,
        )
        self.assertEqual(retracted["state"], "RETRACTED")
        redacted = self.store.redact_memory(
            memory_id,
            expected_resource_revision=3,
        )
        self.assertEqual(redacted["state"], "REDACTED")
        self.assertIsNone(redacted["content"])
        self.assertEqual(redacted["resource_revision"], 4)
        with sqlite_connect(self.database) as connection:
            rows = connection.execute(
                """
                SELECT payload.state, payload.memory_key, payload.title, payload.content
                FROM project_memory_revisions revision
                JOIN project_memory_payloads payload USING(payload_id)
                WHERE revision.memory_id=?
                ORDER BY revision.revision_number
                """,
                (memory_id,),
            ).fetchall()
        self.assertEqual(rows, [("REDACTED", None, None, ""), ("REDACTED", None, None, "")])
        with self.assertRaises(ControllerError) as context:
            self.store.revise_memory(
                memory_id,
                expected_resource_revision=4,
                kind="FACT",
                memory_key=None,
                title=None,
                content="cannot return",
                authority="OPERATOR_DECLARED",
                actor_type="OPERATOR",
                actor_id="operator:test",
            )
        self.assertEqual(context.exception.code, "memory_terminal")

    def test_terminal_memories_reject_resource_revision_only_bumps(self) -> None:
        created = self._create(content="Terminal transition guard.")
        memory_id = created.memory["id"]
        retracted = self.store.retract_memory(
            memory_id,
            expected_resource_revision=1,
        )
        self.assertEqual(retracted["state"], "RETRACTED")
        with sqlite_connect(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE project_memories
                    SET resource_revision=resource_revision+1, updated_at=updated_at
                    WHERE memory_id=?
                    """,
                    (memory_id,),
                )
        redacted = self.store.redact_memory(
            memory_id,
            expected_resource_revision=2,
        )
        self.assertEqual(redacted["state"], "REDACTED")
        with sqlite_connect(self.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    UPDATE project_memories
                    SET resource_revision=resource_revision+1, updated_at=updated_at
                    WHERE memory_id=?
                    """,
                    (memory_id,),
                )

    def test_revision_payload_and_provenance_are_immutable(self) -> None:
        created = self._create(
            provenance=({"kind": "OPERATOR", "id": "operator:test"},)
        )
        memory_id = created.memory["id"]
        with sqlite_connect(self.database) as connection:
            revision_id, payload_id = connection.execute(
                """
                SELECT revision.revision_id, revision.payload_id
                FROM project_memories memory
                JOIN project_memory_revisions revision
                  ON revision.revision_id=memory.current_revision_id
                WHERE memory.memory_id=?
                """,
                (memory_id,),
            ).fetchone()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE project_memory_revisions SET authority='AGENT_PROPOSED' WHERE revision_id=?",
                    (revision_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE project_memory_payloads SET content='rewritten' WHERE payload_id=?",
                    (payload_id,),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM project_memory_provenance WHERE revision_id=?",
                    (revision_id,),
                )

    def test_revision_skip_and_fork_are_rejected(self) -> None:
        created = self._create()
        memory_id = created.memory["id"]
        with sqlite_connect(self.database) as connection:
            current_revision = connection.execute(
                "SELECT current_revision_id FROM project_memories WHERE memory_id=?",
                (memory_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO project_memory_payloads (
                    payload_id, kind, memory_key, title, content, state,
                    redacted_at, redaction_code
                ) VALUES ('manual-payload', 'FACT', NULL, NULL, 'fork', 'AVAILABLE', NULL, NULL)
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO project_memory_revisions (
                        revision_id, memory_id, revision_number, payload_id,
                        payload_sha256, authority, authority_review_id,
                        authority_decision_id, supersedes_revision_id,
                        created_by_actor_type, created_by_actor_id, created_at
                    ) VALUES ('manual-revision', ?, 3, 'manual-payload', ?,
                        'OPERATOR_DECLARED', NULL, NULL, ?, 'OPERATOR', 'operator:test', ?)
                    """,
                    (memory_id, "0" * 64, current_revision, NOW),
                )

    def test_cross_project_run_provenance_and_links_are_rejected(self) -> None:
        self._insert_project(self.database, "beta")
        created = self._create()
        memory_id = created.memory["id"]
        with sqlite_connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                INSERT INTO runs (run_id, project_id, status, metadata_json, created_at)
                VALUES ('run-beta', 'beta', 'QUEUED', '{}', ?)
                """,
                (NOW,),
            )
            revision_id = connection.execute(
                "SELECT current_revision_id FROM project_memories WHERE memory_id=?",
                (memory_id,),
            ).fetchone()[0]
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO project_memory_provenance (
                        provenance_id, revision_id, source_kind, source_actor_id,
                        objective_id, task_id, attempt_id, run_id, review_id,
                        judge_decision_id, recovery_action_id, legacy_context_id,
                        legacy_memory_id, created_at
                    ) VALUES ('cross-provenance', ?, 'RUN', NULL,
                        NULL, NULL, NULL, 'run-beta', NULL, NULL, NULL, NULL, NULL, ?)
                    """,
                    (revision_id, NOW),
                )
            connection.rollback()
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO project_memory_revision_links (
                        link_id, revision_id, link_kind, objective_id, task_id,
                        attempt_id, run_id, review_id, judge_decision_id,
                        recovery_action_id, created_at
                    ) VALUES ('cross-link', ?, 'RUN', NULL, NULL, NULL,
                        'run-beta', NULL, NULL, NULL, ?)
                    """,
                    (revision_id, NOW),
                )

    def test_legacy_rows_migrate_one_for_one_without_fake_revisions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "legacy.db"
            self._migrate(database, through=31)
            self._insert_project(database, "legacy")
            with sqlite_connect(database) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute(
                    """
                    INSERT INTO memory_records (
                        memory_id, project_id, category, title, body,
                        source_run_id, created_at
                    ) VALUES ('old-memory', 'legacy', 'decision', 'Old title',
                        'Old body', NULL, ?)
                    """,
                    (NOW,),
                )
                for sequence, context_id, content in (
                    (1, "context-one", "first repeated key"),
                    (2, "context-two", "second repeated key"),
                ):
                    connection.execute(
                        """
                        INSERT INTO shared_context_entries (
                            context_id, context_sequence, scope, project_id,
                            objective_id, kind, context_key, content, source_type,
                            source_task_id, source_assignment_id, source_attempt_id,
                            created_at
                        ) VALUES (?, ?, 'PROJECT', 'legacy', NULL, 'FACT',
                            'same-key', ?, 'CONTROL_PLANE', NULL, NULL, NULL, ?)
                        """,
                        (context_id, sequence, content, NOW),
                    )
            migration = [
                path for path in self._migrations()
                if path.name.startswith("032_")
            ][0]
            with sqlite_connect(database) as connection:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.executescript(migration.read_text(encoding="utf-8"))
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM project_memories").fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM project_memory_revisions").fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM project_memory_revisions WHERE payload_sha256 IS NULL"
                    ).fetchone()[0],
                    3,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM project_memory_revisions WHERE authority='LEGACY_UNVERIFIED'"
                    ).fetchone()[0],
                    3,
                )
                rows = connection.execute(
                    """
                    SELECT payload.memory_key, payload.title, payload.content
                    FROM project_memory_revisions revision
                    JOIN project_memory_payloads payload USING(payload_id)
                    ORDER BY revision.revision_id
                    """
                ).fetchall()
                self.assertIn((None, "Old title", "Old body"), rows)
                self.assertIn(("same-key", None, "first repeated key"), rows)
                self.assertIn(("same-key", None, "second repeated key"), rows)
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_corrupt_persisted_secret_fails_closed_without_echo(self) -> None:
        sentinel = "password=do-not-echo-persisted-memory"
        with sqlite_connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                INSERT INTO project_memories (
                    memory_id, project_id, scope, objective_id, state,
                    current_revision_id, current_revision_number,
                    resource_revision, created_at, updated_at
                ) VALUES ('raw-memory', 'alpha', 'PROJECT', NULL, 'ACTIVE',
                    'raw-revision', 1, 1, ?, ?)
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO project_memory_payloads (
                    payload_id, kind, memory_key, title, content, state,
                    redacted_at, redaction_code
                ) VALUES ('raw-payload', 'NOTE', NULL, NULL, ?, 'AVAILABLE', NULL, NULL)
                """,
                (sentinel,),
            )
            connection.execute(
                """
                INSERT INTO project_memory_revisions (
                    revision_id, memory_id, revision_number, payload_id,
                    payload_sha256, authority, authority_review_id,
                    authority_decision_id, supersedes_revision_id,
                    created_by_actor_type, created_by_actor_id, created_at
                ) VALUES ('raw-revision', 'raw-memory', 1, 'raw-payload', ?,
                    'OPERATOR_DECLARED', NULL, NULL, NULL, 'OPERATOR', 'operator:test', ?)
                """,
                ("0" * 64, NOW),
            )
        with self.assertRaises(ControllerError) as context:
            self.store.get_memory("raw-memory")
        self.assertEqual(context.exception.code, "project_memory_projection_failed")
        rendered = json.dumps(
            {"title": context.exception.title, "detail": context.exception.detail}
        )
        self.assertNotIn(sentinel, rendered)

    def test_concurrent_same_revision_has_one_winner(self) -> None:
        created = self._create()
        memory_id = created.memory["id"]

        def revise(index: int):
            try:
                return self.store.revise_memory(
                    memory_id,
                    expected_resource_revision=1,
                    kind="FACT",
                    memory_key="concurrency",
                    title="Concurrent revision",
                    content=f"winner candidate {index}",
                    authority="OPERATOR_DECLARED",
                    actor_type="OPERATOR",
                    actor_id="operator:test",
                )
            except ControllerError as error:
                return error

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(revise, (1, 2)))
        successes = [item for item in results if not isinstance(item, ControllerError)]
        failures = [item for item in results if isinstance(item, ControllerError)]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].code, "memory_revision_conflict")
        final = self.store.get_memory(memory_id)
        self.assertEqual(final["revision"], 2)
        self.assertEqual(final["resource_revision"], 2)

    def test_corrupt_persisted_hash_fails_closed(self) -> None:
        sentinel = "benign-but-corrupted-memory-payload"
        with sqlite_connect(self.database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute(
                """
                INSERT INTO project_memories (
                    memory_id, project_id, scope, objective_id, state,
                    current_revision_id, current_revision_number,
                    resource_revision, created_at, updated_at
                ) VALUES ('raw-hash-memory', 'alpha', 'PROJECT', NULL, 'ACTIVE',
                    'raw-hash-revision', 1, 1, ?, ?)
                """,
                (NOW, NOW),
            )
            connection.execute(
                """
                INSERT INTO project_memory_payloads (
                    payload_id, kind, memory_key, title, content, state,
                    redacted_at, redaction_code
                ) VALUES ('raw-hash-payload', 'NOTE', 'integrity.check',
                    'Integrity check', ?, 'AVAILABLE', NULL, NULL)
                """,
                (sentinel,),
            )
            connection.execute(
                """
                INSERT INTO project_memory_revisions (
                    revision_id, memory_id, revision_number, payload_id,
                    payload_sha256, authority, authority_review_id,
                    authority_decision_id, supersedes_revision_id,
                    created_by_actor_type, created_by_actor_id, created_at
                ) VALUES ('raw-hash-revision', 'raw-hash-memory', 1,
                    'raw-hash-payload', ?, 'OPERATOR_DECLARED', NULL, NULL,
                    NULL, 'OPERATOR', 'operator:test', ?)
                """,
                ("0" * 64, NOW),
            )
        with self.assertRaises(ControllerError) as context:
            self.store.get_memory("raw-hash-memory")
        self.assertEqual(context.exception.code, "project_memory_projection_failed")
        rendered = json.dumps(
            {"title": context.exception.title, "detail": context.exception.detail}
        )
        self.assertNotIn(sentinel, rendered)

    def test_corrupt_current_revision_pointer_fails_closed_as_503(self) -> None:
        created = self._create(content="Pointer integrity guard.")
        memory_id = created.memory["id"]
        with sqlite_connect(self.database) as connection:
            connection.execute("DROP TRIGGER project_memory_update_guard")
            connection.execute(
                "UPDATE project_memories SET current_revision_id='missing-revision' WHERE memory_id=?",
                (memory_id,),
            )
        with self.assertRaises(ControllerError) as context:
            self.store.get_memory(memory_id)
        self.assertEqual(context.exception.status, 503)
        self.assertEqual(context.exception.code, "project_memory_projection_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
