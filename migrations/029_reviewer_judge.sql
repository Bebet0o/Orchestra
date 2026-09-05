-- Task acceptance is opt-in; historical task/review semantics are unchanged.
ALTER TABLE orchestration_tasks ADD COLUMN review_required INTEGER NOT NULL DEFAULT 0
    CHECK (review_required IN (0, 1));
ALTER TABLE orchestration_tasks ADD COLUMN reviewer_role_id TEXT REFERENCES roles(role_id);
ALTER TABLE orchestration_tasks ADD COLUMN review_state TEXT NOT NULL DEFAULT 'NONE'
    CHECK (review_state IN ('NONE','PENDING','PASS','NEEDS_FIX','BLOCKED','HUMAN_REVIEW','FAILED'));

CREATE TABLE task_reviews (
    review_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    objective_id TEXT REFERENCES objective_queue(objective_id),
    plan_id TEXT NOT NULL REFERENCES orchestration_plans(plan_id),
    task_id TEXT NOT NULL REFERENCES orchestration_tasks(orchestration_task_id),
    assignment_id TEXT NOT NULL REFERENCES worker_pool_assignments(assignment_id),
    attempt_id TEXT NOT NULL UNIQUE REFERENCES orchestration_attempts(attempt_id),
    worker_execution_id TEXT REFERENCES worker_executions(execution_id),
    worker_snapshot_id TEXT NOT NULL REFERENCES context_snapshots(context_snapshot_id),
    worker_result_reference TEXT NOT NULL,
    worker_result_sha256 TEXT NOT NULL CHECK (length(worker_result_sha256)=64),
    role_id TEXT NOT NULL REFERENCES roles(role_id),
    runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('hermes','native')),
    runtime_config_id TEXT NOT NULL,
    model_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('PENDING','RUNNING','COMPLETED','FAILED')),
    execution_id TEXT NOT NULL UNIQUE,
    claim_owner TEXT,
    legacy_execution_id TEXT REFERENCES reviewer_executions(execution_id),
    failure_code TEXT CHECK (failure_code IN ('INVALID_CONTEXT','INVALID_OUTPUT','RUNTIME_FAILED','INTERRUPTED')),
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE TABLE review_context_snapshots (
    review_id TEXT PRIMARY KEY REFERENCES task_reviews(review_id),
    context_snapshot_id TEXT NOT NULL UNIQUE,
    context_schema_version INTEGER NOT NULL CHECK (context_schema_version=1),
    worker_snapshot_id TEXT NOT NULL REFERENCES context_snapshots(context_snapshot_id),
    worker_snapshot_sha256 TEXT NOT NULL CHECK (length(worker_snapshot_sha256)=64),
    worker_result_reference TEXT NOT NULL,
    worker_result_sha256 TEXT NOT NULL CHECK (length(worker_result_sha256)=64),
    projection_json TEXT NOT NULL CHECK (json_valid(projection_json)),
    projection_sha256 TEXT NOT NULL CHECK (length(projection_sha256)=64),
    created_at TEXT NOT NULL
);
CREATE TABLE structured_review_results (
    review_id TEXT PRIMARY KEY REFERENCES task_reviews(review_id),
    schema_version INTEGER NOT NULL CHECK (schema_version=1),
    assessment TEXT NOT NULL CHECK (assessment IN ('pass','needs_fix','blocked','human_review')),
    canonical_json TEXT NOT NULL CHECK (json_valid(canonical_json) AND length(CAST(canonical_json AS BLOB))<=65536),
    result_sha256 TEXT NOT NULL CHECK (length(result_sha256)=64),
    created_at TEXT NOT NULL
);
CREATE TABLE judge_decisions (
    decision_id TEXT PRIMARY KEY,
    review_id TEXT NOT NULL UNIQUE REFERENCES structured_review_results(review_id),
    disposition TEXT NOT NULL CHECK (disposition IN ('PASS','NEEDS_FIX','BLOCKED','HUMAN_REVIEW')),
    reason TEXT NOT NULL,
    review_sha256 TEXT NOT NULL CHECK (length(review_sha256)=64),
    approval_id TEXT UNIQUE REFERENCES approvals(approval_id),
    decided_at TEXT NOT NULL
);
CREATE TABLE review_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    review_id TEXT NOT NULL REFERENCES task_reviews(review_id),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('review_requested','review_started','review_completed','review_failed','judge_decided','human_review_required','task_accepted','human_decided','integration_started','integration_failed')),
    created_at TEXT NOT NULL,
    UNIQUE (review_id, event_kind)
);
CREATE TRIGGER task_review_completion_guard BEFORE UPDATE OF status ON orchestration_tasks
WHEN NEW.status='COMPLETED' AND NEW.review_required=1 AND (
    NEW.review_state<>'PASS' OR NOT EXISTS (
        SELECT 1 FROM task_reviews r JOIN judge_decisions d USING(review_id)
        JOIN orchestration_attempts a ON a.attempt_id=r.attempt_id
        LEFT JOIN approvals h ON h.approval_id=d.approval_id
        LEFT JOIN runs w ON w.run_id=a.run_id
        WHERE r.task_id=NEW.orchestration_task_id AND r.status='COMPLETED'
          AND a.attempt_number=NEW.attempt_count
          AND (a.run_id IS NULL OR w.status='COMPLETED')
          AND (d.disposition='PASS' OR (d.disposition='HUMAN_REVIEW' AND h.status='APPROVED'))
    )
)
BEGIN SELECT RAISE(ABORT, 'required task review has not accepted the result'); END;
CREATE TRIGGER task_review_policy_guard BEFORE UPDATE OF review_required, reviewer_role_id ON orchestration_tasks
WHEN OLD.status NOT IN ('PENDING','READY') OR OLD.attempt_count>0
BEGIN SELECT RAISE(ABORT, 'started task review policy is immutable'); END;
CREATE TRIGGER task_review_subject_guard BEFORE INSERT ON task_reviews
WHEN NOT EXISTS (
    SELECT 1 FROM orchestration_tasks t
    JOIN orchestration_attempts a ON a.orchestration_task_id=t.orchestration_task_id
    JOIN worker_pool_assignments w ON w.attempt_id=a.attempt_id AND w.orchestration_task_id=t.orchestration_task_id
    JOIN context_snapshots s ON s.context_snapshot_id=a.context_snapshot_id
    WHERE t.orchestration_task_id=NEW.task_id AND t.project_id=NEW.project_id
      AND t.plan_id=NEW.plan_id AND t.review_required=1
      AND a.attempt_id=NEW.attempt_id AND a.status='COMPLETED'
      AND a.worker_execution_id IS NEW.worker_execution_id
      AND w.assignment_id=NEW.assignment_id
      AND s.context_snapshot_id=NEW.worker_snapshot_id AND s.consumer_kind='WORKER'
      AND s.attempt_id=a.attempt_id AND s.assignment_id=w.assignment_id
      AND s.orchestration_task_id=t.orchestration_task_id AND s.objective_id IS NEW.objective_id
)
BEGIN SELECT RAISE(ABORT, 'invalid review subject provenance'); END;
CREATE TRIGGER reviewed_attempt_immutable BEFORE UPDATE ON orchestration_attempts
WHEN EXISTS (SELECT 1 FROM task_reviews WHERE attempt_id=OLD.attempt_id)
BEGIN SELECT RAISE(ABORT, 'reviewed worker attempt is immutable'); END;
CREATE TRIGGER reviewed_task_result_immutable BEFORE UPDATE OF result_json ON orchestration_tasks
WHEN NEW.result_json<>OLD.result_json AND EXISTS (SELECT 1 FROM task_reviews WHERE task_id=OLD.orchestration_task_id)
BEGIN SELECT RAISE(ABORT, 'reviewed task result is immutable'); END;
CREATE TRIGGER reviewed_worker_immutable BEFORE UPDATE ON worker_executions
WHEN EXISTS (SELECT 1 FROM task_reviews WHERE worker_execution_id=OLD.execution_id)
BEGIN SELECT RAISE(ABORT, 'reviewed worker execution is immutable'); END;
CREATE TRIGGER task_reviews_identity BEFORE UPDATE OF review_id, project_id, objective_id, plan_id, task_id, assignment_id, attempt_id, worker_execution_id, worker_snapshot_id, worker_result_reference, worker_result_sha256, role_id, runtime_kind, runtime_config_id, model_id, execution_id, created_at ON task_reviews
BEGIN SELECT RAISE(ABORT, 'review identity is immutable'); END;
CREATE TRIGGER task_reviews_terminal BEFORE UPDATE ON task_reviews WHEN OLD.status IN ('COMPLETED','FAILED')
BEGIN SELECT RAISE(ABORT, 'terminal review is immutable'); END;
CREATE TRIGGER task_reviews_transition BEFORE UPDATE OF status ON task_reviews
WHEN NOT ((OLD.status='PENDING' AND NEW.status IN ('RUNNING','FAILED')) OR (OLD.status='RUNNING' AND NEW.status IN ('COMPLETED','FAILED')))
BEGIN SELECT RAISE(ABORT, 'invalid review transition'); END;
CREATE TRIGGER review_context_snapshots_immutable_update BEFORE UPDATE ON review_context_snapshots
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER review_context_snapshots_immutable_delete BEFORE DELETE ON review_context_snapshots
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER review_context_snapshots_no_replace BEFORE INSERT ON review_context_snapshots
WHEN EXISTS (SELECT 1 FROM review_context_snapshots WHERE review_id=NEW.review_id OR context_snapshot_id=NEW.context_snapshot_id)
BEGIN SELECT RAISE(ABORT, 'review history cannot be replaced'); END;
CREATE TRIGGER structured_review_results_immutable_update BEFORE UPDATE ON structured_review_results
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER structured_review_results_immutable_delete BEFORE DELETE ON structured_review_results
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER structured_review_results_no_replace BEFORE INSERT ON structured_review_results
WHEN EXISTS (SELECT 1 FROM structured_review_results WHERE review_id=NEW.review_id)
BEGIN SELECT RAISE(ABORT, 'review history cannot be replaced'); END;
CREATE TRIGGER judge_decisions_immutable_update BEFORE UPDATE ON judge_decisions
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER judge_decisions_immutable_delete BEFORE DELETE ON judge_decisions
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER judge_decisions_no_replace BEFORE INSERT ON judge_decisions
WHEN EXISTS (SELECT 1 FROM judge_decisions WHERE review_id=NEW.review_id OR decision_id=NEW.decision_id OR approval_id=NEW.approval_id)
BEGIN SELECT RAISE(ABORT, 'review history cannot be replaced'); END;
CREATE TRIGGER review_events_immutable_update BEFORE UPDATE ON review_events
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER review_events_immutable_delete BEFORE DELETE ON review_events
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER review_events_no_replace BEFORE INSERT ON review_events
WHEN EXISTS (SELECT 1 FROM review_events WHERE event_id=NEW.event_id)
BEGIN SELECT RAISE(ABORT, 'review history cannot be replaced'); END;
CREATE TRIGGER task_reviews_immutable_delete BEFORE DELETE ON task_reviews
BEGIN SELECT RAISE(ABORT, 'review history is immutable'); END;
CREATE TRIGGER task_reviews_no_replace BEFORE INSERT ON task_reviews
WHEN EXISTS (SELECT 1 FROM task_reviews WHERE review_id=NEW.review_id OR attempt_id=NEW.attempt_id OR execution_id=NEW.execution_id)
BEGIN SELECT RAISE(ABORT, 'review history cannot be replaced'); END;
CREATE TRIGGER review_context_ownership BEFORE INSERT ON review_context_snapshots
WHEN NOT EXISTS (
    SELECT 1 FROM task_reviews r JOIN context_snapshots s ON s.context_snapshot_id=r.worker_snapshot_id
    WHERE r.review_id=NEW.review_id AND r.worker_snapshot_id=NEW.worker_snapshot_id
      AND s.projection_sha256=NEW.worker_snapshot_sha256
      AND r.worker_result_reference=NEW.worker_result_reference
      AND r.worker_result_sha256=NEW.worker_result_sha256
)
BEGIN SELECT RAISE(ABORT, 'review context ownership mismatch'); END;
CREATE TRIGGER judge_evidence_guard BEFORE INSERT ON judge_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM structured_review_results s WHERE s.review_id=NEW.review_id
      AND upper(s.assessment)=NEW.disposition AND s.result_sha256=NEW.review_sha256
)
BEGIN SELECT RAISE(ABORT, 'judge requires matching validated evidence'); END;
CREATE TRIGGER reviewed_assignment_identity BEFORE UPDATE OF orchestration_task_id, attempt_id, assignment_id ON worker_pool_assignments
WHEN EXISTS (SELECT 1 FROM task_reviews WHERE assignment_id=OLD.assignment_id)
BEGIN SELECT RAISE(ABORT, 'reviewed worker assignment identity is immutable'); END;
INSERT INTO schema_migrations VALUES (29, strftime('%Y-%m-%dT%H:%M:%fZ','now'));
PRAGMA user_version=29;
