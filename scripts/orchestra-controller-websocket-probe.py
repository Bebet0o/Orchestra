#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from controller_api.service_support import read_session
from controller_api.websocket_probe import WebSocketProbeError, probe_websocket


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Probe the local Orchestra Controller WebSocket transport"
    )
    result.add_argument("--host", default="127.0.0.1")
    result.add_argument("--port", type=int, default=8765)
    result.add_argument(
        "--origin",
        default=os.environ.get(
            "ORCHESTRA_CONTROLLER_CONSOLE_ORIGIN",
            "http://127.0.0.1:8787",
        ),
    )
    result.add_argument(
        "--session-file",
        type=Path,
        default=Path(
            os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
        ) / "secrets" / "controller-session",
    )
    result.add_argument("--timeout", type=float, default=3.0)
    return result


def main() -> None:
    arguments = parser().parse_args()
    try:
        token = read_session(arguments.session_file)
        result = probe_websocket(
            arguments.host,
            arguments.port,
            token=token,
            origin=arguments.origin,
            timeout=arguments.timeout,
        )
    except (WebSocketProbeError, RuntimeError) as error:
        raise SystemExit(f"controller_websocket_probe_failed: {error}") from error
    print(
        "Controller WebSocket probe: PASS "
        f"status={result.status} "
        f"replay_from={result.replay_from} "
        f"latest_sequence={result.latest_sequence}"
    )


if __name__ == "__main__":
    main()
