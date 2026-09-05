-- v0.2-G: durable, immutable model-routing authority.
CREATE TABLE model_routing_policies (
    policy_sha256 TEXT PRIMARY KEY CHECK (
        length(policy_sha256)=64
        AND policy_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
    canonical_json TEXT NOT NULL CHECK (
        json_valid(canonical_json)
        AND json_type(canonical_json)='object'
        AND json_extract(canonical_json,'$.version')=policy_version
        AND json_type(canonical_json,'$.rules')='array'
        AND json_array_length(canonical_json,'$.rules') <= 64
        AND length(CAST(canonical_json AS BLOB)) <= 131072
    ),
    created_at TEXT NOT NULL
);

CREATE TABLE model_route_decisions (
    decision_id TEXT PRIMARY KEY CHECK (length(decision_id) BETWEEN 1 AND 256),
    runtime_request_id TEXT NOT NULL UNIQUE CHECK (
        length(runtime_request_id) BETWEEN 1 AND 256
    ),
    execution_kind TEXT NOT NULL CHECK (
        execution_kind IN ('PLANNER','WORKER','REVIEWER')
    ),
    execution_id TEXT NOT NULL CHECK (length(execution_id) BETWEEN 1 AND 256),
    role_id TEXT NOT NULL REFERENCES roles(role_id) ON DELETE RESTRICT,
    runtime_role TEXT NOT NULL CHECK (
        runtime_role IN ('planner','worker','reviewer')
    ),
    runtime_kind TEXT NOT NULL CHECK (runtime_kind IN ('hermes','native')),
    orchestration_task_id TEXT REFERENCES orchestration_tasks(orchestration_task_id)
        ON DELETE RESTRICT,
    task_kind TEXT CHECK (
        task_kind IS NULL OR length(task_kind) BETWEEN 1 AND 64
    ),
    configured_model_id TEXT NOT NULL CHECK (
        length(configured_model_id) BETWEEN 1 AND 256
    ),
    selected_model_id TEXT NOT NULL CHECK (
        length(selected_model_id) BETWEEN 1 AND 256
    ),
    request_json TEXT NOT NULL CHECK (
        json_valid(request_json)
        AND json_type(request_json)='object'
        AND length(CAST(request_json AS BLOB)) <= 4096
    ),
    request_sha256 TEXT NOT NULL CHECK (
        length(request_sha256)=64
        AND request_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    policy_sha256 TEXT NOT NULL REFERENCES model_routing_policies(policy_sha256)
        ON DELETE RESTRICT,
    policy_version INTEGER NOT NULL CHECK (policy_version >= 1),
    rule_id TEXT NOT NULL CHECK (length(rule_id) BETWEEN 1 AND 128),
    reason TEXT NOT NULL CHECK (
        reason IN ('rule_match','configured_default','runtime_managed')
    ),
    created_at TEXT NOT NULL,
    CHECK (
        (execution_kind='PLANNER' AND runtime_role='planner')
        OR (execution_kind='WORKER' AND runtime_role='worker')
        OR (execution_kind='REVIEWER' AND runtime_role='reviewer')
    ),
    UNIQUE (execution_kind, execution_id)
);

CREATE INDEX idx_model_route_decisions_task
    ON model_route_decisions(orchestration_task_id, created_at);
CREATE INDEX idx_model_route_decisions_role
    ON model_route_decisions(role_id, created_at);

CREATE TRIGGER model_routing_policy_no_replace
BEFORE INSERT ON model_routing_policies
WHEN EXISTS (
    SELECT 1 FROM model_routing_policies
    WHERE policy_sha256=NEW.policy_sha256
)
BEGIN
    SELECT RAISE(ABORT, 'model routing policy cannot be replaced');
END;

CREATE TRIGGER model_routing_policy_immutable_update
BEFORE UPDATE ON model_routing_policies
BEGIN
    SELECT RAISE(ABORT, 'model routing policy is immutable');
END;

CREATE TRIGGER model_routing_policy_immutable_delete
BEFORE DELETE ON model_routing_policies
BEGIN
    SELECT RAISE(ABORT, 'model routing policy is immutable');
END;

CREATE TRIGGER model_route_request_guard
BEFORE INSERT ON model_route_decisions
WHEN NOT (
    json_extract(NEW.request_json,'$.runtime_request_id')=NEW.runtime_request_id
    AND json_extract(NEW.request_json,'$.role_id')=NEW.role_id
    AND json_extract(NEW.request_json,'$.runtime_role')=NEW.runtime_role
    AND json_extract(NEW.request_json,'$.runtime_kind')=NEW.runtime_kind
    AND json_extract(NEW.request_json,'$.configured_model_id')=NEW.configured_model_id
    AND json_extract(NEW.request_json,'$.task_kind') IS NEW.task_kind
)
BEGIN
    SELECT RAISE(ABORT, 'model route request provenance mismatch');
END;

CREATE TRIGGER model_route_policy_guard
BEFORE INSERT ON model_route_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM model_routing_policies policy
    WHERE policy.policy_sha256=NEW.policy_sha256
      AND policy.policy_version=NEW.policy_version
)
BEGIN
    SELECT RAISE(ABORT, 'model route policy provenance mismatch');
END;

CREATE TRIGGER model_route_role_guard
BEFORE INSERT ON model_route_decisions
WHEN NOT EXISTS (
    SELECT 1 FROM roles role
    WHERE role.role_id=NEW.role_id
      AND role.enabled=1
      AND role.runtime_kind=NEW.runtime_kind
      AND role.model_id=NEW.configured_model_id
      AND (
          (NEW.runtime_role='planner' AND role.role_kind='orchestrator')
          OR (NEW.runtime_role='worker' AND role.role_kind='worker')
          OR (NEW.runtime_role='reviewer' AND role.role_kind='reviewer')
      )
)
BEGIN
    SELECT RAISE(ABORT, 'model route role provenance mismatch');
END;

CREATE TRIGGER model_route_task_guard
BEFORE INSERT ON model_route_decisions
WHEN NEW.orchestration_task_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM orchestration_tasks task
    WHERE task.orchestration_task_id=NEW.orchestration_task_id
      AND task.kind IS NEW.task_kind
)
BEGIN
    SELECT RAISE(ABORT, 'model route task provenance mismatch');
END;

CREATE TRIGGER model_route_semantics_guard
BEFORE INSERT ON model_route_decisions
WHEN NOT (
    (
        NEW.reason='runtime_managed'
        AND NEW.runtime_kind='hermes'
        AND NEW.selected_model_id=NEW.configured_model_id
        AND NEW.rule_id='runtime-managed-model'
    )
    OR (
        NEW.reason='configured_default'
        AND NEW.runtime_kind='native'
        AND NEW.selected_model_id=NEW.configured_model_id
        AND NEW.rule_id='configured-role-model'
        AND NOT EXISTS (
            SELECT 1
            FROM model_routing_policies policy,
                 json_each(policy.canonical_json, '$.rules') rule
            WHERE policy.policy_sha256=NEW.policy_sha256
              AND (json_extract(rule.value,'$.role_id') IS NULL
                   OR json_extract(rule.value,'$.role_id')=NEW.role_id)
              AND (json_extract(rule.value,'$.runtime_role') IS NULL
                   OR json_extract(rule.value,'$.runtime_role')=NEW.runtime_role)
              AND (json_extract(rule.value,'$.runtime_kind') IS NULL
                   OR json_extract(rule.value,'$.runtime_kind')=NEW.runtime_kind)
              AND (json_extract(rule.value,'$.task_kind') IS NULL
                   OR json_extract(rule.value,'$.task_kind') IS NEW.task_kind)
        )
    )
    OR (
        NEW.reason='rule_match'
        AND NEW.runtime_kind='native'
        AND EXISTS (
            SELECT 1
            FROM model_routing_policies policy,
                 json_each(policy.canonical_json, '$.rules') rule
            WHERE policy.policy_sha256=NEW.policy_sha256
              AND json_extract(rule.value,'$.rule_id')=NEW.rule_id
              AND json_extract(rule.value,'$.model_id')=NEW.selected_model_id
              AND (json_extract(rule.value,'$.role_id') IS NULL
                   OR json_extract(rule.value,'$.role_id')=NEW.role_id)
              AND (json_extract(rule.value,'$.runtime_role') IS NULL
                   OR json_extract(rule.value,'$.runtime_role')=NEW.runtime_role)
              AND (json_extract(rule.value,'$.runtime_kind') IS NULL
                   OR json_extract(rule.value,'$.runtime_kind')=NEW.runtime_kind)
              AND (json_extract(rule.value,'$.task_kind') IS NULL
                   OR json_extract(rule.value,'$.task_kind') IS NEW.task_kind)
              AND NOT EXISTS (
                  SELECT 1
                  FROM json_each(policy.canonical_json, '$.rules') earlier
                  WHERE CAST(earlier.key AS INTEGER) < CAST(rule.key AS INTEGER)
                    AND (json_extract(earlier.value,'$.role_id') IS NULL
                         OR json_extract(earlier.value,'$.role_id')=NEW.role_id)
                    AND (json_extract(earlier.value,'$.runtime_role') IS NULL
                         OR json_extract(earlier.value,'$.runtime_role')=NEW.runtime_role)
                    AND (json_extract(earlier.value,'$.runtime_kind') IS NULL
                         OR json_extract(earlier.value,'$.runtime_kind')=NEW.runtime_kind)
                    AND (json_extract(earlier.value,'$.task_kind') IS NULL
                         OR json_extract(earlier.value,'$.task_kind') IS NEW.task_kind)
              )
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'model route decision does not match policy semantics');
END;

CREATE TRIGGER model_route_decision_immutable_update
BEFORE UPDATE ON model_route_decisions
BEGIN
    SELECT RAISE(ABORT, 'model route decision is immutable');
END;

CREATE TRIGGER model_route_decision_immutable_delete
BEFORE DELETE ON model_route_decisions
BEGIN
    SELECT RAISE(ABORT, 'model route decision is immutable');
END;

ALTER TABLE orchestrator_executions
ADD COLUMN model_route_decision_id TEXT
    REFERENCES model_route_decisions(decision_id) ON DELETE RESTRICT;

ALTER TABLE worker_executions
ADD COLUMN model_route_decision_id TEXT
    REFERENCES model_route_decisions(decision_id) ON DELETE RESTRICT;

ALTER TABLE reviewer_executions
ADD COLUMN model_route_decision_id TEXT
    REFERENCES model_route_decisions(decision_id) ON DELETE RESTRICT;

CREATE TRIGGER orchestrator_model_route_insert_guard
BEFORE INSERT ON orchestrator_executions
WHEN NEW.model_route_decision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_route_decisions route
    WHERE route.decision_id=NEW.model_route_decision_id
      AND route.execution_kind='PLANNER'
      AND route.execution_id=NEW.execution_id
      AND route.role_id=NEW.role_id
      AND route.runtime_kind=NEW.runtime_kind
)
BEGIN
    SELECT RAISE(ABORT, 'invalid planner model route linkage');
END;

CREATE TRIGGER orchestrator_model_route_update_guard
BEFORE UPDATE OF model_route_decision_id ON orchestrator_executions
WHEN NEW.model_route_decision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_route_decisions route
    WHERE route.decision_id=NEW.model_route_decision_id
      AND route.execution_kind='PLANNER'
      AND route.execution_id=NEW.execution_id
      AND route.role_id=NEW.role_id
      AND route.runtime_kind=NEW.runtime_kind
)
BEGIN
    SELECT RAISE(ABORT, 'invalid planner model route linkage');
END;

CREATE TRIGGER orchestrator_model_route_immutable
BEFORE UPDATE OF model_route_decision_id ON orchestrator_executions
WHEN OLD.model_route_decision_id IS NOT NULL
  AND NEW.model_route_decision_id IS NOT OLD.model_route_decision_id
BEGIN
    SELECT RAISE(ABORT, 'planner model route linkage is immutable');
END;

CREATE TRIGGER worker_model_route_insert_guard
BEFORE INSERT ON worker_executions
WHEN NEW.model_route_decision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_route_decisions route
    WHERE route.decision_id=NEW.model_route_decision_id
      AND route.execution_kind='WORKER'
      AND route.execution_id=NEW.execution_id
      AND route.role_id=NEW.role_id
      AND route.runtime_kind=NEW.runtime_kind
)
BEGIN
    SELECT RAISE(ABORT, 'invalid worker model route linkage');
END;

CREATE TRIGGER worker_model_route_update_guard
BEFORE UPDATE OF model_route_decision_id ON worker_executions
WHEN NEW.model_route_decision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_route_decisions route
    WHERE route.decision_id=NEW.model_route_decision_id
      AND route.execution_kind='WORKER'
      AND route.execution_id=NEW.execution_id
      AND route.role_id=NEW.role_id
      AND route.runtime_kind=NEW.runtime_kind
)
BEGIN
    SELECT RAISE(ABORT, 'invalid worker model route linkage');
END;

CREATE TRIGGER worker_model_route_immutable
BEFORE UPDATE OF model_route_decision_id ON worker_executions
WHEN OLD.model_route_decision_id IS NOT NULL
  AND NEW.model_route_decision_id IS NOT OLD.model_route_decision_id
BEGIN
    SELECT RAISE(ABORT, 'worker model route linkage is immutable');
END;

CREATE TRIGGER reviewer_model_route_insert_guard
BEFORE INSERT ON reviewer_executions
WHEN NEW.model_route_decision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_route_decisions route
    WHERE route.decision_id=NEW.model_route_decision_id
      AND route.execution_kind='REVIEWER'
      AND route.execution_id=NEW.execution_id
      AND route.role_id=NEW.role_id
      AND route.runtime_kind=NEW.runtime_kind
)
BEGIN
    SELECT RAISE(ABORT, 'invalid reviewer model route linkage');
END;

CREATE TRIGGER reviewer_model_route_update_guard
BEFORE UPDATE OF model_route_decision_id ON reviewer_executions
WHEN NEW.model_route_decision_id IS NOT NULL AND NOT EXISTS (
    SELECT 1 FROM model_route_decisions route
    WHERE route.decision_id=NEW.model_route_decision_id
      AND route.execution_kind='REVIEWER'
      AND route.execution_id=NEW.execution_id
      AND route.role_id=NEW.role_id
      AND route.runtime_kind=NEW.runtime_kind
)
BEGIN
    SELECT RAISE(ABORT, 'invalid reviewer model route linkage');
END;

CREATE TRIGGER reviewer_model_route_immutable
BEFORE UPDATE OF model_route_decision_id ON reviewer_executions
WHEN OLD.model_route_decision_id IS NOT NULL
  AND NEW.model_route_decision_id IS NOT OLD.model_route_decision_id
BEGIN
    SELECT RAISE(ABORT, 'reviewer model route linkage is immutable');
END;

-- Once a route is linked, the execution-side identity used by that immutable
-- decision must not drift. Otherwise a later UPDATE could leave the FK link
-- intact while making the execution row contradict the route provenance.
CREATE TRIGGER orchestrator_model_route_identity_immutable
BEFORE UPDATE OF execution_id, role_id, runtime_kind ON orchestrator_executions
WHEN OLD.model_route_decision_id IS NOT NULL
  AND (
      NEW.execution_id IS NOT OLD.execution_id
      OR NEW.role_id IS NOT OLD.role_id
      OR NEW.runtime_kind IS NOT OLD.runtime_kind
  )
BEGIN
    SELECT RAISE(ABORT, 'planner model route execution identity is immutable');
END;

CREATE TRIGGER worker_model_route_identity_immutable
BEFORE UPDATE OF execution_id, role_id, runtime_kind ON worker_executions
WHEN OLD.model_route_decision_id IS NOT NULL
  AND (
      NEW.execution_id IS NOT OLD.execution_id
      OR NEW.role_id IS NOT OLD.role_id
      OR NEW.runtime_kind IS NOT OLD.runtime_kind
  )
BEGIN
    SELECT RAISE(ABORT, 'worker model route execution identity is immutable');
END;

CREATE TRIGGER reviewer_model_route_identity_immutable
BEFORE UPDATE OF execution_id, role_id, runtime_kind ON reviewer_executions
WHEN OLD.model_route_decision_id IS NOT NULL
  AND (
      NEW.execution_id IS NOT OLD.execution_id
      OR NEW.role_id IS NOT OLD.role_id
      OR NEW.runtime_kind IS NOT OLD.runtime_kind
  )
BEGIN
    SELECT RAISE(ABORT, 'reviewer model route execution identity is immutable');
END;

INSERT INTO schema_migrations(version, applied_at)
VALUES (31, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version=31;
