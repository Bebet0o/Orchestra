CREATE TABLE worker_pool_assignments (
    assignment_id TEXT PRIMARY KEY,
    pool_name TEXT NOT NULL,
    queue_sequence INTEGER NOT NULL CHECK (queue_sequence > 0),
    orchestration_task_id TEXT NOT NULL,
    attempt_id TEXT,
    role_id TEXT NOT NULL,
    runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('hermes', 'native')),
    status TEXT NOT NULL CHECK (
        status IN (
            'QUEUED',
            'RUNNING',
            'COMPLETED',
            'FAILED',
            'CANCELLED',
            'INTERRUPTED'
        )
    ),
    controller_instance_id TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    failure_reason TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    FOREIGN KEY (orchestration_task_id)
        REFERENCES orchestration_tasks(orchestration_task_id)
        ON DELETE CASCADE,
    FOREIGN KEY (attempt_id)
        REFERENCES orchestration_attempts(attempt_id)
        ON DELETE SET NULL,
    FOREIGN KEY (role_id)
        REFERENCES roles(role_id)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_worker_pool_one_active_task
    ON worker_pool_assignments(orchestration_task_id)
    WHERE status IN ('QUEUED', 'RUNNING');

CREATE UNIQUE INDEX idx_worker_pool_queue_sequence
    ON worker_pool_assignments(pool_name, queue_sequence);

CREATE INDEX idx_worker_pool_fifo
    ON worker_pool_assignments(pool_name, status, queue_sequence);

CREATE TABLE worker_pool_events (
    pool_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    assignment_id TEXT NOT NULL,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN (
            'QUEUED',
            'SLOT_ACQUIRED',
            'COMPLETED',
            'FAILED',
            'CANCELLED',
            'INTERRUPTED',
            'SLOT_RELEASED'
        )
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (assignment_id)
        REFERENCES worker_pool_assignments(assignment_id)
        ON DELETE CASCADE
);

CREATE INDEX idx_worker_pool_events_assignment
    ON worker_pool_events(assignment_id, pool_event_id);

INSERT INTO schema_migrations(version, applied_at)
VALUES (
    26,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

PRAGMA user_version = 26;
