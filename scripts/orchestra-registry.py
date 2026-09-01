#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(
    os.environ.get(
        "ORCHESTRA_ROOT",
        "/opt/orchestra",
    )
)

REPO = ROOT / "repo"
CONTROLLER_FILE = REPO / "config" / "controller.toml"
PROJECTS_DIRECTORY = REPO / "config" / "projects.d"
POLICIES_DIRECTORY = REPO / "config" / "policies"
DATABASE = ROOT / "state" / "controller" / "orchestra.db"

PROJECT_ID_PATTERN = re.compile(
    r"^[a-z][a-z0-9-]{1,62}$"
)


class RegistryError(RuntimeError):
    pass


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except Exception as error:
        raise RegistryError(
            f"Unable to parse {path}: {error}"
        ) from error


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def require_table(
    data: dict[str, Any],
    name: str,
    source: Path,
) -> dict[str, Any]:
    value = data.get(name)

    if not isinstance(value, dict):
        raise RegistryError(
            f"{source}: missing [{name}] table"
        )

    return value


def require_string(
    table: dict[str, Any],
    key: str,
    source: Path,
) -> str:
    value = table.get(key)

    if not isinstance(value, str) or not value.strip():
        raise RegistryError(
            f"{source}: {key} must be a non-empty string"
        )

    return value.strip()


def require_boolean(
    table: dict[str, Any],
    key: str,
    source: Path,
) -> bool:
    value = table.get(key)

    if not isinstance(value, bool):
        raise RegistryError(
            f"{source}: {key} must be a boolean"
        )

    return value


def require_integer(
    table: dict[str, Any],
    key: str,
    source: Path,
) -> int:
    value = table.get(key)

    if not isinstance(value, int) or isinstance(value, bool):
        raise RegistryError(
            f"{source}: {key} must be an integer"
        )

    return value


def load_controller() -> dict[str, Any]:
    controller = read_toml(CONTROLLER_FILE)

    if controller.get("schema_version") != 1:
        raise RegistryError(
            "Unsupported controller schema version"
        )

    if "ORCHESTRA_ROOT" in os.environ:
        controller["root"] = str(ROOT)

    return controller


def validate_project(
    source: Path,
    controller: dict[str, Any],
) -> dict[str, Any]:
    data = read_toml(source)

    if data.get("schema_version") != 1:
        raise RegistryError(
            f"{source}: unsupported schema version"
        )

    project = require_table(data, "project", source)
    paths = require_table(data, "paths", source)
    git = require_table(data, "git", source)
    execution = require_table(data, "execution", source)
    review = require_table(data, "review", source)

    project_id = require_string(
        project,
        "id",
        source,
    )

    if not PROJECT_ID_PATTERN.fullmatch(project_id):
        raise RegistryError(
            f"{source}: invalid project id {project_id!r}"
        )

    display_name = require_string(
        project,
        "name",
        source,
    )

    enabled = require_boolean(
        project,
        "enabled",
        source,
    )

    policy_id = require_string(
        project,
        "policy",
        source,
    )

    policy_file = (
        POLICIES_DIRECTORY
        / f"{policy_id}.toml"
    )

    if not policy_file.is_file():
        raise RegistryError(
            f"{source}: missing policy {policy_id!r}"
        )

    repo_path = Path(
        require_string(
            paths,
            "repo",
            source,
        )
    ).resolve(strict=False)

    data_path = Path(
        require_string(
            paths,
            "data",
            source,
        )
    ).resolve(strict=False)

    workspace_root = (
        Path(controller["root"])
        / "workspaces"
    ).resolve(strict=False)

    project_data_root = (
        Path(controller["root"])
        / "project-data"
    ).resolve(strict=False)

    if not repo_path.is_absolute():
        raise RegistryError(
            f"{source}: repo path must be absolute"
        )

    if not data_path.is_absolute():
        raise RegistryError(
            f"{source}: data path must be absolute"
        )

    if not is_within(repo_path, workspace_root):
        raise RegistryError(
            f"{source}: repo path escapes "
            f"{workspace_root}"
        )

    if not is_within(data_path, project_data_root):
        raise RegistryError(
            f"{source}: data path escapes "
            f"{project_data_root}"
        )

    default_branch = require_string(
        git,
        "default_branch",
        source,
    )

    allow_push = require_boolean(
        git,
        "allow_push",
        source,
    )

    require_clean = require_boolean(
        git,
        "require_clean",
        source,
    )

    if allow_push:
        raise RegistryError(
            f"{source}: automatic push is forbidden"
        )

    writer_concurrency = require_integer(
        execution,
        "writer_concurrency",
        source,
    )

    max_parallel_tasks = require_integer(
        execution,
        "max_parallel_tasks",
        source,
    )

    if writer_concurrency != 1:
        raise RegistryError(
            f"{source}: writer_concurrency must be 1"
        )

    if not 1 <= max_parallel_tasks <= 64:
        raise RegistryError(
            f"{source}: max_parallel_tasks "
            f"must be between 1 and 64"
        )

    review_required = require_boolean(
        review,
        "required",
        source,
    )

    if not review_required:
        raise RegistryError(
            f"{source}: independent review is mandatory"
        )

    config_hash = hashlib.sha256(
        source.read_bytes()
    ).hexdigest()

    return {
        "project_id": project_id,
        "display_name": display_name,
        "enabled": enabled,
        "policy_id": policy_id,
        "repo_path": str(repo_path),
        "data_path": str(data_path),
        "default_branch": default_branch,
        "require_clean": require_clean,
        "writer_concurrency": writer_concurrency,
        "max_parallel_tasks": max_parallel_tasks,
        "config_source": str(source),
        "config_hash": config_hash,
    }


def discover_projects() -> list[dict[str, Any]]:
    controller = load_controller()
    projects: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_repos: set[str] = set()

    for source in sorted(
        PROJECTS_DIRECTORY.glob("*.toml")
    ):
        project = validate_project(
            source,
            controller,
        )

        project_id = project["project_id"]
        repo_path = project["repo_path"]

        if project_id in seen_ids:
            raise RegistryError(
                f"Duplicate project id: {project_id}"
            )

        if repo_path in seen_repos:
            raise RegistryError(
                f"Duplicate repository path: {repo_path}"
            )

        seen_ids.add(project_id)
        seen_repos.add(repo_path)
        projects.append(project)

    return projects


def command_validate(
    as_json: bool,
) -> None:
    projects = discover_projects()

    if as_json:
        print(
            json.dumps(
                projects,
                indent=2,
                sort_keys=True,
            )
        )
    else:
        print(
            "Project registry: PASS "
            f"({len(projects)} project(s))"
        )


def command_list(
    as_json: bool,
) -> None:
    projects = discover_projects()

    if as_json:
        print(
            json.dumps(
                projects,
                indent=2,
                sort_keys=True,
            )
        )
        return

    if not projects:
        print("No registered projects.")
        return

    for project in projects:
        state = (
            "enabled"
            if project["enabled"]
            else "disabled"
        )

        print(
            f"{project['project_id']}: "
            f"{project['display_name']} "
            f"({state})"
        )


def command_sync() -> None:
    projects = discover_projects()

    connection = sqlite3.connect(
        DATABASE,
        timeout=10,
    )

    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        now = utc_now()

        connection.execute("BEGIN IMMEDIATE")

        for project in projects:
            connection.execute(
                """
                INSERT INTO projects (
                    project_id,
                    display_name,
                    repo_path,
                    data_path,
                    policy_id,
                    enabled,
                    config_source,
                    config_hash,
                    registered_at,
                    updated_at,
                    default_branch,
                    archived,
                    repository_mode,
                    resource_revision
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 'existing', 1)
                ON CONFLICT(project_id)
                DO UPDATE SET
                    resource_revision = projects.resource_revision + CASE WHEN
                           projects.display_name IS NOT excluded.display_name
                        OR projects.repo_path IS NOT excluded.repo_path
                        OR projects.data_path IS NOT excluded.data_path
                        OR projects.policy_id IS NOT excluded.policy_id
                        OR projects.enabled IS NOT CASE
                            WHEN projects.archived = 1 THEN 0
                            ELSE excluded.enabled
                        END
                        OR projects.config_source IS NOT excluded.config_source
                        OR projects.config_hash IS NOT excluded.config_hash
                        OR projects.default_branch IS NOT excluded.default_branch
                    THEN 1 ELSE 0 END,
                    display_name = excluded.display_name,
                    repo_path = excluded.repo_path,
                    data_path = excluded.data_path,
                    policy_id = excluded.policy_id,
                    enabled = CASE
                        WHEN projects.archived = 1 THEN 0
                        ELSE excluded.enabled
                    END,
                    config_source = excluded.config_source,
                    config_hash = excluded.config_hash,
                    default_branch = excluded.default_branch,
                    updated_at = CASE WHEN
                           projects.display_name IS NOT excluded.display_name
                        OR projects.repo_path IS NOT excluded.repo_path
                        OR projects.data_path IS NOT excluded.data_path
                        OR projects.policy_id IS NOT excluded.policy_id
                        OR projects.enabled IS NOT CASE
                            WHEN projects.archived = 1 THEN 0
                            ELSE excluded.enabled
                        END
                        OR projects.config_source IS NOT excluded.config_source
                        OR projects.config_hash IS NOT excluded.config_hash
                        OR projects.default_branch IS NOT excluded.default_branch
                    THEN excluded.updated_at ELSE projects.updated_at END
                """,
                (
                    project["project_id"],
                    project["display_name"],
                    project["repo_path"],
                    project["data_path"],
                    project["policy_id"],
                    int(project["enabled"]),
                    project["config_source"],
                    project["config_hash"],
                    now,
                    now,
                    project["default_branch"],
                ),
            )

        project_ids = [
            project["project_id"]
            for project in projects
        ]

        if project_ids:
            placeholders = ",".join(
                "?"
                for _ in project_ids
            )

            connection.execute(
                f"""
                UPDATE projects
                SET enabled = 0,
                    updated_at = ?,
                    resource_revision = resource_revision + 1
                WHERE project_id NOT IN ({placeholders})
                  AND enabled != 0
                """,
                (now, *project_ids),
            )
        else:
            connection.execute(
                """
                UPDATE projects
                SET enabled = 0,
                    updated_at = ?,
                    resource_revision = resource_revision + 1
                WHERE enabled != 0
                """,
                (now,),
            )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        "Project registry sync: PASS "
        f"({len(projects)} project(s))"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestra project registry"
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=True,
    )

    validate_parser = subparsers.add_parser(
        "validate"
    )

    validate_parser.add_argument(
        "--json",
        action="store_true",
    )

    list_parser = subparsers.add_parser(
        "list"
    )

    list_parser.add_argument(
        "--json",
        action="store_true",
    )

    subparsers.add_parser("sync")

    arguments = parser.parse_args()

    try:
        if arguments.command == "validate":
            command_validate(arguments.json)
        elif arguments.command == "list":
            command_list(arguments.json)
        elif arguments.command == "sync":
            command_sync()
    except RegistryError as error:
        raise SystemExit(
            f"Registry validation failed: {error}"
        ) from error


if __name__ == "__main__":
    main()
