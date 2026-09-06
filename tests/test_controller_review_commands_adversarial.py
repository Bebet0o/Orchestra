from __future__ import annotations

import json
import sqlite3
from tests.sqlite_test_support import sqlite_connect
import threading
from contextlib import closing
from pathlib import Path

from controller_api.review_commands import ReviewCommandStore
from test_controller_api import TOKEN
import test_controller_review_commands as review_tests


class ReviewCommandAdversarialTest(review_tests.ReviewCommandTest):
    def test_different_actions_are_mutually_exclusive(self) -> None:
        first = self.command(
            review_tests.DEBT_REVIEW,
            "acknowledge-debt",
            key="adv-cross-action-01",
        )
        second = self.command(
            review_tests.DEBT_REVIEW,
            "request-human-review",
            key="adv-cross-action-02",
        )
        self.assertEqual(first[0], 202)
        self.assertEqual(second[0], 409)
        self.assertEqual(second[2]["code"], "review_action_conflict")
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions "
                    "WHERE review_id=?",
                    (review_tests.DEBT_REVIEW,),
                ).fetchone()[0],
                1,
            )

    def test_different_actions_are_mutually_exclusive_in_reverse(self) -> None:
        first = self.command(
            review_tests.DEBT_REVIEW,
            "request-human-review",
            key="adv-cross-reverse-01",
        )
        second = self.command(
            review_tests.DEBT_REVIEW,
            "acknowledge-debt",
            key="adv-cross-reverse-02",
        )
        self.assertEqual(first[0], 202)
        self.assertEqual(second[0], 409)
        self.assertEqual(second[2]["code"], "review_action_conflict")

    def test_concurrent_different_actions_record_only_one(self) -> None:
        token = self.csrf("csrf-adv-cross-race")
        barrier = threading.Barrier(3)
        results: list[tuple[int, dict[str, object]]] = []
        lock = threading.Lock()

        def worker(command: str, key: str) -> None:
            barrier.wait()
            status, _, payload = self.post(
                f"/api/v1/reviews/{review_tests.DEBT_REVIEW}/commands/{command}",
                {"reason": "concurrent cross action"},
                key=key,
                csrf=token,
            )
            with lock:
                results.append((status, payload))

        threads = (
            threading.Thread(
                target=worker,
                args=("acknowledge-debt", "adv-cross-race-01"),
            ),
            threading.Thread(
                target=worker,
                args=("request-human-review", "adv-cross-race-02"),
            ),
        )
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())

        self.assertEqual(sorted(status for status, _ in results), [202, 409])
        self.assertEqual(
            [payload.get("code") for status, payload in results if status == 409],
            ["review_action_conflict"],
        )
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions "
                    "WHERE review_id=?",
                    (review_tests.DEBT_REVIEW,),
                ).fetchone()[0],
                1,
            )

    def test_database_constraint_rejects_second_action_for_review(self) -> None:
        self.command(
            review_tests.DEBT_REVIEW,
            "acknowledge-debt",
            key="adv-db-unique-01",
        )
        with closing(sqlite_connect(self.fixture.database)) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO controller_review_actions (
                        action_id, review_id, run_id, command,
                        reason_present, status, created_at
                    ) VALUES (?, ?, ?, 'request-human-review', 0, 'RECORDED', ?)
                    """,
                    (
                        "review-action-" + "f" * 32,
                        review_tests.DEBT_REVIEW,
                        "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "2026-07-21T00:00:00.000Z",
                    ),
                )

    def test_orphan_review_is_projection_failure_not_not_found(self) -> None:
        with closing(sqlite_connect(self.fixture.database)) as connection:
            connection.execute(
                "DELETE FROM runs WHERE run_id=?",
                ("run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",),
            )
            connection.commit()
        status, _, payload = self.command(
            review_tests.FIX_REVIEW,
            "request-human-review",
            key="adv-orphan-run-01",
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")

    def test_visible_incomplete_idempotency_reservation_fails_closed(self) -> None:
        key = "adv-stale-reservation-01"
        path = f"/api/v1/reviews/{review_tests.FIX_REVIEW}/commands/request-human-review"
        body = {"reason": "stale"}
        csrf = self.csrf("csrf-" + key)
        store = ReviewCommandStore(self.fixture.settings)
        session_fp = store.shared._session_fingerprint(TOKEN)
        key_hash = store.shared._key_hash(TOKEN, key)
        request_hash = store.shared._request_hash(TOKEN, "POST", path, body)
        with closing(sqlite_connect(self.fixture.database)) as connection:
            connection.execute(
                """
                INSERT INTO controller_review_idempotency (
                    session_fingerprint, key_hash, method, route, request_hash,
                    response_status, response_json, operation_id,
                    created_at, completed_at
                ) VALUES (?, ?, 'POST', ?, ?, NULL, NULL, NULL, ?, NULL)
                """,
                (
                    session_fp,
                    key_hash,
                    path,
                    request_hash,
                    "2026-07-21T00:00:00.000Z",
                ),
            )
            connection.commit()
        status, _, payload = self.post(
            path,
            body,
            key=key,
            csrf=csrf,
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "idempotency_reservation_invalid")
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions"
                ).fetchone()[0],
                0,
            )

    def test_unknown_verdict_fails_closed_without_mutation(self) -> None:
        with closing(sqlite_connect(self.fixture.database)) as connection:
            connection.execute(
                "UPDATE review_results SET verdict='UNKNOWN' WHERE review_id=?",
                (review_tests.FIX_REVIEW,),
            )
            connection.commit()
        status, _, payload = self.command(
            review_tests.FIX_REVIEW,
            "request-human-review",
            key="adv-unknown-verdict-01",
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["code"], "review_projection_invalid")
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions"
                ).fetchone()[0],
                0,
            )

    def test_historical_tables_are_immutable(self) -> None:
        candidates = (
            "review_results",
            "reviewer_executions",
            "runs",
            "tasks",
            "orchestration_tasks",
            "objective_queue",
            "recoveries",
            "recovery_executions",
            "approvals",
        )
        with closing(sqlite_connect(self.fixture.database)) as connection:
            existing = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            immutable = tuple(table for table in candidates if table in existing)
            before = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in immutable
            }
        status, _, _ = self.command(
            review_tests.DEBT_REVIEW,
            "acknowledge-debt",
            key="adv-immutability-01",
            reason="redacted human reason",
        )
        self.assertEqual(status, 202)
        with closing(sqlite_connect(self.fixture.database)) as connection:
            after = {
                table: connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
                for table in immutable
            }
            raw = "\n".join(
                str(item)
                for table in (
                    "controller_review_actions",
                    "controller_review_operations",
                    "controller_review_command_audit",
                    "controller_review_idempotency",
                    "events",
                )
                for row in connection.execute(f"SELECT * FROM {table}")
                for item in row
            )
        self.assertEqual(before, after)
        self.assertNotIn("redacted human reason", raw)
        self.assertNotIn(TOKEN, raw)
        self.assertNotIn("adv-immutability-01", raw)

    @staticmethod
    def _migration_fixture() -> sqlite3.Connection:
        connection = sqlite_connect(":memory:")
        connection.executescript(
            """
            PRAGMA foreign_keys=ON;
            CREATE TABLE schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            INSERT INTO schema_migrations VALUES (13, 'x');
            CREATE TABLE runs (
                run_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL
            );
            CREATE TABLE review_results (
                review_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                verdict TEXT NOT NULL
            );
            INSERT INTO runs VALUES (
                'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 'alpha'
            );
            INSERT INTO review_results VALUES (
                'review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                'PASS_WITH_DEBT'
            );
            CREATE TABLE controller_review_operations (
                operation_id TEXT PRIMARY KEY CHECK (
                    operation_id GLOB 'operation-[0-9a-f]*'
                ),
                command_kind TEXT NOT NULL,
                state TEXT NOT NULL,
                target_id TEXT NOT NULL,
                result_json TEXT NOT NULL,
                error_code TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                finished_at TEXT,
                FOREIGN KEY (target_id)
                    REFERENCES review_results(review_id)
            );
            CREATE TABLE controller_review_idempotency (
                session_fingerprint TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                method TEXT NOT NULL,
                route TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                response_status INTEGER,
                response_json TEXT,
                operation_id TEXT,
                created_at TEXT NOT NULL,
                completed_at TEXT,
                PRIMARY KEY (session_fingerprint, key_hash),
                FOREIGN KEY (operation_id)
                    REFERENCES controller_review_operations(operation_id)
            );
            CREATE TABLE controller_review_command_audit (
                audit_id TEXT PRIMARY KEY CHECK (
                    audit_id GLOB 'audit-[0-9a-f]*'
                ),
                operation_id TEXT NOT NULL UNIQUE,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT NOT NULL,
                session_fingerprint TEXT NOT NULL,
                idempotency_key_hash TEXT NOT NULL,
                request_hash TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason_present INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (operation_id)
                    REFERENCES controller_review_operations(operation_id),
                FOREIGN KEY (resource_id)
                    REFERENCES review_results(review_id)
            );
            CREATE TABLE controller_review_actions (
                action_id TEXT PRIMARY KEY CHECK (
                    action_id GLOB 'review-action-[0-9a-f]*'
                ),
                review_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                command TEXT NOT NULL,
                reason_present INTEGER NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (review_id, command),
                FOREIGN KEY (review_id)
                    REFERENCES review_results(review_id),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX idx_controller_review_actions_run
                ON controller_review_actions(run_id, created_at);
            PRAGMA user_version=13;
            """
        )
        return connection

    @staticmethod
    def _migration_sql() -> str:
        return (
            Path(__file__).resolve().parents[1]
            / "migrations"
            / "014_controller_review_command_hardening.sql"
        ).read_text(encoding="utf-8")

    def test_migration_014_enforces_business_and_identifier_constraints(self) -> None:
        with closing(self._migration_fixture()) as connection:
            connection.executescript(self._migration_sql())
            self.assertEqual(
                connection.execute("PRAGMA user_version").fetchone()[0],
                14,
            )
            connection.execute(
                """
                INSERT INTO controller_review_actions VALUES (
                    ?, ?, ?, 'acknowledge-debt', 0, 'RECORDED', 'x'
                )
                """,
                (
                    "review-action-" + "1" * 32,
                    "review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO controller_review_actions VALUES (
                        ?, ?, ?, 'request-human-review', 0, 'RECORDED', 'x'
                    )
                    """,
                    (
                        "review-action-" + "2" * 32,
                        "review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                        "run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    ),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO controller_review_operations (
                        operation_id, command_kind, state, target_id,
                        result_json, created_at, updated_at
                    ) VALUES (?, 'review.request-human-review', 'SUCCEEDED',
                              ?, '{}', 'x', 'x')
                    """,
                    (
                        "operation-z",
                        "review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    ),
                )
            valid_operation = "operation-" + "a" * 32
            connection.execute(
                """
                INSERT INTO controller_review_operations (
                    operation_id, command_kind, state, target_id,
                    result_json, created_at, updated_at
                ) VALUES (?, 'review.request-human-review', 'SUCCEEDED',
                          ?, '{}', 'x', 'x')
                """,
                (
                    valid_operation,
                    "review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                ),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO controller_review_command_audit (
                        audit_id, operation_id, actor_type, actor_id, action,
                        resource_type, resource_id, session_fingerprint,
                        idempotency_key_hash, request_hash, outcome,
                        reason_present, created_at
                    ) VALUES (?, ?, 'session', 'actor',
                              'request-human-review', 'review', ?,
                              'session-fp', 'key-hash', 'request-hash',
                              'SUCCEEDED', 0, 'x')
                    """,
                    (
                        "audit-z",
                        valid_operation,
                        "review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                    ),
                )

    def test_migration_014_rejects_preexisting_ambiguous_actions(self) -> None:
        with closing(self._migration_fixture()) as connection:
            connection.executescript(
                """
                INSERT INTO controller_review_actions VALUES (
                    'review-action-11111111111111111111111111111111',
                    'review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'acknowledge-debt', 0, 'RECORDED', 'x'
                );
                INSERT INTO controller_review_actions VALUES (
                    'review-action-22222222222222222222222222222222',
                    'review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'run-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
                    'request-human-review', 0, 'RECORDED', 'x'
                );
                """
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.executescript(self._migration_sql())

    def test_rerun_bypass_variants_remain_blocked(self) -> None:
        variants = (
            "RERUN",
            "ReRun",
            "rerun%2F",
            "%72erun",
            "rerun/",
        )
        for index, variant in enumerate(variants):
            csrf = self.csrf(f"csrf-adv-rerun-{index:02d}")
            status, _, payload = self.post(
                f"/api/v1/reviews/{review_tests.FIX_REVIEW}/commands/{variant}",
                {"reason": "blocked"},
                key=f"adv-rerun-{index:04d}",
                csrf=csrf,
            )
            self.assertNotEqual(status, 202, variant)
            self.assertIn(status, (400, 404, 409), variant)
            self.assertIsInstance(payload, dict)
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    import unittest

    unittest.main()
