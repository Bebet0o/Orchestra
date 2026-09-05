-- v0.2-F: durable, bounded corrective attempts for Judge NEEDS_FIX.
ALTER TABLE orchestration_tasks
ADD COLUMN recovery_max_retries INTEGER NOT NULL DEFAULT 0
    CHECK (recovery_max_retries BETWEEN 0 AND 3);

ALTER TABLE orchestration_tasks
ADD COLUMN recovery_retry_count INTEGER NOT NULL DEFAULT 0
    CHECK (recovery_retry_count BETWEEN 0 AND 3
           AND recovery_retry_count <= recovery_max_retries);

ALTER TABLE orchestration_tasks
ADD COLUMN recovery_policy_sha256 TEXT
    CHECK (recovery_policy_sha256 IS NULL OR length(recovery_policy_sha256) = 64);

ALTER TABLE context_snapshots
ADD COLUMN recovery_action_id TEXT REFERENCES recovery_actions(recovery_action_id);

CREATE TABLE recovery_actions (
    recovery_action_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    objective_id TEXT REFERENCES objective_queue(objective_id),
    plan_id TEXT NOT NULL REFERENCES orchestration_plans(plan_id),
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(orchestration_task_id),
    source_attempt_id TEXT NOT NULL REFERENCES orchestration_attempts(attempt_id),
    source_review_id TEXT NOT NULL REFERENCES task_reviews(review_id),
    source_decision_id TEXT NOT NULL UNIQUE REFERENCES judge_decisions(decision_id),
    -- Sequence max_retries + 1 is the durable exhaustion record.
    recovery_sequence INTEGER NOT NULL CHECK (recovery_sequence BETWEEN 1 AND 4),
    max_retries INTEGER NOT NULL CHECK (max_retries BETWEEN 0 AND 3),
    status TEXT NOT NULL CHECK (status IN (
        'PENDING', 'DISPATCHED', 'ATTEMPT_CREATED', 'COMPLETED',
        'EXHAUSTED', 'CANCELLED'
    )),
    reason_json TEXT NOT NULL CHECK (
        json_valid(reason_json) AND json_type(reason_json) = 'object'
        AND length(CAST(reason_json AS BLOB)) <= 65536
    ),
    reason_sha256 TEXT NOT NULL CHECK (length(reason_sha256) = 64),
    target_assignment_id TEXT UNIQUE REFERENCES worker_pool_assignments(assignment_id),
    target_attempt_id TEXT UNIQUE REFERENCES orchestration_attempts(attempt_id),
    approval_id TEXT UNIQUE REFERENCES approvals(approval_id),
    created_at TEXT NOT NULL,
    dispatched_at TEXT,
    attempt_created_at TEXT,
    finished_at TEXT,
    CHECK (
        (recovery_sequence <= max_retries AND status <> 'EXHAUSTED')
        OR (recovery_sequence = max_retries + 1 AND status = 'EXHAUSTED')
    ),
    CHECK (
        (status = 'PENDING' AND target_assignment_id IS NULL
            AND target_attempt_id IS NULL AND approval_id IS NULL
            AND dispatched_at IS NULL AND attempt_created_at IS NULL
            AND finished_at IS NULL)
        OR (status = 'DISPATCHED' AND target_assignment_id IS NOT NULL
            AND target_attempt_id IS NULL AND approval_id IS NULL
            AND dispatched_at IS NOT NULL AND attempt_created_at IS NULL
            AND finished_at IS NULL)
        OR (status = 'ATTEMPT_CREATED' AND target_assignment_id IS NOT NULL
            AND target_attempt_id IS NOT NULL AND approval_id IS NULL
            AND dispatched_at IS NOT NULL AND attempt_created_at IS NOT NULL
            AND finished_at IS NULL)
        OR (status = 'COMPLETED' AND target_assignment_id IS NOT NULL
            AND target_attempt_id IS NOT NULL
            AND approval_id IS NULL
            AND dispatched_at IS NOT NULL AND attempt_created_at IS NOT NULL
            AND finished_at IS NOT NULL)
        OR (status = 'EXHAUSTED' AND target_assignment_id IS NULL
            AND target_attempt_id IS NULL AND finished_at IS NOT NULL)
        OR (status = 'CANCELLED' AND target_attempt_id IS NULL
            AND approval_id IS NULL
            AND finished_at IS NOT NULL)
    ),
    UNIQUE (task_id, recovery_sequence)
);

CREATE INDEX idx_recovery_actions_task
    ON recovery_actions(task_id, recovery_sequence);
CREATE INDEX idx_recovery_actions_status
    ON recovery_actions(status, created_at);

CREATE TABLE recovery_events (
    recovery_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    recovery_action_id TEXT NOT NULL REFERENCES recovery_actions(recovery_action_id),
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'recovery_requested', 'recovery_dispatched',
        'recovery_attempt_created', 'recovery_completed',
        'recovery_exhausted', 'recovery_escalated', 'recovery_cancelled'
    )),
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(orchestration_task_id),
    source_attempt_id TEXT NOT NULL REFERENCES orchestration_attempts(attempt_id),
    source_review_id TEXT NOT NULL REFERENCES task_reviews(review_id),
    source_decision_id TEXT NOT NULL REFERENCES judge_decisions(decision_id),
    target_attempt_id TEXT REFERENCES orchestration_attempts(attempt_id),
    created_at TEXT NOT NULL,
    UNIQUE (recovery_action_id, event_kind)
);

-- The task row is the current-result projection. Attempt rows are historical
-- authorities, so recovery may advance task.result_json without rewriting R1.
DROP TRIGGER reviewed_task_result_immutable;

CREATE TRIGGER reviewed_task_result_immutable
BEFORE UPDATE OF result_json ON orchestration_tasks
WHEN NEW.result_json <> OLD.result_json
  AND EXISTS (SELECT 1 FROM task_reviews WHERE task_id=OLD.orchestration_task_id)
  AND NOT (
      OLD.status='RUNNING'
      AND EXISTS (
          SELECT 1 FROM recovery_actions action
          JOIN orchestration_attempts attempt
            ON attempt.attempt_id=action.target_attempt_id
          WHERE action.task_id=OLD.orchestration_task_id
            AND action.status='ATTEMPT_CREATED'
            AND attempt.status IN ('RUNNING','COMPLETED')
            AND attempt.attempt_number=OLD.attempt_count
            AND (attempt.status='RUNNING' OR attempt.result_json=NEW.result_json)
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'reviewed task result is immutable outside corrective completion');
END;

CREATE TRIGGER recovery_action_subject_guard
BEFORE INSERT ON recovery_actions
WHEN NOT EXISTS (
    SELECT 1
    FROM task_reviews r
    JOIN judge_decisions d ON d.review_id = r.review_id
    JOIN structured_review_results result ON result.review_id = r.review_id
    JOIN orchestration_attempts a ON a.attempt_id = r.attempt_id
    JOIN orchestration_tasks t ON t.orchestration_task_id = r.task_id
    WHERE r.review_id = NEW.source_review_id
      AND d.decision_id = NEW.source_decision_id
      AND d.disposition = 'NEEDS_FIX'
      AND a.attempt_id = NEW.source_attempt_id
      AND t.orchestration_task_id = NEW.task_id
      AND t.project_id = NEW.project_id
      AND t.plan_id = NEW.plan_id
      AND r.objective_id IS NEW.objective_id
      AND r.status = 'COMPLETED'
      AND t.review_required = 1
      AND t.review_state = 'NEEDS_FIX'
      AND t.recovery_policy_sha256 IS NOT NULL
      AND NEW.max_retries = t.recovery_max_retries
      AND NEW.recovery_sequence = t.recovery_retry_count + 1
      AND json_extract(NEW.reason_json, '$.source_attempt_id') = r.attempt_id
      AND json_extract(NEW.reason_json, '$.previous_result.reference') = r.worker_result_reference
      AND json_extract(NEW.reason_json, '$.previous_result.sha256') = r.worker_result_sha256
      AND json_extract(NEW.reason_json, '$.source_review.review_id') = r.review_id
      AND json_extract(NEW.reason_json, '$.source_review.sha256') = result.result_sha256
      AND json_extract(NEW.reason_json, '$.judge.decision_id') = d.decision_id
      AND json_extract(NEW.reason_json, '$.judge.disposition') = 'NEEDS_FIX'
      AND (NEW.approval_id IS NULL OR EXISTS (
          SELECT 1 FROM approvals approval
          WHERE approval.approval_id=NEW.approval_id
            AND approval.run_id=a.run_id
            AND approval.status='PENDING'
            AND approval.options_json='["ACKNOWLEDGE"]'
      ))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid recovery subject provenance');
END;

CREATE TRIGGER recovery_action_identity_immutable
BEFORE UPDATE OF recovery_action_id, project_id, objective_id, plan_id, task_id,
    source_attempt_id, source_review_id, source_decision_id, recovery_sequence,
    max_retries, reason_json, reason_sha256, created_at
ON recovery_actions
BEGIN
    SELECT RAISE(ABORT, 'recovery action identity is immutable');
END;

CREATE TRIGGER recovery_action_transition_guard
BEFORE UPDATE OF status ON recovery_actions
WHEN NOT (
    (OLD.status = 'PENDING' AND NEW.status IN ('DISPATCHED', 'CANCELLED'))
    OR (OLD.status = 'DISPATCHED' AND NEW.status IN ('ATTEMPT_CREATED', 'CANCELLED'))
    OR (OLD.status = 'ATTEMPT_CREATED' AND NEW.status = 'COMPLETED')
)
BEGIN
    SELECT RAISE(ABORT, 'invalid recovery action transition');
END;

CREATE TRIGGER recovery_dispatch_guard
BEFORE UPDATE OF status, target_assignment_id ON recovery_actions
WHEN NEW.status='DISPATCHED' AND NOT EXISTS (
    SELECT 1 FROM worker_pool_assignments assignment
    JOIN orchestration_tasks task
      ON task.orchestration_task_id=assignment.orchestration_task_id
    JOIN roles role ON role.role_id=task.role_id
    WHERE assignment.assignment_id=NEW.target_assignment_id
      AND assignment.orchestration_task_id=NEW.task_id
      AND assignment.status='QUEUED'
      AND assignment.role_id=task.role_id
      AND assignment.runtime_kind=role.runtime_kind
      AND task.status='BLOCKED'
      AND task.review_state='NEEDS_FIX'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid recovery dispatch linkage');
END;

CREATE TRIGGER recovery_task_policy_immutable
BEFORE UPDATE OF recovery_max_retries, recovery_policy_sha256
ON orchestration_tasks
WHEN OLD.recovery_policy_sha256 IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'snapshotted recovery policy is immutable');
END;

CREATE TRIGGER recovery_task_budget_guard
BEFORE UPDATE OF recovery_retry_count ON orchestration_tasks
WHEN NEW.recovery_retry_count<>OLD.recovery_retry_count AND NOT (
    NEW.recovery_retry_count=OLD.recovery_retry_count+1
    AND EXISTS (
        SELECT 1 FROM recovery_actions action
        WHERE action.task_id=OLD.orchestration_task_id
          AND action.status='DISPATCHED'
          AND action.recovery_sequence=NEW.recovery_retry_count
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid recovery budget mutation');
END;

CREATE TRIGGER recovery_action_terminal_immutable
BEFORE UPDATE ON recovery_actions
WHEN OLD.status IN ('COMPLETED', 'EXHAUSTED', 'CANCELLED')
BEGIN
    SELECT RAISE(ABORT, 'terminal recovery action is immutable');
END;

CREATE TRIGGER recovery_action_delete_guard
BEFORE DELETE ON recovery_actions
BEGIN
    SELECT RAISE(ABORT, 'recovery action history is immutable');
END;

CREATE TRIGGER recovery_event_update_guard
BEFORE UPDATE ON recovery_events
BEGIN
    SELECT RAISE(ABORT, 'recovery events are immutable');
END;

CREATE TRIGGER recovery_event_delete_guard
BEFORE DELETE ON recovery_events
BEGIN
    SELECT RAISE(ABORT, 'recovery events are immutable');
END;

CREATE TRIGGER recovery_attempt_link_guard
BEFORE UPDATE OF target_attempt_id, status ON recovery_actions
WHEN NEW.status = 'ATTEMPT_CREATED' AND NOT EXISTS (
    SELECT 1
    FROM orchestration_attempts a
    JOIN worker_pool_assignments w ON w.attempt_id = a.attempt_id
    WHERE a.attempt_id = NEW.target_attempt_id
      AND a.orchestration_task_id = NEW.task_id
      AND w.assignment_id = NEW.target_assignment_id
      AND w.orchestration_task_id = NEW.task_id
)
BEGIN
    SELECT RAISE(ABORT, 'invalid corrective attempt linkage');
END;

CREATE TRIGGER recovery_snapshot_guard
BEFORE INSERT ON context_snapshots
WHEN NEW.recovery_action_id IS NOT NULL AND NOT EXISTS (
    SELECT 1
    FROM recovery_actions r
    WHERE r.recovery_action_id = NEW.recovery_action_id
      AND r.task_id = NEW.orchestration_task_id
      AND r.target_assignment_id = NEW.assignment_id
      AND r.target_attempt_id = NEW.attempt_id
      AND r.status = 'ATTEMPT_CREATED'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid recovery context snapshot linkage');
END;

INSERT INTO schema_migrations(version, applied_at)
VALUES (30, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 30;
