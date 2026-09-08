-- v0.3-A: canonical, durable Structured Project Memory authority.
-- shared_context_entries remain the v0.2 runtime-context compatibility source
-- until 0.3-B; context snapshots remain the historical authority for what an
-- execution actually received.

CREATE TABLE project_memories (
    memory_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    scope TEXT NOT NULL CHECK (scope IN ('PROJECT', 'OBJECTIVE')),
    objective_id TEXT REFERENCES objective_queue(objective_id) ON DELETE RESTRICT,
    state TEXT NOT NULL CHECK (state IN ('ACTIVE', 'RETRACTED', 'REDACTED')),
    -- Forward pointer is trigger-guarded rather than FK-backed so the stable
    -- aggregate can be inserted before revision 1 by migration runners that
    -- execute each SQL statement in autocommit mode.
    current_revision_id TEXT NOT NULL,
    current_revision_number INTEGER NOT NULL CHECK (current_revision_number >= 1),
    resource_revision INTEGER NOT NULL CHECK (resource_revision >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (
        (scope='PROJECT' AND objective_id IS NULL)
        OR (scope='OBJECTIVE' AND objective_id IS NOT NULL)
    )
);

CREATE INDEX idx_project_memories_project
    ON project_memories(project_id, state, scope, objective_id, memory_id);

CREATE TABLE project_memory_payloads (
    payload_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN (
            'FACT', 'CONSTRAINT', 'DECISION', 'ASSUMPTION',
            'FINDING', 'RESULT', 'REFERENCE', 'NOTE'
        )
    ),
    memory_key TEXT,
    title TEXT,
    content TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('AVAILABLE', 'REDACTED')),
    redacted_at TEXT,
    redaction_code TEXT,
    CHECK (
        (state='AVAILABLE' AND redacted_at IS NULL AND redaction_code IS NULL)
        OR (
            state='REDACTED'
            AND memory_key IS NULL
            AND title IS NULL
            AND content=''
            AND redacted_at IS NOT NULL
            AND redaction_code IS NOT NULL
            AND length(redaction_code) BETWEEN 1 AND 128
        )
    )
);

CREATE TABLE project_memory_revisions (
    revision_id TEXT PRIMARY KEY,
    memory_id TEXT NOT NULL
        REFERENCES project_memories(memory_id)
        ON DELETE RESTRICT DEFERRABLE INITIALLY DEFERRED,
    revision_number INTEGER NOT NULL CHECK (revision_number >= 1),
    payload_id TEXT NOT NULL UNIQUE
        REFERENCES project_memory_payloads(payload_id) ON DELETE RESTRICT,
    payload_sha256 TEXT CHECK (
        payload_sha256 IS NULL OR (
            length(payload_sha256)=64
            AND payload_sha256 NOT GLOB '*[^0-9a-f]*'
        )
    ),
    authority TEXT NOT NULL CHECK (
        authority IN (
            'OPERATOR_DECLARED', 'CONTROL_PLANE_OBSERVED', 'REVIEW_ACCEPTED',
            'AGENT_PROPOSED', 'LEGACY_UNVERIFIED'
        )
    ),
    authority_review_id TEXT REFERENCES task_reviews(review_id) ON DELETE RESTRICT,
    authority_decision_id TEXT REFERENCES judge_decisions(decision_id) ON DELETE RESTRICT,
    supersedes_revision_id TEXT
        REFERENCES project_memory_revisions(revision_id) ON DELETE RESTRICT,
    created_by_actor_type TEXT NOT NULL CHECK (
        created_by_actor_type IN ('OPERATOR', 'CONTROL_PLANE', 'AGENT', 'MIGRATION')
    ),
    created_by_actor_id TEXT NOT NULL CHECK (length(created_by_actor_id) BETWEEN 1 AND 256),
    created_at TEXT NOT NULL,
    UNIQUE (memory_id, revision_number),
    CHECK (
        (authority='OPERATOR_DECLARED' AND created_by_actor_type='OPERATOR')
        OR (authority='CONTROL_PLANE_OBSERVED' AND created_by_actor_type='CONTROL_PLANE')
        OR (authority='REVIEW_ACCEPTED' AND created_by_actor_type='CONTROL_PLANE')
        OR (authority='AGENT_PROPOSED' AND created_by_actor_type='AGENT')
        OR (authority='LEGACY_UNVERIFIED' AND created_by_actor_type='MIGRATION')
    ),
    CHECK (
        (authority='REVIEW_ACCEPTED'
         AND authority_review_id IS NOT NULL
         AND authority_decision_id IS NOT NULL)
        OR (authority<>'REVIEW_ACCEPTED'
            AND authority_review_id IS NULL
            AND authority_decision_id IS NULL)
    ),
    CHECK (
        (revision_number=1 AND supersedes_revision_id IS NULL)
        OR (revision_number>1 AND supersedes_revision_id IS NOT NULL)
    )
);

CREATE INDEX idx_project_memory_revisions_memory
    ON project_memory_revisions(memory_id, revision_number);
CREATE INDEX idx_project_memory_revisions_payload_hash
    ON project_memory_revisions(payload_sha256, authority);

CREATE TABLE project_memory_provenance (
    provenance_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL
        REFERENCES project_memory_revisions(revision_id) ON DELETE RESTRICT,
    source_kind TEXT NOT NULL CHECK (
        source_kind IN (
            'OPERATOR', 'CONTROL_PLANE', 'OBJECTIVE', 'TASK', 'ATTEMPT', 'RUN',
            'REVIEW', 'JUDGE_DECISION', 'RECOVERY',
            'LEGACY_CONTEXT', 'LEGACY_MEMORY'
        )
    ),
    source_actor_id TEXT,
    objective_id TEXT REFERENCES objective_queue(objective_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES orchestration_tasks(orchestration_task_id) ON DELETE RESTRICT,
    attempt_id TEXT REFERENCES orchestration_attempts(attempt_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    review_id TEXT REFERENCES task_reviews(review_id) ON DELETE RESTRICT,
    judge_decision_id TEXT REFERENCES judge_decisions(decision_id) ON DELETE RESTRICT,
    recovery_action_id TEXT REFERENCES recovery_actions(recovery_action_id) ON DELETE RESTRICT,
    legacy_context_id TEXT REFERENCES shared_context_entries(context_id) ON DELETE RESTRICT,
    legacy_memory_id TEXT REFERENCES memory_records(memory_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (
        (source_kind IN ('OPERATOR','CONTROL_PLANE')
         AND source_actor_id IS NOT NULL
         AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
         AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
         AND recovery_action_id IS NULL AND legacy_context_id IS NULL
         AND legacy_memory_id IS NULL)
        OR (source_kind='OBJECTIVE' AND source_actor_id IS NULL
            AND objective_id IS NOT NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='TASK' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NOT NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='ATTEMPT' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NOT NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='RUN' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NOT NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='REVIEW' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NOT NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='JUDGE_DECISION' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NOT NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='RECOVERY' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NOT NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='LEGACY_CONTEXT' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NOT NULL
            AND legacy_memory_id IS NULL)
        OR (source_kind='LEGACY_MEMORY' AND source_actor_id IS NULL
            AND objective_id IS NULL AND task_id IS NULL AND attempt_id IS NULL
            AND run_id IS NULL AND review_id IS NULL AND judge_decision_id IS NULL
            AND recovery_action_id IS NULL AND legacy_context_id IS NULL
            AND legacy_memory_id IS NOT NULL)
    )
);

CREATE INDEX idx_project_memory_provenance_revision
    ON project_memory_provenance(revision_id, created_at, provenance_id);

CREATE TABLE project_memory_revision_links (
    link_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL
        REFERENCES project_memory_revisions(revision_id) ON DELETE RESTRICT,
    link_kind TEXT NOT NULL CHECK (
        link_kind IN ('OBJECTIVE','TASK','ATTEMPT','RUN','REVIEW','JUDGE_DECISION','RECOVERY')
    ),
    objective_id TEXT REFERENCES objective_queue(objective_id) ON DELETE RESTRICT,
    task_id TEXT REFERENCES orchestration_tasks(orchestration_task_id) ON DELETE RESTRICT,
    attempt_id TEXT REFERENCES orchestration_attempts(attempt_id) ON DELETE RESTRICT,
    run_id TEXT REFERENCES runs(run_id) ON DELETE RESTRICT,
    review_id TEXT REFERENCES task_reviews(review_id) ON DELETE RESTRICT,
    judge_decision_id TEXT REFERENCES judge_decisions(decision_id) ON DELETE RESTRICT,
    recovery_action_id TEXT REFERENCES recovery_actions(recovery_action_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    CHECK (
        (link_kind='OBJECTIVE' AND objective_id IS NOT NULL
         AND task_id IS NULL AND attempt_id IS NULL AND run_id IS NULL
         AND review_id IS NULL AND judge_decision_id IS NULL AND recovery_action_id IS NULL)
        OR (link_kind='TASK' AND objective_id IS NULL
            AND task_id IS NOT NULL AND attempt_id IS NULL AND run_id IS NULL
            AND review_id IS NULL AND judge_decision_id IS NULL AND recovery_action_id IS NULL)
        OR (link_kind='ATTEMPT' AND objective_id IS NULL
            AND task_id IS NULL AND attempt_id IS NOT NULL AND run_id IS NULL
            AND review_id IS NULL AND judge_decision_id IS NULL AND recovery_action_id IS NULL)
        OR (link_kind='RUN' AND objective_id IS NULL
            AND task_id IS NULL AND attempt_id IS NULL AND run_id IS NOT NULL
            AND review_id IS NULL AND judge_decision_id IS NULL AND recovery_action_id IS NULL)
        OR (link_kind='REVIEW' AND objective_id IS NULL
            AND task_id IS NULL AND attempt_id IS NULL AND run_id IS NULL
            AND review_id IS NOT NULL AND judge_decision_id IS NULL AND recovery_action_id IS NULL)
        OR (link_kind='JUDGE_DECISION' AND objective_id IS NULL
            AND task_id IS NULL AND attempt_id IS NULL AND run_id IS NULL
            AND review_id IS NULL AND judge_decision_id IS NOT NULL AND recovery_action_id IS NULL)
        OR (link_kind='RECOVERY' AND objective_id IS NULL
            AND task_id IS NULL AND attempt_id IS NULL AND run_id IS NULL
            AND review_id IS NULL AND judge_decision_id IS NULL AND recovery_action_id IS NOT NULL)
    )
);

CREATE INDEX idx_project_memory_links_revision
    ON project_memory_revision_links(revision_id, created_at, link_id);

-- Memory headers are stable identities. Revision advancement and lifecycle
-- changes are the only mutable projection fields.
CREATE TRIGGER project_memory_insert_guard
BEFORE INSERT ON project_memories
WHEN NEW.state<>'ACTIVE'
  OR NEW.current_revision_number<>1
  OR NEW.resource_revision<>1
  OR NEW.updated_at<NEW.created_at
BEGIN
    SELECT RAISE(ABORT, 'invalid initial project memory state');
END;

CREATE TRIGGER project_memory_scope_guard
BEFORE INSERT ON project_memories
WHEN NEW.scope='OBJECTIVE' AND NOT EXISTS (
    SELECT 1
    FROM objective_queue objective
    JOIN json_each(objective.project_scope_json) scope
      ON scope.value=NEW.project_id
    WHERE objective.objective_id=NEW.objective_id
)
BEGIN
    SELECT RAISE(ABORT, 'objective is not scoped to project memory project');
END;

CREATE TRIGGER project_memory_identity_immutable
BEFORE UPDATE OF memory_id, project_id, scope, objective_id, created_at
ON project_memories
BEGIN
    SELECT RAISE(ABORT, 'project memory identity is immutable');
END;

CREATE TRIGGER project_memory_update_guard
BEFORE UPDATE ON project_memories
WHEN NOT (
    NEW.updated_at>=OLD.updated_at
    AND (
        -- Attest/link the current revision without changing its content.
        (OLD.state='ACTIVE'
         AND OLD.state=NEW.state
         AND NEW.current_revision_id=OLD.current_revision_id
         AND NEW.current_revision_number=OLD.current_revision_number
         AND NEW.resource_revision=OLD.resource_revision+1)
        OR
        -- Create exactly one immutable successor while ACTIVE.
        (OLD.state='ACTIVE' AND NEW.state='ACTIVE'
         AND NEW.current_revision_number=OLD.current_revision_number+1
         AND NEW.current_revision_id<>OLD.current_revision_id
         AND NEW.resource_revision=OLD.resource_revision+1
         AND EXISTS (
             SELECT 1 FROM project_memory_revisions revision
             WHERE revision.revision_id=NEW.current_revision_id
               AND revision.memory_id=OLD.memory_id
               AND revision.revision_number=NEW.current_revision_number
               AND revision.supersedes_revision_id=OLD.current_revision_id
         ))
        OR
        -- Retraction is a terminal business transition.
        (OLD.state='ACTIVE' AND NEW.state='RETRACTED'
         AND NEW.current_revision_id=OLD.current_revision_id
         AND NEW.current_revision_number=OLD.current_revision_number
         AND NEW.resource_revision=OLD.resource_revision+1)
        OR
        -- Security redaction remains possible after a prior retraction.
        (OLD.state IN ('ACTIVE','RETRACTED') AND NEW.state='REDACTED'
         AND NEW.current_revision_id=OLD.current_revision_id
         AND NEW.current_revision_number=OLD.current_revision_number
         AND NEW.resource_revision=OLD.resource_revision+1
         AND NOT EXISTS (
             SELECT 1
             FROM project_memory_revisions revision
             JOIN project_memory_payloads payload USING(payload_id)
             WHERE revision.memory_id=OLD.memory_id
               AND payload.state<>'REDACTED'
         ))
    )
)
BEGIN
    SELECT RAISE(ABORT, 'invalid project memory transition');
END;

CREATE TRIGGER project_memory_delete_guard
BEFORE DELETE ON project_memories
BEGIN
    SELECT RAISE(ABORT, 'project memory history is immutable');
END;

CREATE TRIGGER project_memory_payload_insert_guard
BEFORE INSERT ON project_memory_payloads
WHEN NEW.state<>'AVAILABLE' OR NEW.redacted_at IS NOT NULL OR NEW.redaction_code IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'project memory payload must begin available');
END;

CREATE TRIGGER project_memory_payload_update_guard
BEFORE UPDATE ON project_memory_payloads
WHEN NOT (
    OLD.state='AVAILABLE'
    AND NEW.state='REDACTED'
    AND NEW.payload_id=OLD.payload_id
    AND NEW.kind=OLD.kind
    AND NEW.memory_key IS NULL
    AND NEW.title IS NULL
    AND NEW.content=''
    AND NEW.redacted_at IS NOT NULL
    AND NEW.redaction_code IS NOT NULL
    AND length(NEW.redaction_code) BETWEEN 1 AND 128
)
BEGIN
    SELECT RAISE(ABORT, 'project memory payload is immutable except redaction');
END;

CREATE TRIGGER project_memory_payload_delete_guard
BEFORE DELETE ON project_memory_payloads
BEGIN
    SELECT RAISE(ABORT, 'project memory payload history is immutable');
END;

CREATE TRIGGER project_memory_revision_sequence_guard
BEFORE INSERT ON project_memory_revisions
WHEN NOT (
    (NEW.revision_number=1
     AND NEW.supersedes_revision_id IS NULL
     AND EXISTS (
         SELECT 1 FROM project_memories memory
         WHERE memory.memory_id=NEW.memory_id
           AND memory.current_revision_id=NEW.revision_id
           AND memory.current_revision_number=1
     )
     AND NOT EXISTS (
         SELECT 1 FROM project_memory_revisions existing
         WHERE existing.memory_id=NEW.memory_id
     ))
    OR
    (NEW.revision_number>1
     AND EXISTS (
         SELECT 1 FROM project_memories memory
         JOIN project_memory_revisions previous
           ON previous.revision_id=memory.current_revision_id
         WHERE memory.memory_id=NEW.memory_id
           AND memory.state='ACTIVE'
           AND memory.current_revision_number=NEW.revision_number-1
           AND previous.memory_id=NEW.memory_id
           AND previous.revision_number=NEW.revision_number-1
           AND NEW.supersedes_revision_id=previous.revision_id
     ))
)
BEGIN
    SELECT RAISE(ABORT, 'invalid project memory revision sequence');
END;

CREATE TRIGGER project_memory_review_authority_guard
BEFORE INSERT ON project_memory_revisions
WHEN NEW.authority='REVIEW_ACCEPTED' AND NOT EXISTS (
    SELECT 1
    FROM project_memories memory
    JOIN task_reviews review ON review.project_id=memory.project_id
    JOIN judge_decisions decision ON decision.review_id=review.review_id
    LEFT JOIN approvals approval ON approval.approval_id=decision.approval_id
    WHERE memory.memory_id=NEW.memory_id
      AND review.review_id=NEW.authority_review_id
      AND decision.decision_id=NEW.authority_decision_id
      AND review.status='COMPLETED'
      AND (
          decision.disposition='PASS'
          OR (decision.disposition='HUMAN_REVIEW' AND approval.status='APPROVED')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'review accepted memory requires accepted evidence');
END;

CREATE TRIGGER project_memory_revision_update_guard
BEFORE UPDATE ON project_memory_revisions
BEGIN
    SELECT RAISE(ABORT, 'project memory revisions are immutable');
END;

CREATE TRIGGER project_memory_revision_delete_guard
BEFORE DELETE ON project_memory_revisions
BEGIN
    SELECT RAISE(ABORT, 'project memory revisions are immutable');
END;

CREATE TRIGGER project_memory_provenance_project_guard
BEFORE INSERT ON project_memory_provenance
WHEN NOT EXISTS (
    SELECT 1
    FROM project_memory_revisions revision
    JOIN project_memories memory ON memory.memory_id=revision.memory_id
    WHERE revision.revision_id=NEW.revision_id
      AND (
          NEW.source_kind IN ('OPERATOR','CONTROL_PLANE')
          OR (NEW.source_kind='OBJECTIVE' AND EXISTS (
              SELECT 1 FROM objective_queue objective
              JOIN json_each(objective.project_scope_json) scope
                ON scope.value=memory.project_id
              WHERE objective.objective_id=NEW.objective_id
          ))
          OR (NEW.source_kind='TASK' AND EXISTS (
              SELECT 1 FROM orchestration_tasks task
              WHERE task.orchestration_task_id=NEW.task_id
                AND task.project_id=memory.project_id
          ))
          OR (NEW.source_kind='ATTEMPT' AND EXISTS (
              SELECT 1 FROM orchestration_attempts attempt
              JOIN orchestration_tasks task
                ON task.orchestration_task_id=attempt.orchestration_task_id
              WHERE attempt.attempt_id=NEW.attempt_id
                AND task.project_id=memory.project_id
          ))
          OR (NEW.source_kind='RUN' AND EXISTS (
              SELECT 1 FROM runs run
              WHERE run.run_id=NEW.run_id AND run.project_id=memory.project_id
          ))
          OR (NEW.source_kind='REVIEW' AND EXISTS (
              SELECT 1 FROM task_reviews review
              WHERE review.review_id=NEW.review_id
                AND review.project_id=memory.project_id
          ))
          OR (NEW.source_kind='JUDGE_DECISION' AND EXISTS (
              SELECT 1 FROM judge_decisions decision
              JOIN task_reviews review ON review.review_id=decision.review_id
              WHERE decision.decision_id=NEW.judge_decision_id
                AND review.project_id=memory.project_id
          ))
          OR (NEW.source_kind='RECOVERY' AND EXISTS (
              SELECT 1 FROM recovery_actions recovery
              WHERE recovery.recovery_action_id=NEW.recovery_action_id
                AND recovery.project_id=memory.project_id
          ))
          OR (NEW.source_kind='LEGACY_CONTEXT' AND EXISTS (
              SELECT 1 FROM shared_context_entries context
              WHERE context.context_id=NEW.legacy_context_id
                AND context.project_id=memory.project_id
          ))
          OR (NEW.source_kind='LEGACY_MEMORY' AND EXISTS (
              SELECT 1 FROM memory_records legacy
              WHERE legacy.memory_id=NEW.legacy_memory_id
                AND legacy.project_id=memory.project_id
          ))
      )
)
BEGIN
    SELECT RAISE(ABORT, 'project memory provenance crosses project boundary');
END;

CREATE TRIGGER project_memory_provenance_update_guard
BEFORE UPDATE ON project_memory_provenance
BEGIN
    SELECT RAISE(ABORT, 'project memory provenance is immutable');
END;
CREATE TRIGGER project_memory_provenance_delete_guard
BEFORE DELETE ON project_memory_provenance
BEGIN
    SELECT RAISE(ABORT, 'project memory provenance is immutable');
END;

CREATE TRIGGER project_memory_link_project_guard
BEFORE INSERT ON project_memory_revision_links
WHEN NOT EXISTS (
    SELECT 1
    FROM project_memory_revisions revision
    JOIN project_memories memory ON memory.memory_id=revision.memory_id
    WHERE revision.revision_id=NEW.revision_id
      AND (
          (NEW.link_kind='OBJECTIVE' AND EXISTS (
              SELECT 1 FROM objective_queue objective
              JOIN json_each(objective.project_scope_json) scope
                ON scope.value=memory.project_id
              WHERE objective.objective_id=NEW.objective_id
          ))
          OR (NEW.link_kind='TASK' AND EXISTS (
              SELECT 1 FROM orchestration_tasks task
              WHERE task.orchestration_task_id=NEW.task_id
                AND task.project_id=memory.project_id
          ))
          OR (NEW.link_kind='ATTEMPT' AND EXISTS (
              SELECT 1 FROM orchestration_attempts attempt
              JOIN orchestration_tasks task
                ON task.orchestration_task_id=attempt.orchestration_task_id
              WHERE attempt.attempt_id=NEW.attempt_id
                AND task.project_id=memory.project_id
          ))
          OR (NEW.link_kind='RUN' AND EXISTS (
              SELECT 1 FROM runs run
              WHERE run.run_id=NEW.run_id AND run.project_id=memory.project_id
          ))
          OR (NEW.link_kind='REVIEW' AND EXISTS (
              SELECT 1 FROM task_reviews review
              WHERE review.review_id=NEW.review_id
                AND review.project_id=memory.project_id
          ))
          OR (NEW.link_kind='JUDGE_DECISION' AND EXISTS (
              SELECT 1 FROM judge_decisions decision
              JOIN task_reviews review ON review.review_id=decision.review_id
              WHERE decision.decision_id=NEW.judge_decision_id
                AND review.project_id=memory.project_id
          ))
          OR (NEW.link_kind='RECOVERY' AND EXISTS (
              SELECT 1 FROM recovery_actions recovery
              WHERE recovery.recovery_action_id=NEW.recovery_action_id
                AND recovery.project_id=memory.project_id
          ))
      )
)
BEGIN
    SELECT RAISE(ABORT, 'project memory link crosses project boundary');
END;

CREATE TRIGGER project_memory_link_update_guard
BEFORE UPDATE ON project_memory_revision_links
BEGIN
    SELECT RAISE(ABORT, 'project memory links are immutable');
END;
CREATE TRIGGER project_memory_link_delete_guard
BEFORE DELETE ON project_memory_revision_links
BEGIN
    SELECT RAISE(ABORT, 'project memory links are immutable');
END;

-- Preserve legacy memory rows one-for-one. memory_records did not have a
-- structured key, so NULL is preserved instead of inventing one. Unknown
-- categories become NOTE while provenance preserves the exact legacy row.
INSERT INTO project_memories (
    memory_id, project_id, scope, objective_id, state,
    current_revision_id, current_revision_number, resource_revision,
    created_at, updated_at
)
SELECT
    'legacy-memory-record:' || memory_id,
    project_id,
    'PROJECT',
    NULL,
    'ACTIVE',
    'legacy-memory-revision:' || memory_id,
    1,
    1,
    created_at,
    created_at
FROM memory_records;

INSERT INTO project_memory_payloads (
    payload_id, kind, memory_key, title, content, state, redacted_at, redaction_code
)
SELECT
    'legacy-memory-payload:' || memory_id,
    CASE upper(category)
        WHEN 'FACT' THEN 'FACT'
        WHEN 'CONSTRAINT' THEN 'CONSTRAINT'
        WHEN 'DECISION' THEN 'DECISION'
        WHEN 'ASSUMPTION' THEN 'ASSUMPTION'
        WHEN 'FINDING' THEN 'FINDING'
        WHEN 'RESULT' THEN 'RESULT'
        WHEN 'REFERENCE' THEN 'REFERENCE'
        WHEN 'NOTE' THEN 'NOTE'
        ELSE 'NOTE'
    END,
    NULL,
    title,
    body,
    'AVAILABLE',
    NULL,
    NULL
FROM memory_records;

INSERT INTO project_memory_revisions (
    revision_id, memory_id, revision_number, payload_id, payload_sha256,
    authority, authority_review_id, authority_decision_id,
    supersedes_revision_id, created_by_actor_type, created_by_actor_id, created_at
)
SELECT
    'legacy-memory-revision:' || memory_id,
    'legacy-memory-record:' || memory_id,
    1,
    'legacy-memory-payload:' || memory_id,
    NULL,
    'LEGACY_UNVERIFIED',
    NULL,
    NULL,
    NULL,
    'MIGRATION',
    'migration-032',
    created_at
FROM memory_records;

INSERT INTO project_memory_provenance (
    provenance_id, revision_id, source_kind, source_actor_id,
    objective_id, task_id, attempt_id, run_id, review_id,
    judge_decision_id, recovery_action_id, legacy_context_id,
    legacy_memory_id, created_at
)
SELECT
    'legacy-memory-provenance:' || memory_id,
    'legacy-memory-revision:' || memory_id,
    'LEGACY_MEMORY',
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    memory_id,
    created_at
FROM memory_records;

-- Preserve every shared_context_entries row as a distinct memory aggregate.
-- Repeated keys deliberately stay distinct: the v0.2 contract had no semantic
-- deduplication or revision semantics.
INSERT INTO project_memories (
    memory_id, project_id, scope, objective_id, state,
    current_revision_id, current_revision_number, resource_revision,
    created_at, updated_at
)
SELECT
    'legacy-context-entry:' || context_id,
    project_id,
    scope,
    objective_id,
    'ACTIVE',
    'legacy-context-revision:' || context_id,
    1,
    1,
    created_at,
    created_at
FROM shared_context_entries;

INSERT INTO project_memory_payloads (
    payload_id, kind, memory_key, title, content, state, redacted_at, redaction_code
)
SELECT
    'legacy-context-payload:' || context_id,
    kind,
    context_key,
    NULL,
    content,
    'AVAILABLE',
    NULL,
    NULL
FROM shared_context_entries;

INSERT INTO project_memory_revisions (
    revision_id, memory_id, revision_number, payload_id, payload_sha256,
    authority, authority_review_id, authority_decision_id,
    supersedes_revision_id, created_by_actor_type, created_by_actor_id, created_at
)
SELECT
    'legacy-context-revision:' || context_id,
    'legacy-context-entry:' || context_id,
    1,
    'legacy-context-payload:' || context_id,
    NULL,
    'LEGACY_UNVERIFIED',
    NULL,
    NULL,
    NULL,
    'MIGRATION',
    'migration-032',
    created_at
FROM shared_context_entries;

INSERT INTO project_memory_provenance (
    provenance_id, revision_id, source_kind, source_actor_id,
    objective_id, task_id, attempt_id, run_id, review_id,
    judge_decision_id, recovery_action_id, legacy_context_id,
    legacy_memory_id, created_at
)
SELECT
    'legacy-context-provenance:' || context_id,
    'legacy-context-revision:' || context_id,
    'LEGACY_CONTEXT',
    NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
    context_id,
    NULL,
    created_at
FROM shared_context_entries;

INSERT INTO schema_migrations(version, applied_at)
VALUES (32, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version=32;
