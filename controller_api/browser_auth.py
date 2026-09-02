from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import stat
import threading
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .core import ControllerError, SESSION_COOKIE, SESSION_VALUE_PATTERN, Settings

USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{2,63}$")
IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._~:-]{8,200}$")
SESSION_ID_PATTERN = re.compile(r"^ses_[0-9a-f]{32}$")
BROWSER_SESSION_TOKEN_PATTERN = re.compile(r"^bws_[A-Za-z0-9_-]{43}$")
AUTH_AUDIT_ID_PATTERN = re.compile(r"^auth_[0-9a-f]{32}$")
PASSWORD_MIN_CHARS = 12
PASSWORD_MAX_CHARS = 256
PASSWORD_MAX_BYTES = 1024
SESSION_TTL_SECONDS = 12 * 60 * 60
LOGIN_WINDOW_SECONDS = 60
LOGIN_MAX_FAILURES = 5
LOGIN_MAX_CONCURRENT_DERIVATIONS = 2
LOGIN_DERIVATION_WAIT_SECONDS = 0.25
SCRYPT_N = 1 << 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 64 * 1024 * 1024
REQUIRED_TABLES = {
    "controller_operator_credentials",
    "controller_browser_sessions",
    "controller_auth_idempotency",
    "controller_auth_audit",
}


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def utc_after(seconds: int) -> str:
    return (
        (datetime.now(timezone.utc) + timedelta(seconds=seconds))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or not value.isascii():
        raise ValueError("invalid base64 value")
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)


def _fingerprint(secret: str, label: str, value: str, *, length: int = 32) -> str:
    return hmac.new(
        secret.encode("ascii"),
        (label + "\0" + value).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:length]


def _validate_scrypt_parameters(n: int, r: int, p: int) -> None:
    if n < 1024 or n > 65536 or n & (n - 1):
        raise ValueError("invalid scrypt n")
    if not 1 <= r <= 16 or not 1 <= p <= 4:
        raise ValueError("invalid scrypt work factors")



def _parse_utc_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or len(value) != 24 or not value.endswith("Z"):
        raise ValueError("invalid canonical timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError("invalid canonical timestamp")
    if (
        parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        != value
    ):
        raise ValueError("invalid canonical timestamp")
    return parsed


def _validate_stored_scrypt_parameters(n: int, r: int, p: int) -> None:
    _validate_scrypt_parameters(n, r, p)
    if 128 * n * r > SCRYPT_MAXMEM:
        raise ValueError("stored scrypt parameters exceed memory bound")

def derive_password(
    password: str,
    salt: bytes,
    *,
    n: int = SCRYPT_N,
    r: int = SCRYPT_R,
    p: int = SCRYPT_P,
) -> bytes:
    _validate_password(password)
    _validate_scrypt_parameters(n, r, p)
    if not isinstance(salt, bytes) or len(salt) != 16:
        raise ValueError("invalid password salt")
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        maxmem=SCRYPT_MAXMEM,
        dklen=SCRYPT_DKLEN,
    )


def _validate_password(password: str) -> None:
    if not isinstance(password, str):
        raise ControllerError(400, "invalid_login", "Invalid login request")
    try:
        encoded = password.encode("utf-8")
    except UnicodeError as error:
        raise ControllerError(400, "invalid_login", "Invalid login request") from error
    if (
        len(password) < PASSWORD_MIN_CHARS
        or len(password) > PASSWORD_MAX_CHARS
        or len(encoded) > PASSWORD_MAX_BYTES
        or any(ord(character) < 32 for character in password)
    ):
        raise ControllerError(400, "invalid_login", "Invalid login request")


def _validate_username(username: str) -> str:
    if not isinstance(username, str) or not USERNAME_PATTERN.fullmatch(username):
        raise ControllerError(401, "authentication_required", "Authentication required")
    return username


@dataclass(frozen=True)
class AuthenticatedSession:
    secret: str
    actor_id: str
    expires_at: str | None
    session_id: str | None
    kind: str


@dataclass(frozen=True)
class LoginResult:
    payload: dict[str, Any]
    token: str
    max_age: int


class BrowserAuthStore:
    """Durable single-operator browser sessions without storing raw tokens."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._derivation_slots = threading.BoundedSemaphore(
            LOGIN_MAX_CONCURRENT_DERIVATIONS
        )

    @staticmethod
    def _credential_signature(row: sqlite3.Row | None) -> tuple[object, ...] | None:
        if row is None:
            return None
        return tuple(
            row[name]
            for name in (
                "actor_id", "username", "password_algorithm", "password_salt",
                "password_digest", "scrypt_n", "scrypt_r", "scrypt_p",
                "created_at", "updated_at",
            )
        )

    @staticmethod
    def _validate_credential_row(
        row: sqlite3.Row,
        *,
        expected_username: str | None = None,
    ) -> None:
        try:
            actor_id = str(row["actor_id"])
            username = str(row["username"])
            algorithm = str(row["password_algorithm"])
            salt = _b64decode(str(row["password_salt"]))
            digest = _b64decode(str(row["password_digest"]))
            n = int(row["scrypt_n"])
            r = int(row["scrypt_r"])
            p = int(row["scrypt_p"])
            created_at = _parse_utc_timestamp(row["created_at"])
            updated_at = _parse_utc_timestamp(row["updated_at"])
            _validate_stored_scrypt_parameters(n, r, p)
        except (KeyError, TypeError, ValueError) as error:
            raise ControllerError(
                503,
                "browser_auth_operator_invalid",
                "Browser authentication is unavailable",
            ) from error
        if (
            actor_id != "operator"
            or not USERNAME_PATTERN.fullmatch(username)
            or (expected_username is not None and username != expected_username)
            or algorithm != "scrypt"
            or len(salt) != 16
            or len(digest) != SCRYPT_DKLEN
            or updated_at < created_at
        ):
            raise ControllerError(
                503,
                "browser_auth_operator_invalid",
                "Browser authentication is unavailable",
            )

    @staticmethod
    def _session_from_row(
        row: sqlite3.Row,
        token: str,
        *,
        require_current: bool,
    ) -> AuthenticatedSession:
        try:
            session_id = str(row["session_id"])
            actor_id = str(row["actor_id"])
            created_at = _parse_utc_timestamp(row["created_at"])
            expires_at = _parse_utc_timestamp(row["expires_at"])
            revoked_at_value = row["revoked_at"]
            revoked_at = (
                None
                if revoked_at_value is None
                else _parse_utc_timestamp(revoked_at_value)
            )
        except (KeyError, TypeError, ValueError) as error:
            raise ControllerError(
                503,
                "browser_auth_session_invalid",
                "Browser authentication is unavailable",
            ) from error
        if (
            not SESSION_ID_PATTERN.fullmatch(session_id)
            or actor_id != "operator"
            or expires_at <= created_at
            or (revoked_at is not None and revoked_at < created_at)
        ):
            raise ControllerError(
                503,
                "browser_auth_session_invalid",
                "Browser authentication is unavailable",
            )
        if require_current and (
            revoked_at is not None or expires_at <= datetime.now(timezone.utc)
        ):
            raise ControllerError(401, "authentication_required", "Authentication required")
        return AuthenticatedSession(
            secret=token,
            actor_id=actor_id,
            expires_at=str(row["expires_at"]),
            session_id=session_id,
            kind="browser",
        )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.settings.database,
            timeout=5.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def readiness(self) -> tuple[bool, str]:
        if not self.settings.database.is_file():
            return False, "browser_auth_database_unavailable"
        try:
            with closing(self.connect()) as connection:
                tables = {
                    str(row[0])
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                if not REQUIRED_TABLES.issubset(tables):
                    return False, "browser_auth_schema_unavailable"
                rows = connection.execute(
                    """
                    SELECT actor_id, username, password_algorithm, password_salt,
                           password_digest, scrypt_n, scrypt_r, scrypt_p,
                           created_at, updated_at
                    FROM controller_operator_credentials
                    """
                ).fetchall()
                if len(rows) != 1:
                    return False, "browser_auth_operator_unavailable"
                self._validate_credential_row(rows[0])
        except ControllerError:
            return False, "browser_auth_operator_invalid"
        except sqlite3.Error:
            return False, "browser_auth_database_unavailable"
        return True, "ready"

    @staticmethod
    def validate_idempotency_key(value: str | None) -> str:
        if value is None or not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
            raise ControllerError(
                400,
                "invalid_idempotency_key",
                "Invalid Idempotency-Key",
            )
        return value

    @staticmethod
    def session_payload(session: AuthenticatedSession | None) -> dict[str, Any]:
        if session is None:
            return {
                "authenticated": False,
                "actor_id": None,
                "expires_at": None,
            }
        return {
            "authenticated": True,
            "actor_id": session.actor_id,
            "expires_at": session.expires_at,
        }

    def initialize_operator(
        self,
        username: str,
        password: str,
        *,
        actor_id: str = "operator",
        scrypt_n: int = SCRYPT_N,
        scrypt_r: int = SCRYPT_R,
        scrypt_p: int = SCRYPT_P,
    ) -> str:
        username = _validate_username(username)
        _validate_password(password)
        _validate_stored_scrypt_parameters(scrypt_n, scrypt_r, scrypt_p)
        salt = secrets.token_bytes(16)
        digest = derive_password(password, salt, n=scrypt_n, r=scrypt_r, p=scrypt_p)
        now = utc_now()
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    """
                    SELECT actor_id, username, password_algorithm, password_salt,
                           password_digest, scrypt_n, scrypt_r, scrypt_p,
                           created_at, updated_at
                    FROM controller_operator_credentials
                    """
                ).fetchall()
                if existing:
                    if len(existing) != 1:
                        raise ControllerError(503, "browser_auth_operator_invalid", "Browser authentication is unavailable")
                    self._validate_credential_row(existing[0], expected_username=username)
                    connection.rollback()
                    return "valid"
                connection.execute(
                    """
                    INSERT INTO controller_operator_credentials (
                        actor_id, username, password_algorithm, password_salt,
                        password_digest, scrypt_n, scrypt_r, scrypt_p,
                        created_at, updated_at
                    ) VALUES (?, ?, 'scrypt', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (actor_id, username, _b64encode(salt), _b64encode(digest), scrypt_n, scrypt_r, scrypt_p, now, now),
                )
                connection.commit()
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise ControllerError(503, "browser_auth_unavailable", "Browser authentication is unavailable") from error
        return "created"

    def set_password(
        self,
        username: str,
        password: str,
        *,
        scrypt_n: int = SCRYPT_N,
        scrypt_r: int = SCRYPT_R,
        scrypt_p: int = SCRYPT_P,
    ) -> str:
        username = _validate_username(username)
        _validate_password(password)
        _validate_scrypt_parameters(scrypt_n, scrypt_r, scrypt_p)
        salt = secrets.token_bytes(16)
        digest = derive_password(
            password,
            salt,
            n=scrypt_n,
            r=scrypt_r,
            p=scrypt_p,
        )
        now = utc_now()
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    """
                    UPDATE controller_operator_credentials
                    SET password_salt=?, password_digest=?, scrypt_n=?,
                        scrypt_r=?, scrypt_p=?, updated_at=?
                    WHERE username=?
                    """,
                    (
                        _b64encode(salt),
                        _b64encode(digest),
                        scrypt_n,
                        scrypt_r,
                        scrypt_p,
                        now,
                        username,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ControllerError(
                        404,
                        "operator_not_found",
                        "Operator account not found",
                    )
                connection.execute(
                    """
                    UPDATE controller_browser_sessions
                    SET revoked_at=?
                    WHERE revoked_at IS NULL
                    """,
                    (now,),
                )
                connection.execute(
                    """
                    INSERT INTO controller_auth_audit (
                        auth_audit_id, action, outcome, actor_id,
                        session_fingerprint, username_fingerprint,
                        source_fingerprint, request_id, created_at
                    ) VALUES (?, 'password_change', 'success', 'operator',
                              NULL, NULL, NULL, 'local-cli', ?)
                    """,
                    ("auth_" + uuid.uuid4().hex, now),
                )
                connection.commit()
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "browser_auth_unavailable",
                "Browser authentication is unavailable",
            ) from error
        return "updated"

    @staticmethod
    def _cookie_value(cookie_header: str | None) -> str:
        if not cookie_header or len(cookie_header) > 4096:
            raise ControllerError(401, "authentication_required", "Authentication required")
        values: list[str] = []
        for segment in cookie_header.split(";"):
            name, separator, value = segment.strip().partition("=")
            if separator and name == SESSION_COOKIE:
                values.append(value)
        if len(values) != 1 or not SESSION_VALUE_PATTERN.fullmatch(values[0]):
            raise ControllerError(401, "authentication_required", "Authentication required")
        return values[0]

    def authenticate_cookie(
        self,
        cookie_header: str | None,
        bootstrap_secret: str,
    ) -> AuthenticatedSession:
        supplied = self._cookie_value(cookie_header)
        if hmac.compare_digest(supplied, bootstrap_secret):
            return AuthenticatedSession(
                secret=supplied,
                actor_id="controller-probe",
                expires_at=None,
                session_id=None,
                kind="bootstrap",
            )
        if not BROWSER_SESSION_TOKEN_PATTERN.fullmatch(supplied):
            raise ControllerError(401, "authentication_required", "Authentication required")
        token_hash = hashlib.sha256(supplied.encode("ascii")).hexdigest()
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    """
                    SELECT session_id, actor_id, created_at, expires_at, revoked_at
                    FROM controller_browser_sessions
                    WHERE token_hash=?
                    """,
                    (token_hash,),
                ).fetchone()
        except sqlite3.Error as error:
            raise ControllerError(503, "browser_auth_unavailable", "Browser authentication is unavailable") from error
        if row is None:
            raise ControllerError(401, "authentication_required", "Authentication required")
        return self._session_from_row(row, supplied, require_current=True)

    def logout_context(
        self,
        cookie_header: str | None,
        bootstrap_secret: str,
    ) -> AuthenticatedSession:
        supplied = self._cookie_value(cookie_header)
        if hmac.compare_digest(supplied, bootstrap_secret):
            raise ControllerError(403, "browser_session_required", "Browser session required")
        if not BROWSER_SESSION_TOKEN_PATTERN.fullmatch(supplied):
            raise ControllerError(401, "authentication_required", "Authentication required")
        token_hash = hashlib.sha256(supplied.encode("ascii")).hexdigest()
        try:
            with closing(self.connect()) as connection:
                row = connection.execute(
                    """
                    SELECT session_id, actor_id, created_at, expires_at, revoked_at
                    FROM controller_browser_sessions
                    WHERE token_hash=?
                    """,
                    (token_hash,),
                ).fetchone()
        except sqlite3.Error as error:
            raise ControllerError(503, "browser_auth_unavailable", "Browser authentication is unavailable") from error
        if row is None:
            raise ControllerError(401, "authentication_required", "Authentication required")
        return self._session_from_row(row, supplied, require_current=False)

    def session_is_current(self, supplied: str, bootstrap_secret: str) -> bool:
        if hmac.compare_digest(supplied, bootstrap_secret):
            return True
        try:
            self.authenticate_cookie(
                f"{SESSION_COOKIE}={supplied}",
                bootstrap_secret,
            )
        except ControllerError:
            return False
        return True

    @staticmethod
    def _request_hash(secret: str, username: str, password: str) -> str:
        return hmac.new(
            secret.encode("ascii"),
            ("login-request\0" + username + "\0" + password).encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _session_token(secret: str, namespace: str, key_hash: str, request_hash: str) -> str:
        digest = hmac.new(
            secret.encode("ascii"),
            ("browser-session\0" + namespace + "\0" + key_hash + "\0" + request_hash).encode("ascii"),
            hashlib.sha256,
        ).digest()
        return "bws_" + _b64encode(digest)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        action: str,
        outcome: str,
        actor_id: str | None,
        session_fingerprint: str | None,
        username_fingerprint: str | None,
        source_fingerprint: str | None,
        request_id: str,
        created_at: str,
    ) -> None:
        audit_id = "auth_" + uuid.uuid4().hex
        if not AUTH_AUDIT_ID_PATTERN.fullmatch(audit_id):
            raise RuntimeError("invalid auth audit id")
        connection.execute(
            """
            INSERT INTO controller_auth_audit (
                auth_audit_id, action, outcome, actor_id,
                session_fingerprint, username_fingerprint,
                source_fingerprint, request_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                audit_id,
                action,
                outcome,
                actor_id,
                session_fingerprint,
                username_fingerprint,
                source_fingerprint,
                request_id,
                created_at,
            ),
        )

    @staticmethod
    @staticmethod
    def _credential_matches(row: sqlite3.Row | None, password: str) -> bool:
        if row is None:
            salt = hashlib.sha256(b"orchestra-browser-auth-dummy").digest()[:16]
            expected = hashlib.sha256(b"orchestra-browser-auth-dummy-digest").digest()
            actual = derive_password(password, salt)
            return hmac.compare_digest(actual, expected)
        BrowserAuthStore._validate_credential_row(row)
        salt = _b64decode(str(row["password_salt"]))
        expected = _b64decode(str(row["password_digest"]))
        actual = derive_password(
            password,
            salt,
            n=int(row["scrypt_n"]),
            r=int(row["scrypt_r"]),
            p=int(row["scrypt_p"]),
        )
        return hmac.compare_digest(actual, expected)

    def login(
        self,
        *,
        username: str,
        password: str,
        idempotency_key: str,
        bootstrap_secret: str,
        source: str,
        user_agent: str,
        request_id: str,
    ) -> LoginResult:
        username = _validate_username(username)
        _validate_password(password)
        idempotency_key = self.validate_idempotency_key(idempotency_key)
        namespace = _fingerprint(bootstrap_secret, "login-source", source)
        key_hash = _fingerprint(bootstrap_secret, "login-key", idempotency_key, length=64)
        request_hash = self._request_hash(bootstrap_secret, username, password)
        username_fp = _fingerprint(bootstrap_secret, "login-username", username)
        source_fp = _fingerprint(bootstrap_secret, "login-source-audit", source)
        user_agent_fp = _fingerprint(bootstrap_secret, "login-agent", user_agent or "-")

        def replay_result(connection: sqlite3.Connection) -> LoginResult | None:
            replay = connection.execute(
                "SELECT request_hash, response_status, session_id FROM controller_auth_idempotency WHERE namespace=? AND key_hash=?",
                (namespace, key_hash),
            ).fetchone()
            if replay is None:
                return None
            if not hmac.compare_digest(str(replay["request_hash"]), request_hash):
                raise ControllerError(409, "idempotency_key_conflict", "Idempotency-Key conflict")
            if int(replay["response_status"]) != 200 or replay["session_id"] is None:
                raise ControllerError(401, "authentication_required", "Authentication required")
            row = connection.execute(
                "SELECT session_id, actor_id, created_at, expires_at, revoked_at FROM controller_browser_sessions WHERE session_id=?",
                (str(replay["session_id"]),),
            ).fetchone()
            if row is None:
                raise ControllerError(503, "browser_auth_session_invalid", "Browser authentication is unavailable")
            session = self._session_from_row(row, self._session_token(bootstrap_secret, namespace, key_hash, request_hash), require_current=True)
            lifetime = int((_parse_utc_timestamp(row["expires_at"]) - _parse_utc_timestamp(row["created_at"])).total_seconds())
            if not 1 <= lifetime <= SESSION_TTL_SECONDS:
                raise ControllerError(503, "browser_auth_session_invalid", "Browser authentication is unavailable")
            return LoginResult(payload=self.session_payload(session), token=session.secret, max_age=lifetime)

        def failure_count(connection: sqlite3.Connection, threshold: str) -> int:
            return int(connection.execute(
                """
                SELECT COUNT(*) FROM controller_auth_audit
                WHERE action='login' AND outcome='failure'
                  AND source_fingerprint=?
                  AND julianday(created_at) >= julianday(?)
                """,
                (source_fp, threshold),
            ).fetchone()[0])

        try:
            with closing(self.connect()) as connection:
                replay = replay_result(connection)
                if replay is not None:
                    return replay
                threshold = (datetime.now(timezone.utc) - timedelta(seconds=LOGIN_WINDOW_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if failure_count(connection, threshold) >= LOGIN_MAX_FAILURES:
                    connection.execute("BEGIN IMMEDIATE")
                    failures = failure_count(connection, threshold)
                    if failures >= LOGIN_MAX_FAILURES:
                        recent_block = int(connection.execute(
                            """
                            SELECT COUNT(*) FROM controller_auth_audit
                            WHERE action='login' AND outcome='rate_limited'
                              AND source_fingerprint=?
                              AND julianday(created_at) >= julianday(?)
                            """,
                            (source_fp, threshold),
                        ).fetchone()[0])
                        if recent_block == 0:
                            now = utc_now()
                            self._audit(connection, action="login", outcome="rate_limited", actor_id=None, session_fingerprint=None, username_fingerprint=username_fp, source_fingerprint=source_fp, request_id=request_id, created_at=now)
                            connection.commit()
                        else:
                            connection.rollback()
                        raise ControllerError(403, "authentication_temporarily_blocked", "Authentication temporarily blocked")
                    connection.rollback()
                credential = connection.execute(
                    """
                    SELECT actor_id, username, password_algorithm, password_salt,
                           password_digest, scrypt_n, scrypt_r, scrypt_p,
                           created_at, updated_at
                    FROM controller_operator_credentials WHERE username=?
                    """,
                    (username,),
                ).fetchone()
                signature = self._credential_signature(credential)
                if not self._derivation_slots.acquire(timeout=LOGIN_DERIVATION_WAIT_SECONDS):
                    raise ControllerError(503, "browser_auth_capacity_exhausted", "Browser authentication is temporarily unavailable")
                try:
                    valid = self._credential_matches(credential, password)
                finally:
                    self._derivation_slots.release()

                connection.execute("BEGIN IMMEDIATE")
                replay = replay_result(connection)
                if replay is not None:
                    connection.rollback()
                    return replay
                now = utc_now()
                threshold = (datetime.now(timezone.utc) - timedelta(seconds=LOGIN_WINDOW_SECONDS)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                failures = failure_count(connection, threshold)
                if failures >= LOGIN_MAX_FAILURES:
                    recent_block = int(connection.execute(
                        """
                        SELECT COUNT(*) FROM controller_auth_audit
                        WHERE action='login' AND outcome='rate_limited'
                          AND source_fingerprint=?
                          AND julianday(created_at) >= julianday(?)
                        """,
                        (source_fp, threshold),
                    ).fetchone()[0])
                    if recent_block == 0:
                        self._audit(connection, action="login", outcome="rate_limited", actor_id=None, session_fingerprint=None, username_fingerprint=username_fp, source_fingerprint=source_fp, request_id=request_id, created_at=now)
                        connection.commit()
                    else:
                        connection.rollback()
                    raise ControllerError(403, "authentication_temporarily_blocked", "Authentication temporarily blocked")
                current_credential = connection.execute(
                    """
                    SELECT actor_id, username, password_algorithm, password_salt,
                           password_digest, scrypt_n, scrypt_r, scrypt_p,
                           created_at, updated_at
                    FROM controller_operator_credentials WHERE username=?
                    """,
                    (username,),
                ).fetchone()
                if self._credential_signature(current_credential) != signature:
                    connection.rollback()
                    raise ControllerError(503, "browser_auth_credential_changed", "Browser authentication is temporarily unavailable")
                if not valid:
                    connection.execute(
                        """
                        INSERT INTO controller_auth_idempotency (
                            namespace, key_hash, method, route, request_hash,
                            response_status, session_id, created_at, completed_at
                        ) VALUES (?, ?, 'POST', '/api/v1/auth/login', ?, 401, NULL, ?, ?)
                        """,
                        (namespace, key_hash, request_hash, now, now),
                    )
                    self._audit(connection, action="login", outcome="failure", actor_id=None, session_fingerprint=None, username_fingerprint=username_fp, source_fingerprint=source_fp, request_id=request_id, created_at=now)
                    connection.commit()
                    raise ControllerError(401, "authentication_required", "Authentication required")
                if current_credential is None:
                    connection.rollback()
                    raise ControllerError(503, "browser_auth_operator_invalid", "Browser authentication is unavailable")
                actor_id = str(current_credential["actor_id"])
                token = self._session_token(bootstrap_secret, namespace, key_hash, request_hash)
                token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
                session_id = "ses_" + token_hash[:32]
                expires_at = utc_after(SESSION_TTL_SECONDS)
                connection.execute(
                    """
                    INSERT INTO controller_browser_sessions (
                        session_id, token_hash, actor_id, created_at, expires_at,
                        revoked_at, source_fingerprint, user_agent_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
                    """,
                    (session_id, token_hash, actor_id, now, expires_at, source_fp, user_agent_fp),
                )
                connection.execute(
                    """
                    INSERT INTO controller_auth_idempotency (
                        namespace, key_hash, method, route, request_hash,
                        response_status, session_id, created_at, completed_at
                    ) VALUES (?, ?, 'POST', '/api/v1/auth/login', ?, 200, ?, ?, ?)
                    """,
                    (namespace, key_hash, request_hash, session_id, now, now),
                )
                self._audit(connection, action="login", outcome="success", actor_id=actor_id, session_fingerprint=token_hash[:32], username_fingerprint=username_fp, source_fingerprint=source_fp, request_id=request_id, created_at=now)
                connection.commit()
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise ControllerError(503, "browser_auth_unavailable", "Browser authentication is unavailable") from error
        return LoginResult(payload={"authenticated": True, "actor_id": actor_id, "expires_at": expires_at}, token=token, max_age=SESSION_TTL_SECONDS)

    def logout(
        self,
        *,
        session: AuthenticatedSession,
        idempotency_key: str,
        bootstrap_secret: str,
        request_id: str,
    ) -> dict[str, Any]:
        idempotency_key = self.validate_idempotency_key(idempotency_key)
        if session.kind != "browser" or session.session_id is None:
            raise ControllerError(
                403,
                "browser_session_required",
                "Browser session required",
            )
        namespace = hashlib.sha256(session.secret.encode("ascii")).hexdigest()[:32]
        key_hash = _fingerprint(session.secret, "logout-key", idempotency_key, length=64)
        request_hash = _fingerprint(session.secret, "logout-request", "{}", length=64)
        now = utc_now()
        try:
            with closing(self.connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                replay = connection.execute(
                    """
                    SELECT request_hash, response_status
                    FROM controller_auth_idempotency
                    WHERE namespace=? AND key_hash=?
                    """,
                    (namespace, key_hash),
                ).fetchone()
                if replay is not None:
                    if not hmac.compare_digest(str(replay["request_hash"]), request_hash):
                        raise ControllerError(
                            409,
                            "idempotency_key_conflict",
                            "Idempotency-Key conflict",
                        )
                    connection.rollback()
                    return self.session_payload(None)
                connection.execute(
                    """
                    UPDATE controller_browser_sessions
                    SET revoked_at=?
                    WHERE session_id=? AND revoked_at IS NULL
                    """,
                    (now, session.session_id),
                )
                connection.execute(
                    """
                    INSERT INTO controller_auth_idempotency (
                        namespace, key_hash, method, route, request_hash,
                        response_status, session_id, created_at, completed_at
                    ) VALUES (?, ?, 'POST', '/api/v1/auth/logout', ?,
                              200, ?, ?, ?)
                    """,
                    (
                        namespace,
                        key_hash,
                        request_hash,
                        session.session_id,
                        now,
                        now,
                    ),
                )
                self._audit(
                    connection,
                    action="logout",
                    outcome="success",
                    actor_id=session.actor_id,
                    session_fingerprint=namespace,
                    username_fingerprint=None,
                    source_fingerprint=None,
                    request_id=request_id,
                    created_at=now,
                )
                connection.commit()
        except ControllerError:
            raise
        except sqlite3.Error as error:
            raise ControllerError(
                503,
                "browser_auth_unavailable",
                "Browser authentication is unavailable",
            ) from error
        return self.session_payload(None)


def secure_secret_file(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (metadata.st_mode & 0o777) != 0o600
        or metadata.st_nlink != 1
    ):
        raise ControllerError(
            503,
            "browser_auth_secret_invalid",
            "Browser authentication secret is invalid",
        )
