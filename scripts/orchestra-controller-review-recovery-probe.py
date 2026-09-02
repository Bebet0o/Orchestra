#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.review_recovery_probe import probe_review_recovery_reads
from controller_api.service_support import ServiceSupportError


def display(value: int | None, empty: str) -> str:
    return str(value) if value is not None else empty


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe Controller review, evidence, integration, and recovery reads"
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
        result = probe_review_recovery_reads(
            arguments.base_url,
            arguments.session_file,
            wait_seconds=arguments.wait_seconds,
        )
    except ServiceSupportError as error:
        parser.error(str(error))

    print(
        "Controller review/recovery probe: PASS "
        f"reviews={result.review_list_status} "
        f"review={display(result.review_status, 'empty')} "
        f"evidence={display(result.evidence_status, 'empty')} "
        f"recoveries={result.recovery_list_status} "
        f"recovery={display(result.recovery_status, 'empty')} "
        f"review_count={result.review_count} "
        f"recovery_count={result.recovery_count}"
    )


if __name__ == "__main__":
    main()
