#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.objective_probe import probe_objective_reads
from controller_api.service_support import ServiceSupportError


def main() -> None:
    parser = argparse.ArgumentParser(description="Probe Controller objective read endpoints")
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
        result = probe_objective_reads(
            arguments.base_url,
            arguments.session_file,
            wait_seconds=arguments.wait_seconds,
        )
    except ServiceSupportError as error:
        parser.error(str(error))
    print(
        "Controller objective probe: PASS "
        f"list={result.list_status} "
        f"detail={result.detail_status if result.detail_status is not None else 'empty'} "
        f"nested={result.nested_status if result.nested_status is not None else 'empty'} "
        f"operation={result.operation_status if result.operation_status is not None else 'none'} "
        f"count={result.objective_count}"
    )


if __name__ == "__main__":
    main()
