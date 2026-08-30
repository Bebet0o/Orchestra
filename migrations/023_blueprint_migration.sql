-- Orchestra milestone 3B-S1: Blueprint becomes the backend authority.
-- Historical request routes and integrity hashes are deliberately preserved.

PRAGMA defer_foreign_keys = ON;

DROP TRIGGER sandbox_profile_revision_update_guard;
DROP TRIGGER sandbox_profile_revision_delete_guard;
DROP TRIGGER sandbox_profile_identity_guard;
DROP TRIGGER sandbox_profile_resource_revision_guard;
DROP TRIGGER sandbox_profile_source_revision_guard;
DROP INDEX idx_sandbox_profiles_state_name;
DROP INDEX idx_sandbox_profile_revisions_profile;

DROP TRIGGER controller_hermesfile_audit_update_guard;
DROP TRIGGER controller_hermesfile_audit_delete_guard;
DROP TRIGGER controller_hermesfile_idempotency_delete_guard;
DROP INDEX idx_controller_hermesfile_operations_target;
DROP INDEX idx_controller_hermesfile_audit_resource;

ALTER TABLE sandbox_profile_revisions
    RENAME TO sandbox_profile_revisions_v22;
ALTER TABLE sandbox_profiles
    RENAME TO sandbox_profiles_v22;
ALTER TABLE controller_hermesfile_operations
    RENAME TO controller_hermesfile_operations_v22;
ALTER TABLE controller_hermesfile_idempotency
    RENAME TO controller_hermesfile_idempotency_v22;
ALTER TABLE controller_hermesfile_command_audit
    RENAME TO controller_hermesfile_command_audit_v22;

CREATE TABLE sandbox_profile_revisions (
    revision_id TEXT PRIMARY KEY CHECK (
        revision_id GLOB 'sandbox-revision-[0-9a-f]*'
        AND length(revision_id) = 49
        AND substr(revision_id, 18) NOT GLOB '*[^0-9a-f]*'
    ),
    sandbox_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    source_format TEXT NOT NULL CHECK (source_format = 'blueprint-v1'),
    api_version TEXT NOT NULL CHECK (api_version = 'hermesops.dev/v1'),
    source_text TEXT NOT NULL CHECK (length(source_text) BETWEEN 1 AND 262144),
    source_sha256 TEXT NOT NULL CHECK (
        length(source_sha256) = 64
        AND source_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_json TEXT NOT NULL CHECK (
        json_valid(canonical_json)
        AND json_type(canonical_json) = 'object'
        AND length(canonical_json) BETWEEN 2 AND 524288
    ),
    canonical_sha256 TEXT NOT NULL CHECK (
        length(canonical_sha256) = 64
        AND canonical_sha256 NOT GLOB '*[^0-9a-f]*'
    ),
    canonical_size INTEGER NOT NULL CHECK (canonical_size BETWEEN 2 AND 524288),
    diagnostics_json TEXT NOT NULL CHECK (
        json_valid(diagnostics_json)
        AND json_type(diagnostics_json) = 'array'
        AND length(diagnostics_json) <= 131072
    ),
    created_at TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    UNIQUE (sandbox_id, source_revision),
    UNIQUE (sandbox_id, revision_id, source_revision),
    FOREIGN KEY (sandbox_id)
        REFERENCES sandbox_profiles(sandbox_id)
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE sandbox_profiles (
    sandbox_id TEXT PRIMARY KEY CHECK (
        sandbox_id GLOB 'sandbox-[0-9a-f]*'
        AND length(sandbox_id) = 40
        AND substr(sandbox_id, 9) NOT GLOB '*[^0-9a-f]*'
    ),
    profile_name TEXT NOT NULL UNIQUE CHECK (
        length(profile_name) BETWEEN 1 AND 63
        AND profile_name = lower(profile_name)
        AND profile_name NOT GLOB '*[^a-z0-9-]*'
        AND substr(profile_name, 1, 1) GLOB '[a-z0-9]'
        AND substr(profile_name, -1, 1) GLOB '[a-z0-9]'
    ),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
    description TEXT NOT NULL DEFAULT '' CHECK (length(description) <= 1000),
    labels_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(labels_json)
        AND json_type(labels_json) = 'object'
        AND length(labels_json) <= 32768
    ),
    source_format TEXT NOT NULL CHECK (source_format = 'blueprint-v1'),
    state TEXT NOT NULL DEFAULT 'draft' CHECK (
        state IN ('draft', 'ready', 'active', 'inactive', 'archived')
    ),
    current_revision_id TEXT NOT NULL,
    current_source_revision INTEGER NOT NULL CHECK (current_source_revision >= 1),
    active_image_digest TEXT CHECK (
        active_image_digest IS NULL
        OR (
            active_image_digest GLOB 'sha256:[0-9a-f]*'
            AND length(active_image_digest) = 71
            AND substr(active_image_digest, 8) NOT GLOB '*[^0-9a-f]*'
        )
    ),
    resource_revision INTEGER NOT NULL DEFAULT 1 CHECK (resource_revision >= 1),
    created_at TEXT NOT NULL CHECK (created_at GLOB '????-??-??T??:??:??.???Z'),
    updated_at TEXT NOT NULL CHECK (
        updated_at GLOB '????-??-??T??:??:??.???Z'
        AND updated_at >= created_at
    ),
    FOREIGN KEY (sandbox_id, current_revision_id, current_source_revision)
        REFERENCES sandbox_profile_revisions(
            sandbox_id, revision_id, source_revision
        )
        ON UPDATE RESTRICT
        ON DELETE RESTRICT
        DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE controller_blueprint_operations (
    operation_id TEXT PRIMARY KEY CHECK (
        length(operation_id) = 42
        AND substr(operation_id, 1, 10) = 'operation-'
        AND substr(operation_id, 11) NOT GLOB '*[^0-9a-f]*'
    ),
    command_kind TEXT NOT NULL CHECK (
        command_kind IN ('blueprint.create', 'blueprint.update')
    ),
    state TEXT NOT NULL CHECK (state IN ('RUNNING', 'SUCCEEDED', 'FAILED')),
    target_id TEXT NOT NULL CHECK (
        length(target_id) = 40
        AND substr(target_id, 1, 8) = 'sandbox-'
        AND substr(target_id, 9) NOT GLOB '*[^0-9a-f]*'
    ),
    result_json TEXT NOT NULL DEFAULT '{}' CHECK (
        json_valid(result_json) AND json_type(result_json) = 'object'
    ),
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE controller_blueprint_idempotency (
    session_fingerprint TEXT NOT NULL CHECK (length(session_fingerprint) = 32),
    key_hash TEXT NOT NULL CHECK (length(key_hash) = 64),
    method TEXT NOT NULL CHECK (method IN ('POST', 'PATCH')),
    route TEXT NOT NULL CHECK (length(route) BETWEEN 1 AND 512),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    response_status INTEGER,
    response_json TEXT CHECK (
        response_json IS NULL OR (
            json_valid(response_json) AND json_type(response_json) = 'object'
        )
    ),
    operation_id TEXT,
    created_at TEXT NOT NULL,
    completed_at TEXT,
    PRIMARY KEY (session_fingerprint, key_hash),
    FOREIGN KEY (operation_id)
        REFERENCES controller_blueprint_operations(operation_id)
        ON DELETE RESTRICT
);

CREATE TABLE controller_blueprint_command_audit (
    audit_id TEXT PRIMARY KEY CHECK (
        length(audit_id) = 38
        AND substr(audit_id, 1, 6) = 'audit-'
        AND substr(audit_id, 7) NOT GLOB '*[^0-9a-f]*'
    ),
    operation_id TEXT NOT NULL UNIQUE,
    actor_type TEXT NOT NULL CHECK (actor_type = 'session'),
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (
        action IN ('blueprint.create', 'blueprint.update')
    ),
    resource_type TEXT NOT NULL CHECK (resource_type = 'sandbox_profile'),
    resource_id TEXT NOT NULL,
    session_fingerprint TEXT NOT NULL CHECK (length(session_fingerprint) = 32),
    idempotency_key_hash TEXT NOT NULL CHECK (length(idempotency_key_hash) = 64),
    request_hash TEXT NOT NULL CHECK (length(request_hash) = 64),
    outcome TEXT NOT NULL CHECK (outcome IN ('SUCCEEDED', 'FAILED')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (operation_id)
        REFERENCES controller_blueprint_operations(operation_id)
        ON DELETE RESTRICT
);

INSERT INTO sandbox_profile_revisions (
    revision_id, sandbox_id, source_revision, source_format, api_version,
    source_text, source_sha256, canonical_json, canonical_sha256,
    canonical_size, diagnostics_json, created_at
)
SELECT
    revision_id, sandbox_id, source_revision, 'blueprint-v1', api_version,
    source_text, source_sha256, canonical_json, canonical_sha256,
    canonical_size, diagnostics_json, created_at
FROM sandbox_profile_revisions_v22;

INSERT INTO sandbox_profiles (
    sandbox_id, profile_name, display_name, description, labels_json,
    source_format, state, current_revision_id, current_source_revision,
    active_image_digest, resource_revision, created_at, updated_at
)
SELECT
    sandbox_id, profile_name, display_name, description, labels_json,
    'blueprint-v1', state, current_revision_id, current_source_revision,
    active_image_digest, resource_revision, created_at, updated_at
FROM sandbox_profiles_v22;

INSERT INTO controller_blueprint_operations (
    operation_id, command_kind, state, target_id, result_json, error_code,
    created_at, updated_at, finished_at
)
SELECT
    operation_id,
    CASE command_kind
        WHEN 'hermesfile.create' THEN 'blueprint.create'
        WHEN 'hermesfile.update' THEN 'blueprint.update'
    END,
    state, target_id, result_json,
    CASE
        WHEN error_code LIKE '%hermesfile%'
            THEN replace(error_code, 'hermesfile', 'blueprint')
        ELSE error_code
    END,
    created_at, updated_at, finished_at
FROM controller_hermesfile_operations_v22;

INSERT INTO controller_blueprint_idempotency (
    session_fingerprint, key_hash, method, route, request_hash,
    response_status, response_json, operation_id, created_at, completed_at
)
SELECT
    session_fingerprint, key_hash, method,
    route,              -- historical request authority: preserve exactly
    request_hash,       -- historical integrity authority: preserve exactly
    response_status,
    CASE
        WHEN json_extract(response_json, '$.data.kind') = 'hermesfile.create'
            THEN json_set(response_json, '$.data.kind', 'blueprint.create')
        WHEN json_extract(response_json, '$.data.kind') = 'hermesfile.update'
            THEN json_set(response_json, '$.data.kind', 'blueprint.update')
        ELSE response_json
    END,
    operation_id, created_at, completed_at
FROM controller_hermesfile_idempotency_v22;

INSERT INTO controller_blueprint_command_audit (
    audit_id, operation_id, actor_type, actor_id, action, resource_type,
    resource_id, session_fingerprint, idempotency_key_hash, request_hash,
    outcome, created_at
)
SELECT
    audit_id, operation_id, actor_type, actor_id,
    CASE action
        WHEN 'hermesfile.create' THEN 'blueprint.create'
        WHEN 'hermesfile.update' THEN 'blueprint.update'
    END,
    resource_type, resource_id, session_fingerprint,
    idempotency_key_hash, request_hash, outcome, created_at
FROM controller_hermesfile_command_audit_v22;

CREATE INDEX idx_sandbox_profiles_state_name
    ON sandbox_profiles(state, profile_name, sandbox_id);
CREATE INDEX idx_sandbox_profile_revisions_profile
    ON sandbox_profile_revisions(sandbox_id, source_revision DESC);
CREATE INDEX idx_controller_blueprint_operations_target
    ON controller_blueprint_operations(target_id, created_at);
CREATE INDEX idx_controller_blueprint_audit_resource
    ON controller_blueprint_command_audit(resource_id, created_at);

CREATE TRIGGER sandbox_profile_revision_update_guard
BEFORE UPDATE ON sandbox_profile_revisions
BEGIN
    SELECT RAISE(ABORT, 'sandbox profile revisions are immutable');
END;

CREATE TRIGGER sandbox_profile_revision_delete_guard
BEFORE DELETE ON sandbox_profile_revisions
BEGIN
    SELECT RAISE(ABORT, 'sandbox profile revisions are immutable');
END;

CREATE TRIGGER sandbox_profile_identity_guard
BEFORE UPDATE OF sandbox_id, profile_name, source_format, created_at
ON sandbox_profiles
BEGIN
    SELECT RAISE(ABORT, 'sandbox profile identity is immutable');
END;

CREATE TRIGGER sandbox_profile_resource_revision_guard
BEFORE UPDATE ON sandbox_profiles
WHEN NEW.resource_revision != OLD.resource_revision + 1
BEGIN
    SELECT RAISE(ABORT, 'sandbox profile resource revision must advance by one');
END;

CREATE TRIGGER sandbox_profile_source_revision_guard
BEFORE UPDATE OF current_revision_id, current_source_revision
ON sandbox_profiles
WHEN
    NEW.current_source_revision != OLD.current_source_revision + 1
    OR NEW.current_revision_id = OLD.current_revision_id
BEGIN
    SELECT RAISE(ABORT, 'sandbox profile source revision must advance by one');
END;

CREATE TRIGGER controller_blueprint_audit_update_guard
BEFORE UPDATE ON controller_blueprint_command_audit
BEGIN
    SELECT RAISE(ABORT, 'controller Blueprint audit is immutable');
END;

CREATE TRIGGER controller_blueprint_audit_delete_guard
BEFORE DELETE ON controller_blueprint_command_audit
BEGIN
    SELECT RAISE(ABORT, 'controller Blueprint audit is immutable');
END;

CREATE TRIGGER controller_blueprint_idempotency_delete_guard
BEFORE DELETE ON controller_blueprint_idempotency
BEGIN
    SELECT RAISE(ABORT, 'controller Blueprint idempotency is immutable');
END;

CREATE TEMP TABLE blueprint_migration_guard (
    violations INTEGER NOT NULL CHECK (violations = 0)
);

INSERT INTO blueprint_migration_guard
SELECT abs(
    (SELECT count(*) FROM sandbox_profile_revisions_v22)
    - (SELECT count(*) FROM sandbox_profile_revisions)
);
INSERT INTO blueprint_migration_guard
SELECT abs(
    (SELECT count(*) FROM sandbox_profiles_v22)
    - (SELECT count(*) FROM sandbox_profiles)
);
INSERT INTO blueprint_migration_guard
SELECT abs(
    (SELECT count(*) FROM controller_hermesfile_operations_v22)
    - (SELECT count(*) FROM controller_blueprint_operations)
);
INSERT INTO blueprint_migration_guard
SELECT abs(
    (SELECT count(*) FROM controller_hermesfile_idempotency_v22)
    - (SELECT count(*) FROM controller_blueprint_idempotency)
);
INSERT INTO blueprint_migration_guard
SELECT abs(
    (SELECT count(*) FROM controller_hermesfile_command_audit_v22)
    - (SELECT count(*) FROM controller_blueprint_command_audit)
);

DROP TABLE controller_hermesfile_command_audit_v22;
DROP TABLE controller_hermesfile_idempotency_v22;
DROP TABLE controller_hermesfile_operations_v22;
DROP TABLE sandbox_profiles_v22;
DROP TABLE sandbox_profile_revisions_v22;

INSERT INTO blueprint_migration_guard
SELECT count(*) FROM pragma_foreign_key_check;

DROP TABLE blueprint_migration_guard;

INSERT INTO schema_migrations(version, applied_at)
VALUES (23, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 23;
