"""Durable shared context and deterministic runtime-neutral projections."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Callable


CONTEXT_SCHEMA_VERSION = 1
MAX_ENTRY_BYTES = 16_384
DEFAULT_MAX_ENTRIES = 64
DEFAULT_MAX_PROJECTION_BYTES = 65_536
DEFAULT_DEPENDENCY_EXCERPT_BYTES = 8_192
CONTEXT_KINDS = {"FACT", "CONSTRAINT", "DECISION", "FINDING", "NOTE", "REFERENCE"}
KEY_PATTERN = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,127}$")


class SharedContextError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def content_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utf8_excerpt(text: str, limit: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    return encoded[:limit].decode("utf-8", errors="ignore"), True


class SharedContextStore:
    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        if not callable(connection_factory):
            raise TypeError("Shared context connection factory must be callable")
        self._connect = connection_factory

    @contextlib.contextmanager
    def _connection(self):  # type: ignore[no-untyped-def]
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _objective_projects(connection: Any, objective_id: str) -> set[str]:
        row = connection.execute(
            "SELECT project_scope_json FROM objective_queue WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if row is None:
            raise SharedContextError("Shared context objective is unknown")
        try:
            projects = json.loads(row[0])
        except (TypeError, json.JSONDecodeError) as error:
            raise SharedContextError("Objective project scope is invalid") from error
        if not isinstance(projects, list) or not all(isinstance(item, str) for item in projects):
            raise SharedContextError("Objective project scope is invalid")
        return set(projects)

    def add(
        self,
        *,
        project_id: str,
        scope: str,
        kind: str,
        key: str,
        content: str,
        objective_id: str | None = None,
        source_type: str = "CONTROL_PLANE",
        source_task_id: str | None = None,
        source_assignment_id: str | None = None,
        source_attempt_id: str | None = None,
    ) -> str:
        if not isinstance(scope, str):
            raise SharedContextError("Shared context scope must be PROJECT or OBJECTIVE")
        if not isinstance(kind, str):
            raise SharedContextError("Shared context kind is invalid")
        if not isinstance(source_type, str):
            raise SharedContextError("Shared context source type is invalid")
        scope = scope.upper()
        kind = kind.upper()
        if scope not in {"PROJECT", "OBJECTIVE"}:
            raise SharedContextError("Shared context scope must be PROJECT or OBJECTIVE")
        if kind not in CONTEXT_KINDS:
            raise SharedContextError("Shared context kind is invalid")
        if not isinstance(key, str) or KEY_PATTERN.fullmatch(key) is None:
            raise SharedContextError("Shared context key is invalid")
        if not isinstance(content, str) or not content.strip():
            raise SharedContextError("Shared context content is empty")
        if len(content.encode("utf-8")) > MAX_ENTRY_BYTES:
            raise SharedContextError("Shared context entry exceeds 16 KiB")
        source_type = source_type.upper()
        if source_type not in {"CONTROL_PLANE", "TASK_RESULT"}:
            raise SharedContextError("Shared context source type is invalid")
        source_ids = (source_task_id, source_assignment_id, source_attempt_id)
        if source_type == "CONTROL_PLANE" and any(source_ids):
            raise SharedContextError("Control-plane context cannot claim task provenance")
        if source_type == "TASK_RESULT" and not all(source_ids):
            raise SharedContextError("Task-result context requires complete provenance")
        if scope == "PROJECT" and objective_id is not None:
            raise SharedContextError("PROJECT context cannot bind an objective")
        if scope == "OBJECTIVE" and objective_id is None:
            raise SharedContextError("OBJECTIVE context requires an objective")

        context_id = "context-" + uuid.uuid4().hex
        now = utc_now()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            project = connection.execute(
                "SELECT enabled FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
            if project is None or not bool(project[0]):
                connection.rollback()
                raise SharedContextError("Shared context project is unknown or disabled")
            if objective_id is not None and project_id not in self._objective_projects(
                connection, objective_id
            ):
                connection.rollback()
                raise SharedContextError("Objective context project is outside objective scope")
            if source_type == "TASK_RESULT":
                source = connection.execute(
                    """
                    SELECT task.project_id, objective.objective_id
                    FROM orchestration_tasks AS task
                    JOIN worker_pool_assignments AS assignment
                      ON assignment.assignment_id = ?
                     AND assignment.orchestration_task_id = task.orchestration_task_id
                    JOIN orchestration_attempts AS attempt
                      ON attempt.attempt_id = ?
                     AND attempt.orchestration_task_id = task.orchestration_task_id
                     AND attempt.attempt_id = assignment.attempt_id
                    JOIN orchestration_plans AS plan ON plan.plan_id = task.plan_id
                    LEFT JOIN objective_queue AS objective ON objective.plan_id = plan.plan_id
                    WHERE task.orchestration_task_id = ?
                      AND task.status = 'COMPLETED'
                      AND assignment.status = 'COMPLETED'
                      AND attempt.status = 'COMPLETED'
                    """,
                    (source_assignment_id, source_attempt_id, source_task_id),
                ).fetchone()
                if source is None or source["project_id"] != project_id:
                    connection.rollback()
                    raise SharedContextError("Task-result context provenance is outside project scope")
                if scope == "OBJECTIVE" and source["objective_id"] != objective_id:
                    connection.rollback()
                    raise SharedContextError("Task-result context provenance is outside objective scope")
            sequence = int(
                connection.execute(
                    "SELECT COALESCE(MAX(context_sequence), 0) + 1 "
                    "FROM shared_context_entries"
                ).fetchone()[0]
            )
            connection.execute(
                """
                INSERT INTO shared_context_entries (
                    context_id, context_sequence, scope, project_id, objective_id,
                    kind, context_key, content, source_type, source_task_id,
                    source_assignment_id, source_attempt_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    context_id,
                    sequence,
                    scope,
                    project_id,
                    objective_id,
                    kind,
                    key,
                    content.strip(),
                    source_type,
                    source_task_id,
                    source_assignment_id,
                    source_attempt_id,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO context_events (
                    event_kind, context_id, context_snapshot_id,
                    projection_sha256, item_count, omitted_count, created_at
                ) VALUES ('ENTRY_CREATED', ?, NULL, NULL, NULL, NULL, ?)
                """,
                (context_id, now),
            )
            connection.commit()
        return context_id

    def get(self, context_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM shared_context_entries WHERE context_id = ?",
                (context_id,),
            ).fetchone()
        if row is None:
            raise SharedContextError("Shared context entry is unknown")
        return dict(row)

    def list(
        self, *, project_id: str, objective_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._connection() as connection:
            if objective_id is not None and project_id not in self._objective_projects(
                connection, objective_id
            ):
                raise SharedContextError("Objective context project is outside objective scope")
            rows = connection.execute(
                """
                SELECT * FROM shared_context_entries
                WHERE project_id = ?
                  AND (scope = 'PROJECT' OR objective_id = ?)
                ORDER BY CASE scope WHEN 'OBJECTIVE' THEN 0 ELSE 1 END,
                         context_sequence, context_id
                """,
                (project_id, objective_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def snapshot_for_attempt(self, attempt_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM context_snapshots WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                return None
            sources = connection.execute(
                """
                SELECT * FROM context_snapshot_sources
                WHERE context_snapshot_id = ? ORDER BY source_position
                """,
                (row["context_snapshot_id"],),
            ).fetchall()
        result = dict(row)
        result["projection"] = json.loads(result.pop("projection_json"))
        result["sources"] = [dict(item) for item in sources]
        return result

    def snapshot_for_objective_attempt(
        self, objective_attempt_id: str
    ) -> dict[str, Any] | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM context_snapshots WHERE objective_attempt_id = ?",
                (objective_attempt_id,),
            ).fetchone()
            if row is None:
                return None
            sources = connection.execute(
                """
                SELECT * FROM context_snapshot_sources
                WHERE context_snapshot_id = ? ORDER BY source_position
                """,
                (row["context_snapshot_id"],),
            ).fetchall()
        result = dict(row)
        result["projection"] = json.loads(result.pop("projection_json"))
        result["sources"] = [dict(item) for item in sources]
        return result


class ContextProjector:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        *,
        max_projection_bytes: int = DEFAULT_MAX_PROJECTION_BYTES,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        dependency_excerpt_bytes: int = DEFAULT_DEPENDENCY_EXCERPT_BYTES,
    ) -> None:
        if not 512 <= max_projection_bytes <= 262_144:
            raise ValueError("Context projection byte limit must be 512..262144")
        if not 1 <= max_entries <= 256:
            raise ValueError("Context projection entry limit must be 1..256")
        if not 128 <= dependency_excerpt_bytes <= 32_768:
            raise ValueError("Dependency excerpt byte limit must be 128..32768")
        self._connect = connection_factory
        self.max_projection_bytes = max_projection_bytes
        self.max_entries = max_entries
        self.dependency_excerpt_bytes = dependency_excerpt_bytes

    @contextlib.contextmanager
    def _connection(self):  # type: ignore[no-untyped-def]
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    def _bound(
        self,
        core: dict[str, Any],
        dependencies: list[dict[str, Any]],
        entries: list[dict[str, Any]],
        omitted_initial: int,
    ) -> dict[str, Any]:
        projection = {
            **core,
            "dependency_results": list(dependencies),
            "shared_context": list(entries),
            "bounding": {
                "max_bytes": self.max_projection_bytes,
                "budget_exhausted": False,
                "omitted_count": omitted_initial,
            },
        }
        mandatory = {
            **core,
            "dependency_results": [],
            "shared_context": [],
            "bounding": projection["bounding"],
        }
        if len(canonical_json(mandatory).encode("utf-8")) > self.max_projection_bytes:
            raise SharedContextError("Mandatory context exceeds projection byte limit")

        while len(canonical_json(projection).encode("utf-8")) > self.max_projection_bytes:
            if projection["shared_context"]:
                projection["shared_context"].pop()
            elif projection["dependency_results"]:
                projection["dependency_results"].pop()
            else:
                raise SharedContextError("Mandatory context exceeds projection byte limit")
            projection["bounding"]["omitted_count"] += 1
        projection["bounding"]["budget_exhausted"] = bool(
            projection["bounding"]["omitted_count"]
        )
        return projection

    def _task_projection(
        self, connection: Any, task_id: str, *, consumer: str = "TASK"
    ) -> dict[str, Any]:
        row = connection.execute(
            """
            SELECT task.*, plan.objective,
                   objective.objective_id, objective.project_scope_json
            FROM orchestration_tasks AS task
            JOIN orchestration_plans AS plan ON plan.plan_id = task.plan_id
            LEFT JOIN objective_queue AS objective ON objective.plan_id = plan.plan_id
            WHERE task.orchestration_task_id = ?
            """,
            (task_id,),
        ).fetchone()
        if row is None:
            raise SharedContextError("Context projection task is unknown")
        if row["project_id"] is None:
            raise SharedContextError("Task context requires a project")
        if row["objective_id"] is not None:
            projects = json.loads(row["project_scope_json"])
            if row["project_id"] not in projects:
                raise SharedContextError("Task project is outside objective scope")

        dependency_rows = connection.execute(
            """
            SELECT parent.orchestration_task_id, parent.task_key, parent.title,
                   parent.result_json, parent.graph_position
            FROM orchestration_dependencies AS dependency
            JOIN orchestration_tasks AS parent
              ON parent.orchestration_task_id = dependency.depends_on_task_id
            WHERE dependency.orchestration_task_id = ?
              AND parent.status = 'COMPLETED'
            ORDER BY parent.graph_position, parent.orchestration_task_id
            """,
            (task_id,),
        ).fetchall()
        dependencies: list[dict[str, Any]] = []
        for parent in dependency_rows:
            result_text = canonical_json(json.loads(parent["result_json"]))
            excerpt, truncated = _utf8_excerpt(
                result_text, self.dependency_excerpt_bytes
            )
            dependencies.append(
                {
                    "task_id": parent["orchestration_task_id"],
                    "task_key": parent["task_key"],
                    "title": parent["title"],
                    "result_reference": "orchestration_task_result:"
                    + parent["orchestration_task_id"],
                    "result_sha256": hashlib.sha256(
                        result_text.encode("utf-8")
                    ).hexdigest(),
                    "result_excerpt": excerpt,
                    "truncated": truncated,
                }
            )

        entry_rows = connection.execute(
            """
            SELECT * FROM shared_context_entries
            WHERE project_id = ?
              AND (scope = 'PROJECT' OR objective_id = ?)
            ORDER BY CASE scope WHEN 'OBJECTIVE' THEN 0 ELSE 1 END,
                     context_sequence, context_id
            """,
            (row["project_id"], row["objective_id"]),
        ).fetchall()
        omitted = max(0, len(entry_rows) - self.max_entries)
        entries = [
            {
                "context_id": item["context_id"],
                "scope": item["scope"],
                "kind": item["kind"],
                "key": item["context_key"],
                "content": item["content"],
            }
            for item in entry_rows[: self.max_entries]
        ]
        core = {
            "context_schema_version": CONTEXT_SCHEMA_VERSION,
            "consumer": consumer,
            "project": {"project_id": row["project_id"]},
            "objective": {
                "objective_id": row["objective_id"],
                "instruction": row["objective"],
            },
            "task": {
                "task_id": row["orchestration_task_id"],
                "task_key": row["task_key"],
                "title": row["title"],
                "instruction": row["instruction"],
                "role_id": row["role_id"],
            },
        }
        return self._bound(core, dependencies, entries, omitted)

    def for_task(self, task_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            projection = self._task_projection(connection, task_id)
            connection.rollback()
        return projection

    def review_input(self, connection: Any, attempt_id: str) -> dict[str, Any]:
        """Bind a completed result to what its worker actually saw.

        No current-context projection or runtime history is consulted here.
        The caller owns the transaction used to freeze this input.
        """
        row = connection.execute(
            """
            SELECT a.*, t.project_id, t.plan_id, t.review_required,
                   s.projection_json, s.projection_sha256, s.objective_id,
                   s.assignment_id, s.consumer_kind,
                   s.orchestration_task_id AS snapshot_task,
                   s.attempt_id AS snapshot_attempt,
                   w.orchestration_task_id AS assignment_task, w.attempt_id AS assignment_attempt
            FROM orchestration_attempts a
            JOIN orchestration_tasks t ON t.orchestration_task_id=a.orchestration_task_id
            JOIN context_snapshots s ON s.context_snapshot_id=a.context_snapshot_id
            JOIN worker_pool_assignments w ON w.assignment_id=s.assignment_id
            WHERE a.attempt_id=?
            """, (attempt_id,),
        ).fetchone()
        if row is None or row["status"] != "COMPLETED" or not row["review_required"]:
            raise SharedContextError("Review requires a completed worker attempt and snapshot")
        task_id = row["orchestration_task_id"]
        if (row["consumer_kind"] != "WORKER" or row["snapshot_task"] != task_id
                or row["assignment_task"] != task_id
                or row["snapshot_attempt"] != attempt_id
                or row["assignment_attempt"] != attempt_id):
            raise SharedContextError("Worker snapshot ownership mismatch")
        if row["objective_id"] is not None:
            objective = connection.execute(
                "SELECT plan_id, project_scope_json FROM objective_queue WHERE objective_id=?",
                (row["objective_id"],),
            ).fetchone()
            if (objective is None or objective["plan_id"] != row["plan_id"]
                    or row["project_id"] not in json.loads(objective["project_scope_json"])):
                raise SharedContextError("Review objective ownership mismatch")
        worker_context = json.loads(row["projection_json"])
        if (content_sha256(worker_context) != row["projection_sha256"]
                or worker_context["project"]["project_id"] != row["project_id"]
                or worker_context["task"]["task_id"] != task_id
                or worker_context["objective"]["objective_id"] != row["objective_id"]):
            raise SharedContextError("Worker snapshot integrity mismatch")
        result = json.loads(row["result_json"])
        reference = "orchestration_attempt_result:" + attempt_id
        if row["worker_execution_id"] is not None:
            execution = connection.execute(
                "SELECT * FROM worker_executions WHERE execution_id=?",
                (row["worker_execution_id"],),
            ).fetchone()
            if (execution is None or execution["run_id"] != row["run_id"]
                    or execution["role_id"] != worker_context["task"]["role_id"]
                    or execution["context_snapshot_id"] != row["context_snapshot_id"]
                    or execution["finished_at"] is None or execution["exit_code"] != 0):
                raise SharedContextError("Worker result ownership mismatch")
            run = connection.execute("SELECT project_id FROM runs WHERE run_id=?",
                                     (row["run_id"],)).fetchone()
            if run is None or run[0] != row["project_id"]:
                raise SharedContextError("Worker run project mismatch")
            result = json.loads(execution["result_json"])
            reference = "worker_execution_result:" + row["worker_execution_id"]
        source_ids = [reference, "context_snapshot:" + row["context_snapshot_id"]]
        for item in worker_context["shared_context"]:
            entry = connection.execute(
                "SELECT project_id, objective_id, content FROM shared_context_entries WHERE context_id=?",
                (item["context_id"],),
            ).fetchone()
            if (entry is None or entry["project_id"] != row["project_id"]
                    or entry["objective_id"] not in (None, row["objective_id"])
                    or entry["content"] != item["content"]):
                raise SharedContextError("Review context source ownership mismatch")
            source_ids.append("context_entry:" + item["context_id"])
        for item in worker_context["dependency_results"]:
            parent = connection.execute(
                "SELECT project_id, plan_id FROM orchestration_tasks WHERE orchestration_task_id=?",
                (item["task_id"],),
            ).fetchone()
            if parent is not None and parent[0] == row["project_id"] and parent[1] == row["plan_id"]:
                source_ids.append(item["result_reference"])
        projection = {
            "context_schema_version": 1, "consumer": "REVIEWER",
            "subject": {"project_id": row["project_id"], "objective_id": row["objective_id"],
                        "plan_id": row["plan_id"], "task_id": task_id,
                        "assignment_id": row["assignment_id"], "attempt_id": attempt_id,
                        "worker_execution_id": row["worker_execution_id"]},
            "objective": worker_context["objective"], "task": worker_context["task"],
            "worker_context_snapshot": {"context_snapshot_id": row["context_snapshot_id"],
                                        "sha256": row["projection_sha256"]},
            "worker_result": {"reference": reference, "sha256": content_sha256(result)},
            "source_ids": sorted(source_ids),
        }
        # Mandatory evidence is never silently truncated.
        if len(canonical_json({"input": projection, "worker_context": worker_context,
                               "worker_result": result}).encode()) > 240_000:
            raise SharedContextError("Review evidence exceeds 240000 bytes")
        return projection

    def for_reviewer(self, task_id: str) -> dict[str, Any]:
        """Compatibility projection preview; execution uses review_input instead."""
        with self._connection() as connection:
            connection.execute("BEGIN")
            return self._task_projection(connection, task_id, consumer="REVIEWER")

    def _planner_projection(self, connection: Any, objective_id: str) -> dict[str, Any]:
        objective = connection.execute(
            "SELECT * FROM objective_queue WHERE objective_id = ?",
            (objective_id,),
        ).fetchone()
        if objective is None:
            raise SharedContextError("Planner context objective is unknown")
        project_ids = json.loads(objective["project_scope_json"])
        entries: list[dict[str, Any]] = []
        for project_id in sorted(project_ids):
            rows = connection.execute(
                """
                SELECT * FROM shared_context_entries
                WHERE project_id = ?
                  AND (scope = 'PROJECT' OR objective_id = ?)
                ORDER BY CASE scope WHEN 'OBJECTIVE' THEN 0 ELSE 1 END,
                         context_sequence, context_id
                """,
                (project_id, objective_id),
            ).fetchall()
            entries.extend(
                {
                    "context_id": item["context_id"],
                    "context_sequence": item["context_sequence"],
                    "project_id": item["project_id"],
                    "scope": item["scope"],
                    "kind": item["kind"],
                    "key": item["context_key"],
                    "content": item["content"],
                }
                for item in rows
            )
        entries.sort(
            key=lambda item: (
                0 if item["scope"] == "OBJECTIVE" else 1,
                item["context_sequence"],
                item["context_id"],
            )
        )
        for item in entries:
            del item["context_sequence"]
        omitted = max(0, len(entries) - self.max_entries)
        return self._bound(
            {
                "context_schema_version": CONTEXT_SCHEMA_VERSION,
                "consumer": "PLANNER",
                "projects": [{"project_id": item} for item in sorted(project_ids)],
                "objective": {
                    "objective_id": objective_id,
                    "instruction": objective["objective"],
                },
            },
            [],
            entries[: self.max_entries],
            omitted,
        )

    def for_planner(self, objective_id: str) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN")
            projection = self._planner_projection(connection, objective_id)
            connection.rollback()
        return projection

    def freeze_planner(
        self, *, objective_id: str, objective_attempt_id: str
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM context_snapshots WHERE objective_attempt_id = ?",
                (objective_attempt_id,),
            ).fetchone()
            if existing is not None:
                if existing["objective_id"] != objective_id:
                    connection.rollback()
                    raise SharedContextError("Planner context snapshot identity conflicts")
                connection.rollback()
                result = dict(existing)
                result["projection"] = json.loads(result.pop("projection_json"))
                return result
            attempt = connection.execute(
                """
                SELECT objective_id FROM objective_attempts
                WHERE objective_attempt_id = ? AND objective_id = ? AND status = 'RUNNING'
                """,
                (objective_attempt_id, objective_id),
            ).fetchone()
            if attempt is None:
                connection.rollback()
                raise SharedContextError("Planner context attempt linkage is invalid")
            projection = self._planner_projection(connection, objective_id)
            snapshot_id = "context-snapshot-" + uuid.uuid4().hex
            projection_text = canonical_json(projection)
            digest = hashlib.sha256(projection_text.encode("utf-8")).hexdigest()
            now = utc_now()
            source_count = len(projection["shared_context"])
            connection.execute(
                """
                INSERT INTO context_snapshots (
                    context_snapshot_id, consumer_kind, objective_id,
                    objective_attempt_id, orchestration_task_id, assignment_id,
                    attempt_id, context_schema_version, projection_json,
                    projection_sha256, source_item_count, omitted_count,
                    budget_exhausted, created_at
                ) VALUES (?, 'PLANNER', ?, ?, NULL, NULL, NULL, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    objective_id,
                    objective_attempt_id,
                    projection_text,
                    digest,
                    source_count,
                    projection["bounding"]["omitted_count"],
                    int(projection["bounding"]["budget_exhausted"]),
                    now,
                ),
            )
            for position, item in enumerate(projection["shared_context"]):
                connection.execute(
                    """
                    INSERT INTO context_snapshot_sources VALUES (?, ?, 'CONTEXT_ENTRY',
                                                                  ?, NULL, ?, 0)
                    """,
                    (
                        snapshot_id,
                        position,
                        item["context_id"],
                        hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
                    ),
                )
            connection.execute(
                "UPDATE objective_attempts SET context_snapshot_id = ? "
                "WHERE objective_attempt_id = ?",
                (snapshot_id, objective_attempt_id),
            )
            connection.execute(
                """
                INSERT INTO context_events (
                    event_kind, context_id, context_snapshot_id, projection_sha256,
                    item_count, omitted_count, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    "PROJECTION_BOUNDED"
                    if projection["bounding"]["budget_exhausted"]
                    else "SNAPSHOT_CREATED",
                    snapshot_id,
                    digest,
                    source_count,
                    projection["bounding"]["omitted_count"],
                    now,
                ),
            )
            connection.commit()
        return {
            "context_snapshot_id": snapshot_id,
            "projection_sha256": digest,
            "projection": projection,
        }

    def freeze_task(
        self, *, task_id: str, assignment_id: str, attempt_id: str
    ) -> dict[str, Any]:
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM context_snapshots WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["orchestration_task_id"] != task_id
                    or existing["assignment_id"] != assignment_id
                ):
                    connection.rollback()
                    raise SharedContextError("Attempt context snapshot identity conflicts")
                connection.rollback()
                result = dict(existing)
                result["projection"] = json.loads(result.pop("projection_json"))
                return result
            link = connection.execute(
                """
                SELECT task.plan_id
                FROM orchestration_attempts AS attempt
                JOIN orchestration_tasks AS task
                  ON task.orchestration_task_id = attempt.orchestration_task_id
                JOIN worker_pool_assignments AS assignment
                  ON assignment.assignment_id = ?
                 AND assignment.orchestration_task_id = task.orchestration_task_id
                 AND assignment.attempt_id = attempt.attempt_id
                WHERE attempt.attempt_id = ?
                  AND task.orchestration_task_id = ?
                  AND attempt.status = 'RUNNING'
                  AND assignment.status = 'RUNNING'
                """,
                (assignment_id, attempt_id, task_id),
            ).fetchone()
            if link is None:
                connection.rollback()
                raise SharedContextError("Context snapshot execution linkage is invalid")
            projection = self._task_projection(connection, task_id)
            snapshot_id = "context-snapshot-" + uuid.uuid4().hex
            projection_text = canonical_json(projection)
            digest = hashlib.sha256(projection_text.encode("utf-8")).hexdigest()
            now = utc_now()
            source_count = len(projection["dependency_results"]) + len(
                projection["shared_context"]
            )
            connection.execute(
                """
                INSERT INTO context_snapshots (
                    context_snapshot_id, consumer_kind, objective_id,
                    objective_attempt_id, orchestration_task_id, assignment_id,
                    attempt_id, context_schema_version, projection_json,
                    projection_sha256, source_item_count, omitted_count,
                    budget_exhausted, created_at
                ) VALUES (?, 'WORKER', ?, NULL, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    projection["objective"]["objective_id"],
                    task_id,
                    assignment_id,
                    attempt_id,
                    projection_text,
                    digest,
                    source_count,
                    projection["bounding"]["omitted_count"],
                    int(projection["bounding"]["budget_exhausted"]),
                    now,
                ),
            )
            position = 0
            for item in projection["dependency_results"]:
                connection.execute(
                    """
                    INSERT INTO context_snapshot_sources VALUES (?, ?, 'DEPENDENCY_RESULT',
                                                                  NULL, ?, ?, ?)
                    """,
                    (
                        snapshot_id,
                        position,
                        item["task_id"],
                        item["result_sha256"],
                        int(item["truncated"]),
                    ),
                )
                position += 1
            for item in projection["shared_context"]:
                connection.execute(
                    """
                    INSERT INTO context_snapshot_sources VALUES (?, ?, 'CONTEXT_ENTRY',
                                                                  ?, NULL, ?, 0)
                    """,
                    (
                        snapshot_id,
                        position,
                        item["context_id"],
                        hashlib.sha256(item["content"].encode("utf-8")).hexdigest(),
                    ),
                )
                position += 1
            connection.execute(
                "UPDATE orchestration_attempts "
                "SET context_snapshot_id = ? WHERE attempt_id = ?",
                (snapshot_id, attempt_id),
            )
            event_kind = (
                "PROJECTION_BOUNDED"
                if projection["bounding"]["budget_exhausted"]
                else "SNAPSHOT_CREATED"
            )
            connection.execute(
                """
                INSERT INTO context_events (
                    event_kind, context_id, context_snapshot_id, projection_sha256,
                    item_count, omitted_count, created_at
                ) VALUES (?, NULL, ?, ?, ?, ?, ?)
                """,
                (
                    event_kind,
                    snapshot_id,
                    digest,
                    source_count,
                    projection["bounding"]["omitted_count"],
                    now,
                ),
            )
            connection.commit()
        return {
            "context_snapshot_id": snapshot_id,
            "projection_sha256": digest,
            "projection": projection,
        }
