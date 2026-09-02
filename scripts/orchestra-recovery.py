#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NoReturn
import orchestra_review_assignment as ASSIGNMENTS


ROOT = Path(
    os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
).resolve()
DATABASE = Path(
    os.environ.get(
        "ORCHESTRA_DB",
        str(ROOT / "state/controller/orchestra.db"),
    )
).resolve()
TRANSACTION_SCRIPT = ROOT / "repo/scripts/orchestra-transaction.py"
INTEGRATOR_SCRIPT = ROOT / "repo/scripts/orchestra-integrator.py"
HERMES_HOME = ROOT / "state/hermes-home"
WORKSPACES = ROOT / "workspaces"
PRIVATE_DOCKER_ENVIRONMENT = {
    **os.environ,
    "DOCKER_HOST": "unix:///run/orchestra-docker/docker.sock",
    "DOCKER_CONTEXT": "default",
    "DOCKER_CONFIG": "/nonexistent/orchestra-empty-docker-config",
}
POLICY_VERSION = "recovery-policy-v1"
ACTIVE_STATUSES = (
    "SNAPSHOTTING",
    "RUNNING",
    "REVIEWING",
    "WAITING_HUMAN",
    "COMMITTING",
    "RECOVERING",
    "FAILED",
)
TERMINAL_STATUSES = ("COMPLETED", "CANCELLED")


class RecoveryError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise RecoveryError(message)


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")

    try:
        result = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)

    return result.astimezone(timezone.utc)


def age_seconds(value: str | None) -> float | None:
    parsed = parse_timestamp(value)

    if parsed is None:
        return None

    return max(
        0.0,
        (datetime.now(timezone.utc) - parsed).total_seconds(),
    )


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=15)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def run_command(
    arguments: list[str],
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        arguments,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        env=(
            PRIVATE_DOCKER_ENVIRONMENT
            if arguments and arguments[0] == "docker"
            else None
        ),
    )

    if check and result.returncode != 0:
        fail(
            "Command failed: "
            + " ".join(arguments)
            + f"\nstdout:\n{result.stdout}"
            + f"\nstderr:\n{result.stderr}"
        )

    return result


def git(
    repository: Path,
    *arguments: str,
    check: bool = True,
) -> str:
    return run_command(
        ["git", "-C", str(repository), *arguments],
        check=check,
    ).stdout.strip()


def load_module(path: Path, name: str) -> Any:
    if not path.is_file():
        fail(f"Required module is absent: {path}")

    spec = importlib.util.spec_from_file_location(name, path)

    if spec is None or spec.loader is None:
        fail(f"Cannot load module: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    )


def payload_sha256(payload: Any) -> str:
    return hashlib.sha256(
        canonical_json(payload).encode("utf-8")
    ).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()

    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)

    return digest.hexdigest()


def ensure_within(path: Path, root: Path) -> Path:
    resolved = path.resolve()

    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        fail(f"Path escapes allowed root: {resolved}")

    return resolved


def load_default_branch(config_source: str) -> str:
    path = Path(config_source)

    if not path.is_file():
        fail(f"Project configuration is absent: {path}")

    with path.open("rb") as stream:
        document = tomllib.load(stream)

    branch = (document.get("git") or {}).get("default_branch")

    if not isinstance(branch, str) or not branch:
        fail(f"git.default_branch is absent in {path}")

    return branch


def get_run(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT
            r.*,
            p.repo_path,
            p.config_source,
            p.enabled AS project_enabled
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


def get_lock(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM project_locks WHERE run_id = ?",
        (run_id,),
    ).fetchone()


def get_snapshot(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM snapshots WHERE run_id = ?",
        (run_id,),
    ).fetchone()


def get_integration(
    connection: sqlite3.Connection,
    run_id: str,
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT *
        FROM integration_executions
        WHERE run_id = ?
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (run_id,),
    ).fetchone()


def add_event(
    connection: sqlite3.Connection,
    *,
    project_id: str,
    run_id: str,
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


def snapshot_evidence(
    run: sqlite3.Row,
    snapshot: sqlite3.Row | None,
) -> dict[str, Any]:
    if snapshot is None:
        return {
            "present": False,
            "valid": False,
            "reason": "snapshot-row-absent",
            "artifacts": {},
        }

    checks = (
        ("bundle_path", "bundle_sha256"),
        ("patch_path", "patch_sha256"),
        ("status_path", "status_sha256"),
        ("refs_path", "refs_sha256"),
        ("manifest_path", "manifest_sha256"),
    )
    artifacts: dict[str, Any] = {}
    valid = True
    reasons: list[str] = []

    for path_column, hash_column in checks:
        path = Path(snapshot[path_column])
        expected = str(snapshot[hash_column])
        entry: dict[str, Any] = {
            "path": str(path),
            "expected_sha256": expected,
            "present": path.is_file(),
        }

        if path.is_file():
            actual = file_sha256(path)
            entry["actual_sha256"] = actual
            entry["hash_match"] = actual == expected

            if actual != expected:
                valid = False
                reasons.append(f"hash-mismatch:{path_column}")
        else:
            entry["actual_sha256"] = None
            entry["hash_match"] = False
            valid = False
            reasons.append(f"missing:{path_column}")

        artifacts[path_column] = entry

    bundle_verified = False
    bundle_path = Path(snapshot["bundle_path"])

    if bundle_path.is_file():
        result = run_command(
            [
                "git",
                "-C",
                str(Path(run["repo_path"])),
                "bundle",
                "verify",
                str(bundle_path),
            ],
            check=False,
        )
        bundle_verified = result.returncode == 0

        if not bundle_verified:
            valid = False
            reasons.append("bundle-verification-failed")

    return {
        "present": True,
        "valid": valid,
        "reason": ",".join(sorted(set(reasons))) or None,
        "verified_column": bool(snapshot["verified"]),
        "bundle_verified": bundle_verified,
        "snapshot_id": snapshot["snapshot_id"],
        "artifacts": artifacts,
    }


def repository_evidence(
    run: sqlite3.Row,
    transaction: Any,
) -> dict[str, Any]:
    repository = Path(run["repo_path"]).resolve()
    worktree = Path(run["worktree_path"]).resolve()
    default_branch = load_default_branch(run["config_source"])
    result: dict[str, Any] = {
        "repository": str(repository),
        "worktree": str(worktree),
        "default_branch": default_branch,
        "repository_present": repository.is_dir(),
        "worktree_present": worktree.is_dir(),
        "worktree_registered": False,
    }

    if not repository.is_dir():
        result["error"] = "repository-absent"
        return result

    result["repository_branch"] = git(
        repository,
        "branch",
        "--show-current",
        check=False,
    )
    result["repository_head"] = git(
        repository,
        "rev-parse",
        "HEAD",
        check=False,
    )
    result["repository_status"] = git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        check=False,
    )
    result["repository_clean"] = not result["repository_status"]

    try:
        result["worktree_registered"] = bool(
            transaction.worktree_registered(repository, worktree)
        )
    except Exception as error:
        result["worktree_registration_error"] = str(error)

    if worktree.is_dir():
        result["worktree_branch"] = git(
            worktree,
            "branch",
            "--show-current",
            check=False,
        )
        result["worktree_head"] = git(
            worktree,
            "rev-parse",
            "HEAD",
            check=False,
        )
        result["worktree_status"] = git(
            worktree,
            "status",
            "--porcelain=v1",
            "--untracked-files=all",
            check=False,
        )
        result["worktree_clean"] = not result["worktree_status"]
        ancestor = run_command(
            [
                "git",
                "-C",
                str(worktree),
                "merge-base",
                "--is-ancestor",
                str(run["base_commit"]),
                "HEAD",
            ],
            check=False,
        )
        result["base_is_ancestor"] = ancestor.returncode == 0
    else:
        result["worktree_branch"] = None
        result["worktree_head"] = None
        result["worktree_status"] = None
        result["worktree_clean"] = None
        result["base_is_ancestor"] = None

    return result


def execution_evidence(
    connection: sqlite3.Connection,
    run_id: str,
) -> dict[str, Any]:
    tasks = [
        dict(row)
        for row in connection.execute(
            """
            SELECT task_id, role, status, heartbeat_at
            FROM tasks
            WHERE run_id = ?
              AND status IN ('QUEUED', 'RUNNING', 'BLOCKED')
            ORDER BY created_at
            """,
            (run_id,),
        )
    ]
    workers = [
        dict(row)
        for row in connection.execute(
            """
            SELECT
                execution_id,
                task_id,
                outer_container_name,
                sandbox_container_id,
                runtime_profile,
                started_at,
                finished_at
            FROM worker_executions
            WHERE run_id = ?
              AND finished_at IS NULL
            ORDER BY created_at
            """,
            (run_id,),
        )
    ]
    reviewers = [
        dict(row)
        for row in connection.execute(
            """
            SELECT
                execution_id,
                task_id,
                outer_container_name,
                sandbox_container_id,
                runtime_profile,
                started_at,
                finished_at
            FROM reviewer_executions
            WHERE run_id = ?
              AND finished_at IS NULL
            ORDER BY created_at
            """,
            (run_id,),
        )
    ]
    return {
        "active_tasks": tasks,
        "unfinished_workers": workers,
        "unfinished_reviewers": reviewers,
    }


def assess_run(run_id: str) -> dict[str, Any]:
    transaction = load_module(
        TRANSACTION_SCRIPT,
        "orchestra_transaction_recovery",
    )

    with connect() as connection:
        run = get_run(connection, run_id)
        lock = get_lock(connection, run_id)
        snapshot = get_snapshot(connection, run_id)
        integration = get_integration(connection, run_id)
        executions = execution_evidence(connection, run_id)

    status = str(run["status"])
    snapshot_state = snapshot_evidence(run, snapshot)
    repository_state = repository_evidence(run, transaction)
    lock_state = dict(lock) if lock is not None else None
    integration_state = (
        dict(integration) if integration is not None else None
    )
    reasons: list[str] = []
    decision: str

    if status in TERMINAL_STATUSES:
        decision = "NO_ACTION"
        reasons.append("run-is-terminal")
    elif not repository_state.get("repository_present"):
        decision = "BLOCK_HUMAN"
        reasons.append("repository-absent")
    elif (
        repository_state.get("repository_branch")
        != repository_state.get("default_branch")
    ):
        decision = "BLOCK_HUMAN"
        reasons.append("default-branch-mismatch")
    elif not repository_state.get("repository_clean"):
        decision = "BLOCK_HUMAN"
        reasons.append("default-branch-dirty")
    elif status == "SNAPSHOTTING" and not snapshot_state["present"]:
        if repository_state.get("repository_head") == run["base_commit"]:
            decision = "ROLLBACK_SAFE"
            reasons.append("snapshot-never-completed")
        else:
            decision = "BLOCK_HUMAN"
            reasons.append("snapshot-missing-and-main-moved")
    elif not snapshot_state["valid"]:
        decision = "BLOCK_HUMAN"
        reasons.append("snapshot-integrity-failed")
    else:
        main_head = repository_state.get("repository_head")
        base_commit = run["base_commit"]
        result_commit = run["result_commit"]

        if main_head not in {base_commit, result_commit}:
            decision = "BLOCK_HUMAN"
            reasons.append("default-branch-diverged")
        elif status == "WAITING_HUMAN":
            decision = "BLOCK_HUMAN"
            reasons.append("existing-human-gate")
        elif status == "COMMITTING":
            if integration_state is None:
                decision = "BLOCK_HUMAN"
                reasons.append("committing-without-integration-record")
            elif integration_state.get("status") not in (
                "PREPARED",
                "FAILED",
            ):
                decision = "BLOCK_HUMAN"
                reasons.append("unexpected-integration-state")
            elif main_head == result_commit:
                decision = "RESUME_SAFE"
                reasons.append("fast-forward-complete-database-pending")
            elif (
                main_head == base_commit
                and repository_state.get("worktree_present")
                and repository_state.get("worktree_registered")
                and repository_state.get("worktree_clean")
                and repository_state.get("worktree_head")
                == result_commit
                and repository_state.get("base_is_ancestor")
            ):
                decision = "RESUME_SAFE"
                reasons.append("prepared-integration-can-resume")
            else:
                decision = "BLOCK_HUMAN"
                reasons.append("committing-state-is-ambiguous")
        elif status == "RUNNING":
            if main_head != base_commit:
                decision = "BLOCK_HUMAN"
                reasons.append("main-moved-during-running")
            elif (
                repository_state.get("worktree_present")
                and repository_state.get("worktree_registered")
                and repository_state.get("worktree_clean")
                and repository_state.get("worktree_branch")
                == run["branch_name"]
                and repository_state.get("base_is_ancestor")
            ):
                decision = "RESUME_SAFE"
                reasons.append("running-transaction-is-coherent")
            elif (
                not repository_state.get("worktree_present")
                and not repository_state.get("worktree_registered")
            ):
                decision = "ROLLBACK_SAFE"
                reasons.append("running-worktree-disappeared")
            else:
                decision = "BLOCK_HUMAN"
                reasons.append("running-worktree-is-ambiguous")
        elif status == "REVIEWING":
            if main_head != base_commit:
                decision = "BLOCK_HUMAN"
                reasons.append("main-moved-during-review")
            elif (
                result_commit
                and repository_state.get("worktree_present")
                and repository_state.get("worktree_registered")
                and repository_state.get("worktree_clean")
                and repository_state.get("worktree_head")
                == result_commit
            ):
                decision = "RESUME_SAFE"
                reasons.append("reviewing-transaction-is-coherent")
            elif (
                not repository_state.get("worktree_present")
                and not repository_state.get("worktree_registered")
            ):
                decision = "ROLLBACK_SAFE"
                reasons.append("review-worktree-disappeared")
            else:
                decision = "BLOCK_HUMAN"
                reasons.append("reviewing-state-is-ambiguous")
        elif status == "FAILED":
            if main_head == base_commit:
                decision = "ROLLBACK_SAFE"
                reasons.append("failed-run-main-unchanged")
            else:
                decision = "BLOCK_HUMAN"
                reasons.append("failed-run-main-diverged")
        elif status == "RECOVERING":
            existing = run["recovery_decision"]

            if existing == "ROLLBACK_SAFE" and main_head == base_commit:
                decision = "ROLLBACK_SAFE"
                reasons.append("continue-interrupted-rollback")
            elif existing == "BLOCK_HUMAN":
                decision = "BLOCK_HUMAN"
                reasons.append("existing-recovery-human-gate")
            elif (
                existing == "RESUME_SAFE"
                and integration_state is not None
                and main_head in {base_commit, result_commit}
            ):
                decision = "RESUME_SAFE"
                reasons.append("continue-interrupted-resume")
            else:
                decision = "BLOCK_HUMAN"
                reasons.append("recovering-state-lacks-safe-proof")
        elif status == "SNAPSHOTTING":
            decision = "ROLLBACK_SAFE"
            reasons.append("snapshotting-run-can-be-aborted")
        else:
            decision = "BLOCK_HUMAN"
            reasons.append(f"unsupported-active-status:{status}")

    if decision in ("RESUME_SAFE", "ROLLBACK_SAFE"):
        if lock_state is None and not (
            status == "COMMITTING"
            and repository_state.get("repository_head")
            == run["result_commit"]
        ):
            if decision == "RESUME_SAFE":
                decision = "BLOCK_HUMAN"
                reasons.append("active-project-lock-absent")

    evidence = {
        "schema_version": 1,
        "policy_version": POLICY_VERSION,
        "observed_at": utc_now(),
        "run": {
            key: run[key]
            for key in (
                "run_id",
                "project_id",
                "status",
                "recovery_decision",
                "base_commit",
                "result_commit",
                "worktree_path",
                "branch_name",
                "snapshot_id",
                "transaction_owner",
                "heartbeat_at",
                "submitted_at",
                "finished_at",
            )
        },
        "heartbeat_age_seconds": age_seconds(run["heartbeat_at"]),
        "lock": lock_state,
        "snapshot": snapshot_state,
        "repository": repository_state,
        "integration": integration_state,
        "executions": executions,
        "decision": decision,
        "reasons": reasons,
    }
    evidence["evidence_sha256"] = payload_sha256(evidence)
    return evidence


def docker_exists() -> bool:
    return shutil.which("docker") is not None


def inspection_document(
    result: subprocess.CompletedProcess[str],
) -> dict[str, Any] | None:
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list) or len(payload) != 1:
        return None
    return payload[0] if isinstance(payload[0], dict) else None


def host_container_ownership(
    container: dict[str, Any],
    *,
    expected_name: str,
    known_names: set[str] | None = None,
) -> str | None:
    """Return the explicit ownership class, never a name-only guess."""
    if known_names is not None and expected_name not in known_names:
        return None
    labels = ((container.get("Config") or {}).get("Labels") or {})
    actual_name = str(container.get("Name") or "").removeprefix("/")
    if actual_name != expected_name:
        return None
    if (
        labels.get("orchestra-runtime-container") == "1"
        and labels.get("orchestra-runtime-request-id") == expected_name
        and re.fullmatch(r"agent-runtime-[a-f0-9]{12}", expected_name)
    ):
        return "NEW_GENERIC"
    return None


def nested_container_ownership(
    container: dict[str, Any],
    *,
    expected_task_id: str | None = None,
    expected_request_id: str | None = None,
    expected_profile: str | None = None,
    known_bindings: set[tuple[str, str]] | None = None,
) -> str | None:
    """Classify only explicitly owned nested sandboxes."""
    labels = ((container.get("Config") or {}).get("Labels") or {})
    name = str(container.get("Name") or "").removeprefix("/")
    if labels.get("orchestra-sandbox") == "1":
        task = str(labels.get("orchestra-task-id") or "")
        request = str(labels.get("orchestra-runtime-request-id") or "")
        if not re.fullmatch(r"task-[a-f0-9]{32}", task):
            return None
        if not re.fullmatch(r"agent-runtime-[a-f0-9]{12}", request):
            return None
        if name != "orchestra-sandbox-" + request.removeprefix("agent-runtime-"):
            return None
        if expected_task_id is not None and task != expected_task_id:
            return None
        if expected_request_id is not None and request != expected_request_id:
            return None
        if known_bindings is not None and (task, request) not in known_bindings:
            return None
        return "NEW_GENERIC"
    return None


def inspect_host_container(name: str) -> dict[str, Any] | None:
    return inspection_document(
        run_command(
            ["docker", "container", "inspect", name],
            check=False,
        )
    )


def inspect_nested_container(container_id: str) -> dict[str, Any] | None:
    return inspection_document(
        run_command(
            ["docker", "container", "inspect", container_id],
            check=False,
        )
    )


def remove_host_container(
    name: str,
    *,
    known_names: set[str] | None = None,
) -> bool:
    if not name or not docker_exists():
        return False

    container = inspect_host_container(name)
    if container is None or host_container_ownership(
        container,
        expected_name=name,
        known_names=known_names,
    ) is None:
        return False
    container_id = str(container.get("Id") or "")
    if re.fullmatch(r"[a-f0-9]{64}", container_id) is None:
        return False

    removed = run_command(
        ["docker", "rm", "-f", container_id],
        check=False,
    )
    return removed.returncode == 0


def remove_nested_container(
    container_id: str,
    *,
    expected_task_id: str | None = None,
    expected_request_id: str | None = None,
    expected_profile: str | None = None,
    known_bindings: set[tuple[str, str]] | None = None,
) -> bool:
    if not container_id or not docker_exists():
        return False

    container = inspect_nested_container(container_id)
    if container is None or nested_container_ownership(
        container,
        expected_task_id=expected_task_id,
        expected_request_id=expected_request_id,
        expected_profile=expected_profile,
        known_bindings=known_bindings,
    ) is None:
        return False
    full_container_id = str(container.get("Id") or "")
    if (
        re.fullmatch(r"[a-f0-9]{64}", full_container_id) is None
        or not full_container_id.startswith(container_id)
    ):
        return False

    removed = run_command(
        ["docker", "rm", "-f", full_container_id],
        check=False,
    )
    return removed.returncode == 0


def remove_profile(profile_name: str) -> bool:
    if not profile_name.startswith((
        "agent-runtime-",
        "runtime-worker-",
        "runtime-reviewer-",
    )):
        return False

    path = ensure_within(
        HERMES_HOME / "profiles" / profile_name,
        HERMES_HOME / "profiles",
    )

    if not path.exists():
        return False

    shutil.rmtree(path)
    return True


def prune_empty_clone_parents(path: Path, root: Path) -> list[str]:
    """Remove empty clone parents without ever deleting the clone root."""
    removed: list[str] = []
    current = path.resolve()
    root = root.resolve()

    while current != root:
        ensure_within(current, root)

        try:
            current.rmdir()
        except FileNotFoundError:
            pass
        except OSError:
            break
        else:
            removed.append(str(current))

        current = current.parent

    return removed


def remove_clone_tree(kind: str, project_id: str, run_id: str) -> bool:
    if kind not in ("worker", "reviewer"):
        fail(f"Invalid clone kind: {kind}")

    root = WORKSPACES / f".orchestra-{kind}-clones"
    path = ensure_within(root / project_id / run_id, root)

    if not path.exists():
        return False

    if kind == "reviewer":
        try:
            path.chmod(path.stat().st_mode | 0o700)
        except OSError:
            pass

        for child in path.rglob("*"):
            try:
                child.chmod(child.stat().st_mode | 0o200)
            except OSError:
                pass

    shutil.rmtree(path)
    prune_empty_clone_parents(path.parent, root)
    return True


def cleanup_run_resources(run_id: str) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []

    with connect() as connection:
        run = get_run(connection, run_id)
        rows = connection.execute(
            """
            SELECT
                'worker' AS execution_kind,
                execution_id,
                task_id,
                outer_container_name,
                sandbox_container_id,
                runtime_profile
            FROM worker_executions
            WHERE run_id = ?
              AND finished_at IS NULL
            UNION ALL
            SELECT
                'reviewer' AS execution_kind,
                execution_id,
                task_id,
                outer_container_name,
                sandbox_container_id,
                runtime_profile
            FROM reviewer_executions
            WHERE run_id = ?
              AND finished_at IS NULL
            """,
            (run_id, run_id),
        ).fetchall()

    for row in rows:
        outer_name = str(row["outer_container_name"] or "")
        profile = str(row["runtime_profile"] or "")
        request_id = (
            outer_name
            if outer_name.startswith("agent-runtime-")
            else None
        )
        action = {
            "kind": row["execution_kind"],
            "execution_id": row["execution_id"],
            "outer_container_removed": remove_host_container(
                outer_name,
                known_names={outer_name},
            ),
            "sandbox_removed": remove_nested_container(
                row["sandbox_container_id"] or "",
                expected_task_id=str(row["task_id"]),
                expected_request_id=request_id,
                expected_profile=profile,
            ),
            "profile_removed": remove_profile(
                profile
            ),
        }
        actions.append(action)

    remove_clone_tree("worker", run["project_id"], run_id)
    remove_clone_tree("reviewer", run["project_id"], run_id)

    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE tasks
            SET status = 'FAILED',
                finished_at = ?,
                heartbeat_at = ?
            WHERE run_id = ?
              AND status IN ('QUEUED', 'RUNNING', 'BLOCKED')
            """,
            (now, now, run_id),
        )
        connection.execute(
            """
            UPDATE worker_executions
            SET exit_code = COALESCE(exit_code, 75),
                failure_reason = COALESCE(
                    failure_reason,
                    'Recovered abandoned worker execution'
                ),
                finished_at = COALESCE(finished_at, ?)
            WHERE run_id = ?
              AND finished_at IS NULL
            """,
            (now, run_id),
        )
        connection.execute(
            """
            UPDATE reviewer_executions
            SET exit_code = COALESCE(exit_code, 75),
                failure_reason = COALESCE(
                    failure_reason,
                    'Recovered abandoned reviewer execution'
                ),
                finished_at = COALESCE(finished_at, ?)
            WHERE run_id = ?
              AND finished_at IS NULL
            """,
            (now, run_id),
        )
        ASSIGNMENTS.recover_active_assignments(
            connection,
            run_id=run_id,
            actor_id=f"recovery:{run_id}",
        )
        connection.commit()

    return actions


def insert_recovery(
    *,
    run_id: str,
    owner: str,
    evidence: dict[str, Any],
) -> str:
    recovery_id = "recovery-" + uuid.uuid4().hex
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO recovery_executions (
                recovery_id,
                run_id,
                role_id,
                source_profile,
                controller_owner,
                policy_version,
                observed_status,
                decision,
                outcome,
                evidence_sha256,
                evidence_json,
                actions_json,
                failure_reason,
                created_at,
                started_at,
                finished_at
            )
            VALUES (
                ?, ?, 'recovery', 'ops-recovery', ?, ?, ?, ?,
                'ASSESSED', ?, ?, '[]', NULL, ?, ?, NULL
            )
            """,
            (
                recovery_id,
                run_id,
                owner,
                POLICY_VERSION,
                evidence["run"]["status"],
                evidence["decision"],
                evidence["evidence_sha256"],
                json.dumps(evidence, sort_keys=True),
                now,
                now,
            ),
        )
        connection.commit()

    return recovery_id


def finish_recovery(
    recovery_id: str,
    *,
    outcome: str,
    actions: list[dict[str, Any]],
    failure_reason: str | None = None,
) -> None:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE recovery_executions
            SET outcome = ?,
                actions_json = ?,
                failure_reason = ?,
                finished_at = ?
            WHERE recovery_id = ?
            """,
            (
                outcome,
                json.dumps(actions, sort_keys=True),
                failure_reason,
                utc_now(),
                recovery_id,
            ),
        )
        connection.commit()


def ensure_lock_for_block(
    connection: sqlite3.Connection,
    run: sqlite3.Row,
    owner: str,
) -> None:
    existing = connection.execute(
        "SELECT * FROM project_locks WHERE project_id = ?",
        (run["project_id"],),
    ).fetchone()

    if existing is not None:
        if existing["run_id"] != run["run_id"]:
            fail("Project is locked by another run")
        return

    now = utc_now()
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
            run["project_id"],
            run["run_id"],
            run["transaction_owner"] or owner,
            now,
            now,
        ),
    )


def apply_resume(
    run_id: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    transaction = load_module(
        TRANSACTION_SCRIPT,
        "orchestra_transaction_resume",
    )
    actions = cleanup_run_resources(run_id)
    status = evidence["run"]["status"]

    if status in ("RUNNING", "REVIEWING"):
        now = utc_now()

        with connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            run = get_run(connection, run_id)
            connection.execute(
                """
                UPDATE runs
                SET recovery_decision = 'RESUME_SAFE',
                    heartbeat_at = ?
                WHERE run_id = ?
                  AND status = ?
                """,
                (now, run_id, status),
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
                project_id=run["project_id"],
                run_id=run_id,
                event_type="RECOVERY_RESUME_READY",
                payload={
                    "status": status,
                    "policy_version": POLICY_VERSION,
                },
            )
            connection.commit()

        actions.append(
            {
                "action": "resume-ready",
                "status": status,
            }
        )
        return actions

    if status not in ("COMMITTING", "RECOVERING"):
        fail(f"RESUME_SAFE cannot apply to status {status}")

    with connect() as connection:
        run = get_run(connection, run_id)
        integration = get_integration(connection, run_id)

    if integration is None:
        fail("Integration record is absent during commit recovery")

    repository = Path(run["repo_path"])
    worktree = Path(run["worktree_path"])
    main_head = git(repository, "rev-parse", "HEAD")

    if main_head == run["base_commit"]:
        git(
            repository,
            "merge",
            "--ff-only",
            "--no-edit",
            run["result_commit"],
        )
        main_head = git(repository, "rev-parse", "HEAD")
        actions.append(
            {
                "action": "fast-forward",
                "main_after": main_head,
            }
        )

    if main_head != run["result_commit"]:
        fail("Recovered integration did not reach reviewed result")

    if git(
        repository,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    ):
        fail("Default branch is dirty during commit recovery")

    transaction.cleanup_worktree(
        repository,
        worktree,
        run["branch_name"],
    )
    actions.append({"action": "transaction-worktree-cleaned"})
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = get_run(connection, run_id)

        if current["status"] not in ("COMMITTING", "RECOVERING"):
            connection.rollback()
            fail(f"Run changed during recovery: {current['status']}")

        cursor = connection.execute(
            """
            UPDATE integration_executions
            SET status = 'COMPLETED',
                main_after = ?,
                failure_reason = NULL,
                finished_at = ?
            WHERE integration_id = ?
              AND status IN ('PREPARED', 'FAILED')
            """,
            (main_head, now, integration["integration_id"]),
        )

        if cursor.rowcount != 1:
            connection.rollback()
            fail("Recoverable integration record changed")

        connection.execute(
            "DELETE FROM project_locks WHERE run_id = ?",
            (run_id,),
        )
        connection.execute(
            """
            UPDATE runs
            SET status = 'COMPLETED',
                recovery_decision = 'RESUME_SAFE',
                finished_at = ?,
                heartbeat_at = ?
            WHERE run_id = ?
            """,
            (now, now, run_id),
        )
        add_event(
            connection,
            project_id=run["project_id"],
            run_id=run_id,
            event_type="RECOVERY_INTEGRATION_COMPLETED",
            payload={
                "integration_id": integration["integration_id"],
                "result_commit": main_head,
            },
        )
        connection.commit()

    actions.append(
        {
            "action": "integration-finalized",
            "integration_id": integration["integration_id"],
        }
    )
    return actions


def rollback_without_snapshot(run_id: str) -> list[dict[str, Any]]:
    transaction = load_module(
        TRANSACTION_SCRIPT,
        "orchestra_transaction_abort_snapshotting",
    )

    with connect() as connection:
        run = get_run(connection, run_id)

    repository = Path(run["repo_path"])
    worktree = Path(run["worktree_path"])
    transaction.cleanup_worktree(
        repository,
        worktree,
        run["branch_name"],
    )
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE approvals
            SET status = 'CANCELLED',
                resolved_at = ?
            WHERE run_id = ?
              AND status = 'PENDING'
            """,
            (now, run_id),
        )
        connection.execute(
            "DELETE FROM project_locks WHERE run_id = ?",
            (run_id,),
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
            (now, now, run_id),
        )
        add_event(
            connection,
            project_id=run["project_id"],
            run_id=run_id,
            event_type="RECOVERY_SNAPSHOT_START_ABORTED",
            severity="WARNING",
        )
        connection.commit()

    return [{"action": "snapshotting-run-cancelled"}]


def apply_rollback(
    run_id: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    actions = cleanup_run_resources(run_id)

    if (
        evidence["run"]["status"] == "SNAPSHOTTING"
        and not evidence["snapshot"]["present"]
    ):
        actions.extend(rollback_without_snapshot(run_id))
        return actions

    with connect() as connection:
        run = get_run(connection, run_id)

        if run["status"] == "COMMITTING":
            connection.execute("BEGIN IMMEDIATE")
            now = utc_now()
            connection.execute(
                """
                UPDATE integration_executions
                SET status = 'FAILED',
                    failure_reason = COALESCE(
                        failure_reason,
                        'Cancelled by ROLLBACK_SAFE recovery'
                    ),
                    finished_at = COALESCE(finished_at, ?)
                WHERE run_id = ?
                  AND status = 'PREPARED'
                """,
                (now, run_id),
            )
            connection.execute(
                """
                UPDATE runs
                SET status = 'RECOVERING',
                    recovery_decision = 'ROLLBACK_SAFE',
                    heartbeat_at = ?
                WHERE run_id = ?
                """,
                (now, run_id),
            )
            connection.commit()

    result = run_command(
        [
            sys.executable,
            str(TRANSACTION_SCRIPT),
            "rollback",
            "--run",
            run_id,
        ]
    )
    payload = json.loads(result.stdout)
    actions.append(
        {
            "action": "transaction-rollback",
            "result": payload,
        }
    )
    return actions


def apply_block(
    run_id: str,
    owner: str,
    evidence: dict[str, Any],
) -> list[dict[str, Any]]:
    actions = cleanup_run_resources(run_id)
    now = utc_now()

    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        run = get_run(connection, run_id)
        ensure_lock_for_block(connection, run, owner)
        approval = connection.execute(
            """
            SELECT approval_id
            FROM approvals
            WHERE run_id = ?
              AND status = 'PENDING'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (run_id,),
        ).fetchone()

        if approval is None:
            approval_id = "approval-" + uuid.uuid4().hex
            connection.execute(
                """
                INSERT INTO approvals (
                    approval_id,
                    run_id,
                    status,
                    question,
                    options_json,
                    decision,
                    created_at,
                    resolved_at
                )
                VALUES (?, ?, 'PENDING', ?, ?, NULL, ?, NULL)
                """,
                (
                    approval_id,
                    run_id,
                    "Recovery state is ambiguous; choose a safe action.",
                    json.dumps(
                        ["RESUME_SAFE", "ROLLBACK_SAFE"],
                        sort_keys=True,
                    ),
                    now,
                ),
            )
        else:
            approval_id = approval["approval_id"]

        connection.execute(
            """
            UPDATE integration_executions
            SET status = 'FAILED',
                failure_reason = COALESCE(
                    failure_reason,
                    'Recovery requires human decision'
                ),
                finished_at = COALESCE(finished_at, ?)
            WHERE run_id = ?
              AND status = 'PREPARED'
            """,
            (now, run_id),
        )
        connection.execute(
            """
            UPDATE runs
            SET status = 'WAITING_HUMAN',
                recovery_decision = 'BLOCK_HUMAN',
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
            project_id=run["project_id"],
            run_id=run_id,
            event_type="RECOVERY_BLOCKED_HUMAN",
            severity="CRITICAL",
            payload={
                "approval_id": approval_id,
                "reasons": evidence["reasons"],
            },
        )
        connection.commit()

    actions.append(
        {
            "action": "human-approval-created",
            "approval_id": approval_id,
        }
    )
    return actions


def recover_run(
    *,
    run_id: str,
    owner: str,
    stale_seconds: int,
    force: bool,
    expected_decision: str | None = None,
) -> dict[str, Any]:
    evidence = assess_run(run_id)
    decision = evidence["decision"]

    if decision == "NO_ACTION":
        return {
            "run_id": run_id,
            "decision": decision,
            "outcome": "NO_ACTION",
            "status": evidence["run"]["status"],
            "evidence_sha256": evidence["evidence_sha256"],
        }

    if expected_decision and decision != expected_decision:
        fail(
            f"Recovery decision changed: {decision} != "
            f"{expected_decision}"
        )

    heartbeat_age = evidence["heartbeat_age_seconds"]

    if not force:
        if heartbeat_age is None:
            fail("Run heartbeat is absent or invalid; use --force")
        if heartbeat_age < stale_seconds:
            fail(
                f"Run is not stale: {heartbeat_age:.1f}s < "
                f"{stale_seconds}s"
            )

    recovery_id = insert_recovery(
        run_id=run_id,
        owner=owner,
        evidence=evidence,
    )
    actions: list[dict[str, Any]] = []

    try:
        if decision == "RESUME_SAFE":
            actions = apply_resume(run_id, evidence)
            outcome = "RESUMED"
        elif decision == "ROLLBACK_SAFE":
            actions = apply_rollback(run_id, evidence)
            outcome = "ROLLED_BACK"
        elif decision == "BLOCK_HUMAN":
            actions = apply_block(run_id, owner, evidence)
            outcome = "BLOCKED"
        else:
            fail(f"Unsupported recovery decision: {decision}")

        finish_recovery(
            recovery_id,
            outcome=outcome,
            actions=actions,
        )
    except Exception as error:
        finish_recovery(
            recovery_id,
            outcome="FAILED",
            actions=actions,
            failure_reason=str(error),
        )
        raise

    with connect() as connection:
        final_run = get_run(connection, run_id)

    return {
        "recovery_id": recovery_id,
        "run_id": run_id,
        "decision": decision,
        "outcome": outcome,
        "status": final_run["status"],
        "recovery_decision": final_run["recovery_decision"],
        "evidence_sha256": evidence["evidence_sha256"],
        "actions": actions,
    }


def active_run_ids(connection: sqlite3.Connection) -> set[str]:
    placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
    return {
        str(row[0])
        for row in connection.execute(
            f"SELECT run_id FROM runs WHERE status IN ({placeholders})",
            ACTIVE_STATUSES,
        )
    }


def cleanup_orphans(*, dry_run: bool) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []

    with connect() as connection:
        active_runs = active_run_ids(connection)
        # ORCHESTRA_ACTIVE_TASK_SANDBOX_PROTECTION_V1
        #
        # A worker/reviewer reserves its SQLite task before creating the
        # nested sandbox. sandbox_container_id is finalized later, so the
        # generic control-plane task label is the authoritative race-free
        # reference during that interval.
        active_task_ids = {
            str(row["task_id"])
            for row in connection.execute(
                """
                SELECT task_id, run_id
                FROM tasks
                WHERE status = 'RUNNING'
                """
            ).fetchall()
            if str(row["run_id"]) in active_runs
        }
        references = connection.execute(
            """
            SELECT
                outer_container_name,
                sandbox_container_id,
                runtime_profile
            FROM worker_executions AS e
            JOIN runs AS r ON r.run_id = e.run_id
            WHERE r.status IN (
                'SNAPSHOTTING', 'RUNNING', 'REVIEWING',
                'WAITING_HUMAN', 'COMMITTING', 'RECOVERING', 'FAILED'
            )
            UNION ALL
            SELECT
                outer_container_name,
                sandbox_container_id,
                runtime_profile
            FROM reviewer_executions AS e
            JOIN runs AS r ON r.run_id = e.run_id
            WHERE r.status IN (
                'SNAPSHOTTING', 'RUNNING', 'REVIEWING',
                'WAITING_HUMAN', 'COMMITTING', 'RECOVERING', 'FAILED'
            )
            """
        ).fetchall()
        execution_bindings = connection.execute(
            """
            SELECT task_id, outer_container_name, runtime_profile
            FROM worker_executions
            UNION ALL
            SELECT task_id, outer_container_name, runtime_profile
            FROM reviewer_executions
            """
        ).fetchall()
        planner_names = {
            str(row["outer_container_name"])
            for row in connection.execute(
                """
                SELECT outer_container_name
                FROM orchestrator_executions
                """
            ).fetchall()
            if row["outer_container_name"]
        }
        active_planner_names = {
            str(row["outer_container_name"])
            for row in connection.execute(
                """
                SELECT outer_container_name
                FROM orchestrator_executions
                WHERE finished_at IS NULL
                """
            ).fetchall()
            if row["outer_container_name"]
        }
        known_run_bindings = {
            (str(row["project_id"]), str(row["run_id"]))
            for row in connection.execute(
                "SELECT project_id, run_id FROM runs"
            ).fetchall()
        }

    referenced_outer = {
        str(row["outer_container_name"])
        for row in references
        if row["outer_container_name"]
    } | active_planner_names
    referenced_sandbox = {
        str(row["sandbox_container_id"])
        for row in references
        if row["sandbox_container_id"]
    }
    referenced_profiles = {
        str(row["runtime_profile"])
        for row in references
        if row["runtime_profile"]
    }
    known_outer_names = planner_names | {
        str(row["outer_container_name"])
        for row in execution_bindings
        if row["outer_container_name"]
    }
    known_profile_names = {
        str(row["runtime_profile"])
        for row in execution_bindings
        if row["runtime_profile"]
    }
    known_bindings = {
        (str(row["task_id"]), str(value))
        for row in execution_bindings
        for value in (row["outer_container_name"], row["runtime_profile"])
        if row["task_id"] and value
    }

    if docker_exists():
        host_candidates: set[str] = set()
        for ownership_filter in ("label=orchestra-runtime-container=1",):
            result = run_command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    ownership_filter,
                    "--format",
                    "{{.Names}}",
                ],
                check=False,
            )
            if result.returncode == 0:
                host_candidates.update(result.stdout.splitlines())

        for name in sorted(host_candidates):
            if name in referenced_outer:
                continue
            container = inspect_host_container(name)
            if container is None:
                continue
            ownership = host_container_ownership(
                container,
                expected_name=name,
                known_names=known_outer_names,
            )
            if ownership is None:
                continue
            actions.append(
                {
                    "resource": "host-container",
                    "ownership": ownership,
                    "name": name,
                    "removed": False
                    if dry_run
                    else remove_host_container(
                        name,
                        known_names=known_outer_names,
                    ),
                }
            )

        nested_candidates: dict[str, str] = {}
        for ownership_filter in ("label=orchestra-sandbox=1",):
            nested = run_command(
                [
                    "docker",
                    "ps",
                    "-a",
                    "--filter",
                    ownership_filter,
                    "--format",
                    "{{.ID}} {{.Names}}",
                ],
                check=False,
            )
            if nested.returncode != 0:
                continue
            for line in nested.stdout.splitlines():
                parts = line.split(maxsplit=1)
                if parts:
                    nested_candidates[parts[0]] = (
                        parts[1] if len(parts) == 2 else ""
                    )

        for container_id, name in sorted(nested_candidates.items()):
            if any(
                container_id == reference
                or container_id.startswith(reference)
                or reference.startswith(container_id)
                for reference in referenced_sandbox
            ):
                continue
            container = inspect_nested_container(container_id)
            if container is None:
                continue
            ownership = nested_container_ownership(
                container,
                known_bindings=known_bindings,
            )
            if ownership is None:
                continue
            labels = ((container.get("Config") or {}).get("Labels") or {})
            container_task_id = str(
                labels.get("orchestra-task-id")
                or ""
            )
            if container_task_id in active_task_ids:
                continue
            actions.append(
                {
                    "resource": "nested-container",
                    "ownership": ownership,
                    "id": container_id,
                    "name": name,
                    "removed": False
                    if dry_run
                    else remove_nested_container(
                        container_id,
                        known_bindings=known_bindings,
                    ),
                }
            )

    profiles_root = HERMES_HOME / "profiles"

    if profiles_root.is_dir():
        for path in profiles_root.iterdir():
            if not path.is_dir():
                continue
            if not path.name.startswith((
                "agent-runtime-",
                "runtime-worker-",
                "runtime-reviewer-",
            )):
                continue
            if path.name not in known_profile_names:
                continue
            if path.name in referenced_profiles:
                continue
            removed = False
            if not dry_run:
                shutil.rmtree(path)
                removed = True
            actions.append(
                {
                    "resource": "runtime-profile",
                    "name": path.name,
                    "removed": removed,
                }
            )

    for kind in ("worker", "reviewer"):
        root = WORKSPACES / f".orchestra-{kind}-clones"

        if not root.is_dir():
            continue

        for project_path in root.iterdir():
            if not project_path.is_dir():
                continue
            for run_path in project_path.iterdir():
                if not run_path.is_dir():
                    continue
                if (
                    project_path.name,
                    run_path.name,
                ) not in known_run_bindings:
                    continue
                if run_path.name in active_runs:
                    continue
                removed = False
                if not dry_run:
                    if kind == "reviewer":
                        try:
                            run_path.chmod(
                                run_path.stat().st_mode | 0o700
                            )
                        except OSError:
                            pass

                        for child in run_path.rglob("*"):
                            try:
                                child.chmod(child.stat().st_mode | 0o200)
                            except OSError:
                                pass
                    shutil.rmtree(run_path)
                    removed = True
                actions.append(
                    {
                        "resource": f"{kind}-clone-tree",
                        "path": str(run_path),
                        "removed": removed,
                    }
                )

            if not dry_run:
                removed_parents = prune_empty_clone_parents(
                    project_path,
                    root,
                )
                for removed_parent in removed_parents:
                    actions.append(
                        {
                            "resource": f"{kind}-clone-parent",
                            "path": removed_parent,
                            "removed": True,
                        }
                    )

    return {
        "dry_run": dry_run,
        "actions": actions,
        "active_runs": sorted(active_runs),
    }


def command_assess(arguments: argparse.Namespace) -> None:
    print(
        json.dumps(
            assess_run(arguments.run),
            indent=2,
            sort_keys=True,
        )
    )


def command_recover(arguments: argparse.Namespace) -> None:
    print(
        json.dumps(
            recover_run(
                run_id=arguments.run,
                owner=arguments.owner,
                stale_seconds=arguments.stale_seconds,
                force=arguments.force,
                expected_decision=arguments.expected_decision,
            ),
            indent=2,
            sort_keys=True,
        )
    )


def command_sweep(arguments: argparse.Namespace) -> None:
    with connect() as connection:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        rows = connection.execute(
            f"""
            SELECT run_id, heartbeat_at
            FROM runs
            WHERE status IN ({placeholders})
            ORDER BY created_at
            """,
            ACTIVE_STATUSES,
        ).fetchall()

    results: list[dict[str, Any]] = []

    for row in rows:
        age = age_seconds(row["heartbeat_at"])

        if age is not None and age < arguments.stale_seconds:
            continue

        if arguments.dry_run:
            evidence = assess_run(row["run_id"])
            results.append(
                {
                    "run_id": row["run_id"],
                    "decision": evidence["decision"],
                    "heartbeat_age_seconds": age,
                    "dry_run": True,
                }
            )
        else:
            results.append(
                recover_run(
                    run_id=row["run_id"],
                    owner=arguments.owner,
                    stale_seconds=arguments.stale_seconds,
                    force=False,
                )
            )

    orphan_result = cleanup_orphans(dry_run=arguments.dry_run)
    print(
        json.dumps(
            {
                "runs": results,
                "orphans": orphan_result,
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_cleanup_orphans(arguments: argparse.Namespace) -> None:
    print(
        json.dumps(
            cleanup_orphans(dry_run=arguments.dry_run),
            indent=2,
            sort_keys=True,
        )
    )


def command_status(arguments: argparse.Namespace) -> None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT
                rx.*,
                r.status AS run_status,
                r.recovery_decision
            FROM recovery_executions AS rx
            JOIN runs AS r ON r.run_id = rx.run_id
            WHERE rx.recovery_id = ?
            """,
            (arguments.recovery,),
        ).fetchone()

    if row is None:
        fail(f"Unknown recovery execution: {arguments.recovery}")

    print(json.dumps(dict(row), indent=2, sort_keys=True))


def command_list(arguments: argparse.Namespace) -> None:
    query = """
        SELECT
            recovery_id,
            run_id,
            observed_status,
            decision,
            outcome,
            policy_version,
            controller_owner,
            created_at,
            finished_at,
            failure_reason
        FROM recovery_executions
    """
    parameters: tuple[Any, ...] = ()

    if arguments.run:
        query += " WHERE run_id = ?"
        parameters = (arguments.run,)

    query += " ORDER BY created_at DESC"

    with connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(query, parameters)
        ]

    print(json.dumps(rows, indent=2, sort_keys=True))


def command_self_test(_: argparse.Namespace) -> None:
    cases = {
        ("APPROVE", "PASS"): "RESUME_SAFE",
        ("missing-worktree", "main-base"): "ROLLBACK_SAFE",
        ("main-diverged", "snapshot-valid"): "BLOCK_HUMAN",
    }

    if set(cases.values()) != {
        "RESUME_SAFE",
        "ROLLBACK_SAFE",
        "BLOCK_HUMAN",
    }:
        fail("Recovery decision set is incomplete")

    if POLICY_VERSION != "recovery-policy-v1":
        fail("Unexpected recovery policy version")

    print("Orchestra recovery decision matrix: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra deterministic recovery manager"
    )
    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    assess = subparsers.add_parser("assess")
    assess.add_argument("--run", required=True)
    assess.set_defaults(function=command_assess)

    recover = subparsers.add_parser("recover")
    recover.add_argument("--run", required=True)
    recover.add_argument("--owner", required=True)
    recover.add_argument(
        "--stale-seconds",
        type=int,
        default=300,
    )
    recover.add_argument("--force", action="store_true")
    recover.add_argument(
        "--expected-decision",
        choices=(
            "RESUME_SAFE",
            "ROLLBACK_SAFE",
            "BLOCK_HUMAN",
        ),
    )
    recover.set_defaults(function=command_recover)

    sweep = subparsers.add_parser("sweep")
    sweep.add_argument("--owner", required=True)
    sweep.add_argument(
        "--stale-seconds",
        type=int,
        default=300,
    )
    sweep.add_argument("--dry-run", action="store_true")
    sweep.set_defaults(function=command_sweep)

    cleanup = subparsers.add_parser("cleanup-orphans")
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.set_defaults(function=command_cleanup_orphans)

    status = subparsers.add_parser("status")
    status.add_argument("--recovery", required=True)
    status.set_defaults(function=command_status)

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--run")
    list_parser.set_defaults(function=command_list)

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        arguments.function(arguments)
    except RecoveryError as error:
        print(f"Recovery error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
