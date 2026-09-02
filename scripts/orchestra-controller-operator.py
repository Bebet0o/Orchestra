#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import stat
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.browser_auth import BrowserAuthStore, secure_secret_file
from controller_api.core import ControllerError, Settings


def default_initial_password_path() -> Path:
    root = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra"))
    return root / "secrets" / "controller-initial-password"


def _secure_parent(path: Path) -> None:
    metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or (metadata.st_mode & 0o777) != 0o700
    ):
        raise ControllerError(
            503,
            "browser_auth_secret_parent_invalid",
            "Browser authentication secret directory is invalid",
        )


def _read_password(path: Path) -> str:
    _secure_parent(path)
    secure_secret_file(path)
    value = path.read_text(encoding="utf-8").rstrip("\r\n")
    if "\n" in value or "\r" in value:
        raise ControllerError(
            503,
            "browser_auth_secret_invalid",
            "Browser authentication secret is invalid",
        )
    return value


def _write_password(path: Path, password: str) -> None:
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
        payload = (password + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("initial password write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        directory = os.open(
            path.parent,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        secure_secret_file(path)
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        try:
            directory = os.open(
                path.parent,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            pass
        raise


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Manage the local Orchestra browser operator credential"
    )
    result.add_argument("command", choices=("ensure", "check", "set-password"))
    result.add_argument("--username", default="operator")
    result.add_argument(
        "--initial-password-file",
        type=Path,
        default=default_initial_password_path(),
    )
    return result


def main() -> None:
    arguments = parser().parse_args()
    settings = Settings.from_environment()
    store = BrowserAuthStore(settings)
    try:
        ready, reason = store.readiness()
        if arguments.command == "check":
            if not ready:
                raise ControllerError(
                    503,
                    reason,
                    "Browser authentication is unavailable",
                )
            print("Controller browser operator: valid")
            return

        if arguments.command == "ensure":
            if ready:
                print("Controller browser operator: valid")
                return
            if reason != "browser_auth_operator_unavailable":
                raise ControllerError(
                    503,
                    reason,
                    "Browser authentication is unavailable",
                )
            created_file = False
            if arguments.initial_password_file.exists() or arguments.initial_password_file.is_symlink():
                password = _read_password(arguments.initial_password_file)
            else:
                password = secrets.token_urlsafe(24)
                _write_password(arguments.initial_password_file, password)
                created_file = True
            try:
                state = store.initialize_operator(arguments.username, password)
            except Exception:
                if created_file:
                    try:
                        arguments.initial_password_file.unlink()
                    except OSError:
                        pass
                raise
            print(
                "Controller browser operator: "
                f"{state} initial_password_file={arguments.initial_password_file}"
            )
            return

        first = getpass.getpass("New Orchestra operator password: ")
        second = getpass.getpass("Confirm password: ")
        if first != second:
            raise ControllerError(400, "password_confirmation_mismatch", "Passwords do not match")
        state = store.set_password(arguments.username, first)
        try:
            if arguments.initial_password_file.exists() or arguments.initial_password_file.is_symlink():
                secure_secret_file(arguments.initial_password_file)
                arguments.initial_password_file.unlink()
        except OSError as error:
            raise ControllerError(
                503,
                "initial_password_cleanup_failed",
                "Initial password cleanup failed",
            ) from error
        print(f"Controller browser operator: password {state}; all browser sessions revoked")
    except (ControllerError, OSError, UnicodeError) as error:
        code = error.code if isinstance(error, ControllerError) else "browser_auth_operator_error"
        raise SystemExit(f"{code}: {error}") from error


if __name__ == "__main__":
    main()
