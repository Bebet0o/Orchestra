#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.objective_command_probe import probe_objective_commands
from controller_api.service_support import ServiceSupportError


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe secure Controller objective mutations without dispatching work"
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
        result = probe_objective_commands(
            arguments.base_url,
            arguments.session_file,
            wait_seconds=arguments.wait_seconds,
        )
    except ServiceSupportError as error:
        parser.error(str(error))
    print(
        "Controller objective command probe: PASS "
        f"csrf={result.csrf_status} "
        f"create={result.create_status} "
        f"pause={result.pause_status} "
        f"resume={result.resume_status} "
        f"cancel={result.cancel_status} "
        f"operation={result.operation_status} "
        f"objective={result.objective_status}"
    )


if __name__ == "__main__":
    main()
