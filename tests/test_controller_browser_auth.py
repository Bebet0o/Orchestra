from __future__ import annotations

import json
import sqlite3
import struct
import unittest
from contextlib import closing
from http.cookies import SimpleCookie
from pathlib import Path

from controller_api.browser_auth import BROWSER_SESSION_TOKEN_PATTERN, BrowserAuthStore
from controller_api.core import ControllerError
from tests.test_controller_api import APIFixture, TOKEN
from tests.test_controller_websocket_transport import ORIGIN, RawWebSocketClient

MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "017_browser_session_lifecycle.sql"
PASSWORD = "correct horse battery staple"


class BrowserAuthFixture:
    def __init__(self) -> None:
        self.api = APIFixture()
        with sqlite3.connect(self.api.database) as connection:
            connection.executescript(MIGRATION.read_text(encoding="utf-8"))
        self.store: BrowserAuthStore = self.api.server.service.browser_auth
        self.assert_state = self.store.initialize_operator(
            "operator",
            PASSWORD,
            scrypt_n=1024,
        )

    def close(self) -> None:
        self.api.close()

    def json_request(
        self,
        method: str,
        path: str,
        body: dict[str, object] | None = None,
        *,
        cookie: str | None = None,
        key: str | None = None,
        csrf: str | None = None,
        origin: str = ORIGIN,
    ) -> tuple[int, dict[str, str], dict[str, object] | None]:
        headers = {"Origin": origin, "User-Agent": "Orchestra-Test-Browser/1"}
        encoded = None
        if body is not None:
            encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["Content-Length"] = str(len(encoded))
        if cookie is not None:
            headers["Cookie"] = cookie
        if key is not None:
            headers["Idempotency-Key"] = key
        if csrf is not None:
            headers["X-CSRF-Token"] = csrf
        return self.api.request(
            method,
            path,
            headers_override=headers,
            body=encoded,
        )

    @staticmethod
    def cookie(headers: dict[str, str]) -> tuple[str, str]:
        raw = headers["set-cookie"]
        parsed = SimpleCookie()
        parsed.load(raw)
        token = parsed["orchestra_session"].value
        return f"orchestra_session={token}", token

    def login(self, *, key: str = "browser-login-0001", password: str = PASSWORD):
        status, headers, payload = self.json_request(
            "POST",
            "/api/v1/auth/login",
            {"username": "operator", "password": password},
            key=key,
        )
        return status, headers, payload

    def csrf(self, cookie: str, key: str = "browser-csrf-0001") -> str:
        status, _, payload = self.json_request(
            "POST",
            "/api/v1/auth/csrf",
            {},
            cookie=cookie,
            key=key,
        )
        if status != 200 or payload is None:
            raise AssertionError(f"csrf failed: {status} {payload}")
        return str(payload["data"]["token"])


class ControllerBrowserAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = BrowserAuthFixture()
        self.clients: list[RawWebSocketClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        self.fixture.close()

    def test_login_session_csrf_logout_and_cookie_contract(self) -> None:
        status, headers, payload = self.fixture.login()
        self.assertEqual(status, 200)
        self.assertIsNotNone(payload)
        self.assertTrue(payload["data"]["authenticated"])
        self.assertEqual(payload["data"]["actor_id"], "operator")
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn("orchestra_session", serialized)
        raw_cookie = headers["set-cookie"]
        for attribute in ("HttpOnly", "Secure", "SameSite=Strict", "Path=/"):
            self.assertIn(attribute, raw_cookie)
        cookie, token = self.fixture.cookie(headers)
        self.assertRegex(token, BROWSER_SESSION_TOKEN_PATTERN)

        status, session_headers, session = self.fixture.json_request(
            "GET", "/api/v1/auth/session", cookie=cookie
        )
        self.assertEqual(status, 200)
        self.assertEqual(session["data"]["actor_id"], "operator")
        self.assertEqual(session_headers["access-control-allow-origin"], ORIGIN)
        self.assertEqual(session_headers["access-control-allow-credentials"], "true")

        csrf = self.fixture.csrf(cookie)
        status, logout_headers, logout = self.fixture.json_request(
            "POST",
            "/api/v1/auth/logout",
            {},
            cookie=cookie,
            key="browser-logout-0001",
            csrf=csrf,
        )
        self.assertEqual(status, 200)
        self.assertFalse(logout["data"]["authenticated"])
        self.assertIn("Max-Age=0", logout_headers["set-cookie"])
        status, _, _ = self.fixture.json_request(
            "GET", "/api/v1/auth/session", cookie=cookie
        )
        self.assertEqual(status, 401)
        self.assertFalse(self.fixture.store.session_is_current(token, TOKEN))

    def test_login_is_idempotent_and_conflicts_on_changed_credentials(self) -> None:
        first = self.fixture.login(key="same-login-key")
        second = self.fixture.login(key="same-login-key")
        self.assertEqual(first[0], 200)
        self.assertEqual(second[0], 200)
        self.assertEqual(first[1]["set-cookie"], second[1]["set-cookie"])
        conflict = self.fixture.login(key="same-login-key", password="different password value")
        self.assertEqual(conflict[0], 409)
        self.assertEqual(conflict[2]["code"], "idempotency_key_conflict")

    def test_failed_logins_are_generic_rate_limited_and_audited(self) -> None:
        for index in range(5):
            status, _, payload = self.fixture.login(
                key=f"failed-login-{index:04d}",
                password="wrong password value",
            )
            self.assertEqual(status, 401)
            self.assertEqual(payload["code"], "authentication_required")
        status, _, payload = self.fixture.login(
            key="failed-login-blocked",
            password="wrong password value",
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "authentication_temporarily_blocked")
        with closing(sqlite3.connect(self.fixture.api.database)) as connection:
            outcomes = [
                row[0]
                for row in connection.execute(
                    "SELECT outcome FROM controller_auth_audit ORDER BY created_at"
                )
            ]
        self.assertEqual(outcomes.count("failure"), 5)
        self.assertEqual(outcomes.count("rate_limited"), 1)

    def test_raw_password_and_browser_token_are_not_persisted(self) -> None:
        status, headers, _ = self.fixture.login(key="secret-storage-check")
        self.assertEqual(status, 200)
        _, token = self.fixture.cookie(headers)
        raw = self.fixture.api.database.read_bytes()
        self.assertNotIn(PASSWORD.encode("utf-8"), raw)
        self.assertNotIn(token.encode("ascii"), raw)

    def test_bootstrap_probe_cookie_remains_compatible(self) -> None:
        status, _, payload = self.fixture.api.request(
            "GET",
            "/api/v1/auth/session",
            authenticated=True,
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["actor_id"], "controller-probe")
        status, _, payload = self.fixture.api.request(
            "GET", "/api/v1/system/capabilities", authenticated=True
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["features"]["browser_session_lifecycle"])

    def test_cors_preflight_is_exact_and_bounded(self) -> None:
        status, headers, payload = self.fixture.api.request(
            "OPTIONS",
            "/api/v1/auth/login",
            headers_override={
                "Origin": ORIGIN,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type, idempotency-key",
            },
        )
        self.assertEqual(status, 204)
        self.assertIsNone(payload)
        self.assertEqual(headers["access-control-allow-origin"], ORIGIN)
        self.assertEqual(headers["access-control-allow-credentials"], "true")
        status, _, payload = self.fixture.api.request(
            "OPTIONS",
            "/api/v1/auth/login",
            headers_override={
                "Origin": "http://127.0.0.1:9999",
                "Access-Control-Request-Method": "POST",
            },
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["code"], "origin_forbidden")

    def test_browser_websocket_is_invalidated_by_logout(self) -> None:
        status, headers, _ = self.fixture.login(key="websocket-login-key")
        self.assertEqual(status, 200)
        cookie, token = self.fixture.cookie(headers)
        client = RawWebSocketClient(self.fixture.api, token=token)
        self.clients.append(client)
        self.assertEqual(client.status, 101)
        self.assertEqual(client.read_json()["type"], "subscribed")
        csrf = self.fixture.csrf(cookie, key="websocket-csrf-key")
        status, _, _ = self.fixture.json_request(
            "POST",
            "/api/v1/auth/logout",
            {},
            cookie=cookie,
            key="websocket-logout-key",
            csrf=csrf,
        )
        self.assertEqual(status, 200)
        opcode, payload = client.read_frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1008)

    def test_password_change_revokes_all_browser_sessions(self) -> None:
        status, headers, _ = self.fixture.login(key="password-change-login")
        self.assertEqual(status, 200)
        _, token = self.fixture.cookie(headers)
        self.assertTrue(self.fixture.store.session_is_current(token, TOKEN))
        self.fixture.store.set_password(
            "operator",
            "replacement horse battery staple",
            scrypt_n=1024,
        )
        self.assertFalse(self.fixture.store.session_is_current(token, TOKEN))


class LegacyBootstrapCompatibilityTest(unittest.TestCase):
    def test_stale_bootstrap_cookie_is_401_without_browser_schema(self) -> None:
        fixture = APIFixture()
        try:
            stale = "A" * 64
            self.assertNotEqual(stale, TOKEN)
            with self.assertRaises(ControllerError) as captured:
                fixture.server.service.browser_auth.authenticate_cookie(
                    f"orchestra_session={stale}",
                    TOKEN,
                )
            self.assertEqual(captured.exception.status, 401)
            self.assertEqual(captured.exception.code, "authentication_required")
        finally:
            fixture.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
