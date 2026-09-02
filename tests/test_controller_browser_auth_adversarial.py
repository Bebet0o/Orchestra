from __future__ import annotations

import importlib.util
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import controller_api.browser_auth as browser_auth
from controller_api.browser_auth import BrowserAuthStore
from controller_api.core import ControllerError
from tests.test_controller_browser_auth import BrowserAuthFixture, PASSWORD
from tests.test_controller_websocket_transport import ORIGIN

ROOT = Path(__file__).resolve().parents[1]
MIGRATION_018 = ROOT / "migrations" / "018_browser_auth_hardening.sql"


class BrowserAuthAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = BrowserAuthFixture()
        with sqlite3.connect(self.fixture.api.database) as connection:
            connection.executescript(MIGRATION_018.read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.fixture.close()

    def _login_store(self, key: str, password: str = PASSWORD, source: str = "127.0.0.1"):
        return self.fixture.store.login(
            username="operator",
            password=password,
            idempotency_key=key,
            bootstrap_secret="A" * 64,
            source=source,
            user_agent="adversarial-test",
            request_id="request-" + key,
        )

    def test_scrypt_does_not_hold_sqlite_write_lock(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        original = browser_auth.derive_password
        result: list[BaseException | None] = []

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test derivation timeout")
            return original(*args, **kwargs)

        def worker() -> None:
            try:
                self._login_store("lock-check-0001", "wrong password value")
            except BaseException as error:
                result.append(error)
            else:
                result.append(None)

        with mock.patch.object(browser_auth, "derive_password", side_effect=blocked):
            thread = threading.Thread(target=worker)
            thread.start()
            self.assertTrue(entered.wait(2))
            connection = sqlite3.connect(self.fixture.api.database, timeout=0.25)
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.rollback()
            finally:
                connection.close()
            release.set()
            thread.join(4)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], ControllerError)
        self.assertEqual(result[0].status, 401)

    def test_concurrent_scrypt_derivations_are_bounded(self) -> None:
        entered = 0
        entered_lock = threading.Lock()
        two_entered = threading.Event()
        release = threading.Event()
        original = browser_auth.derive_password
        outcomes: list[BaseException] = []

        def blocked(*args, **kwargs):
            nonlocal entered
            with entered_lock:
                entered += 1
                if entered == 2:
                    two_entered.set()
            if not release.wait(3):
                raise RuntimeError("test derivation timeout")
            return original(*args, **kwargs)

        def worker(index: int) -> None:
            try:
                self._login_store(f"bounded-{index:04d}", source=f"source-{index}")
            except BaseException as error:
                outcomes.append(error)

        with mock.patch.object(browser_auth, "derive_password", side_effect=blocked):
            threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
            for thread in threads:
                thread.start()
            self.assertTrue(two_entered.wait(2))
            with self.assertRaises(ControllerError) as captured:
                self._login_store("bounded-third", source="source-third")
            self.assertEqual(captured.exception.status, 503)
            self.assertEqual(captured.exception.code, "browser_auth_capacity_exhausted")
            release.set()
            for thread in threads:
                thread.join(4)
                self.assertFalse(thread.is_alive())
        self.assertEqual(outcomes, [])

    def test_corrupt_credential_fails_readiness_and_login_closed(self) -> None:
        with sqlite3.connect(self.fixture.api.database) as connection:
            connection.execute(
                "UPDATE controller_operator_credentials SET scrypt_n=65536, scrypt_r=16"
            )
        ready, reason = self.fixture.store.readiness()
        self.assertFalse(ready)
        self.assertEqual(reason, "browser_auth_operator_invalid")
        with self.assertRaises(ControllerError) as captured:
            self._login_store("corrupt-credential")
        self.assertEqual(captured.exception.status, 503)
        self.assertEqual(captured.exception.code, "browser_auth_operator_invalid")

    def test_logout_replay_remains_idempotent_after_revocation(self) -> None:
        status, headers, _ = self.fixture.login(key="logout-replay-login")
        self.assertEqual(status, 200)
        cookie, _ = self.fixture.cookie(headers)
        csrf = self.fixture.csrf(cookie, key="logout-replay-csrf")
        first = self.fixture.json_request(
            "POST", "/api/v1/auth/logout", {}, cookie=cookie,
            key="logout-replay-key", csrf=csrf, origin=ORIGIN,
        )
        second = self.fixture.json_request(
            "POST", "/api/v1/auth/logout", {}, cookie=cookie,
            key="logout-replay-key", csrf=csrf, origin=ORIGIN,
        )
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[1]["set-cookie"], second[1]["set-cookie"])
        self.assertFalse(first[2]["data"]["authenticated"])
        self.assertFalse(second[2]["data"]["authenticated"])

    def test_rate_limit_does_not_self_extend_or_amplify_audit(self) -> None:
        for index in range(5):
            with self.assertRaises(ControllerError) as captured:
                self._login_store(f"rate-failure-{index}", "wrong password value")
            self.assertEqual(captured.exception.status, 401)
        for index in range(3):
            with self.assertRaises(ControllerError) as captured:
                self._login_store(f"rate-block-{index}", "wrong password value")
            self.assertEqual(captured.exception.status, 403)
        with sqlite3.connect(self.fixture.api.database) as connection:
            rows = dict(connection.execute(
                "SELECT outcome, COUNT(*) FROM controller_auth_audit GROUP BY outcome"
            ))
        self.assertEqual(rows.get("failure"), 5)
        self.assertEqual(rows.get("rate_limited"), 1)

    def test_session_immutable_fields_and_single_revocation_transition(self) -> None:
        status, headers, _ = self.fixture.login(key="session-immutability")
        self.assertEqual(status, 200)
        _, token = self.fixture.cookie(headers)
        token_hash = __import__("hashlib").sha256(token.encode("ascii")).hexdigest()
        with sqlite3.connect(self.fixture.api.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_browser_sessions SET expires_at='2099-01-01T00:00:00.000Z' WHERE token_hash=?",
                    (token_hash,),
                )
            now = browser_auth.utc_now()
            connection.execute(
                "UPDATE controller_browser_sessions SET revoked_at=? WHERE token_hash=?",
                (now, token_hash),
            )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_browser_sessions SET revoked_at=? WHERE token_hash=?",
                    (browser_auth.utc_now(), token_hash),
                )

    def test_auth_idempotency_is_immutable(self) -> None:
        self.fixture.login(key="immutable-idempotency")
        with sqlite3.connect(self.fixture.api.database) as connection:
            row = connection.execute(
                "SELECT namespace, key_hash FROM controller_auth_idempotency LIMIT 1"
            ).fetchone()
            self.assertIsNotNone(row)
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE controller_auth_idempotency SET response_status=401 WHERE namespace=? AND key_hash=?",
                    row,
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM controller_auth_idempotency WHERE namespace=? AND key_hash=?",
                    row,
                )

    def test_noncanonical_auth_timestamp_is_rejected(self) -> None:
        with sqlite3.connect(self.fixture.api.database) as connection:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """
                    INSERT INTO controller_auth_audit (
                        auth_audit_id, action, outcome, actor_id,
                        session_fingerprint, username_fingerprint,
                        source_fingerprint, request_id, created_at
                    ) VALUES (?, 'login', 'failure', NULL, NULL, NULL, NULL, ?, ?)
                    """,
                    ("auth_" + "a" * 32, "timestamp-test", "2026-01-01 00:00:00"),
                )

    def test_partial_initial_password_file_is_removed(self) -> None:
        script = ROOT / "scripts" / "orchestra-controller-operator.py"
        spec = importlib.util.spec_from_file_location("operator_script", script)
        self.assertIsNotNone(spec)
        self.assertIsNotNone(spec.loader)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            parent.chmod(0o700)
            target = parent / "initial-password"
            calls = 0
            original_write = os.write

            def failing_write(descriptor: int, payload: bytes) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return original_write(descriptor, payload[:1])
                raise OSError("injected write failure")

            with mock.patch.object(module.os, "write", side_effect=failing_write):
                with self.assertRaises(OSError):
                    module._write_password(target, "correct horse battery staple")
            self.assertFalse(target.exists())
            self.assertFalse(target.is_symlink())


if __name__ == "__main__":
    unittest.main(verbosity=2)
