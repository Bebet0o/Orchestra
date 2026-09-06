from __future__ import annotations

import json
import sqlite3
from tests.sqlite_test_support import sqlite_connect
import sys
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from controller_api.core import ControllerError  # noqa: E402
from controller_api.event_journal import EventJournal  # noqa: E402
from test_controller_api import APIFixture  # noqa: E402
import test_controller_review_commands as review_tests  # noqa: E402


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "015_controller_event_journal.sql"
)


class EventJournalUnitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite_connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute(
            "CREATE TABLE schema_migrations ("
            "version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        self.connection.executescript(MIGRATION.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.connection.close()

    def emit(self, *, event_type: str = "objective.created", data=None):
        return EventJournal.emit(
            self.connection,
            event_type=event_type,
            actor_type="operator",
            actor_id="operator:local-controller-session",
            aggregate_type="objective",
            aggregate_id="objective-" + "a" * 32,
            correlation_id="corr_" + "b" * 32,
            causation_id="operation-" + "c" * 32,
            project_id="alpha",
            objective_id="objective-" + "a" * 32,
            data=data or {"state": "queued"},
            occurred_at="2026-07-21T00:00:00.000Z",
        )

    def test_emit_requires_existing_transaction(self) -> None:
        with self.assertRaises(ControllerError) as caught:
            self.emit()
        self.assertEqual(caught.exception.code, "event_journal_transaction_required")

    def test_sequence_and_aggregate_revision_are_monotonic(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        first = self.emit()
        second = self.emit(event_type="objective.state_changed", data={"state": "paused"})
        self.connection.commit()
        self.assertEqual((first["sequence"], second["sequence"]), (1, 2))
        self.assertEqual(
            (first["aggregate"]["revision"], second["aggregate"]["revision"]),
            (1, 2),
        )

    def test_rollback_keeps_event_invisible(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit()
        self.connection.rollback()
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM controller_event_journal"
            ).fetchone()[0],
            0,
        )

    def test_redaction_rejects_sensitive_keys_and_values(self) -> None:
        for data in (
            {"session_token": "hidden"},
            {"nested": {"api_key": "hidden"}},
            {"value": "Bearer abcdefghijklmnopqrstuvwxyz"},
            {"value": "-----BEGIN PRI" + "VATE KEY-----"},
        ):
            self.connection.execute("BEGIN IMMEDIATE")
            with self.assertRaises(ControllerError) as caught:
                self.emit(data=data)
            self.connection.rollback()
            self.assertEqual(caught.exception.code, "event_journal_redaction_failed")

    def test_unknown_well_formed_type_is_preserved(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit(event_type="review.human_review_requested")
        self.connection.commit()
        events = EventJournal.read_after(self.connection, after_sequence=0)
        self.assertEqual(events[0]["type"], "review.human_review_requested")

    def test_bounded_replay_and_filters(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit()
        EventJournal.emit(
            self.connection,
            event_type="review.debt_acknowledged",
            actor_type="operator",
            actor_id="operator:local-controller-session",
            aggregate_type="review",
            aggregate_id="review-" + "d" * 32,
            correlation_id="corr_" + "e" * 32,
            causation_id="operation-" + "f" * 32,
            project_id="beta",
            objective_id=None,
            data={"reason_present": True},
            occurred_at="2026-07-21T00:00:01.000Z",
        )
        self.connection.commit()
        self.assertEqual(
            [event["sequence"] for event in EventJournal.read_after(
                self.connection, after_sequence=0, limit=1
            )],
            [1],
        )
        self.assertEqual(
            [event["type"] for event in EventJournal.read_after(
                self.connection, after_sequence=0, project_id="beta"
            )],
            ["review.debt_acknowledged"],
        )

    def test_journal_rows_are_immutable(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit()
        self.connection.commit()
        for statement in (
            "UPDATE controller_event_journal SET event_type='objective.updated'",
            "DELETE FROM controller_event_journal",
        ):
            with self.assertRaises(sqlite3.IntegrityError):
                self.connection.execute(statement)

    def test_malformed_timestamp_fails_closed(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit()
        self.connection.commit()
        self.connection.execute("DROP TRIGGER controller_event_journal_immutable_update")
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.connection.execute(
            "UPDATE controller_event_journal SET occurred_at='not-a-timestampZ'"
        )
        with self.assertRaises(ControllerError) as caught:
            EventJournal.read_after(self.connection, after_sequence=0)
        self.assertEqual(caught.exception.code, "event_journal_corrupt")

    def test_corruption_fails_closed(self) -> None:
        self.connection.execute("BEGIN IMMEDIATE")
        self.emit()
        self.connection.commit()
        self.connection.execute("DROP TRIGGER controller_event_journal_immutable_update")
        self.connection.execute("PRAGMA ignore_check_constraints = ON")
        self.connection.execute(
            "UPDATE controller_event_journal SET redacted_data_json='not-json'"
        )
        with self.assertRaises(ControllerError) as caught:
            EventJournal.read_after(self.connection, after_sequence=0)
        self.assertEqual(caught.exception.code, "event_journal_corrupt")


class EventJournalCommandIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = APIFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def post(self, path: str, body: dict[str, object], *, key: str, csrf: str | None = None):
        headers = {"Content-Type": "application/json", "Idempotency-Key": key}
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        return self.fixture.request(
            "POST",
            path,
            authenticated=True,
            headers_override=headers,
            body=json.dumps(body, separators=(",", ":")).encode(),
        )

    def csrf(self, key: str) -> str:
        status, _, payload = self.post("/api/v1/auth/csrf", {}, key=key)
        self.assertEqual(status, 200)
        return str(payload["data"]["token"])

    @staticmethod
    def create_body() -> dict[str, object]:
        return {
            "project_ids": ["alpha"],
            "title": "Controller event journal objective",
            "description": "Exercise durable event persistence.",
            "priority": 90,
            "not_before": "2099-01-01T00:00:00Z",
            "max_parallel_tasks": 1,
            "planning_max_attempts": 3,
            "constraints": ["Never persist secrets"],
        }

    def test_objective_lifecycle_is_atomic_ordered_and_idempotent(self) -> None:
        csrf = self.csrf("csrf-event-objective")
        body = self.create_body()
        first = self.post(
            "/api/v1/objectives", body, key="event-objective-create", csrf=csrf
        )
        replay = self.post(
            "/api/v1/objectives", body, key="event-objective-create", csrf=csrf
        )
        self.assertEqual(first[0], 202)
        self.assertEqual(first[2], replay[2])
        objective_id = str(first[2]["data"]["target"]["id"])
        for command in ("pause", "resume", "cancel"):
            status, _, _ = self.post(
                f"/api/v1/objectives/{objective_id}/commands/{command}",
                {"reason": "must not be persisted"},
                key=f"event-objective-{command}",
                csrf=csrf,
            )
            self.assertEqual(status, 202)
        with closing(sqlite_connect(self.fixture.database)) as connection:
            connection.row_factory = sqlite3.Row
            rows = list(
                connection.execute(
                    """
                    SELECT sequence, event_type, aggregate_revision,
                           causation_id, redacted_data_json
                    FROM controller_event_journal
                    WHERE aggregate_type='objective' AND aggregate_id=?
                    ORDER BY sequence
                    """,
                    (objective_id,),
                )
            )
        self.assertEqual(
            [row["event_type"] for row in rows],
            [
                "objective.created",
                "objective.state_changed",
                "objective.state_changed",
                "objective.state_changed",
            ],
        )
        self.assertEqual([row["aggregate_revision"] for row in rows], [1, 2, 3, 4])
        self.assertEqual(len({row["causation_id"] for row in rows}), 4)
        persisted = "\n".join(str(row["redacted_data_json"]) for row in rows)
        self.assertNotIn("must not be persisted", persisted)
        self.assertNotIn("Controller event journal objective", persisted)

    def test_review_commands_emit_redacted_domain_events(self) -> None:
        csrf = self.csrf("csrf-event-review")
        cases = (
            (
                review_tests.DEBT_REVIEW,
                "acknowledge-debt",
                "review.debt_acknowledged",
            ),
            (
                review_tests.FIX_REVIEW,
                "request-human-review",
                "review.human_review_requested",
            ),
        )
        for index, (review_id, command, expected_type) in enumerate(cases, start=1):
            status, _, payload = self.post(
                f"/api/v1/reviews/{review_id}/commands/{command}",
                {"reason": "private human explanation"},
                key=f"event-review-{index}",
                csrf=csrf,
            )
            self.assertEqual(status, 202)
            operation_id = str(payload["data"]["id"])
            with closing(sqlite_connect(self.fixture.database)) as connection:
                row = connection.execute(
                    """
                    SELECT event_type, aggregate_revision, causation_id,
                           project_id, redacted_data_json
                    FROM controller_event_journal
                    WHERE aggregate_type='review' AND aggregate_id=?
                    """,
                    (review_id,),
                ).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row[0], expected_type)
            self.assertEqual(row[1], 1)
            self.assertEqual(row[2], operation_id)
            self.assertEqual(row[3], "alpha")
            self.assertNotIn("private human explanation", row[4])
            self.assertTrue(json.loads(row[4])["reason_present"])

    def test_failed_commands_leave_no_journal_event(self) -> None:
        csrf = self.csrf("csrf-event-failure")
        status, _, _ = self.post(
            "/api/v1/objectives/objective-" + "f" * 32 + "/commands/pause",
            {"reason": "missing"},
            key="event-failure-objective",
            csrf=csrf,
        )
        self.assertEqual(status, 404)
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_event_journal"
                ).fetchone()[0],
                0,
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
