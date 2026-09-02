#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller_api.service_support import (
    ServiceSupportError,
    ensure_session,
    read_session,
    rotate_session,
)


def default_path() -> Path:
    root = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra"))
    return root / "secrets" / "controller-session"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Manage the local Orchestra Controller session file"
    )
    result.add_argument(
        "command",
        choices=("ensure", "check", "rotate"),
    )
    result.add_argument("--path", type=Path, default=default_path())
    return result


def main() -> None:
    arguments = parser().parse_args()
    try:
        if arguments.command == "ensure":
            state = ensure_session(arguments.path)
        elif arguments.command == "rotate":
            state = rotate_session(arguments.path)
        else:
            read_session(arguments.path)
            state = "valid"
    except ServiceSupportError as error:
        raise SystemExit(f"controller_session_invalid: {error}") from error

    print(f"Controller session: {state}")


if __name__ == "__main__":
    main()
