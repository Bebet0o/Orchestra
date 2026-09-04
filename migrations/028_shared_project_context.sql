CREATE TABLE shared_context_entries (
    context_id TEXT PRIMARY KEY,
    context_sequence INTEGER NOT NULL UNIQUE CHECK (context_sequence > 0),
    scope TEXT NOT NULL CHECK (scope IN ('PROJECT', 'OBJECTIVE')),
    project_id TEXT NOT NULL,
    objective_id TEXT,
    kind TEXT NOT NULL CHECK (
        kind IN ('FACT', 'CONSTRAINT', 'DECISION', 'FINDING', 'NOTE', 'REFERENCE')
    ),
    context_key TEXT NOT NULL,
    content TEXT NOT NULL CHECK (
        length(content) > 0 AND length(CAST(content AS BLOB)) <= 16384
    ),
    source_type TEXT NOT NULL CHECK (
        source_type IN ('CONTROL_PLANE', 'TASK_RESULT')
    ),
    source_task_id TEXT,
    source_assignment_id TEXT,
    source_attempt_id TEXT,
    created_at TEXT NOT NULL,
    CHECK (
        (scope = 'PROJECT' AND objective_id IS NULL)
        OR (scope = 'OBJECTIVE' AND objective_id IS NOT NULL)
    ),
    CHECK (
        (source_type = 'CONTROL_PLANE'
         AND source_task_id IS NULL
         AND source_assignment_id IS NULL
         AND source_attempt_id IS NULL)
        OR
        (source_type = 'TASK_RESULT'
         AND source_task_id IS NOT NULL
         AND source_assignment_id IS NOT NULL
         AND source_attempt_id IS NOT NULL)
    ),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (objective_id) REFERENCES objective_queue(objective_id) ON DELETE CASCADE,
    FOREIGN KEY (source_task_id)
        REFERENCES orchestration_tasks(orchestration_task_id) ON DELETE SET NULL,
    FOREIGN KEY (source_assignment_id)
        REFERENCES worker_pool_assignments(assignment_id) ON DELETE SET NULL,
    FOREIGN KEY (source_attempt_id)
        REFERENCES orchestration_attempts(attempt_id) ON DELETE SET NULL
);

CREATE INDEX idx_shared_context_project
    ON shared_context_entries(project_id, scope, context_sequence);

CREATE INDEX idx_shared_context_objective
    ON shared_context_entries(objective_id, context_sequence);

CREATE TABLE context_snapshots (
    context_snapshot_id TEXT PRIMARY KEY,
    consumer_kind TEXT NOT NULL CHECK (
        consumer_kind IN ('PLANNER', 'WORKER', 'REVIEWER')
    ),
    objective_id TEXT,
    objective_attempt_id TEXT UNIQUE,
    orchestration_task_id TEXT,
    assignment_id TEXT UNIQUE,
    attempt_id TEXT UNIQUE,
    context_schema_version INTEGER NOT NULL CHECK (context_schema_version = 1),
    projection_json TEXT NOT NULL CHECK (
        json_valid(projection_json) AND json_type(projection_json) = 'object'
    ),
    projection_sha256 TEXT NOT NULL CHECK (length(projection_sha256) = 64),
    source_item_count INTEGER NOT NULL CHECK (source_item_count >= 0),
    omitted_count INTEGER NOT NULL CHECK (omitted_count >= 0),
    budget_exhausted INTEGER NOT NULL CHECK (budget_exhausted IN (0, 1)),
    created_at TEXT NOT NULL,
    CHECK (
        (consumer_kind = 'PLANNER'
         AND objective_id IS NOT NULL
         AND objective_attempt_id IS NOT NULL
         AND orchestration_task_id IS NULL
         AND assignment_id IS NULL
         AND attempt_id IS NULL)
        OR
        (consumer_kind IN ('WORKER', 'REVIEWER')
         AND objective_attempt_id IS NULL
         AND orchestration_task_id IS NOT NULL
         AND attempt_id IS NOT NULL)
    ),
    FOREIGN KEY (objective_id) REFERENCES objective_queue(objective_id) ON DELETE CASCADE,
    FOREIGN KEY (objective_attempt_id)
        REFERENCES objective_attempts(objective_attempt_id) ON DELETE CASCADE,
    FOREIGN KEY (orchestration_task_id)
        REFERENCES orchestration_tasks(orchestration_task_id) ON DELETE CASCADE,
    FOREIGN KEY (assignment_id)
        REFERENCES worker_pool_assignments(assignment_id) ON DELETE SET NULL,
    FOREIGN KEY (attempt_id)
        REFERENCES orchestration_attempts(attempt_id) ON DELETE CASCADE
);

CREATE INDEX idx_context_snapshots_task
    ON context_snapshots(orchestration_task_id, created_at);

CREATE TABLE context_snapshot_sources (
    context_snapshot_id TEXT NOT NULL,
    source_position INTEGER NOT NULL CHECK (source_position >= 0),
    source_kind TEXT NOT NULL CHECK (
        source_kind IN ('CONTEXT_ENTRY', 'DEPENDENCY_RESULT')
    ),
    context_id TEXT,
    source_task_id TEXT,
    source_sha256 TEXT NOT NULL CHECK (length(source_sha256) = 64),
    truncated INTEGER NOT NULL CHECK (truncated IN (0, 1)),
    PRIMARY KEY (context_snapshot_id, source_position),
    CHECK (
        (source_kind = 'CONTEXT_ENTRY' AND context_id IS NOT NULL AND source_task_id IS NULL)
        OR
        (source_kind = 'DEPENDENCY_RESULT' AND context_id IS NULL AND source_task_id IS NOT NULL)
    ),
    FOREIGN KEY (context_snapshot_id)
        REFERENCES context_snapshots(context_snapshot_id) ON DELETE CASCADE,
    FOREIGN KEY (context_id)
        REFERENCES shared_context_entries(context_id) ON DELETE RESTRICT,
    FOREIGN KEY (source_task_id)
        REFERENCES orchestration_tasks(orchestration_task_id) ON DELETE RESTRICT
);

CREATE TABLE context_events (
    context_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN ('ENTRY_CREATED', 'SNAPSHOT_CREATED', 'PROJECTION_BOUNDED')
    ),
    context_id TEXT,
    context_snapshot_id TEXT,
    projection_sha256 TEXT,
    item_count INTEGER,
    omitted_count INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (context_id)
        REFERENCES shared_context_entries(context_id) ON DELETE CASCADE,
    FOREIGN KEY (context_snapshot_id)
        REFERENCES context_snapshots(context_snapshot_id) ON DELETE CASCADE
);

CREATE TRIGGER shared_context_entries_immutable_update
BEFORE UPDATE ON shared_context_entries
BEGIN
    SELECT RAISE(ABORT, 'shared context entries are immutable');
END;

CREATE TRIGGER shared_context_entries_immutable_delete
BEFORE DELETE ON shared_context_entries
BEGIN
    SELECT RAISE(ABORT, 'shared context entries are immutable');
END;

CREATE TRIGGER context_snapshots_immutable_update
BEFORE UPDATE ON context_snapshots
BEGIN
    SELECT RAISE(ABORT, 'context snapshots are immutable');
END;

CREATE TRIGGER context_snapshots_immutable_delete
BEFORE DELETE ON context_snapshots
BEGIN
    SELECT RAISE(ABORT, 'context snapshots are immutable');
END;

CREATE TRIGGER context_snapshot_sources_immutable_update
BEFORE UPDATE ON context_snapshot_sources
BEGIN
    SELECT RAISE(ABORT, 'context snapshot sources are immutable');
END;

CREATE TRIGGER context_snapshot_sources_immutable_delete
BEFORE DELETE ON context_snapshot_sources
BEGIN
    SELECT RAISE(ABORT, 'context snapshot sources are immutable');
END;

ALTER TABLE orchestration_attempts
ADD COLUMN context_snapshot_id TEXT
    REFERENCES context_snapshots(context_snapshot_id) ON DELETE RESTRICT;

ALTER TABLE objective_attempts
ADD COLUMN context_snapshot_id TEXT
    REFERENCES context_snapshots(context_snapshot_id) ON DELETE RESTRICT;

ALTER TABLE worker_executions
ADD COLUMN context_snapshot_id TEXT
    REFERENCES context_snapshots(context_snapshot_id) ON DELETE RESTRICT;

ALTER TABLE orchestrator_executions
ADD COLUMN context_snapshot_id TEXT
    REFERENCES context_snapshots(context_snapshot_id) ON DELETE RESTRICT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (28, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 28;
