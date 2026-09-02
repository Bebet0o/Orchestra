from __future__ import annotations

import http.client
import ipaddress
import json
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .core import SESSION_VALUE_PATTERN

MAX_SESSION_FILE_BYTES = 512
MAX_RESPONSE_BYTES = 64 * 1024


class ServiceSupportError(RuntimeError):
    """Expected session or probe failure."""


@dataclass(frozen=True)
class ProbeResult:
    health_status: int
    ready_status: int
    capabilities_status: int


def _secure_parent(path: Path) -> None:
    parent = path.parent
    try:
        metadata = parent.lstat()
    except OSError as error:
        raise ServiceSupportError(
            f"Controller session directory unavailable: {parent}"
        ) from error

    if not stat.S_ISDIR(metadata.st_mode):
        raise ServiceSupportError(
            "Controller session parent must be a directory."
        )
    if metadata.st_uid != os.geteuid():
        raise ServiceSupportError(
            "Controller session directory must be owned by the service user."
        )
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        raise ServiceSupportError(
            "Controller session directory must have mode 0700."
        )


def read_session(path: Path) -> str:
    _secure_parent(path)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as error:
        raise ServiceSupportError(
            "Controller session file does not exist."
        ) from error
    except OSError as error:
        raise ServiceSupportError(
            "Controller session file cannot be opened safely."
        ) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ServiceSupportError(
                "Controller session path must be a regular file."
            )
        if metadata.st_uid != os.geteuid():
            raise ServiceSupportError(
                "Controller session file must be owned by the service user."
            )
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise ServiceSupportError(
                "Controller session file must have mode 0600."
            )
        if metadata.st_nlink != 1:
            raise ServiceSupportError(
                "Controller session file must not have additional hard links."
            )
        if metadata.st_size > MAX_SESSION_FILE_BYTES:
            raise ServiceSupportError(
                "Controller session file is too large."
            )
        with os.fdopen(
            descriptor,
            "r",
            encoding="ascii",
            closefd=False,
        ) as stream:
            token = stream.read(MAX_SESSION_FILE_BYTES + 1).strip()
    except UnicodeError as error:
        raise ServiceSupportError(
            "Controller session value must be ASCII."
        ) from error
    finally:
        os.close(descriptor)

    if not SESSION_VALUE_PATTERN.fullmatch(token):
        raise ServiceSupportError(
            "Controller session value has an invalid format."
        )
    return token


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_new_session(path: Path, token: str) -> None:
    _secure_parent(path)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)

    descriptor = -1
    created = False
    try:
        descriptor = os.open(path, flags, 0o600)
        created = True
        os.fchmod(descriptor, 0o600)

        payload = (token + "\n").encode("ascii")
        written = 0
        while written < len(payload):
            written += os.write(descriptor, payload[written:])

        os.fsync(descriptor)

        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
        ):
            raise ServiceSupportError(
                "New Controller session file failed validation."
            )
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
            descriptor = -1
        if created:
            try:
                path.unlink()
                _fsync_directory(path.parent)
            except OSError:
                pass
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    _fsync_directory(path.parent)
    read_session(path)


def ensure_session(path: Path) -> str:
    _secure_parent(path)
    try:
        read_session(path)
    except ServiceSupportError as error:
        if path.exists() or path.is_symlink():
            raise
        token = secrets.token_urlsafe(48)
        try:
            _write_new_session(path, token)
        except FileExistsError:
            read_session(path)
            return "valid"
        return "created"
    return "valid"


def rotate_session(path: Path) -> str:
    _secure_parent(path)
    if path.exists() or path.is_symlink():
        read_session(path)

    temporary = path.with_name(
        f".{path.name}.rotate-{os.getpid()}-{secrets.token_hex(8)}"
    )
    token = secrets.token_urlsafe(48)
    _write_new_session(temporary, token)
    try:
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    read_session(path)
    return "rotated"


def _validated_base_url(base_url: str) -> tuple[str, int]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        raise ServiceSupportError(
            "Controller probe accepts plain HTTP on loopback only."
        )
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ServiceSupportError(
            "Controller probe base URL must not contain a path or query."
        )
    host = parsed.hostname
    if host is None:
        raise ServiceSupportError("Controller probe host is missing.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError as error:
        raise ServiceSupportError(
            "Controller probe host must be a literal loopback IP."
        ) from error
    if not address.is_loopback:
        raise ServiceSupportError(
            "Controller probe host must be a literal loopback IP."
        )
    try:
        port = parsed.port or 80
    except ValueError as error:
        raise ServiceSupportError("Controller probe port is invalid.") from error
    if not 1 <= port <= 65535:
        raise ServiceSupportError("Controller probe port is invalid.")
    return host, port


def _request(
    host: str,
    port: int,
    path: str,
    *,
    token: str | None = None,
    timeout: float = 2.0,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Cookie"] = f"orchestra_session={token}"
    try:
        connection.request("GET", path, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ServiceSupportError(
                f"Controller probe response is too large for {path}."
            )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ServiceSupportError(
                f"Controller probe received invalid JSON from {path}."
            ) from error
        if not isinstance(payload, dict):
            raise ServiceSupportError(
                f"Controller probe received a non-object response from {path}."
            )
        return response.status, payload
    finally:
        connection.close()


def probe_controller(
    base_url: str,
    session_file: Path,
    *,
    wait_seconds: float = 20.0,
) -> ProbeResult:
    if not 0.0 <= wait_seconds <= 300.0:
        raise ServiceSupportError(
            "Controller probe wait must be between 0 and 300 seconds."
        )
    host, port = _validated_base_url(base_url)
    token = read_session(session_file)
    deadline = time.monotonic() + wait_seconds
    last_error: Exception | None = None

    while True:
        try:
            health_status, health = _request(host, port, "/health")
            if health_status != 200 or health.get("status") != "ok":
                raise ServiceSupportError("Controller /health is not healthy.")

            ready_status, ready = _request(host, port, "/ready")
            if ready_status != 200 or ready.get("status") != "ready":
                raise ServiceSupportError("Controller /ready is not ready.")

            capabilities_status, capabilities = _request(
                host,
                port,
                "/api/v1/system/capabilities",
                token=token,
            )
            data = capabilities.get("data")
            if (
                capabilities_status != 200
                or not isinstance(data, dict)
                or "api_versions" not in data
            ):
                raise ServiceSupportError(
                    "Controller authenticated capability probe failed."
                )

            return ProbeResult(
                health_status=health_status,
                ready_status=ready_status,
                capabilities_status=capabilities_status,
            )
        except (
            OSError,
            http.client.HTTPException,
            ServiceSupportError,
        ) as error:
            last_error = error
            if time.monotonic() >= deadline:
                raise ServiceSupportError(
                    f"Controller probe failed: {error}"
                ) from error
            time.sleep(0.25)
