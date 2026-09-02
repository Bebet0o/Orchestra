#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.execution_probe import probe_execution_reads
from controller_api.service_support import ServiceSupportError


def display(value: int | None, empty: str) -> str:
    return str(value) if value is not None else empty


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe Controller task, run, worker, and event-log reads"
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
        result = probe_execution_reads(
            arguments.base_url,
            arguments.session_file,
            wait_seconds=arguments.wait_seconds,
        )
    except ServiceSupportError as error:
        parser.error(str(error))
    print(
        "Controller execution probe: PASS "
        f"tasks={display(result.task_list_status, 'empty')} "
        f"task={display(result.task_status, 'empty')} "
        f"runs={display(result.run_list_status, 'empty')} "
        f"run={display(result.run_status, 'none')} "
        f"logs={display(result.log_status, 'none')} "
        f"task_count={result.task_count} "
        f"run_count={result.run_count}"
    )


if __name__ == "__main__":
    main()
