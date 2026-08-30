#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from controller_api.blueprint import BlueprintReport, validate_path


def emit_human(report: BlueprintReport) -> None:
    for diagnostic in report.diagnostics:
        stream = sys.stderr if diagnostic.severity == "error" else sys.stdout
        print(
            f"{diagnostic.severity.upper()} "
            f"{diagnostic.code} {diagnostic.path}: {diagnostic.message}",
            file=stream,
        )
    if report.valid and report.result is not None:
        print(
            "Hermesfile validation: PASS "
            f"name={report.result.name} "
            f"source_sha256={report.result.source_sha256} "
            f"canonical_sha256={report.result.canonical_sha256}"
        )


def command_validate(arguments: argparse.Namespace) -> int:
    report = validate_path(arguments.path)
    if arguments.json:
        print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
    else:
        emit_human(report)
    return 0 if report.valid else 1


def command_fingerprint(arguments: argparse.Namespace) -> int:
    report = validate_path(arguments.path)
    if not report.valid or report.result is None:
        if arguments.json:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            emit_human(report)
        return 1
    payload = report.result.metadata()
    if arguments.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(f"name={payload['name']}")
        print(f"source_format={payload['source_format']}")
        print(f"api_version={payload['api_version']}")
        print(f"source_sha256={payload['source_sha256']}")
        print(f"canonical_sha256={payload['canonical_sha256']}")
        print(f"canonical_size={payload['canonical_size']}")
    return 0


def _write_output(path: Path, payload: bytes, *, force: bool) -> None:
    if path.is_symlink():
        raise OSError("output path must not be a symlink")
    if path.exists() and not force:
        raise FileExistsError("output already exists; use --force to replace it")
    parent = path.parent.resolve(strict=True)
    target = parent / path.name
    temporary = parent / f".{path.name}.tmp-{os.getpid()}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(temporary, flags, 0o644)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if target.exists() and not force:
            raise FileExistsError("output already exists; use --force to replace it")
        os.replace(temporary, target)
        directory = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def command_canonicalize(arguments: argparse.Namespace) -> int:
    report = validate_path(arguments.path)
    if not report.valid or report.result is None:
        if arguments.json_errors:
            print(json.dumps(report.as_dict(), indent=2, sort_keys=True))
        else:
            emit_human(report)
        return 1
    payload = report.result.canonical_bytes + b"\n"
    if arguments.output is None:
        sys.stdout.buffer.write(payload)
        return 0
    try:
        _write_output(arguments.output, payload, force=arguments.force)
    except OSError as error:
        print(f"hermesfile_output_failed: {error}", file=sys.stderr)
        return 1
    print(
        "Hermesfile canonicalization: PASS "
        f"output={arguments.output} "
        f"canonical_sha256={report.result.canonical_sha256}"
    )
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Validate and canonicalize Hermesfile v1 sources"
    )
    subparsers = result.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate")
    validate.add_argument("path", type=Path)
    validate.add_argument("--json", action="store_true")
    validate.set_defaults(handler=command_validate)

    fingerprint = subparsers.add_parser("fingerprint")
    fingerprint.add_argument("path", type=Path)
    fingerprint.add_argument("--json", action="store_true")
    fingerprint.set_defaults(handler=command_fingerprint)

    canonicalize = subparsers.add_parser("canonicalize")
    canonicalize.add_argument("path", type=Path)
    canonicalize.add_argument("--output", type=Path)
    canonicalize.add_argument("--force", action="store_true")
    canonicalize.add_argument("--json-errors", action="store_true")
    canonicalize.set_defaults(handler=command_canonicalize)

    return result


def main() -> None:
    arguments = parser().parse_args()
    raise SystemExit(arguments.handler(arguments))


if __name__ == "__main__":
    main()
