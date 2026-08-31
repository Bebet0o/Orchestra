# Changelog

Notable project changes are recorded here. The current project is Orchestra;
older entries retain HermesOps and Hermesfile names where those were the names
of the released or implemented historical interfaces.

Post-transition Orchestra changes remain under [Unreleased] until an Orchestra
release is published.

## [Unreleased]

### Added

- Orchestra project identity and public repository bootstrap on the preserved
  HermesOps foundation.
- Immutable OCI worker distribution data, environment resolution, exact
  sandbox materialization, and shared worker/reviewer preparation.
- A trusted two-phase worker publication and acceptance process with exact
  candidate binding and fresh anonymous digest-pull verification.
- Orchestra Blueprint v1 parsing, validation, canonicalization, fingerprinting,
  persistence, immutable revisions, lifecycle operations, and current
  API/CLI/Console/documentation surfaces.
- SQLite schema migration 22 to 23 for the Blueprint authority cutover.
- Refreshed public project, contribution, security, and changelog documentation
  for the current Orchestra implementation.

### Changed

- The current project-definition authority is Orchestra Blueprint with source
  format `blueprint-v1`; Hermesfile remains only historical or migration input.
- The default production worker environment resolves to the accepted immutable
  `ghcr.io/bebet0o/orchestra-worker@sha256:3d23329275ebe922b88a180aaf4ceeb48e2007ad591232179e30736083669f49`
  artifact.
- Production worker and reviewer paths materialize the same exact OCI authority
  instead of silently selecting the legacy local-image environment.
- Current Controller, OpenAPI, CLI, Console, schema, example, and specification
  terminology and routes use Blueprint and `/blueprints`.

### Fixed

- Docker push digest parsing accepts the bounded tag-prefixed format emitted by
  Docker while retaining strict tag, algorithm, digest, size, and ambiguity
  checks.
- The schema 22 to 23 migration preserves source and canonical bytes and hashes,
  sandbox/profile/revision identity, project and objective linkage, historical
  request routes, and historical integrity domains; malformed semantic input
  fails atomically.

### Security

- Published worker identity is bound to an immutable OCI digest rather than a
  mutable tag or daemon-local image ID.
- Final worker acceptance requires candidate reauthorization, detached exact
  source checkout, a fresh anonymous exact-digest pull, RepoDigest validation,
  and artifact metadata binding.
- Worker and reviewer sandbox preparation rejects mismatched RepoDigests and
  does not expose the host Docker socket.

## Historical HermesOps development

The entries below preserve the names and capabilities of the completed
HermesOps foundation from which Orchestra was created.

### HermesOps 0.2.0 (historical release)

#### Added

- Durable, immutable Hermesfile v1 sandbox-profile source revisions and
  authenticated profile reads.
- `AgentRuntime`, `HermesRuntime`, runtime-neutral execution events, and the
  first bounded `NativeRuntime` primitive.
- `ModelProvider`, deterministic fake implementations, and the minimal
  OpenAI-compatible provider adapter.
- Bounded Objective Lifecycle Console integration using secure objective
  creation, pause, resume, cancel, and operation-read contracts.
- Hermesfile lifecycle persistence and Controller routes, including strict
  non-persisting validation, guided template creation, immutable revisions,
  `If-Match` concurrency, runtime projection, and redacted audit/events.
- Authenticated, redacted reads for plans, DAG edges, attempts, and reviewer
  assignments.
- Executable Hermesfile v1 semantic validation, canonical JSON, and
  deterministic source/canonical SHA-256 fingerprints.
- Public Debian 12 installer, preflight, validation, conservative uninstaller,
  local and CI secret scanning, examples, reproducible worker-image export,
  Apache-2.0 licensing, and machine-readable API/event/schema contracts.
- The Console web foundation, browser sessions, same-origin Controller client,
  operational dashboard, project lifecycle, and objective lifecycle.

#### Changed

- Public version markers were normalized to `0.1.0-alpha` for the historical
  foundation.
- Fresh installation began with zero registered projects; local project
  configuration stayed untracked and fixtures moved under `tests/fixtures/`.
- The upstream Hermes WebUI was documented as a temporary compatibility
  interface rather than the final control-plane Console.
- Controller APIs gained bounded project, objective, execution, review,
  recovery, event, and command surfaces with explicit contracts.

#### Fixed

- Controller HTTP documentation and OpenAPI were synchronized, multi-project
  objectives and numeric priorities were preserved, and event transport gained
  a machine-readable AsyncAPI contract.
- Unsupported Hermesfile v0 secret eligibility was prohibited.
- Minimal-host preflight, administrative command lookup, dependency handling,
  source-archive validation, deferred authentication, user-service ordering,
  and deterministic service restart behavior were corrected.
- Scanner-safe CSRF and Hermesfile schema identifiers preserved their security
  contracts without resembling tracked secret assignments.

#### Security

- Generated secrets remained outside the repository, `auth.json` and
  `secrets/` received explicit protection, and divergent upgrades created
  backups.
- Workers did not receive the host Docker socket.
- Controller browser sessions, CSRF, idempotency, immutable audit, replayable
  events, read-only review, and deterministic recovery boundaries were added
  incrementally through the historical 0.2.0 work.

Historical architecture and milestone details remain under `docs/milestones/`,
`docs/adr/`, and the v0 Hermesfile specification and schema.
