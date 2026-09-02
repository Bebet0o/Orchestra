-- Orchestra Blueprint current authority moves to orchestra.dev/v1.
-- Existing revision source, canonical bytes, hashes, and metadata remain
-- immutable; the legacy API version is retained only on historical rows.

PRAGMA defer_foreign_keys = ON;

DROP TRIGGER sandbox_profile_revision_update_guard;
DROP TRIGGER sandbox_profile_revision_delete_guard;
DROP TRIGGER sandbox_profile_identity_guard;
DROP TRIGGER sandbox_profile_resource_revision_guard;
DROP TRIGGER sandbox_profile_source_revision_guard;
DROP INDEX idx_sandbox_profiles_state_name;
DROP INDEX idx_sandbox_profile_revisions_profile;

ALTER TABLE sandbox_profile_revisions
    RENAME TO sandbox_profile_revisions_v23;
ALTER TABLE sandbox_profiles
    RENAME TO sandbox_profiles_v23;

CREATE TABLE sandbox_profile_revisions (
    revision_id TEXT PRIMARY KEY CHECK (
        revision_id GLOB 'sandbox-revision-[0-9a-f]*'
        AND length(revision_id) = 49
        AND substr(revision_id, 18) NOT GLOB '*[^0-9a-f]*'
    ),
    sandbox_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL CHECK (source_revision >= 1),
    source_format TEXT NOT NULL CHECK (source_format = 'blueprint-v1'),
    api_version TEXT NOT NULL CHECK (
        api_version IN ('hermesops.dev/v1', 'orchestra.dev/v1')
    ),
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

CREATE TEMP TABLE blueprint_apiversion_migration_guard (
    violations INTEGER NOT NULL CHECK (violations = 0)
);

INSERT INTO blueprint_apiversion_migration_guard
SELECT count(*)
FROM sandbox_profile_revisions_v23
WHERE source_format != 'blueprint-v1'
   OR api_version != 'hermesops.dev/v1';

INSERT INTO blueprint_apiversion_migration_guard
SELECT count(*)
FROM sandbox_profiles_v23
WHERE source_format != 'blueprint-v1';

INSERT INTO sandbox_profile_revisions (
    revision_id, sandbox_id, source_revision, source_format, api_version,
    source_text, source_sha256, canonical_json, canonical_sha256,
    canonical_size, diagnostics_json, created_at
)
SELECT
    revision_id, sandbox_id, source_revision, source_format, api_version,
    source_text, source_sha256, canonical_json, canonical_sha256,
    canonical_size, diagnostics_json, created_at
FROM sandbox_profile_revisions_v23;

INSERT INTO sandbox_profiles (
    sandbox_id, profile_name, display_name, description, labels_json,
    source_format, state, current_revision_id, current_source_revision,
    active_image_digest, resource_revision, created_at, updated_at
)
SELECT
    sandbox_id, profile_name, display_name, description, labels_json,
    source_format, state, current_revision_id, current_source_revision,
    active_image_digest, resource_revision, created_at, updated_at
FROM sandbox_profiles_v23;

CREATE INDEX idx_sandbox_profiles_state_name
    ON sandbox_profiles(state, profile_name, sandbox_id);
CREATE INDEX idx_sandbox_profile_revisions_profile
    ON sandbox_profile_revisions(sandbox_id, source_revision DESC);

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

CREATE TRIGGER sandbox_profile_revision_api_version_insert_guard
BEFORE INSERT ON sandbox_profile_revisions
WHEN NEW.api_version != 'orchestra.dev/v1'
BEGIN
    SELECT RAISE(ABORT, 'new Blueprint revisions require orchestra.dev/v1');
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

INSERT INTO blueprint_apiversion_migration_guard
SELECT abs(
    (SELECT count(*) FROM sandbox_profile_revisions_v23)
    - (SELECT count(*) FROM sandbox_profile_revisions)
);
INSERT INTO blueprint_apiversion_migration_guard
SELECT abs(
    (SELECT count(*) FROM sandbox_profiles_v23)
    - (SELECT count(*) FROM sandbox_profiles)
);

DROP TABLE sandbox_profiles_v23;
DROP TABLE sandbox_profile_revisions_v23;

INSERT INTO blueprint_apiversion_migration_guard
SELECT count(*) FROM pragma_foreign_key_check;

DROP TABLE blueprint_apiversion_migration_guard;

INSERT INTO schema_migrations(version, applied_at)
VALUES (24, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

PRAGMA user_version = 24;
