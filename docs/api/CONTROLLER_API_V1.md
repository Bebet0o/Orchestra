# Orchestra Controller API v1

Status: **Design contract**
Base path: `/api/v1`
Transport: JSON over HTTP, plus WebSocket for events

The machine-readable companion is:

[`../../specs/controller-api-v1.openapi.json`](../../specs/controller-api-v1.openapi.json)

## Design goals

The API must be:

- local-first and secure by default;
- stable enough for a dedicated Console;
- command-oriented for state transitions;
- idempotent for mutations;
- explicit about confirmations and conflicts;
- independent from the SQLite schema;
- independent from Hermes Agent provider details;
- suitable for replayable, auditable long-running operations.

## Media type and encoding

Requests and responses use:

```text
Content-Type: application/json
Accept: application/json
UTF-8
```

Timestamps are UTC RFC 3339 strings.

Identifiers are opaque strings. Clients must not infer type or chronology from
their shape.

## Authentication

Initial beta deployment uses an authenticated server-side session.

Read requests require:

```text
Cookie: orchestra_session=...
```

Mutating requests additionally require:

```text
X-CSRF-Token: ...
Idempotency-Key: <client-generated opaque value>
```

The Console must not place credentials or session values in URLs.

## Standard response metadata

Successful resource responses include:

```json
{
  "data": {},
  "meta": {
    "request_id": "req_...",
    "resource_revision": 12
  }
}
```

Collection responses include:

```json
{
  "data": [],
  "meta": {
    "request_id": "req_...",
    "next_cursor": null
  }
}
```

Accepted asynchronous commands use HTTP `202`:

```json
{
  "data": {
    "operation_id": "op_...",
    "state": "accepted"
  },
  "meta": {
    "request_id": "req_..."
  }
}
```

## Error envelope

Errors use `application/problem+json`.

```json
{
  "type": "https://orchestra.dev/problems/resource-conflict",
  "title": "Resource conflict",
  "status": 409,
  "code": "resource_conflict",
  "detail": "The objective changed after the page was loaded.",
  "request_id": "req_...",
  "resource": {
    "type": "objective",
    "id": "obj_..."
  },
  "current_revision": 14
}
```

Required fields:

- `type`;
- `title`;
- `status`;
- `code`;
- `request_id`.

`detail` must be safe to display and must not contain raw secrets or stack
traces.

## Error codes

Initial stable codes:

```text
authentication_required
authentication_failed
csrf_failed
permission_denied
validation_failed
resource_not_found
resource_conflict
idempotency_conflict
confirmation_required
confirmation_expired
dependency_unavailable
policy_denied
rate_limited
operation_in_progress
unsafe_recovery_state
internal_error
```

## Idempotency

Every non-GET command requires `Idempotency-Key`.

Rules:

1. the key is scoped to actor and operation;
2. the normalized request body is hashed;
3. an identical retry returns the original result;
4. the same key with a different body returns
   `409 idempotency_conflict`;
5. accepted asynchronous operations return the same operation ID on retry;
6. keys have a documented retention period no shorter than the longest normal
   client retry window.

The Console generates keys and persists them only until the authoritative
response is received.

## Optimistic concurrency

Resources that can be edited or transitioned expose an integer
`resource_revision`.

Commands that can overwrite newer operator intent require:

```text
If-Match: "14"
```

A stale revision returns `409 resource_conflict` with the current revision.

Revision checks do not replace idempotency.

## Confirmation flow

A dangerous command may return:

```text
HTTP 409
code: confirmation_required
```

Example:

```json
{
  "type": "https://orchestra.dev/problems/confirmation-required",
  "title": "Confirmation required",
  "status": 409,
  "code": "confirmation_required",
  "detail": "This action will discard an ambiguous worktree.",
  "request_id": "req_...",
  "confirmation": {
    "id": "confirm_...",
    "risk": "destructive",
    "expires_at": "2026-07-18T20:00:00Z",
    "required_phrase": "ROLL BACK PROJECT ALPHA",
    "consequences": [
      "Uncommitted changes owned by no active run will be discarded.",
      "A recovery audit record will be created."
    ]
  }
}
```

The Console submits:

```http
POST /api/v1/confirmations/{confirmation_id}
```

```json
{
  "phrase": "ROLL BACK PROJECT ALPHA"
}
```

The Controller executes the sealed original command. The confirmation body
cannot alter its target or parameters.

## Pagination

Collections use cursor pagination:

```text
?limit=50&cursor=opaque
```

Rules:

- default limit: 50;
- maximum limit: 200;
- stable sort order documented per endpoint;
- cursors are opaque;
- filters are explicit query parameters;
- log endpoints enforce byte and time bounds.

## Core endpoints

### System

```text
GET /system/status
GET /system/health
GET /system/capabilities
```

`/system/status` returns operator-facing component state.

`/system/health` returns liveness/readiness without secrets.

`/system/capabilities` lets the Console feature-detect API and Blueprint
versions.

### Authentication

```text
POST /auth/login
POST /auth/logout
GET  /auth/session
POST /auth/csrf
```

A deployment may use an alternate upstream authentication mechanism, but the
Console-facing contract remains session-based.

### Projects

```text
GET  /projects
POST /projects
GET  /projects/{project_id}
PATCH /projects/{project_id}
POST /projects/{project_id}/commands/{command}
```

Implemented project commands in milestone 2S:

```text
enable
disable
rescan
archive
```

`delete` remains reserved by the design contract and returns
`project_command_unavailable`; it is not exposed by the Console.

Project creation body:

```json
{
  "name": "Example Project",
  "slug": "example-project",
  "repository": {
    "mode": "clone",
    "url": "git@github.com:example/project.git",
    "default_branch": "main"
  },
  "policy_id": "default",
  "sandbox_profile_id": "sandbox_default"
}
```

The API never returns private Git credentials.

### Objectives

```text
GET  /objectives
POST /objectives
GET  /projects/{project_id}/objectives
POST /projects/{project_id}/objectives
GET  /objectives/{objective_id}
POST /objectives/{objective_id}/commands/{command}
```

The top-level collection is canonical and preserves the current Orchestra
ability to submit one objective across multiple projects. The nested project
collection is a convenience view and injects the path project as the only
project when creating an objective.

Initial objective commands:

```text
plan
start
pause
resume
cancel
replan
archive
```

Objective creation body:

```json
{
  "project_ids": ["tradingbot"],
  "title": "Implement persistent market replay",
  "description": "Create the next independently reviewable milestone.",
  "priority": 100,
  "not_before": null,
  "max_parallel_tasks": 1,
  "planning_max_attempts": 3,
  "constraints": [
    "No direct push",
    "Preserve existing replay parity"
  ]
}
```

### Tasks

```text
GET /objectives/{objective_id}/tasks
GET /tasks/{task_id}
POST /tasks/{task_id}/commands/{command}
```

Initial task commands:

```text
retry
skip
block
unblock
cancel
```

The Console cannot mark a task `done` directly. Completion is derived from a
successful run, required review, and integration policy.

### Runs

```text
GET /tasks/{task_id}/runs
GET /runs/{run_id}
GET /runs/{run_id}/logs
GET /runs/{run_id}/artifacts
POST /runs/{run_id}/commands/{command}
```

Initial run commands:

```text
cancel
retry
request-review
open-recovery
```

Log queries support:

```text
?after_sequence=1234&limit=500
```

Log responses are redacted and bounded.

### Reviews

```text
GET /reviews
GET /reviews/{review_id}
GET /reviews/{review_id}/evidence
POST /reviews/{review_id}/commands/{command}
```

Initial commands:

```text
rerun
acknowledge-debt
request-human-review
```

A reviewer verdict is immutable. A rerun creates a new review attempt.

### Recovery

```text
GET /recoveries
GET /recoveries/{recovery_id}
POST /recoveries/{recovery_id}/decisions
```

Decision body:

```json
{
  "decision": "RESUME_SAFE",
  "reason": "The worktree diff and active snapshot belong to the interrupted run."
}
```

Allowed decisions:

```text
RESUME_SAFE
ROLLBACK_SAFE
BLOCK_HUMAN
```

The Controller rejects decisions unsupported by current evidence.

### Orchestration plans and reviewer assignments

```text
GET /plans
GET /plans/{plan_id}
GET /plans/{plan_id}/tasks
GET /plans/{plan_id}/dependencies
GET /plans/{plan_id}/attempts
GET /reviewer-assignments
GET /reviewer-assignments/{assignment_id}
GET /runs/{run_id}/reviewer-assignments
```

Plan and reviewer-assignment collections use the standard bounded cursor
pagination. Plan lists may filter by `project_id` and public `state`.
Reviewer-assignment lists may additionally filter by public `run_id`.

These resources expose identifiers, state, timestamps, bounded counts, role
metadata and links between public resources. The Controller never returns plan
JSON, task instructions, acceptance criteria, completion markers, raw results,
raw failures, host paths, container names, executor identities, assignment
owners, prompts, logs, credentials or provider data through these routes.
Presence of a private failure is represented only by bounded metadata.

### Sandbox profiles

```text
GET  /sandboxes
POST /sandboxes
GET  /sandboxes/{sandbox_id}
PATCH /sandboxes/{sandbox_id}
POST /sandboxes/{sandbox_id}/builds
GET  /sandbox-builds/{build_id}
GET  /sandbox-builds/{build_id}/logs
POST /sandbox-builds/{build_id}/commands/{command}
POST /sandboxes/{sandbox_id}/commands/{command}
```

Build commands:

```text
cancel
retry
```

Profile commands:

```text
activate
rollback
archive
delete
```

Create/update operations accept:

```json
{
  "name": "Default Python Worker",
  "source_format": "blueprint-v1",
  "source": "apiVersion: orchestra.dev/v1\n..."
}
```

The Controller parses and validates the source. It returns canonical metadata,
diagnostics, and references, never an unreviewed shell command.

Milestone 2O implements authenticated `GET /sandboxes` and
`GET /sandboxes/{sandbox_id}` reads over validated durable source revisions.
The public projection excludes raw source and canonical JSON.

### Blueprint lifecycle

The current Controller exposes a dedicated Blueprint source lifecycle rather
than enabling the broader future sandbox build and activation contract:

```text
GET /blueprints
POST /blueprints
GET /blueprints/template
POST /blueprints/validate
GET /blueprints/{sandbox_id}
PATCH /blueprints/{sandbox_id}
GET /blueprints/{sandbox_id}/revisions
GET /blueprints/{sandbox_id}/revisions/{source_revision}
GET /blueprints/{sandbox_id}/diff
```

The validation endpoint is non-persisting. Create and update accept exactly one
bounded `source` string. Update requires the current quoted resource revision in
`If-Match`; each accepted change creates a new immutable source revision. The
current and revision-detail reads return source only to an authenticated
operator so the Console editor can function. Collections remain redacted.
Canonical previews and runtime projections are deterministic derivatives of the
persisted source, never separate writable sources of truth. Revision comparison
returns bounded path/kind records and no value payloads.

Audit, idempotency, operations, and event records contain hashes and bounded
metadata only. Secret-like material is rejected before persistence and is never
echoed in diagnostics. Build, image activation, secret binding, revision
deletion, and generic sandbox commands remain unavailable in milestone 2T.

### Operations

```text
GET /operations/{operation_id}
```

Long-running actions expose:

```text
accepted
running
succeeded
failed
cancelled
waiting_confirmation
```

Operations link to affected resources and emitted events.

### Confirmations

```text
GET  /confirmations/{confirmation_id}
POST /confirmations/{confirmation_id}
```

Confirmation records are actor-bound, single-use, and expire.

### Backups

```text
GET /backups
POST /backups
GET /backups/{backup_id}
POST /backups/{backup_id}/commands/{command}
```

Commands:

```text
verify
restore
delete
```

`restore` and `delete` require confirmation.

### Secrets

```text
GET    /secrets
PUT    /secrets/{secret_id}
DELETE /secrets/{secret_id}
POST   /secrets/{secret_id}/test
```

GET returns metadata only:

```json
{
  "id": "provider-openai-codex",
  "type": "provider-auth",
  "configured": true,
  "updated_at": "2026-07-18T19:00:00Z"
}
```

PUT responses never echo the provided value.

### Audit

```text
GET /audit
GET /audit/{audit_id}
```

Audit data is read-only through the public API.

## Machine-readable surface parity

[`../../specs/controller-api-v1.openapi.json`](../../specs/controller-api-v1.openapi.json)
is normative for HTTP. Every endpoint listed in this document must exist in
that OpenAPI document in the same commit. The contract regression test compares
the two surfaces exactly.

[`../../specs/controller-events-v1.asyncapi.json`](../../specs/controller-events-v1.asyncapi.json)
is normative for the authenticated WebSocket transport.

The API is a domain projection, not a direct table-shaped wrapper. The mapping
from the current SQLite model and the persistence required by later milestones
is defined in
[`../architecture/CONTROLLER_PERSISTENCE_DELTA.md`](../architecture/CONTROLLER_PERSISTENCE_DELTA.md).

### Objective compatibility

The existing runtime stores `project_scope_json`, accepts repeated `--project`
arguments, uses numeric priority `-1000..1000` with lower values first, and
supports `not_before`, task concurrency, and planning-attempt limits. API v1
preserves those semantics. A future Console may display friendly priority
labels, but labels are presentation only and are never persisted as the API
value.

## Resource state models

### Objective

```text
draft
planning
planned
running
paused
blocked
succeeded
failed
cancelled
archived
```

### Task

```text
pending
ready
claimed
running
reviewing
integrating
blocked
succeeded
failed
cancelled
skipped
```

### Run

```text
created
preparing
running
waiting_review
waiting_integration
succeeded
failed
cancelled
interrupted
recovery_required
```

### Sandbox build

```text
draft
validating
queued
building
testing
ready
failed
cancelled
```

### Sandbox profile

```text
draft
ready
active
inactive
archived
```

The Controller is the only authority for transitions.

## API versioning

- Base namespace: `/api/v1`.
- Additive response fields may be introduced without a new namespace.
- Clients must ignore unknown response fields; response schemas therefore remain forward-compatible.
- Clients must not send unknown request fields.
- Removed or behavior-breaking fields require a new API namespace after the
  first beta.
- `/system/capabilities` exposes supported versions:

```json
{
  "api_versions": ["v1"],
  "event_schema_versions": [1],
  "blueprint_versions": ["v1"]
}
```

## Observability headers

Every response includes:

```text
X-Request-ID
```

When applicable:

```text
ETag
Retry-After
Location
```

The Controller accepts an optional valid correlation ID from trusted internal
services but creates its own when absent.

## Security invariants

- no raw SQLite query endpoint;
- no arbitrary shell endpoint;
- no arbitrary host-path mount endpoint;
- no direct Docker API proxy;
- no provider secret returned by GET;
- no command mutation without idempotency;
- no destructive operation without policy evaluation;
- no trusted success result from Hermes Agent without Controller validation;
- no review transport failure mapped to PASS.
