ALTER TABLE orchestration_plans
ADD COLUMN graph_schema_version INTEGER NOT NULL DEFAULT 1
    CHECK (graph_schema_version = 1);

ALTER TABLE orchestration_plans
ADD COLUMN graph_activated_at TEXT;

ALTER TABLE orchestration_tasks
ADD COLUMN title TEXT NOT NULL DEFAULT '';

ALTER TABLE orchestration_tasks
ADD COLUMN graph_position INTEGER NOT NULL DEFAULT 0
    CHECK (graph_position >= 0);

UPDATE orchestration_tasks
SET title = task_key;

UPDATE orchestration_tasks AS task
SET graph_position = (
    SELECT COUNT(*) - 1
    FROM orchestration_tasks AS earlier
    WHERE earlier.plan_id = task.plan_id
      AND (
          earlier.created_at < task.created_at
          OR (
              earlier.created_at = task.created_at
              AND earlier.orchestration_task_id <= task.orchestration_task_id
          )
      )
);

UPDATE orchestration_plans
SET graph_activated_at = COALESCE(started_at, created_at)
WHERE status <> 'DRAFT';

CREATE INDEX idx_orchestration_tasks_graph_position
    ON orchestration_tasks(plan_id, graph_position);

CREATE TABLE task_graph_events (
    graph_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id TEXT NOT NULL,
    orchestration_task_id TEXT,
    assignment_id TEXT,
    attempt_id TEXT,
    event_kind TEXT NOT NULL CHECK (
        event_kind IN (
            'CREATED',
            'READY',
            'DISPATCHED',
            'RUNNING',
            'COMPLETED',
            'FAILED',
            'BLOCKED',
            'CANCELLED'
        )
    ),
    created_at TEXT NOT NULL,
    FOREIGN KEY (plan_id)
        REFERENCES orchestration_plans(plan_id)
        ON DELETE CASCADE,
    FOREIGN KEY (orchestration_task_id)
        REFERENCES orchestration_tasks(orchestration_task_id)
        ON DELETE CASCADE,
    FOREIGN KEY (assignment_id)
        REFERENCES worker_pool_assignments(assignment_id)
        ON DELETE SET NULL,
    FOREIGN KEY (attempt_id)
        REFERENCES orchestration_attempts(attempt_id)
        ON DELETE SET NULL
);

CREATE INDEX idx_task_graph_events_task
    ON task_graph_events(orchestration_task_id, graph_event_id);

CREATE UNIQUE INDEX idx_task_graph_assignment_dispatched
    ON task_graph_events(assignment_id)
    WHERE event_kind = 'DISPATCHED';

CREATE TRIGGER orchestration_dependency_same_plan_insert
BEFORE INSERT ON orchestration_dependencies
FOR EACH ROW
WHEN
    (SELECT plan_id FROM orchestration_tasks
     WHERE orchestration_task_id = NEW.orchestration_task_id) <> NEW.plan_id
    OR
    (SELECT plan_id FROM orchestration_tasks
     WHERE orchestration_task_id = NEW.depends_on_task_id) <> NEW.plan_id
BEGIN
    SELECT RAISE(ABORT, 'dependency endpoints must belong to its plan');
END;

INSERT INTO schema_migrations(version, applied_at)
VALUES (
    27,
    strftime('%Y-%m-%dT%H:%M:%fZ', 'now')
);

PRAGMA user_version = 27;
