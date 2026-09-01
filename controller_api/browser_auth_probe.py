from __future__ import annotations

import http.client
import json
import os
import secrets
from dataclasses import dataclass
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

from .browser_auth import secure_secret_file


class BrowserAuthProbeError(RuntimeError):
    pass


@dataclass(frozen=True)
class BrowserAuthProbeResult:
    login_status: int
    session_status: int
    csrf_status: int
    logout_status: int
    invalidated_status: int


def _read_password(path: Path) -> str:
    secure_secret_file(path)
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if not value or "\n" in value or "\r" in value:
        raise BrowserAuthProbeError("Initial password file is invalid.")
    return value


def _request(
    host: str,
    port: int,
    method: str,
    path: str,
    *,
    origin: str,
    body: dict[str, object] | None = None,
    cookie: str | None = None,
    idempotency_key: str | None = None,
    csrf: str | None = None,
    timeout: float = 3.0,
) -> tuple[int, list[tuple[str, str]], dict[str, object]]:
    encoded = None
    headers = {
        "Accept": "application/json",
        "Origin": origin,
        "User-Agent": "Orchestra-Browser-Auth-Probe/1",
    }
    if body is not None:
        encoded = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(encoded))
    if cookie is not None:
        headers["Cookie"] = cookie
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read(64 * 1024 + 1)
        if len(raw) > 64 * 1024:
            raise BrowserAuthProbeError("Browser auth probe response is too large.")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise BrowserAuthProbeError("Browser auth probe received invalid JSON.") from error
        if not isinstance(payload, dict):
            raise BrowserAuthProbeError("Browser auth probe response is not an object.")
        return response.status, response.getheaders(), payload
    finally:
        connection.close()


def _cookie_from_headers(headers: list[tuple[str, str]]) -> str:
    values = [value for name, value in headers if name.lower() == "set-cookie"]
    if len(values) != 1:
        raise BrowserAuthProbeError("Login did not return one session cookie.")
    parsed = SimpleCookie()
    parsed.load(values[0])
    morsel = parsed.get("orchestra_session")
    if morsel is None or not morsel.value:
        raise BrowserAuthProbeError("Login session cookie is missing.")
    return f"orchestra_session={morsel.value}"


def probe_browser_auth(
    base_url: str,
    origin: str,
    username: str,
    password_file: Path,
    *,
    timeout: float = 3.0,
) -> BrowserAuthProbeResult:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise BrowserAuthProbeError("Browser auth probe requires loopback HTTP.")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise BrowserAuthProbeError("Browser auth probe base URL must not contain a path.")
    host = parsed.hostname
    port = parsed.port or 80
    password = _read_password(password_file)
    nonce = secrets.token_hex(8)

    login_status, login_headers, login = _request(
        host,
        port,
        "POST",
        "/api/v1/auth/login",
        origin=origin,
        body={"username": username, "password": password},
        idempotency_key=f"browser-login-{nonce}",
        timeout=timeout,
    )
    if login_status != 200 or login.get("data", {}).get("authenticated") is not True:
        raise BrowserAuthProbeError(f"Browser login failed with HTTP {login_status}.")
    serialized = json.dumps(login, sort_keys=True)
    if password in serialized or "orchestra_session" in serialized:
        raise BrowserAuthProbeError("Login response leaked authentication material.")
    cookie = _cookie_from_headers(login_headers)

    session_status, _, session = _request(
        host,
        port,
        "GET",
        "/api/v1/auth/session",
        origin=origin,
        cookie=cookie,
        timeout=timeout,
    )
    if session_status != 200 or session.get("data", {}).get("actor_id") != "operator":
        raise BrowserAuthProbeError("Browser session lookup failed.")

    csrf_status, _, csrf_payload = _request(
        host,
        port,
        "POST",
        "/api/v1/auth/csrf",
        origin=origin,
        body={},
        cookie=cookie,
        idempotency_key=f"browser-csrf-{nonce}",
        timeout=timeout,
    )
    csrf = csrf_payload.get("data", {}).get("token")
    if csrf_status != 200 or not isinstance(csrf, str) or not csrf:
        raise BrowserAuthProbeError("Browser CSRF challenge failed.")

    logout_status, logout_headers, logout = _request(
        host,
        port,
        "POST",
        "/api/v1/auth/logout",
        origin=origin,
        body={},
        cookie=cookie,
        idempotency_key=f"browser-logout-{nonce}",
        csrf=csrf,
        timeout=timeout,
    )
    if logout_status != 200 or logout.get("data", {}).get("authenticated") is not False:
        raise BrowserAuthProbeError("Browser logout failed.")
    clear_values = [value for name, value in logout_headers if name.lower() == "set-cookie"]
    if len(clear_values) != 1 or "Max-Age=0" not in clear_values[0]:
        raise BrowserAuthProbeError("Browser logout did not clear the cookie.")

    invalidated_status, _, _ = _request(
        host,
        port,
        "GET",
        "/api/v1/auth/session",
        origin=origin,
        cookie=cookie,
        timeout=timeout,
    )
    if invalidated_status != 401:
        raise BrowserAuthProbeError("Logged-out browser session is still accepted.")

    return BrowserAuthProbeResult(
        login_status=login_status,
        session_status=session_status,
        csrf_status=csrf_status,
        logout_status=logout_status,
        invalidated_status=invalidated_status,
    )
