from __future__ import annotations

import json
import sqlite3
from tests.sqlite_test_support import sqlite_connect
import sys
import threading
import unittest
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_controller_api import APIFixture  # noqa: E402

DEBT_REVIEW = "review-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FIX_REVIEW = "review-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"


class ReviewCommandTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = APIFixture()

    def tearDown(self) -> None:
        self.fixture.close()

    def post(
        self,
        path: str,
        body: dict[str, object],
        *,
        key: str = "review-key-0001",
        csrf: str | None = None,
        if_match: str | None = None,
    ):
        headers = {
            "Content-Type": "application/json",
            "Idempotency-Key": key,
        }
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        if if_match is not None:
            headers["If-Match"] = if_match
        return self.fixture.request(
            "POST",
            path,
            authenticated=True,
            headers_override=headers,
            body=json.dumps(body, separators=(",", ":")).encode("utf-8"),
        )

    def csrf(self, key: str = "review-csrf-0001") -> str:
        status, _, payload = self.post(
            "/api/v1/auth/csrf", {}, key=key
        )
        self.assertEqual(status, 200)
        return str(payload["data"]["token"])

    def command(
        self,
        review_id: str,
        command: str,
        *,
        key: str,
        reason: str | None = "human decision",
    ):
        token = self.csrf("csrf-" + key)
        return self.post(
            f"/api/v1/reviews/{review_id}/commands/{command}",
            {"reason": reason},
            key=key,
            csrf=token,
        )

    def test_capabilities_advertise_only_safe_review_commands(self) -> None:
        status, _, payload = self.fixture.request(
            "GET", "/api/v1/system/capabilities", authenticated=True
        )
        self.assertEqual(status, 200)
        features = payload["data"]["features"]
        self.assertTrue(features["review_writes"])
        self.assertEqual(
            features["review_write_commands"],
            ["acknowledge-debt", "request-human-review"],
        )
        self.assertFalse(features["review_rerun"])
        self.assertFalse(features["review_write_if_match"])

    def test_acknowledge_debt_is_atomic_audited_and_redacted(self) -> None:
        status, _, payload = self.command(
            DEBT_REVIEW,
            "acknowledge-debt",
            key="ack-debt-0001",
            reason="private operator note",
        )
        self.assertEqual(status, 202)
        operation = payload["data"]
        self.assertEqual(operation["kind"], "review.acknowledge-debt")
        self.assertEqual(operation["target"], {"type": "review", "id": DEBT_REVIEW})
        self.assertTrue(operation["result"]["reason_present"])
        with closing(sqlite_connect(self.fixture.database)) as connection:
            action = connection.execute(
                "SELECT command, reason_present, status FROM controller_review_actions"
            ).fetchone()
            self.assertEqual(action, ("acknowledge-debt", 1, "RECORDED"))
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
            self.assertNotIn("private operator note", raw)

    def test_request_human_review_records_no_execution(self) -> None:
        before_review = self._review_row(FIX_REVIEW)
        status, _, payload = self.command(
            FIX_REVIEW,
            "request-human-review",
            key="human-request-01",
        )
        self.assertEqual(status, 202)
        self.assertEqual(payload["data"]["kind"], "review.request-human-review")
        self.assertEqual(self._review_row(FIX_REVIEW), before_review)
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM reviewer_executions"
                ).fetchone()[0],
                0,
            )
            self.assertEqual(
                connection.execute(
                    "SELECT event_type FROM events ORDER BY event_id DESC LIMIT 1"
                ).fetchone()[0],
                "REVIEW_HUMAN_REQUESTED",
            )

    def _review_row(self, review_id: str):
        with closing(sqlite_connect(self.fixture.database)) as connection:
            return connection.execute(
                "SELECT review_id, run_id, verdict, summary, details_json, created_at "
                "FROM review_results WHERE review_id=?",
                (review_id,),
            ).fetchone()

    def test_acknowledge_debt_rejects_other_verdicts(self) -> None:
        status, _, payload = self.command(
            FIX_REVIEW,
            "acknowledge-debt",
            key="ack-wrong-verdict",
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "review_debt_not_acknowledgeable")

    def test_if_match_is_explicitly_unavailable(self) -> None:
        token = self.csrf("csrf-review-if-match")
        status, _, payload = self.post(
            f"/api/v1/reviews/{FIX_REVIEW}/commands/request-human-review",
            {"reason": "precondition"},
            key="review-if-match-1",
            csrf=token,
            if_match='"revision-1"',
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "review_precondition_unavailable")

    def test_rerun_is_explicitly_unavailable(self) -> None:
        status, _, payload = self.command(
            FIX_REVIEW, "rerun", key="review-rerun-01"
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "review_rerun_unavailable")
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions"
                ).fetchone()[0],
                0,
            )

    def test_unknown_command_is_unavailable(self) -> None:
        status, _, payload = self.command(
            FIX_REVIEW, "merge", key="review-unknown-01"
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "review_command_unavailable")

    def test_idempotent_replay_is_identical(self) -> None:
        token = self.csrf("csrf-review-replay")
        path = f"/api/v1/reviews/{FIX_REVIEW}/commands/request-human-review"
        first = self.post(
            path, {"reason": "same"}, key="review-replay-01", csrf=token
        )
        second = self.post(
            path, {"reason": "same"}, key="review-replay-01", csrf=token
        )
        self.assertEqual(first[0], 202)
        self.assertEqual(first[2], second[2])
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions"
                ).fetchone()[0],
                1,
            )

    def test_reused_key_with_different_body_conflicts_first(self) -> None:
        token = self.csrf("csrf-review-conflict")
        path = f"/api/v1/reviews/{FIX_REVIEW}/commands/request-human-review"
        self.post(path, {"reason": "one"}, key="review-conflict-1", csrf=token)
        status, _, payload = self.post(
            path, {"reason": "two"}, key="review-conflict-1", csrf=token
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "idempotency_conflict")

    def test_same_action_with_different_key_conflicts(self) -> None:
        self.command(
            FIX_REVIEW, "request-human-review", key="review-action-01"
        )
        status, _, payload = self.command(
            FIX_REVIEW, "request-human-review", key="review-action-02"
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "review_action_already_recorded")

    def test_concurrent_different_keys_record_one_action(self) -> None:
        token = self.csrf("csrf-review-concurrent")
        path = f"/api/v1/reviews/{FIX_REVIEW}/commands/request-human-review"
        barrier = threading.Barrier(3)
        results: list[tuple[int, dict[str, object]]] = []
        lock = threading.Lock()

        def worker(key: str) -> None:
            barrier.wait()
            status, _, payload = self.post(
                path, {"reason": "concurrent"}, key=key, csrf=token
            )
            with lock:
                results.append((status, payload))

        threads = [
            threading.Thread(target=worker, args=("review-race-0001",)),
            threading.Thread(target=worker, args=("review-race-0002",)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(timeout=5)
        self.assertEqual(sorted(status for status, _ in results), [202, 409])
        self.assertEqual(
            [payload.get("code") for status, payload in results if status == 409],
            ["review_action_already_recorded"],
        )
        with closing(sqlite_connect(self.fixture.database)) as connection:
            self.assertEqual(
                connection.execute(
                    "SELECT COUNT(*) FROM controller_review_actions"
                ).fetchone()[0],
                1,
            )

    def test_unknown_review_fails_closed(self) -> None:
        status, _, payload = self.command(
            "review-cccccccccccccccccccccccccccccccc",
            "request-human-review",
            key="review-missing-01",
        )
        self.assertEqual(status, 404)
        self.assertEqual(payload["code"], "review_not_found")

    def test_reason_validation_and_unknown_fields(self) -> None:
        token = self.csrf("csrf-review-body")
        path = f"/api/v1/reviews/{FIX_REVIEW}/commands/request-human-review"
        status, _, payload = self.post(
            path, {"unknown": True}, key="review-body-0001", csrf=token
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["code"], "unknown_request_field")

    def test_review_operation_is_readable(self) -> None:
        status, _, payload = self.command(
            FIX_REVIEW,
            "request-human-review",
            key="review-operation-1",
        )
        self.assertEqual(status, 202)
        operation_id = payload["data"]["id"]
        read_status, _, read_payload = self.fixture.request(
            "GET",
            f"/api/v1/operations/{operation_id}",
            authenticated=True,
        )
        self.assertEqual(read_status, 200)
        self.assertEqual(read_payload["data"]["target"]["type"], "review")

    def test_human_verdict_cannot_be_requested_again(self) -> None:
        with closing(sqlite_connect(self.fixture.database)) as connection:
            connection.execute(
                "UPDATE review_results SET verdict='HUMAN' WHERE review_id=?",
                (FIX_REVIEW,),
            )
            connection.commit()
        status, _, payload = self.command(
            FIX_REVIEW,
            "request-human-review",
            key="already-human-01",
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["code"], "human_review_already_required")


if __name__ == "__main__":
    unittest.main(verbosity=2)
