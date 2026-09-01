#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn


ROOT = Path(
    os.environ.get(
        "ORCHESTRA_ROOT",
        "/opt/orchestra",
    )
).resolve()

DATABASE = Path(
    os.environ.get(
        "ORCHESTRA_DB",
        str(ROOT / "state/controller/orchestra.db"),
    )
).resolve()

BACKUPS_ROOT = ROOT / "backups" / "transactions"
WORKTREES_ROOT = ROOT / "workspaces" / ".orchestra-worktrees"


class TransactionError(RuntimeError):
    pass


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def fail(message: str) -> NoReturn:
    raise TransactionError(message)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(
        DATABASE,
        timeout=10,
    )

    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")

    return connection


def run_command(
    arguments: list[str],
    *,
    check: bool = True,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        check=False,
    )

    if check and result.returncode != 0:
        stdout = result.stdout
        stderr = result.stderr

        if isinstance(stdout, bytes):
            stdout = stdout.decode(errors="replace")

        if isinstance(stderr, bytes):
            stderr = stderr.decode(errors="replace")

        fail(
            "Command failed: "
            + " ".join(arguments)
            + f"\nstdout:\n{stdout}"
            + f"\nstderr:\n{stderr}"
        )

    return result


def git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> str:
    result = run_command(
        ["git", "-C", str(repository), *arguments],
        check=check,
    )

    return result.stdout.strip()


def git_bytes(
    repository: Path,
    *arguments: str,
) -> bytes:
    result = run_command(
        ["git", "-C", str(repository), *arguments],
        text=False,
    )

    return result.stdout


def sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(
            lambda: stream.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary = path.with_name(
        path.name + ".tmp"
    )

    with temporary.open("wb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())

    temporary.replace(path)
    path.chmod(0o640)


def write_json(path: Path, payload: Any) -> None:
    write_bytes(
        path,
        (
            json.dumps(
                payload,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        ).encode(),
    )


def ensure_within(
    path: Path,
    allowed_root: Path,
) -> None:
    try:
        path.resolve().relative_to(
            allowed_root.resolve()
        )
    except ValueError as error:
        fail(
            f"Path {path} escapes {allowed_root}"
        )


def current_owner() -> str:
    return (
        f"{socket.gethostname()}:"
        f"uid={os.getuid()}:"
        f"pid={os.getpid()}"
    )


def load_project(
    connection: sqlite3.Connection,
    project_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            project_id,
            display_name,
            repo_path,
            data_path,
            enabled,
            config_source
        FROM projects
        WHERE project_id = ?
        """,
        (project_id,),
    ).fetchone()

    if row is None:
        fail(f"Unknown project: {project_id}")

    source = Path(row["config_source"])

    if not source.is_file():
        fail(
            f"Project configuration is absent: {source}"
        )

    with source.open("rb") as stream:
        document = tomllib.load(stream)

    git_configuration = document.get("git") or {}
    default_branch = git_configuration.get(
        "default_branch"
    )

    if not isinstance(default_branch, str):
        fail(
            f"{source}: git.default_branch is absent"
        )

    result = dict(row)
    result["default_branch"] = default_branch

    return result


def add_event(
    connection: sqlite3.Connection,
    *,
    project_id: str | None,
    run_id: str | None,
    event_type: str,
    severity: str = "INFO",
    payload: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO events (
            project_id,
            run_id,
            task_id,
            event_type,
            severity,
            payload_json,
            created_at
        )
        VALUES (?, ?, NULL, ?, ?, ?, ?)
        """,
        (
            project_id,
            run_id,
            event_type,
            severity,
            json.dumps(payload or {}, sort_keys=True),
            utc_now(),
        ),
    )


def validate_repository(
    project: dict[str, Any],
) -> tuple[Path, str]:
    repository = Path(
        project["repo_path"]
    ).resolve()

    ensure_within(
        repository,
        ROOT / "workspaces",
    )

    if not repository.is_dir():
        fail(
            f"Repository is absent: {repository}"
        )

    top_level = Path(
        git(repository, "rev-parse", "--show-toplevel")
    ).resolve()

    if top_level != repository:
        fail(
            f"Unexpected Git top-level: {top_level}"
        )

    status = git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    if status:
        fail(
            f"Repository is not clean:\n{status}"
        )

    branch = git(
        repository,
        "branch",
        "--show-current",
    )

    if branch != project["default_branch"]:
        fail(
            f"Repository branch is {branch!r}; "
            f"expected {project['default_branch']!r}"
        )

    base_commit = git(
        repository,
        "rev-parse",
        "HEAD",
    )

    return repository, base_commit


def worktree_registered(
    repository: Path,
    worktree: Path,
) -> bool:
    listing = git(
        repository,
        "worktree",
        "list",
        "--porcelain",
    )

    expected = str(worktree.resolve())

    return any(
        line == f"worktree {expected}"
        for line in listing.splitlines()
    )


def branch_exists(
    repository: Path,
    branch_name: str,
) -> bool:
    result = run_command(
        [
            "git",
            "-C",
            str(repository),
            "show-ref",
            "--verify",
            "--quiet",
            f"refs/heads/{branch_name}",
        ],
        check=False,
    )

    return result.returncode == 0


def cleanup_worktree(
    repository: Path,
    worktree: Path,
    branch_name: str,
) -> None:
    if worktree_registered(
        repository,
        worktree,
    ):
        run_command(
            [
                "git",
                "-C",
                str(repository),
                "worktree",
                "unlock",
                str(worktree),
            ],
            check=False,
        )

        run_command(
            [
                "git",
                "-C",
                str(repository),
                "worktree",
                "remove",
                "--force",
                str(worktree),
            ],
            check=True,
        )
    elif worktree.exists():
        shutil.rmtree(worktree)

    run_command(
        [
            "git",
            "-C",
            str(repository),
            "worktree",
            "prune",
        ],
        check=True,
    )

    if branch_exists(
        repository,
        branch_name,
    ):
        run_command(
            [
                "git",
                "-C",
                str(repository),
                "branch",
                "-D",
                branch_name,
            ],
            check=True,
        )

    parent = worktree.parent

    while (
        parent != WORKTREES_ROOT
        and parent.exists()
    ):
        try:
            parent.rmdir()
        except OSError:
            break

        parent = parent.parent


def create_snapshot(
    *,
    repository: Path,
    project_id: str,
    run_id: str,
    snapshot_id: str,
    base_commit: str,
    branch_name: str,
    worktree_path: Path,
) -> dict[str, Any]:
    directory = (
        BACKUPS_ROOT
        / project_id
        / run_id
    )

    if directory.exists():
        fail(
            f"Snapshot directory already exists: {directory}"
        )

    directory.mkdir(
        parents=True,
        mode=0o750,
    )

    bundle_path = directory / "repository.bundle"
    patch_path = directory / "working-tree.patch"
    status_path = directory / "status.porcelain-v2"
    refs_path = directory / "refs.txt"
    manifest_path = directory / "manifest.json"

    run_command(
        [
            "git",
            "-C",
            str(repository),
            "bundle",
            "create",
            str(bundle_path),
            "--all",
        ]
    )

    bundle_path.chmod(0o640)

    run_command(
        [
            "git",
            "-C",
            str(repository),
            "bundle",
            "verify",
            str(bundle_path),
        ]
    )

    write_bytes(
        patch_path,
        git_bytes(
            repository,
            "diff",
            "--binary",
            "HEAD",
        ),
    )

    write_bytes(
        status_path,
        git_bytes(
            repository,
            "status",
            "--porcelain=v2",
            "--branch",
            "--untracked-files=all",
        ),
    )

    write_bytes(
        refs_path,
        git_bytes(
            repository,
            "show-ref",
            "--head",
        ),
    )

    hashes = {
        "bundle_sha256": sha256(bundle_path),
        "patch_sha256": sha256(patch_path),
        "status_sha256": sha256(status_path),
        "refs_sha256": sha256(refs_path),
    }

    manifest = {
        "schema_version": 1,
        "snapshot_id": snapshot_id,
        "project_id": project_id,
        "run_id": run_id,
        "repository": str(repository),
        "base_commit": base_commit,
        "branch_name": branch_name,
        "worktree_path": str(worktree_path),
        "created_at": utc_now(),
        "artifacts": {
            "bundle": str(bundle_path),
            "patch": str(patch_path),
            "status": str(status_path),
            "refs": str(refs_path),
        },
        "hashes": hashes,
    }

    write_json(
        manifest_path,
        manifest,
    )

    hashes["manifest_sha256"] = sha256(
        manifest_path
    )

    return {
        "snapshot_id": snapshot_id,
        "project_id": project_id,
        "run_id": run_id,
        "base_commit": base_commit,
        "bundle_path": str(bundle_path),
        "patch_path": str(patch_path),
        "status_path": str(status_path),
        "refs_path": str(refs_path),
        "manifest_path": str(manifest_path),
        **hashes,
        "created_at": manifest["created_at"],
    }


def verify_snapshot(
    connection: sqlite3.Connection,
    run_id: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT
            s.*,
            p.repo_path
        FROM snapshots AS s
        JOIN projects AS p
          ON p.project_id = s.project_id
        WHERE s.run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        fail(
            f"Snapshot not found for run: {run_id}"
        )

    checks = (
        ("bundle_path", "bundle_sha256"),
        ("patch_path", "patch_sha256"),
        ("status_path", "status_sha256"),
        ("refs_path", "refs_sha256"),
        ("manifest_path", "manifest_sha256"),
    )

    for path_column, hash_column in checks:
        path = Path(row[path_column])

        if not path.is_file():
            fail(
                f"Snapshot artifact is absent: {path}"
            )

        actual = sha256(path)
        expected = row[hash_column]

        if actual != expected:
            fail(
                f"Snapshot hash mismatch for {path}: "
                f"{actual} != {expected}"
            )

    repository = Path(row["repo_path"])

    run_command(
        [
            "git",
            "-C",
            str(repository),
            "bundle",
            "verify",
            row["bundle_path"],
        ]
    )

    now = utc_now()

    connection.execute(
        """
        UPDATE snapshots
        SET verified = 1,
            verified_at = ?
        WHERE run_id = ?
        """,
        (now, run_id),
    )

    return {
        "run_id": run_id,
        "snapshot_id": row["snapshot_id"],
        "verified": True,
        "verified_at": now,
        "bundle": row["bundle_path"],
    }


def get_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
            r.*,
            p.repo_path,
            p.project_id AS joined_project_id
        FROM runs AS r
        JOIN projects AS p
          ON p.project_id = r.project_id
        WHERE r.run_id = ?
        """,
        (run_id,),
    ).fetchone()

    if row is None:
        fail(f"Unknown run: {run_id}")

    return row


def command_begin(arguments: argparse.Namespace) -> None:
    run_id = (
        "run-"
        + datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        + "-"
        + uuid.uuid4().hex[:10]
    )

    snapshot_id = "snapshot-" + uuid.uuid4().hex
    owner = arguments.owner or current_owner()

    try:
        metadata = json.loads(
            arguments.metadata_json
        )
    except json.JSONDecodeError as error:
        fail(
            f"Invalid metadata JSON: {error}"
        )

    if not isinstance(metadata, dict):
        fail("Metadata must be a JSON object")

    repository: Path | None = None
    worktree_path: Path | None = None
    branch_name = f"orchestra/run/{run_id}"
    base_commit = ""

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        project = load_project(
            connection,
            arguments.project,
        )

        if not project["enabled"]:
            connection.rollback()
            fail(
                f"Project is disabled: {arguments.project}"
            )

        existing_lock = connection.execute(
            """
            SELECT run_id, holder, heartbeat_at
            FROM project_locks
            WHERE project_id = ?
            """,
            (arguments.project,),
        ).fetchone()

        if existing_lock is not None:
            connection.rollback()
            fail(
                "Project is already locked by "
                f"run={existing_lock['run_id']} "
                f"holder={existing_lock['holder']}"
            )

        repository, base_commit = (
            validate_repository(project)
        )

        worktree_path = (
            WORKTREES_ROOT
            / arguments.project
            / run_id
        ).resolve()

        ensure_within(
            worktree_path,
            WORKTREES_ROOT,
        )

        now = utc_now()

        connection.execute(
            """
            INSERT INTO runs (
                run_id,
                project_id,
                status,
                recovery_decision,
                base_commit,
                result_commit,
                worktree_path,
                metadata_json,
                created_at,
                started_at,
                finished_at,
                heartbeat_at,
                branch_name,
                snapshot_id,
                transaction_owner,
                submitted_at
            )
            VALUES (
                ?, ?, 'SNAPSHOTTING', NULL,
                ?, NULL, ?, ?, ?, ?, NULL, ?,
                ?, ?, ?, NULL
            )
            """,
            (
                run_id,
                arguments.project,
                base_commit,
                str(worktree_path),
                json.dumps(
                    metadata,
                    sort_keys=True,
                ),
                now,
                now,
                now,
                branch_name,
                snapshot_id,
                owner,
            ),
        )

        connection.execute(
            """
            INSERT INTO project_locks (
                project_id,
                run_id,
                holder,
                acquired_at,
                heartbeat_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                arguments.project,
                run_id,
                owner,
                now,
                now,
            ),
        )

        add_event(
            connection,
            project_id=arguments.project,
            run_id=run_id,
            event_type="TRANSACTION_RESERVED",
            payload={
                "owner": owner,
                "base_commit": base_commit,
            },
        )

        connection.commit()

    assert repository is not None
    assert worktree_path is not None

    try:
        snapshot = create_snapshot(
            repository=repository,
            project_id=arguments.project,
            run_id=run_id,
            snapshot_id=snapshot_id,
            base_commit=base_commit,
            branch_name=branch_name,
            worktree_path=worktree_path,
        )

        worktree_path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        git(
            repository,
            "worktree",
            "add",
            "-b",
            branch_name,
            str(worktree_path),
            base_commit,
        )

        git(
            repository,
            "worktree",
            "lock",
            "--reason",
            f"Orchestra active run {run_id}",
            str(worktree_path),
        )

        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            connection.execute(
                """
                INSERT INTO snapshots (
                    snapshot_id,
                    project_id,
                    run_id,
                    base_commit,
                    bundle_path,
                    patch_path,
                    status_path,
                    refs_path,
                    manifest_path,
                    bundle_sha256,
                    patch_sha256,
                    status_sha256,
                    refs_sha256,
                    manifest_sha256,
                    verified,
                    created_at,
                    verified_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, 0, ?, NULL
                )
                """,
                (
                    snapshot["snapshot_id"],
                    snapshot["project_id"],
                    snapshot["run_id"],
                    snapshot["base_commit"],
                    snapshot["bundle_path"],
                    snapshot["patch_path"],
                    snapshot["status_path"],
                    snapshot["refs_path"],
                    snapshot["manifest_path"],
                    snapshot["bundle_sha256"],
                    snapshot["patch_sha256"],
                    snapshot["status_sha256"],
                    snapshot["refs_sha256"],
                    snapshot["manifest_sha256"],
                    snapshot["created_at"],
                ),
            )

            now = utc_now()

            connection.execute(
                """
                UPDATE runs
                SET status = 'RUNNING',
                    heartbeat_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )

            connection.execute(
                """
                UPDATE project_locks
                SET heartbeat_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )

            add_event(
                connection,
                project_id=arguments.project,
                run_id=run_id,
                event_type="TRANSACTION_STARTED",
                payload={
                    "worktree": str(worktree_path),
                    "branch": branch_name,
                    "snapshot_id": snapshot_id,
                },
            )

            connection.commit()

    except Exception:
        cleanup_worktree(
            repository,
            worktree_path,
            branch_name,
        )

        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")

            connection.execute(
                """
                DELETE FROM project_locks
                WHERE run_id = ?
                """,
                (run_id,),
            )

            connection.execute(
                """
                UPDATE runs
                SET status = 'FAILED',
                    finished_at = ?,
                    heartbeat_at = ?
                WHERE run_id = ?
                """,
                (
                    utc_now(),
                    utc_now(),
                    run_id,
                ),
            )

            add_event(
                connection,
                project_id=arguments.project,
                run_id=run_id,
                event_type="TRANSACTION_START_FAILED",
                severity="ERROR",
            )

            connection.commit()

        raise

    payload = {
        "run_id": run_id,
        "project_id": arguments.project,
        "status": "RUNNING",
        "owner": owner,
        "base_commit": base_commit,
        "branch_name": branch_name,
        "worktree_path": str(worktree_path),
        "snapshot_id": snapshot_id,
    }

    print(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
    )


def command_verify(arguments: argparse.Namespace) -> None:
    with connect() as connection:
        run = get_run(
            connection,
            arguments.run,
        )

        if run["status"] not in (
            "RUNNING",
            "REVIEWING",
        ):
            fail(
                f"Run status is not active: {run['status']}"
            )

        lock = connection.execute(
            """
            SELECT *
            FROM project_locks
            WHERE run_id = ?
            """,
            (arguments.run,),
        ).fetchone()

        if lock is None:
            fail("Active transaction lock is absent")

        snapshot_result = verify_snapshot(
            connection,
            arguments.run,
        )

        connection.commit()

    repository = Path(run["repo_path"])
    worktree = Path(run["worktree_path"])

    if not worktree.is_dir():
        fail(
            f"Worktree is absent: {worktree}"
        )

    if not worktree_registered(
        repository,
        worktree,
    ):
        fail(
            "Worktree is not registered in Git"
        )

    branch = git(
        worktree,
        "branch",
        "--show-current",
    )

    if branch != run["branch_name"]:
        fail(
            f"Unexpected worktree branch: {branch}"
        )

    head = git(
        worktree,
        "rev-parse",
        "HEAD",
    )

    ancestor = run_command(
        [
            "git",
            "-C",
            str(worktree),
            "merge-base",
            "--is-ancestor",
            run["base_commit"],
            head,
        ],
        check=False,
    )

    if ancestor.returncode != 0:
        fail(
            "Base commit is not an ancestor of HEAD"
        )

    print(
        json.dumps(
            {
                "run_id": arguments.run,
                "status": run["status"],
                "branch": branch,
                "head": head,
                "worktree": str(worktree),
                "lock_holder": lock["holder"],
                "snapshot": snapshot_result,
                "verified": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_heartbeat(
    arguments: argparse.Namespace,
) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        run = get_run(
            connection,
            arguments.run,
        )

        if run["status"] not in (
            "RUNNING",
            "REVIEWING",
            "RECOVERING",
        ):
            connection.rollback()
            fail(
                f"Cannot heartbeat status {run['status']}"
            )

        now = utc_now()

        connection.execute(
            """
            UPDATE runs
            SET heartbeat_at = ?
            WHERE run_id = ?
            """,
            (now, arguments.run),
        )

        connection.execute(
            """
            UPDATE project_locks
            SET heartbeat_at = ?
            WHERE run_id = ?
            """,
            (now, arguments.run),
        )

        connection.commit()

    print(
        json.dumps(
            {
                "run_id": arguments.run,
                "heartbeat_at": now,
            },
            indent=2,
        )
    )


def command_submit(
    arguments: argparse.Namespace,
) -> None:
    with connect() as connection:
        run = get_run(
            connection,
            arguments.run,
        )

    if run["status"] != "RUNNING":
        fail(
            f"Run cannot be submitted from "
            f"status {run['status']}"
        )

    repository = Path(run["repo_path"])
    worktree = Path(run["worktree_path"])

    if not worktree_registered(
        repository,
        worktree,
    ):
        fail("Worktree registration is absent")

    current_branch = git(
        worktree,
        "branch",
        "--show-current",
    )

    if current_branch != run["branch_name"]:
        fail(
            f"Unexpected branch: {current_branch}"
        )

    status = git(
        worktree,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )

    if status:
        fail(
            "Worktree must be clean before submission:\n"
            + status
        )

    result_commit = git(
        worktree,
        "rev-parse",
        "HEAD",
    )

    if result_commit == run["base_commit"]:
        fail(
            "Submission requires at least one commit"
        )

    ancestor = run_command(
        [
            "git",
            "-C",
            str(worktree),
            "merge-base",
            "--is-ancestor",
            run["base_commit"],
            result_commit,
        ],
        check=False,
    )

    if ancestor.returncode != 0:
        fail(
            "Result commit does not descend "
            "from the transaction base"
        )

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        now = utc_now()

        connection.execute(
            """
            UPDATE runs
            SET status = 'REVIEWING',
                result_commit = ?,
                submitted_at = ?,
                heartbeat_at = ?
            WHERE run_id = ?
              AND status = 'RUNNING'
            """,
            (
                result_commit,
                now,
                now,
                arguments.run,
            ),
        )

        connection.execute(
            """
            UPDATE project_locks
            SET heartbeat_at = ?
            WHERE run_id = ?
            """,
            (now, arguments.run),
        )

        add_event(
            connection,
            project_id=run["project_id"],
            run_id=arguments.run,
            event_type="TRANSACTION_SUBMITTED",
            payload={
                "result_commit": result_commit,
            },
        )

        connection.commit()

    print(
        json.dumps(
            {
                "run_id": arguments.run,
                "status": "REVIEWING",
                "result_commit": result_commit,
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_rollback(
    arguments: argparse.Namespace,
) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        run = get_run(
            connection,
            arguments.run,
        )

        if run["status"] not in (
            "SNAPSHOTTING",
            "RUNNING",
            "REVIEWING",
            "WAITING_HUMAN",
            "RECOVERING",
            "FAILED",
        ):
            connection.rollback()
            fail(
                f"Run cannot be rolled back from "
                f"status {run['status']}"
            )

        snapshot_result = verify_snapshot(
            connection,
            arguments.run,
        )

        connection.execute(
            """
            UPDATE runs
            SET status = 'RECOVERING',
                recovery_decision = 'ROLLBACK_SAFE',
                heartbeat_at = ?
            WHERE run_id = ?
            """,
            (
                utc_now(),
                arguments.run,
            ),
        )

        add_event(
            connection,
            project_id=run["project_id"],
            run_id=arguments.run,
            event_type="ROLLBACK_STARTED",
            severity="WARNING",
            payload=snapshot_result,
        )

        connection.commit()

    repository = Path(run["repo_path"])
    worktree = Path(run["worktree_path"])

    cleanup_worktree(
        repository,
        worktree,
        run["branch_name"],
    )

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        now = utc_now()

        connection.execute(
            """
            UPDATE approvals
            SET status = 'CANCELLED',
                resolved_at = ?
            WHERE run_id = ?
              AND status = 'PENDING'
            """,
            (now, arguments.run),
        )

        connection.execute(
            """
            DELETE FROM project_locks
            WHERE run_id = ?
            """,
            (arguments.run,),
        )

        connection.execute(
            """
            UPDATE runs
            SET status = 'CANCELLED',
                recovery_decision = 'ROLLBACK_SAFE',
                finished_at = ?,
                heartbeat_at = ?
            WHERE run_id = ?
            """,
            (
                now,
                now,
                arguments.run,
            ),
        )

        add_event(
            connection,
            project_id=run["project_id"],
            run_id=arguments.run,
            event_type="ROLLBACK_COMPLETED",
            severity="WARNING",
            payload={
                "snapshot_verified": True,
                "worktree_removed": True,
                "branch_removed": True,
            },
        )

        connection.commit()

    print(
        json.dumps(
            {
                "run_id": arguments.run,
                "status": "CANCELLED",
                "recovery_decision": "ROLLBACK_SAFE",
                "snapshot_verified": True,
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_verify_snapshot(
    arguments: argparse.Namespace,
) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")

        result = verify_snapshot(
            connection,
            arguments.run,
        )

        connection.commit()

    print(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
        )
    )


def command_status(
    arguments: argparse.Namespace,
) -> None:
    with connect() as connection:
        run = get_run(
            connection,
            arguments.run,
        )

        lock = connection.execute(
            """
            SELECT *
            FROM project_locks
            WHERE run_id = ?
            """,
            (arguments.run,),
        ).fetchone()

    payload = dict(run)
    payload["lock"] = (
        dict(lock)
        if lock is not None
        else None
    )

    print(
        json.dumps(
            payload,
            indent=2,
            sort_keys=True,
        )
    )


def command_list(
    arguments: argparse.Namespace,
) -> None:
    query = """
        SELECT
            run_id,
            project_id,
            status,
            recovery_decision,
            base_commit,
            result_commit,
            branch_name,
            worktree_path,
            transaction_owner,
            created_at,
            heartbeat_at,
            submitted_at,
            finished_at
        FROM runs
    """

    parameters: tuple[Any, ...] = ()

    if arguments.project:
        query += " WHERE project_id = ?"
        parameters = (arguments.project,)

    query += " ORDER BY created_at DESC"

    with connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                query,
                parameters,
            )
        ]

    print(
        json.dumps(
            rows,
            indent=2,
            sort_keys=True,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra Git transaction manager"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    begin = subparsers.add_parser("begin")
    begin.add_argument(
        "--project",
        required=True,
    )
    begin.add_argument("--owner")
    begin.add_argument(
        "--metadata-json",
        default="{}",
    )
    begin.set_defaults(function=command_begin)

    verify = subparsers.add_parser("verify")
    verify.add_argument("--run", required=True)
    verify.set_defaults(function=command_verify)

    heartbeat = subparsers.add_parser("heartbeat")
    heartbeat.add_argument("--run", required=True)
    heartbeat.set_defaults(function=command_heartbeat)

    submit = subparsers.add_parser("submit")
    submit.add_argument("--run", required=True)
    submit.set_defaults(function=command_submit)

    rollback_parser = subparsers.add_parser(
        "rollback"
    )
    rollback_parser.add_argument(
        "--run",
        required=True,
    )
    rollback_parser.set_defaults(
        function=command_rollback
    )

    verify_snapshot_parser = subparsers.add_parser(
        "verify-snapshot"
    )
    verify_snapshot_parser.add_argument(
        "--run",
        required=True,
    )
    verify_snapshot_parser.set_defaults(
        function=command_verify_snapshot
    )

    status = subparsers.add_parser("status")
    status.add_argument("--run", required=True)
    status.set_defaults(function=command_status)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--project")
    list_parser.set_defaults(function=command_list)

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        arguments.function(arguments)
    except TransactionError as error:
        print(
            f"Transaction error: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
