#!/usr/bin/env python3

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any, NoReturn

from model_provider import ModelProvider

from agent_runtime import (
    AgentRuntime,
    RuntimeEvent,
    RuntimeError as AgentRuntimeError,
    RuntimeRequest,
    RuntimeRole,
    create_runtime,
    record_runtime_failure,
)
from runtime_control import (
    persist_runtime_event,
    require_runtime_state,
    runtime_from_role,
    runtime_kind_of,
)


ROOT = Path(
    os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")
).resolve()
REPO = ROOT / "repo"
DATABASE = ROOT / "state/controller/orchestra.db"
ORCHESTRATOR_SCRIPT = REPO / "scripts/orchestra-orchestrator.py"
EXECUTIONS_ROOT = ROOT / "state/controller/orchestrator-executions"
JSON_BEGIN = "ORCHESTRA_PLAN_JSON_BEGIN"
JSON_END = "ORCHESTRA_PLAN_JSON_END"


class PlannerError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise PlannerError(message)


def utc_now() -> str:
    from datetime import datetime, timezone

    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def persist_transcript(path: Path, output: str) -> None:
    flags = os.O_WRONLY | os.O_TRUNC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(output)


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def load_orchestrator() -> Any:
    specification = importlib.util.spec_from_file_location(
        "orchestra_orchestrator",
        ORCHESTRATOR_SCRIPT,
    )
    if specification is None or specification.loader is None:
        fail("Unable to load Orchestra orchestrator module")

    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def parse_output(output: str, marker: str) -> dict[str, Any]:
    lines = output.splitlines()
    if not any(line.strip() == marker for line in lines):
        fail("Planner completion marker is absent")

    try:
        begin = next(
            index for index, line in enumerate(lines)
            if line.strip() == JSON_BEGIN
        )
        end = next(
            index for index, line in enumerate(lines[begin + 1 :], start=begin + 1)
            if line.strip() == JSON_END
        )
    except StopIteration as error:
        fail("Planner JSON delimiters are absent")

    payload_lines = lines[begin + 1 : end]
    while payload_lines and not payload_lines[0].strip():
        payload_lines.pop(0)
    while payload_lines and not payload_lines[-1].strip():
        payload_lines.pop()
    if payload_lines and payload_lines[0].strip().startswith("```"):
        payload_lines.pop(0)
    if payload_lines and payload_lines[-1].strip() == "```":
        payload_lines.pop()
    content = "\n".join(payload_lines).strip()
    if not content:
        fail("Planner JSON payload is empty")

    try:
        payload = json.loads(content)
    except json.JSONDecodeError as error:
        fail(f"Planner JSON is invalid: {error}")

    if not isinstance(payload, dict):
        fail("Planner JSON payload is not an object")
    return payload


def project_context(project_ids: list[str]) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in project_ids)
    with connect() as connection:
        rows = connection.execute(
            f"""
            SELECT project_id, display_name, repo_path, data_path, policy_id, enabled
            FROM projects
            WHERE project_id IN ({placeholders})
            ORDER BY project_id
            """,
            tuple(project_ids),
        ).fetchall()

    found = {row["project_id"] for row in rows}
    missing = set(project_ids) - found
    if missing:
        fail("Unknown projects: " + ", ".join(sorted(missing)))
    if any(not row["enabled"] for row in rows):
        disabled = [row["project_id"] for row in rows if not row["enabled"]]
        fail("Disabled projects: " + ", ".join(disabled))

    return [dict(row) for row in rows]


def build_prompt(
    objective: str,
    projects: list[dict[str, Any]],
    marker: str,
) -> str:
    project_json = json.dumps(projects, indent=2, sort_keys=True)
    return f"""You are the Orchestra planning role. Produce a small, safe and verifiable
execution DAG for the objective below. You do not modify repositories and you
do not execute tasks.

Objective:
{objective}

Available enabled projects:
{project_json}

Allowed worker roles:
- worker_code
- worker_tests
- worker_docs

Return exactly one JSON object between these exact delimiter lines:
{JSON_BEGIN}
{JSON_END}

The JSON contract is:
{{
  "schema_version": 1,
  "objective": "same objective, normalized without changing intent",
  "max_parallel_tasks": 1,
  "tasks": [
    {{
      "key": "short_lowercase_key",
      "kind": "PIPELINE",
      "project_id": "one available project id",
      "role_id": "one allowed worker role",
      "instruction": "precise implementation instruction including commit expectations",
      "acceptance_criteria": ["objective and observable criterion"],
      "marker": "ONE_LINE_UPPERCASE_COMPLETION_MARKER",
      "dependencies": ["earlier_task_key"],
      "priority": 100,
      "max_attempts": 1
    }}
  ]
}}

Rules:
- Use only PIPELINE tasks.
- Use 1 to 8 tasks.
- Dependencies must form an acyclic graph.
- Each task must be independently reviewable and produce one Git commit.
- A project has writer concurrency 1, so same-project write tasks must be
  dependency-ordered even when logically independent.
- Tests should depend on the code they validate. Documentation should depend
  on the behavior it documents.
- Never request a push, secret access, direct main modification, or bypass of
  review.
- Every task must include concrete acceptance criteria.
- Output no additional JSON object.

After the JSON end delimiter, print this exact completion marker on its own
line:
{marker}
"""


def reserve_execution(
    *,
    execution_id: str,
    runtime_request_id: str,
    prompt_path: Path,
    output_path: Path,
    marker: str,
    runtime_kind: str,
) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO orchestrator_executions (
                execution_id,
                plan_id,
                role_id,
                source_profile,
                outer_container_name,
                prompt_path,
                output_path,
                marker,
                exit_code,
                result_json,
                failure_reason,
                created_at,
                started_at,
                finished_at
            )
            VALUES (?, NULL, 'orchestrator', 'ops-orchestrator', ?, ?, ?, ?,
                    NULL, '{}', NULL, ?, ?, NULL)
            """,
            (
                execution_id,
                runtime_request_id,
                str(prompt_path),
                str(output_path),
                marker,
                now,
                now,
            ),
        )
        connection.execute(
            """
            UPDATE orchestrator_executions
            SET runtime_kind = ?
            WHERE execution_id = ?
            """,
            (runtime_kind, execution_id),
        )
        connection.commit()


def finish_execution(
    execution_id: str,
    *,
    plan_id: str | None,
    exit_code: int | None,
    result: dict[str, Any],
    failure_reason: str | None,
) -> None:
    now = utc_now()
    with connect() as connection:
        connection.execute(
            """
            UPDATE orchestrator_executions
            SET plan_id = ?,
                exit_code = ?,
                result_json = ?,
                failure_reason = ?,
                finished_at = ?
            WHERE execution_id = ?
            """,
            (
                plan_id,
                exit_code,
                json.dumps(result, sort_keys=True),
                failure_reason,
                now,
                execution_id,
            ),
        )
        connection.commit()


def command_generate(
    arguments: argparse.Namespace,
    runtime: AgentRuntime | None = None,
    provider: ModelProvider | None = None,
) -> None:
    if runtime is not None and provider is not None:
        raise ValueError("An injected runtime and provider are mutually exclusive")
    if runtime is None:
        require_runtime_state(ROOT)
        with connect() as connection:
            role = connection.execute(
                """
                SELECT * FROM roles
                WHERE role_id = 'orchestrator' AND enabled = 1
                """
            ).fetchone()
        if role is None:
            fail("Orchestrator role is unknown or disabled")
        runtime_kind, runtime = runtime_from_role(
            ROOT,
            role,
            required_role=RuntimeRole.PLANNER,
            provider=provider,
        )
    else:
        runtime_kind = runtime_kind_of(runtime)
    objective_path = Path(arguments.objective_file).resolve()
    if not objective_path.is_file():
        fail(f"Objective file is absent: {objective_path}")
    objective = objective_path.read_text(encoding="utf-8").strip()
    if not objective:
        fail("Objective is empty")
    if len(objective.encode()) > 16_384:
        fail("Objective exceeds 16 KiB")

    marker = arguments.marker.strip()
    if not marker or "\n" in marker:
        fail("Marker must be one non-empty line")

    project_ids = [
        item.strip()
        for item in arguments.projects.split(",")
        if item.strip()
    ]
    if not project_ids:
        fail("At least one project is required")
    projects = project_context(project_ids)

    orchestrator = load_orchestrator()
    suffix = uuid.uuid4().hex[:12]
    execution_id = "orchestrator-execution-" + uuid.uuid4().hex
    runtime_request_id = f"agent-runtime-{suffix}"
    directory = EXECUTIONS_ROOT / suffix
    directory.mkdir(parents=True, mode=0o750)
    prompt_path = directory / "prompt.txt"
    output_path = directory / "planner.log"
    prompt = build_prompt(objective, projects, marker)
    prompt_path.write_text(prompt + "\n", encoding="utf-8")
    prompt_path.chmod(0o600)
    output_path.touch(mode=0o600)

    reserve_execution(
        execution_id=execution_id,
        runtime_request_id=runtime_request_id,
        prompt_path=prompt_path,
        output_path=output_path,
        marker=marker,
        runtime_kind=runtime_kind.value,
    )

    exit_code: int | None = None
    plan_id: str | None = None
    result: dict[str, Any] = {}
    failure_reason: str | None = None

    try:
        def handle_runtime_event(event: RuntimeEvent) -> None:
            with connect() as connection:
                persist_runtime_event(
                    connection,
                    execution_id=execution_id,
                    runtime_kind=runtime_kind,
                    event=event,
                )

        runtime_result = runtime.execute(
            RuntimeRequest(
                role=RuntimeRole.PLANNER,
                prompt=prompt,
                runtime_config_id="ops-orchestrator",
                request_id=runtime_request_id,
                timeout_seconds=arguments.timeout,
                completion_marker=marker,
                on_event=handle_runtime_event,
            )
        )
        exit_code = 0
        persist_transcript(output_path, runtime_result.output)
        raw_plan = parse_output(runtime_result.output, marker)
        plan = orchestrator.validate_plan(
            raw_plan,
            allow_test_actions=False,
            require_enabled_projects=True,
        )
        used_projects = {
            task["project_id"] for task in plan["tasks"]
        }
        if not used_projects.issubset(set(project_ids)):
            fail("Planner used a project outside the allowed scope")
        if (
            arguments.expected_task_count is not None
            and len(plan["tasks"]) != arguments.expected_task_count
        ):
            fail(
                "Planner task count mismatch: "
                f"{len(plan['tasks'])} != {arguments.expected_task_count}"
            )

        plan_id = orchestrator.insert_plan(
            plan,
            source="AI",
            initial_status=arguments.status,
        )
        result = {
            "execution_id": execution_id,
            "plan_id": plan_id,
            "source_profile": "ops-orchestrator",
            "runtime_request_id": runtime_request_id,
            "runtime_kind": runtime_kind.value,
            "output_path": str(output_path),
            "marker_found": True,
            "plan_sha256": orchestrator.payload_sha256(plan),
            "task_count": len(plan["tasks"]),
            "status": arguments.status,
        }
    except AgentRuntimeError as error:
        failure = record_runtime_failure(
            error,
            lambda output: persist_transcript(output_path, output),
        )
        exit_code = failure.exit_code
        failure_reason = failure.failure_reason
        raise
    except Exception as error:
        failure_reason = str(error)
        raise
    finally:
        finish_execution(
            execution_id,
            plan_id=plan_id,
            exit_code=exit_code,
            result=result,
            failure_reason=failure_reason,
        )

    print(json.dumps(result, indent=2, sort_keys=True))


def command_status(arguments: argparse.Namespace) -> None:
    with connect() as connection:
        row = connection.execute(
            """
            SELECT *
            FROM orchestrator_executions
            WHERE execution_id = ?
            """,
            (arguments.execution,),
        ).fetchone()
    if row is None:
        fail(f"Unknown planner execution: {arguments.execution}")
    print(json.dumps(dict(row), indent=2, sort_keys=True))


def command_self_test(_: argparse.Namespace) -> None:
    marker = "ORCHESTRA_PLAN_OK"
    sample = f"""noise
{JSON_BEGIN}
{{"schema_version":1,"objective":"x","max_parallel_tasks":1,"tasks":[]}}
{JSON_END}
{marker}
"""
    payload = parse_output(sample, marker)
    if payload["schema_version"] != 1:
        fail("Planner output parser contract failed")
    print("Orchestra controlled AI planner parser: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra controlled high-level objective planner"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate")
    generate.add_argument("--objective-file", required=True)
    generate.add_argument("--projects", required=True)
    generate.add_argument("--marker", required=True)
    generate.add_argument(
        "--status",
        choices=("DRAFT", "READY"),
        default="DRAFT",
    )
    generate.add_argument("--timeout", type=int, default=900)
    generate.add_argument("--expected-task-count", type=int)
    generate.set_defaults(function=command_generate)

    status = subparsers.add_parser("status")
    status.add_argument("--execution", required=True)
    status.set_defaults(function=command_status)

    self_test = subparsers.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)

    return parser


def main() -> None:
    parser = build_parser()
    arguments = parser.parse_args()

    try:
        arguments.function(arguments)
    except PlannerError as error:
        print(f"Planner error: {error}", file=sys.stderr)
        raise SystemExit(1) from error
    except AgentRuntimeError as error:
        print(
            f"Planner runtime error [{error.kind.value}]: {error}",
            file=sys.stderr,
        )
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
