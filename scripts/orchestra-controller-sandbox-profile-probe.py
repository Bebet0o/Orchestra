#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from controller_api.sandbox_profile_probe import probe_sandbox_profiles


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8765",
    )
    parser.add_argument(
        "--session-file",
        type=Path,
    )
    arguments = parser.parse_args()
    parsed = urlsplit(arguments.base_url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}:
        raise SystemExit("sandbox_profile_probe_requires_loopback_http")
    root = Path(
        os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
    )
    result = probe_sandbox_profiles(
        host=parsed.hostname,
        port=parsed.port or 80,
        session_file=(
            arguments.session_file
            or root / "secrets" / "controller-session"
        ),
    )
    print(
        "Controller sandbox profile probe: PASS "
        f"list={result.list_status} "
        f"capabilities={result.capabilities_status} "
        f"profiles={result.profile_count}"
    )


if __name__ == "__main__":
    main()
