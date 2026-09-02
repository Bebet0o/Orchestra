#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller_api.service_support import (
    ServiceSupportError,
    probe_controller,
)


def default_session_path() -> Path:
    root = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra"))
    return root / "secrets" / "controller-session"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Probe the local Orchestra Controller API"
    )
    result.add_argument(
        "--base-url",
        default="http://127.0.0.1:8765",
    )
    result.add_argument(
        "--session-file",
        type=Path,
        default=default_session_path(),
    )
    result.add_argument(
        "--wait-seconds",
        type=float,
        default=20.0,
    )
    return result


def main() -> None:
    arguments = parser().parse_args()
    try:
        result = probe_controller(
            arguments.base_url,
            arguments.session_file,
            wait_seconds=arguments.wait_seconds,
        )
    except ServiceSupportError as error:
        raise SystemExit(f"controller_probe_failed: {error}") from error

    print(
        "Controller probe: PASS "
        f"health={result.health_status} "
        f"ready={result.ready_status} "
        f"capabilities={result.capabilities_status}"
    )


if __name__ == "__main__":
    main()
