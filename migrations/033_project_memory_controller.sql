-- v0.3-A2: Controller-owned structured memory commands and event integration.
-- Memory payload bytes remain confined to the canonical project memory tables;
-- operations, idempotency, audit, and EventJournal retain metadata/hashes only.

CREATE TABLE controller_memory_operations (
    operation_id TEXT PRIMARY KEY CHECK (
        length(operation_id)=42
        AND substr(operation_id,1,10)='operation-'
        AND substr(operation_id,11) NOT GLOB '*[^0-9a-f]*'
    ),
    command_kind TEXT NOT NULL CHECK (
        command_kind IN (
            'memory.create', 'memory.revise', 'memory.retract', 'memory.redact'
        )
    ),
    state TEXT NOT NULL CHECK (state IN ('RUNNING','SUCCEEDED','FAILED')),
    target_id TEXT NOT NULL
        REFERENCES project_memories(memory_id) ON DELETE RESTRICT
        CHECK (length(target_id) BETWEEN 1 AND 200),
    result_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(result_json) AND json_type(result_json)='object'
        AND length(result_json)<=16384
    ),
    error_code TEXT CHECK (error_code IS NULL OR length(error_code) BETWEEN 1 AND 128),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX idx_controller_memory_operations_target
    ON controller_memory_operations(target_id, created_at, operation_id);

CREATE TABLE controller_memory_idempotency (
    session_fingerprint TEXT NOT NULL CHECK (length(session_fingerprint)=32),
    key_hash TEXT NOT NULL CHECK (
        length(key_hash)=64 AND key_hash NOT GLOB '*[^0-9a-f]*'
    ),
    method TEXT NOT NULL CHECK (method IN ('POST','PATCH')),
    route TEXT NOT NULL CHECK (length(route) BETWEEN 1 AND 512),
    request_hash TEXT NOT NULL CHECK (
        length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    response_status INTEGER CHECK (response_status IS NULL OR response_status BETWEEN 200 AND 599),
    response_json TEXT CHECK (
        response_json IS NULL OR (
            json_valid(response_json) AND json_type(response_json)='object'
            AND length(response_json)<=32768
        )
    ),
    operation_id TEXT REFERENCES controller_memory_operations(operation_id) ON DELETE RESTRICT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (session_fingerprint, key_hash),
    CHECK (
        (response_status IS NULL AND response_json IS NULL
         AND operation_id IS NULL AND completed_at IS NULL)
        OR (response_status IS NOT NULL AND response_json IS NOT NULL
            AND operation_id IS NOT NULL AND completed_at IS NOT NULL)
    )
);

CREATE TABLE controller_memory_command_audit (
    audit_id TEXT PRIMARY KEY CHECK (
        length(audit_id)=38
        AND substr(audit_id,1,6)='audit-'
        AND substr(audit_id,7) NOT GLOB '*[^0-9a-f]*'
    ),
    operation_id TEXT NOT NULL UNIQUE
        REFERENCES controller_memory_operations(operation_id) ON DELETE RESTRICT,
    actor_type TEXT NOT NULL CHECK (actor_type='session'),
    actor_id TEXT NOT NULL CHECK (actor_id='operator'),
    action TEXT NOT NULL CHECK (
        action IN ('memory.create','memory.revise','memory.retract','memory.redact')
    ),
    resource_type TEXT NOT NULL CHECK (resource_type='memory'),
    resource_id TEXT NOT NULL REFERENCES project_memories(memory_id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES projects(project_id) ON DELETE RESTRICT,
    session_fingerprint TEXT NOT NULL CHECK (length(session_fingerprint)=32),
    idempotency_key_hash TEXT NOT NULL CHECK (
        length(idempotency_key_hash)=64
        AND idempotency_key_hash NOT GLOB '*[^0-9a-f]*'
    ),
    request_hash TEXT NOT NULL CHECK (
        length(request_hash)=64 AND request_hash NOT GLOB '*[^0-9a-f]*'
    ),
    outcome TEXT NOT NULL CHECK (outcome IN ('SUCCEEDED','FAILED')),
    reason_present INTEGER NOT NULL DEFAULT 0 CHECK (reason_present IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_controller_memory_audit_resource
    ON controller_memory_command_audit(project_id, resource_id, created_at, audit_id);

CREATE TRIGGER controller_memory_audit_no_replace
BEFORE INSERT ON controller_memory_command_audit
WHEN EXISTS (
    SELECT 1 FROM controller_memory_command_audit
    WHERE audit_id=NEW.audit_id OR operation_id=NEW.operation_id
)
BEGIN
    SELECT RAISE(ABORT, 'controller memory audit conflicts are immutable');
END;

CREATE TRIGGER controller_memory_audit_update_guard
BEFORE UPDATE ON controller_memory_command_audit
BEGIN
    SELECT RAISE(ABORT, 'controller memory audit is immutable');
END;

CREATE TRIGGER controller_memory_audit_delete_guard
BEFORE DELETE ON controller_memory_command_audit
BEGIN
    SELECT RAISE(ABORT, 'controller memory audit is immutable');
END;

CREATE TRIGGER controller_memory_idempotency_no_replace
BEFORE INSERT ON controller_memory_idempotency
WHEN EXISTS (
    SELECT 1 FROM controller_memory_idempotency
    WHERE session_fingerprint=NEW.session_fingerprint AND key_hash=NEW.key_hash
)
BEGIN
    SELECT RAISE(ABORT, 'controller memory idempotency conflicts are immutable');
END;

CREATE TRIGGER controller_memory_idempotency_update_guard
BEFORE UPDATE ON controller_memory_idempotency
WHEN NOT (
    OLD.response_status IS NULL
    AND OLD.response_json IS NULL
    AND OLD.operation_id IS NULL
    AND OLD.completed_at IS NULL
    AND NEW.session_fingerprint=OLD.session_fingerprint
    AND NEW.key_hash=OLD.key_hash
    AND NEW.method=OLD.method
    AND NEW.route=OLD.route
    AND NEW.request_hash=OLD.request_hash
    AND NEW.created_at=OLD.created_at
    AND NEW.response_status IS NOT NULL
    AND NEW.response_json IS NOT NULL
    AND NEW.operation_id IS NOT NULL
    AND NEW.completed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'controller memory idempotency is immutable after completion');
END;

CREATE TRIGGER controller_memory_idempotency_delete_guard
BEFORE DELETE ON controller_memory_idempotency
BEGIN
    SELECT RAISE(ABORT, 'controller memory idempotency is immutable');
END;

CREATE TRIGGER controller_memory_operations_no_replace
BEFORE INSERT ON controller_memory_operations
WHEN EXISTS (
    SELECT 1 FROM controller_memory_operations WHERE operation_id=NEW.operation_id
)
BEGIN
    SELECT RAISE(ABORT, 'controller memory operation conflicts are immutable');
END;

CREATE TRIGGER controller_memory_operations_delete_guard
BEFORE DELETE ON controller_memory_operations
BEGIN
    SELECT RAISE(ABORT, 'controller memory operations are durable');
END;

CREATE TRIGGER controller_memory_operations_update_guard
BEFORE UPDATE ON controller_memory_operations
WHEN OLD.state IN ('SUCCEEDED','FAILED')
BEGIN
    SELECT RAISE(ABORT, 'controller memory completed operations are immutable');
END;

-- SQLite CHECK constraints cannot be extended in place. Rebuild the journal
-- without changing sequence/event IDs so memory can be a first-class aggregate.
DROP TRIGGER controller_event_journal_immutable_update;
DROP TRIGGER controller_event_journal_immutable_delete;
DROP TRIGGER controller_event_journal_no_replace_insert;
DROP TRIGGER controller_event_journal_timestamp_insert_guard;

ALTER TABLE controller_event_journal RENAME TO controller_event_journal_v32;

CREATE TABLE controller_event_journal (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE CHECK (
        length(event_id)=36
        AND substr(event_id,1,4)='evt_'
        AND substr(event_id,5) NOT GLOB '*[^0-9a-f]*'
    ),
    schema_version INTEGER NOT NULL CHECK (schema_version=1),
    event_type TEXT NOT NULL CHECK (
        length(event_type) BETWEEN 3 AND 128
        AND event_type=lower(event_type)
        AND substr(event_type,1,1) GLOB '[a-z]'
        AND substr(event_type,-1,1) GLOB '[a-z0-9_]'
        AND instr(event_type,'.')>1
        AND instr(event_type,'..')=0
        AND event_type NOT GLOB '*[^a-z0-9_.]*'
    ),
    occurred_at TEXT NOT NULL CHECK (
        length(occurred_at) BETWEEN 20 AND 40
        AND substr(occurred_at,-1,1)='Z'
    ),
    actor_type TEXT NOT NULL CHECK (
        actor_type IN ('operator','system','agent','worker')
    ),
    actor_id TEXT NOT NULL CHECK (length(actor_id) BETWEEN 1 AND 200),
    aggregate_type TEXT NOT NULL CHECK (
        aggregate_type IN (
            'system','project','objective','task','run','review','recovery',
            'sandbox','sandbox_build','backup','notification','confirmation',
            'audit','memory'
        )
    ),
    aggregate_id TEXT NOT NULL CHECK (length(aggregate_id) BETWEEN 1 AND 200),
    aggregate_revision INTEGER NOT NULL CHECK (aggregate_revision>=1),
    project_id TEXT CHECK (project_id IS NULL OR length(project_id) BETWEEN 1 AND 200),
    objective_id TEXT CHECK (objective_id IS NULL OR length(objective_id) BETWEEN 1 AND 200),
    correlation_id TEXT NOT NULL CHECK (
        length(correlation_id)=37
        AND substr(correlation_id,1,5)='corr_'
        AND substr(correlation_id,6) NOT GLOB '*[^0-9a-f]*'
    ),
    causation_id TEXT CHECK (causation_id IS NULL OR length(causation_id) BETWEEN 1 AND 200),
    redacted_data_json TEXT NOT NULL CHECK (
        length(redacted_data_json)<=16384
        AND json_valid(redacted_data_json)
        AND json_type(redacted_data_json)='object'
    ),
    UNIQUE (aggregate_type, aggregate_id, aggregate_revision)
);

INSERT INTO controller_event_journal (
    sequence, event_id, schema_version, event_type, occurred_at,
    actor_type, actor_id, aggregate_type, aggregate_id, aggregate_revision,
    project_id, objective_id, correlation_id, causation_id, redacted_data_json
)
SELECT
    sequence, event_id, schema_version, event_type, occurred_at,
    actor_type, actor_id, aggregate_type, aggregate_id, aggregate_revision,
    project_id, objective_id, correlation_id, causation_id, redacted_data_json
FROM controller_event_journal_v32
ORDER BY sequence;

DROP TABLE controller_event_journal_v32;

CREATE TRIGGER controller_event_journal_immutable_update
BEFORE UPDATE ON controller_event_journal
BEGIN
    SELECT RAISE(ABORT, 'controller event journal is immutable');
END;

CREATE TRIGGER controller_event_journal_immutable_delete
BEFORE DELETE ON controller_event_journal
BEGIN
    SELECT RAISE(ABORT, 'controller event journal is immutable');
END;

CREATE TRIGGER controller_event_journal_no_replace_insert
BEFORE INSERT ON controller_event_journal
WHEN EXISTS (
    SELECT 1
    FROM controller_event_journal
    WHERE sequence=NEW.sequence
       OR event_id=NEW.event_id
       OR (
            aggregate_type=NEW.aggregate_type
            AND aggregate_id=NEW.aggregate_id
            AND aggregate_revision=NEW.aggregate_revision
       )
)
BEGIN
    SELECT RAISE(ABORT, 'controller event journal conflicts are immutable');
END;

CREATE TRIGGER controller_event_journal_timestamp_insert_guard
BEFORE INSERT ON controller_event_journal
WHEN length(NEW.occurred_at)<20
  OR length(NEW.occurred_at)>27
  OR substr(NEW.occurred_at,5,1)!='-'
  OR substr(NEW.occurred_at,8,1)!='-'
  OR substr(NEW.occurred_at,11,1)!='T'
  OR substr(NEW.occurred_at,14,1)!=':'
  OR substr(NEW.occurred_at,17,1)!=':'
  OR substr(NEW.occurred_at,-1,1)!='Z'
  OR substr(NEW.occurred_at,1,4) GLOB '*[^0-9]*'
  OR substr(NEW.occurred_at,6,2) GLOB '*[^0-9]*'
  OR substr(NEW.occurred_at,9,2) GLOB '*[^0-9]*'
  OR substr(NEW.occurred_at,12,2) GLOB '*[^0-9]*'
  OR substr(NEW.occurred_at,15,2) GLOB '*[^0-9]*'
  OR substr(NEW.occurred_at,18,2) GLOB '*[^0-9]*'
  OR (
        length(NEW.occurred_at)>20
        AND (
            substr(NEW.occurred_at,20,1)!='.'
            OR length(NEW.occurred_at)<22
            OR substr(NEW.occurred_at,21,length(NEW.occurred_at)-21) GLOB '*[^0-9]*'
        )
    )
  OR CAST(substr(NEW.occurred_at,1,4) AS INTEGER)<1
  OR CAST(substr(NEW.occurred_at,6,2) AS INTEGER) NOT BETWEEN 1 AND 12
  OR CAST(substr(NEW.occurred_at,9,2) AS INTEGER)<1
  OR CAST(substr(NEW.occurred_at,12,2) AS INTEGER) NOT BETWEEN 0 AND 23
  OR CAST(substr(NEW.occurred_at,15,2) AS INTEGER) NOT BETWEEN 0 AND 59
  OR CAST(substr(NEW.occurred_at,18,2) AS INTEGER) NOT BETWEEN 0 AND 59
  OR CAST(substr(NEW.occurred_at,9,2) AS INTEGER)>CASE
        CAST(substr(NEW.occurred_at,6,2) AS INTEGER)
        WHEN 1 THEN 31
        WHEN 2 THEN 28+CASE
            WHEN CAST(substr(NEW.occurred_at,1,4) AS INTEGER)%400=0
              OR (
                    CAST(substr(NEW.occurred_at,1,4) AS INTEGER)%4=0
                    AND CAST(substr(NEW.occurred_at,1,4) AS INTEGER)%100!=0
                 )
            THEN 1 ELSE 0 END
        WHEN 3 THEN 31 WHEN 4 THEN 30 WHEN 5 THEN 31 WHEN 6 THEN 30
        WHEN 7 THEN 31 WHEN 8 THEN 31 WHEN 9 THEN 30 WHEN 10 THEN 31
        WHEN 11 THEN 30 WHEN 12 THEN 31 ELSE 0
    END
BEGIN
    SELECT RAISE(ABORT, 'invalid controller event timestamp');
END;

CREATE INDEX idx_controller_event_journal_project_sequence
    ON controller_event_journal(project_id, sequence);
CREATE INDEX idx_controller_event_journal_objective_sequence
    ON controller_event_journal(objective_id, sequence);
CREATE INDEX idx_controller_event_journal_aggregate_revision
    ON controller_event_journal(aggregate_type, aggregate_id, aggregate_revision);
CREATE INDEX idx_controller_event_journal_correlation
    ON controller_event_journal(correlation_id, sequence);

INSERT INTO schema_migrations(version, applied_at)
VALUES (33, strftime('%Y-%m-%dT%H:%M:%fZ','now'));

PRAGMA user_version=33;
