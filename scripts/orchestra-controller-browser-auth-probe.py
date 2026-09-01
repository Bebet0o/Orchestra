#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller_api.browser_auth_probe import BrowserAuthProbeError, probe_browser_auth


def default_password_file() -> Path:
    root = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra"))
    return root / "secrets" / "controller-initial-password"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Probe Orchestra browser session lifecycle")
    result.add_argument("--base-url", default="http://127.0.0.1:8765")
    result.add_argument("--origin", default="http://127.0.0.1:8787")
    result.add_argument("--username", default="operator")
    result.add_argument("--password-file", type=Path, default=default_password_file())
    result.add_argument("--timeout", type=float, default=3.0)
    return result


def main() -> None:
    arguments = parser().parse_args()
    try:
        result = probe_browser_auth(
            arguments.base_url,
            arguments.origin,
            arguments.username,
            arguments.password_file,
            timeout=arguments.timeout,
        )
    except (BrowserAuthProbeError, OSError) as error:
        raise SystemExit(f"browser_auth_probe_failed: {error}") from error
    print(
        "Controller browser auth probe: PASS "
        f"login={result.login_status} session={result.session_status} "
        f"csrf={result.csrf_status} logout={result.logout_status} "
        f"invalidated={result.invalidated_status}"
    )


if __name__ == "__main__":
    main()
