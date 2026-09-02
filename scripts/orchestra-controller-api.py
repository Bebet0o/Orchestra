#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from controller_api.core import ControllerError, ControllerService, Settings
from controller_api.server import serve


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Orchestra read-only Controller API skeleton"
    )
    subparsers = result.add_subparsers(dest="command", required=True)

    serve_parser = subparsers.add_parser("serve")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)
    serve_parser.add_argument(
        "--log-level",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        default="INFO",
    )

    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--host", default="127.0.0.1")
    check_parser.add_argument("--port", type=int, default=8765)
    check_parser.add_argument("--root", type=Path)
    check_parser.add_argument("--database", type=Path)
    check_parser.add_argument("--session-file", type=Path)
    return result


def settings_for(arguments: argparse.Namespace) -> Settings:
    if getattr(arguments, "root", None) is None:
        return Settings.from_environment(
            host=arguments.host,
            port=arguments.port,
        )
    return Settings.from_root(
        arguments.root,
        host=arguments.host,
        port=arguments.port,
        database=arguments.database,
        session_file=arguments.session_file,
    )


def main() -> None:
    arguments = parser().parse_args()
    settings = settings_for(arguments)
    try:
        if arguments.command == "serve":
            logging.basicConfig(
                level=getattr(logging, arguments.log_level),
                format=(
                    "%(asctime)s %(levelname)s "
                    "%(name)s %(message)s"
                ),
            )
            serve(settings)
            return
        service = ControllerService(settings)
        ready, reasons = service.readiness()
        print(
            json.dumps(
                {
                    "service": "orchestra-controller-api",
                    "version": service.version(),
                    "ready": ready,
                    "reasons": reasons,
                    "database": str(settings.database),
                    "host": settings.host,
                    "port": settings.port,
                },
                indent=2,
                sort_keys=True,
            )
        )
        if not ready:
            raise SystemExit(1)
    except ControllerError as error:
        raise SystemExit(f"{error.code}: {error}") from error


if __name__ == "__main__":
    main()
