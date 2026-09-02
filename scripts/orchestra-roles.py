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

import yaml


ROOT = Path(
    os.environ.get(
        "ORCHESTRA_ROOT",
        "/opt/orchestra",
    )
)

REPO = ROOT / "repo"
ROLES_FILE = REPO / "config" / "roles.toml"
PROFILE_TEMPLATES = REPO / "profiles"
HERMES_HOME = ROOT / "state" / "hermes-home"
PROFILE_ROOT = HERMES_HOME / "profiles"
GLOBAL_AUTH = HERMES_HOME / "auth.json"
DATABASE = ROOT / "state" / "controller" / "orchestra.db"

PROFILE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{2,63}$")

ALLOWED_KINDS = {
    "orchestrator",
    "worker",
    "reviewer",
    "recovery",
}

ALLOWED_REASONING = {
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
}

ALLOWED_TOOLSETS = {
    "terminal",
    "file",
    "web",
    "browser",
    "skills",
    "todo",
    "memory",
    "session_search",
}

ALLOWED_WORKSPACE_MODES = {
    "none",
    "write",
    "read_only",
    "controller_only",
}

IMPLEMENTATION_TOOLSETS = {
    "terminal",
    "file",
    "web",
    "browser",
}


class RoleError(RuntimeError):
    pass


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def load_document() -> dict[str, Any]:
    with ROLES_FILE.open("rb") as stream:
        document = tomllib.load(stream)

    if document.get("schema_version") != 1:
        raise RoleError("Unsupported roles schema version")

    if document.get("provider") != "openai-codex":
        raise RoleError("Provider must be openai-codex")

    if document.get("model") != "gpt-5.6-sol":
        raise RoleError("Model must be gpt-5.6-sol")

    if document.get("auth_strategy") != "shared-root-symlink":
        raise RoleError("Unsupported auth strategy")

    return document


def require_string(
    table: dict[str, Any],
    key: str,
    role_id: str,
) -> str:
    value = table.get(key)

    if not isinstance(value, str) or not value.strip():
        raise RoleError(
            f"{role_id}: {key} must be a non-empty string"
        )

    return value.strip()


def require_bool(
    table: dict[str, Any],
    key: str,
    role_id: str,
) -> bool:
    value = table.get(key)

    if not isinstance(value, bool):
        raise RoleError(
            f"{role_id}: {key} must be a boolean"
        )

    return value


def require_int(
    table: dict[str, Any],
    key: str,
    role_id: str,
) -> int:
    value = table.get(key)

    if not isinstance(value, int) or isinstance(value, bool):
        raise RoleError(
            f"{role_id}: {key} must be an integer"
        )

    return value


def require_string_list(
    table: dict[str, Any],
    key: str,
    role_id: str,
) -> list[str]:
    value = table.get(key)

    if (
        not isinstance(value, list)
        or not all(
            isinstance(item, str) and item.strip()
            for item in value
        )
    ):
        raise RoleError(
            f"{role_id}: {key} must be a string list"
        )

    return [item.strip() for item in value]


def role_hash(role: dict[str, Any]) -> str:
    canonical = json.dumps(
        role,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    return hashlib.sha256(canonical).hexdigest()


def discover_roles() -> list[dict[str, Any]]:
    document = load_document()
    raw_roles = document.get("roles")

    if not isinstance(raw_roles, dict) or not raw_roles:
        raise RoleError("No roles defined")

    result: list[dict[str, Any]] = []
    seen_profiles: set[str] = set()

    for role_id, table in raw_roles.items():
        if not isinstance(table, dict):
            raise RoleError(f"{role_id}: invalid role table")

        profile = require_string(table, "profile", role_id)
        kind = require_string(table, "kind", role_id)
        description = require_string(
            table,
            "description",
            role_id,
        )

        reasoning_effort = require_string(
            table,
            "reasoning_effort",
            role_id,
        )

        max_turns = require_int(
            table,
            "max_turns",
            role_id,
        )

        toolsets = require_string_list(
            table,
            "toolsets",
            role_id,
        )

        skills = require_string_list(
            table,
            "skills",
            role_id,
        )

        workspace_mode = require_string(
            table,
            "workspace_mode",
            role_id,
        )

        may_commit = require_bool(
            table,
            "may_commit",
            role_id,
        )

        may_push = require_bool(
            table,
            "may_push",
            role_id,
        )

        network = require_bool(
            table,
            "network",
            role_id,
        )

        cpu = require_int(table, "cpu", role_id)
        memory_mb = require_int(
            table,
            "memory_mb",
            role_id,
        )

        if not PROFILE_PATTERN.fullmatch(profile):
            raise RoleError(
                f"{role_id}: invalid profile name {profile!r}"
            )

        if profile in seen_profiles:
            raise RoleError(
                f"Duplicate profile name: {profile}"
            )

        if kind not in ALLOWED_KINDS:
            raise RoleError(
                f"{role_id}: invalid kind {kind!r}"
            )

        if reasoning_effort not in ALLOWED_REASONING:
            raise RoleError(
                f"{role_id}: invalid reasoning effort"
            )

        unknown_toolsets = set(toolsets) - ALLOWED_TOOLSETS

        if unknown_toolsets:
            raise RoleError(
                f"{role_id}: unknown toolsets "
                f"{sorted(unknown_toolsets)!r}"
            )

        if workspace_mode not in ALLOWED_WORKSPACE_MODES:
            raise RoleError(
                f"{role_id}: invalid workspace mode"
            )

        if may_push:
            raise RoleError(
                f"{role_id}: push must remain disabled"
            )

        if not 1 <= max_turns <= 500:
            raise RoleError(
                f"{role_id}: max_turns out of range"
            )

        if not 1 <= cpu <= 64:
            raise RoleError(
                f"{role_id}: cpu out of range"
            )

        if not 512 <= memory_mb <= 131072:
            raise RoleError(
                f"{role_id}: memory_mb out of range"
            )

        if kind == "orchestrator":
            forbidden = set(toolsets) & IMPLEMENTATION_TOOLSETS

            if forbidden:
                raise RoleError(
                    f"{role_id}: orchestrator has "
                    f"implementation tools {sorted(forbidden)!r}"
                )

            if workspace_mode != "none":
                raise RoleError(
                    f"{role_id}: orchestrator workspace "
                    "must be none"
                )

            if may_commit:
                raise RoleError(
                    f"{role_id}: orchestrator cannot commit"
                )

        if kind == "worker":
            if workspace_mode != "write":
                raise RoleError(
                    f"{role_id}: worker workspace must be write"
                )

            if not may_commit:
                raise RoleError(
                    f"{role_id}: worker must be allowed "
                    "to commit inside its transaction"
                )

        if kind == "reviewer":
            if workspace_mode != "read_only":
                raise RoleError(
                    f"{role_id}: reviewer must be read_only"
                )

            if may_commit:
                raise RoleError(
                    f"{role_id}: reviewer cannot commit"
                )

        if kind == "recovery":
            if workspace_mode != "controller_only":
                raise RoleError(
                    f"{role_id}: recovery workspace must "
                    "be controller_only"
                )

            if may_commit:
                raise RoleError(
                    f"{role_id}: recovery cannot commit"
                )

            if "web" in toolsets or "browser" in toolsets:
                raise RoleError(
                    f"{role_id}: recovery cannot browse the web"
                )

        role = {
            "role_id": role_id,
            "profile": profile,
            "kind": kind,
            "description": description,
            "reasoning_effort": reasoning_effort,
            "max_turns": max_turns,
            "toolsets": toolsets,
            "skills": skills,
            "workspace_mode": workspace_mode,
            "may_commit": may_commit,
            "may_push": may_push,
            "network": network,
            "cpu": cpu,
            "memory_mb": memory_mb,
        }

        role["config_hash"] = role_hash(role)

        template = (
            PROFILE_TEMPLATES
            / profile
            / "SOUL.md"
        )

        if not template.is_file():
            raise RoleError(
                f"{role_id}: missing SOUL template {template}"
            )

        seen_profiles.add(profile)
        result.append(role)

    return result


def command_validate() -> None:
    roles = discover_roles()

    print(
        "Role definitions: PASS "
        f"({len(roles)} role(s))"
    )


def command_sync() -> None:
    roles = discover_roles()
    now = utc_now()

    connection = sqlite3.connect(
        DATABASE,
        timeout=10,
    )

    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("BEGIN IMMEDIATE")

        for role in roles:
            connection.execute(
                """
                INSERT INTO roles (
                    role_id,
                    profile_name,
                    role_kind,
                    description,
                    reasoning_effort,
                    max_turns,
                    toolsets_json,
                    skills_json,
                    workspace_mode,
                    may_commit,
                    may_push,
                    network_enabled,
                    cpu_limit,
                    memory_mb,
                    enabled,
                    config_source,
                    config_hash,
                    registered_at,
                    updated_at
                )
                VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, 1, ?, ?, ?, ?
                )
                ON CONFLICT(role_id)
                DO UPDATE SET
                    profile_name = excluded.profile_name,
                    role_kind = excluded.role_kind,
                    description = excluded.description,
                    reasoning_effort =
                        excluded.reasoning_effort,
                    max_turns = excluded.max_turns,
                    toolsets_json = excluded.toolsets_json,
                    skills_json = excluded.skills_json,
                    workspace_mode = excluded.workspace_mode,
                    may_commit = excluded.may_commit,
                    may_push = excluded.may_push,
                    network_enabled =
                        excluded.network_enabled,
                    cpu_limit = excluded.cpu_limit,
                    memory_mb = excluded.memory_mb,
                    enabled = 1,
                    config_source = excluded.config_source,
                    config_hash = excluded.config_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    role["role_id"],
                    role["profile"],
                    role["kind"],
                    role["description"],
                    role["reasoning_effort"],
                    role["max_turns"],
                    json.dumps(role["toolsets"]),
                    json.dumps(role["skills"]),
                    role["workspace_mode"],
                    int(role["may_commit"]),
                    int(role["may_push"]),
                    int(role["network"]),
                    role["cpu"],
                    role["memory_mb"],
                    str(ROLES_FILE),
                    role["config_hash"],
                    now,
                    now,
                ),
            )

        role_ids = [role["role_id"] for role in roles]
        placeholders = ",".join("?" for _ in role_ids)

        connection.execute(
            f"""
            UPDATE roles
            SET enabled = 0,
                updated_at = ?
            WHERE role_id NOT IN ({placeholders})
            """,
            (now, *role_ids),
        )

        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        "Role registry sync: PASS "
        f"({len(roles)} role(s))"
    )


def meaningful_env_lines(path: Path) -> list[str]:
    if not path.exists():
        return []

    return [
        line.strip()
        for line in path.read_text().splitlines()
        if line.strip()
        and not line.lstrip().startswith("#")
    ]


def command_verify_profiles() -> None:
    roles = discover_roles()

    if not GLOBAL_AUTH.is_file():
        raise RoleError("Global auth.json is absent")

    expected_auth = GLOBAL_AUTH.resolve(strict=True)

    for role in roles:
        profile = role["profile"]
        directory = PROFILE_ROOT / profile
        config_path = directory / "config.yaml"
        soul_path = directory / "SOUL.md"
        profile_metadata = directory / "profile.yaml"
        auth_path = directory / "auth.json"
        no_skills_marker = (
            directory / ".no-bundled-skills"
        )

        for required in (
            directory,
            config_path,
            soul_path,
            profile_metadata,
            no_skills_marker,
        ):
            if not required.exists():
                raise RoleError(
                    f"{profile}: missing {required}"
                )

        if not auth_path.is_symlink():
            raise RoleError(
                f"{profile}: auth.json is not a symlink"
            )

        if auth_path.resolve(strict=True) != expected_auth:
            raise RoleError(
                f"{profile}: auth.json target is incorrect"
            )

        config = yaml.safe_load(
            config_path.read_text()
        ) or {}

        model = config.get("model") or {}
        agent = config.get("agent") or {}
        terminal = config.get("terminal") or {}
        platform_toolsets = (
            config.get("platform_toolsets") or {}
        )

        if model.get("provider") != "openai-codex":
            raise RoleError(
                f"{profile}: provider mismatch"
            )

        if model.get("default") != "gpt-5.6-sol":
            raise RoleError(
                f"{profile}: model mismatch"
            )

        if (
            agent.get("reasoning_effort")
            != role["reasoning_effort"]
        ):
            raise RoleError(
                f"{profile}: reasoning effort mismatch"
            )

        if agent.get("max_turns") != role["max_turns"]:
            raise RoleError(
                f"{profile}: max_turns mismatch"
            )

        if (
            platform_toolsets.get("cli")
            != role["toolsets"]
        ):
            raise RoleError(
                f"{profile}: CLI toolsets mismatch"
            )

        expected_terminal = {
            "backend": "docker",
            "cwd": "/workspace",
            "home_mode": "profile",
            "docker_mount_cwd_to_workspace": False,
            "docker_run_as_host_user": False,
            "docker_network": role["network"],
            "container_cpu": role["cpu"],
            "container_memory": role["memory_mb"],
        }

        for key, expected in expected_terminal.items():
            actual = terminal.get(key)

            if actual != expected:
                raise RoleError(
                    f"{profile}: terminal.{key}="
                    f"{actual!r}, expected {expected!r}"
                )

        template = (
            PROFILE_TEMPLATES
            / profile
            / "SOUL.md"
        )

        if soul_path.read_bytes() != template.read_bytes():
            raise RoleError(
                f"{profile}: SOUL differs from template"
            )

        metadata = yaml.safe_load(
            profile_metadata.read_text(
                encoding="utf-8",
            )
        ) or {}

        actual_description = metadata.get(
            "description"
        )

        if actual_description != role["description"]:
            raise RoleError(
                f"{profile}: profile description mismatch; "
                f"actual={actual_description!r}, "
                f"expected={role['description']!r}"
            )

        if metadata.get("description_auto") not in (
            None,
            False,
        ):
            raise RoleError(
                f"{profile}: description must be "
                "user-authored, not automatic"
            )

        if meaningful_env_lines(directory / ".env"):
            raise RoleError(
                f"{profile}: local .env contains secrets"
            )

        skills_directory = directory / "skills"

        actual_skills: set[str] = set()

        if skills_directory.is_dir():
            for skill_file in skills_directory.rglob(
                "SKILL.md"
            ):
                text = skill_file.read_text(
                    encoding="utf-8",
                    errors="replace",
                )

                lines = text.splitlines()

                if (
                    not lines
                    or lines[0].strip() != "---"
                ):
                    raise RoleError(
                        f"{profile}: invalid skill "
                        f"frontmatter in {skill_file}"
                    )

                try:
                    end = (
                        lines[1:].index("---")
                        + 1
                    )
                except ValueError as error:
                    raise RoleError(
                        f"{profile}: unterminated "
                        f"frontmatter in {skill_file}"
                    ) from error

                metadata = yaml.safe_load(
                    "\n".join(lines[1:end])
                ) or {}

                name = metadata.get("name")

                if not isinstance(name, str) or not name.strip():
                    raise RoleError(
                        f"{profile}: skill name absent "
                        f"in {skill_file}"
                    )

                actual_skills.add(name.strip())

        if actual_skills != set(role["skills"]):
            raise RoleError(
                f"{profile}: skills mismatch; "
                f"actual={sorted(actual_skills)!r}, "
                f"expected={sorted(role['skills'])!r}"
            )

        print(
            f"{profile}: PASS "
            f"({role['kind']}, "
            f"{role['reasoning_effort']})"
        )

    print(
        "Hermes role profiles: PASS "
        f"({len(roles)} profile(s))"
    )


def command_list() -> None:
    for role in discover_roles():
        print(
            f"{role['role_id']}: "
            f"profile={role['profile']} "
            f"kind={role['kind']} "
            f"workspace={role['workspace_mode']} "
            f"reasoning={role['reasoning_effort']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestra role registry"
    )

    parser.add_argument(
        "command",
        choices=(
            "validate",
            "sync",
            "verify-profiles",
            "list",
        ),
    )

    arguments = parser.parse_args()

    try:
        if arguments.command == "validate":
            command_validate()
        elif arguments.command == "sync":
            command_sync()
        elif arguments.command == "verify-profiles":
            command_verify_profiles()
        elif arguments.command == "list":
            command_list()
    except RoleError as error:
        raise SystemExit(
            f"Role validation failed: {error}"
        ) from error


if __name__ == "__main__":
    main()
