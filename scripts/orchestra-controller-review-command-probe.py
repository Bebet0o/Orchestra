#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.review_command_probe import probe_review_commands
from controller_api.service_support import ServiceSupportError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe bounded human review commands without reruns or integration"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument(
        "--session-file",
        type=Path,
        default=Path(
            os.environ.get(
                "ORCHESTRA_CONTROLLER_SESSION_FILE",
                "/opt/orchestra/secrets/controller-session",
            )
        ),
    )
    parser.add_argument("--wait-seconds", type=float, default=10)
    arguments = parser.parse_args()
    try:
        result = probe_review_commands(
            arguments.base_url,
            arguments.session_file,
            wait_seconds=arguments.wait_seconds,
        )
    except ServiceSupportError as error:
        parser.error(str(error))
    print(
        "Controller review command probe: PASS "
        f"csrf={result.csrf_status} "
        f"command={result.command_status} "
        f"operation={result.operation_status} "
        f"review={result.review_status}"
    )


if __name__ == "__main__":
    main()
