ALTER TABLE roles
ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'hermes'
CHECK (runtime_kind IN ('hermes', 'native'));

ALTER TABLE roles
ADD COLUMN model_id TEXT NOT NULL DEFAULT 'gpt-5.6-sol'
CHECK (length(model_id) BETWEEN 1 AND 256);

ALTER TABLE orchestrator_executions
ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'hermes'
CHECK (runtime_kind IN ('hermes', 'native'));

ALTER TABLE worker_executions
ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'hermes'
CHECK (runtime_kind IN ('hermes', 'native'));

ALTER TABLE reviewer_executions
ADD COLUMN runtime_kind TEXT NOT NULL DEFAULT 'hermes'
CHECK (runtime_kind IN ('hermes', 'native'));

CREATE TABLE runtime_events (
    runtime_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    execution_id TEXT NOT NULL,
    runtime_request_id TEXT NOT NULL,
    runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('hermes', 'native')),
    role TEXT NOT NULL CHECK (role IN ('planner', 'worker', 'reviewer')),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('started', 'heartbeat')),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_runtime_events_execution
    ON runtime_events(execution_id, runtime_event_id);

CREATE UNIQUE INDEX idx_runtime_events_started
    ON runtime_events(execution_id, event_kind)
    WHERE event_kind = 'started';

INSERT INTO schema_migrations(version, applied_at)
VALUES (
    25,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

PRAGMA user_version = 25;
