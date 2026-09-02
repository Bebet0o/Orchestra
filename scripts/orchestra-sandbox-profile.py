#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from controller_api.core import ControllerError, Settings
from controller_api.sandbox_profiles import SandboxProfileStore


def emit_error(error: ControllerError, *, as_json: bool) -> int:
    payload = {
        "status": error.status,
        "code": error.code,
        "title": error.title,
        "detail": error.detail,
    }
    if error.resource is not None:
        payload["resource"] = error.resource
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"{error.code}: {error.title}", file=sys.stderr)
        if error.detail:
            print(error.detail, file=sys.stderr)
    return 1


def store() -> SandboxProfileStore:
    return SandboxProfileStore(Settings.from_environment())


def command_import(arguments: argparse.Namespace) -> int:
    try:
        result = store().import_path(arguments.path)
    except ControllerError as error:
        return emit_error(error, as_json=arguments.json)
    payload = result.as_dict()
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        profile = payload["profile"]
        print(
            "Sandbox profile import: PASS "
            f"id={profile['id']} "
            f"profile_name={profile['profile_name']} "
            f"source_revision={profile['source_revision']} "
            f"resource_revision={profile['resource_revision']} "
            f"created={str(payload['created']).lower()} "
            f"revision_created={str(payload['revision_created']).lower()}"
        )
    return 0


def command_list(arguments: argparse.Namespace) -> int:
    try:
        items, _ = store().list_profiles(
            limit=arguments.limit,
            cursor=None,
            state=arguments.state,
            cursor_secret="operator-cli-local-cursor-secret",
        )
    except ControllerError as error:
        return emit_error(error, as_json=arguments.json)
    if arguments.json:
        print(json.dumps({"profiles": items}, indent=2, sort_keys=True))
    else:
        for item in items:
            print(
                f"{item['id']}\t{item['profile_name']}\t"
                f"{item['state']}\t{item['source_revision']}"
            )
    return 0


def command_show(arguments: argparse.Namespace) -> int:
    try:
        profile = store().get_profile(arguments.sandbox_id)
    except ControllerError as error:
        return emit_error(error, as_json=arguments.json)
    if arguments.json:
        print(json.dumps(profile, indent=2, sort_keys=True))
    else:
        for key in (
            "id",
            "profile_name",
            "name",
            "state",
            "source_format",
            "source_revision",
            "source_sha256",
            "canonical_sha256",
            "resource_revision",
            "created_at",
            "updated_at",
        ):
            print(f"{key}={profile[key]}")
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Manage persisted Orchestra sandbox profile sources"
    )
    subparsers = result.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("path", type=Path)
    import_parser.add_argument("--json", action="store_true")
    import_parser.set_defaults(handler=command_import)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--state")
    list_parser.add_argument("--limit", type=int, default=50)
    list_parser.add_argument("--json", action="store_true")
    list_parser.set_defaults(handler=command_list)

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("sandbox_id")
    show_parser.add_argument("--json", action="store_true")
    show_parser.set_defaults(handler=command_show)

    return result


def main() -> None:
    arguments = parser().parse_args()
    raise SystemExit(arguments.handler(arguments))


if __name__ == "__main__":
    main()
