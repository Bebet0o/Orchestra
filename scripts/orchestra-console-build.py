#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path

SOURCE_FILES = (
    Path("index.html"),
    Path("app.js"),
    Path("controller-client.js"),
    Path("styles.css"),
)
OUTPUT_MAP = {
    Path("index.html"): Path("index.html"),
    Path("app.js"): Path("assets/app.js"),
    Path("controller-client.js"): Path("assets/controller-client.js"),
    Path("styles.css"): Path("assets/styles.css"),
}
MAX_SOURCE_SIZE = 256 * 1024


class ConsoleBuildError(RuntimeError):
    pass


def _regular_single_link(path: Path) -> os.stat_result:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ConsoleBuildError(f"Console source is unavailable: {path.name}") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ConsoleBuildError(f"Console source must be a regular single-link file: {path.name}")
    if metadata.st_size <= 0 or metadata.st_size > MAX_SOURCE_SIZE:
        raise ConsoleBuildError(f"Console source size is invalid: {path.name}")
    return metadata


def _source_bytes(source: Path) -> dict[Path, bytes]:
    if source.is_symlink() or not source.is_dir():
        raise ConsoleBuildError("Console source directory is invalid")
    actual = {
        path.relative_to(source)
        for path in source.rglob("*")
        if path.is_file() or path.is_symlink()
    }
    expected = set(SOURCE_FILES)
    if actual != expected:
        raise ConsoleBuildError(
            f"Console source file set mismatch: missing={sorted(map(str, expected-actual))} "
            f"unexpected={sorted(map(str, actual-expected))}"
        )
    result: dict[Path, bytes] = {}
    for relative in SOURCE_FILES:
        path = source / relative
        metadata = _regular_single_link(path)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError as error:
            raise ConsoleBuildError(f"Console source cannot be opened safely: {relative}") from error
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise ConsoleBuildError(f"Console source changed type: {relative}")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                data = stream.read(MAX_SOURCE_SIZE + 1)
        finally:
            os.close(descriptor)
        if (
            opened.st_dev != metadata.st_dev
            or opened.st_ino != metadata.st_ino
            or opened.st_size != metadata.st_size
            or len(data) != opened.st_size
            or len(data) > MAX_SOURCE_SIZE
        ):
            raise ConsoleBuildError(f"Console source changed while reading: {relative}")
        if b"\0" in data:
            raise ConsoleBuildError(f"Console source contains NUL bytes: {relative}")
        try:
            data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ConsoleBuildError(f"Console source is not UTF-8: {relative}") from error
        result[relative] = data
    return result


def _manifest(files: dict[Path, bytes]) -> bytes:
    entries = {}
    for source_relative in SOURCE_FILES:
        output_relative = OUTPUT_MAP[source_relative]
        data = files[source_relative]
        entries[output_relative.as_posix()] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        }
    payload = {
        "schema_version": 1,
        "entrypoint": "index.html",
        "files": entries,
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_tree(destination: Path, files: dict[Path, bytes]) -> None:
    if destination.exists() and destination.is_symlink():
        raise ConsoleBuildError("Console output must not be a symlink")
    parent = destination.parent
    parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}.", dir=parent))
    try:
        for source_relative in SOURCE_FILES:
            output_relative = OUTPUT_MAP[source_relative]
            target = temporary / output_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(files[source_relative])
            target.chmod(0o644)
        manifest = temporary / "asset-manifest.json"
        manifest.write_bytes(_manifest(files))
        manifest.chmod(0o644)
        for directory in sorted(
            (path for path in temporary.rglob("*") if path.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            directory.chmod(0o755)
        temporary.chmod(0o755)
        backup = None
        if destination.exists():
            backup = destination.with_name(f".{destination.name}.previous")
            if backup.exists():
                shutil.rmtree(backup)
            destination.rename(backup)
        temporary.rename(destination)
        if backup is not None:
            shutil.rmtree(backup)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build(source: Path, output: Path) -> None:
    _write_tree(output, _source_bytes(source))


def tree_bytes(root: Path) -> dict[str, bytes]:
    if root.is_symlink() or not root.is_dir():
        raise ConsoleBuildError("Console distribution directory is invalid")
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ConsoleBuildError(f"Console distribution contains a symlink: {relative}")
        if path.is_file():
            metadata = path.stat()
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ConsoleBuildError(f"Console distribution file is unsafe: {relative}")
            result[relative] = path.read_bytes()
    return result


def check(source: Path, expected: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="orchestra-console-check-") as temporary:
        candidate = Path(temporary) / "dist"
        build(source, candidate)
        actual_tree = tree_bytes(candidate)
        expected_tree = tree_bytes(expected)
        if actual_tree != expected_tree:
            missing = sorted(set(expected_tree) - set(actual_tree))
            unexpected = sorted(set(actual_tree) - set(expected_tree))
            changed = sorted(
                path for path in set(actual_tree) & set(expected_tree)
                if actual_tree[path] != expected_tree[path]
            )
            raise ConsoleBuildError(
                f"Committed Console distribution is not reproducible: "
                f"missing={missing} unexpected={unexpected} changed={changed}"
            )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Build the deterministic Orchestra Console distribution")
    subparsers = result.add_subparsers(dest="command", required=True)
    build_parser = subparsers.add_parser("build")
    build_parser.add_argument("--source", type=Path, required=True)
    build_parser.add_argument("--output", type=Path, required=True)
    check_parser = subparsers.add_parser("check")
    check_parser.add_argument("--source", type=Path, required=True)
    check_parser.add_argument("--expected", type=Path, required=True)
    return result


def main() -> int:
    arguments = parser().parse_args()
    try:
        if arguments.command == "build":
            build(arguments.source.absolute(), arguments.output.absolute())
            print("ORCHESTRA_CONSOLE_BUILD_PASS")
        else:
            check(arguments.source.absolute(), arguments.expected.absolute())
            print("ORCHESTRA_CONSOLE_REPRODUCIBLE_BUILD_PASS")
    except ConsoleBuildError as error:
        print(f"Console build failed: {error}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
