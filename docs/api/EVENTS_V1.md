# Orchestra Events v1

Status: **Design contract**
Transport: authenticated WebSocket
Endpoint: `/api/v1/events`
Schema: [`../../specs/events-v1.schema.json`](../../specs/events-v1.schema.json)
Transport contract: [`../../specs/controller-events-v1.asyncapi.json`](../../specs/controller-events-v1.asyncapi.json)

## Purpose

The event stream keeps Orchestra Console synchronized with durable Controller
state without giving the browser direct access to SQLite or internal service
logs.

Events are immutable facts emitted after durable state changes.

They are not commands and they do not grant permission to perform an action.

## Delivery semantics

The initial contract provides:

- persisted globally increasing sequence numbers;
- stable event IDs;
- at-least-once delivery;
- replay after a sequence number;
- client-side deduplication by event ID;
- ordering by sequence number;
- bounded replay windows with snapshot fallback;
- heartbeat frames;
- explicit schema versioning.

Exactly-once network delivery is not promised.

The authoritative state remains the query API. Events tell clients what changed
and when to refresh or update local read models.

## Connection

The Console opens:

```text
wss://<controller-origin>/api/v1/events
```

For loopback development:

```text
ws://127.0.0.1:<port>/api/v1/events
```

Authentication uses the existing HTTP-only session cookie. Credentials must
not be placed in the URL.

The client may request replay using a header:

```text
Last-Event-Sequence: 1842
```

or an initial protocol message:

```json
{
  "type": "subscribe",
  "after_sequence": 1842,
  "topics": [
    "system",
    "projects",
    "objectives",
    "runs",
    "sandboxes"
  ]
}
```

Server response:

```json
{
  "type": "subscribed",
  "connection_id": "conn_...",
  "replay_from": 1843,
  "latest_sequence": 1902
}
```

## Event envelope

```json
{
  "schema_version": 1,
  "sequence": 1843,
  "event_id": "evt_...",
  "type": "run.state_changed",
  "occurred_at": "2026-07-18T19:14:53Z",
  "actor": {
    "type": "system",
    "id": "system:orchestrator"
  },
  "aggregate": {
    "type": "run",
    "id": "run_...",
    "revision": 7
  },
  "correlation_id": "corr_...",
  "causation_id": "cmd_...",
  "project_id": "project_...",
  "objective_id": "objective_...",
  "data": {
    "previous_state": "running",
    "state": "waiting_review"
  }
}
```

Required envelope fields:

```text
schema_version
sequence
event_id
type
occurred_at
actor
aggregate
correlation_id
data
```

Optional contextual identifiers may be `null`.

## Ordering

`sequence` is a persisted Controller-wide monotonic integer.

Rules:

- events committed in one SQLite transaction receive deterministic sequence
  order;
- the event is visible only after the state transaction commits;
- a client processes increasing sequence numbers;
- a duplicate event ID is ignored;
- a lower sequence received after a higher one is a replay duplicate;
- a sequence gap triggers replay;
- inability to fill a gap triggers a resource snapshot refresh.

Sequence numbers are not wall-clock timestamps.

## Replay

On reconnect, the client requests events after its last fully applied sequence.

Example:

```text
last applied: 1842
request: after_sequence=1842
server replay begins: 1843
```

The Controller may return:

```json
{
  "type": "replay_unavailable",
  "oldest_available_sequence": 5000,
  "latest_sequence": 8120,
  "required_action": "refresh_snapshot"
}
```

The Console then:

1. fetches current resource snapshots through HTTP;
2. records the reported latest sequence;
3. reconnects after that sequence;
4. clearly marks the brief synchronization state.

The Console must never invent missing state from later events.

## Heartbeats

The server sends a protocol heartbeat at least every 30 seconds:

```json
{
  "type": "heartbeat",
  "occurred_at": "2026-07-18T19:15:00Z",
  "latest_sequence": 1902
}
```

The client may answer:

```json
{
  "type": "heartbeat_ack",
  "latest_applied_sequence": 1902
}
```

Heartbeats are protocol messages, not persisted domain events.

## Topic filtering

Topics reduce traffic but do not alter authorization.

Initial topics:

```text
system
projects
objectives
tasks
runs
reviews
recovery
sandboxes
backups
notifications
audit
```

A subscription without topics receives all events the actor is authorized to
observe.

The Controller may require a snapshot refresh when topic filters change.

## Initial event catalog

### System

```text
system.component_state_changed
system.health_changed
system.capacity_changed
system.degraded
system.recovered
```

### Projects

```text
project.created
project.updated
project.enabled
project.disabled
project.rescanned
project.archived
project.deleted
project.health_changed
project.blocker_added
project.blocker_resolved
```

### Objectives

```text
objective.created
objective.updated
objective.state_changed
objective.plan_requested
objective.plan_accepted
objective.plan_rejected
objective.archived
```

### Tasks

```text
task.created
task.dependency_added
task.state_changed
task.claimed
task.blocked
task.unblocked
task.cancelled
```

### Runs

```text
run.created
run.state_changed
run.progress_updated
run.heartbeat_recorded
run.log_available
run.artifact_available
run.interrupted
run.recovery_required
```

`run.progress_updated` is rate-limited and represents durable progress
checkpoints, not every output token.

### Reviews

```text
review.created
review.state_changed
review.verdict_recorded
review.evidence_available
review.debt_acknowledged
review.human_review_requested
```

### Recovery

```text
recovery.opened
recovery.evidence_updated
recovery.decision_proposed
recovery.confirmation_required
recovery.decision_applied
recovery.blocked_human
recovery.closed
```

### Sandboxes

```text
sandbox.created
sandbox.updated
sandbox.validation_failed
sandbox.build_created
sandbox.build_state_changed
sandbox.build_log_available
sandbox.image_verified
sandbox.activated
sandbox.rolled_back
sandbox.archived
sandbox.deleted
```

### Backups

```text
backup.created
backup.verified
backup.restore_started
backup.restore_completed
backup.failed
backup.deleted
```

### Notifications

```text
notification.queued
notification.sent
notification.retry_scheduled
notification.failed
```

### Audit and confirmations

```text
confirmation.created
confirmation.consumed
confirmation.expired
audit.recorded
```

Audit event payloads contain references and classification only, not complete
sensitive audit data.

## State-change payload

State transitions use a common form:

```json
{
  "previous_state": "running",
  "state": "waiting_review",
  "reason_code": "worker_completed",
  "summary": "Worker completed with a clean commit."
}
```

`summary` is safe for operator display and contains no secret values.

## Progress payload

```json
{
  "phase": "tests",
  "completed": 42,
  "total": 100,
  "unit": "tests",
  "percent": 42,
  "message": "Running offline test suite",
  "estimated_completion_at": null,
  "resource_usage": {
    "cpu_percent": 180.2,
    "memory_bytes": 2147483648,
    "disk_write_bytes": 10485760
  }
}
```

Progress fields are optional because not every tool can estimate totals.

The Controller records bounded checkpoints. Raw high-volume output remains
available through paginated log endpoints.

## Redaction

Events must never include:

- provider tokens;
- `auth.json` contents;
- secret values;
- session cookies;
- CSRF tokens;
- private SSH keys;
- arbitrary environment dumps;
- unbounded raw command output;
- full file contents unless an explicit safe artifact endpoint authorizes it.

Paths may be normalized or replaced with project-relative paths.

## Event compatibility

Clients must:

- reject an unsupported `schema_version`;
- ignore unknown event types;
- ignore unknown data fields;
- preserve sequence and event ID even for ignored types;
- refresh authoritative resource state when an event cannot be applied safely.

The Controller must:

- never change the meaning of an existing event field within schema version 1;
- add fields only as optional;
- introduce a new schema version for incompatible envelope changes;
- continue exposing supported schema versions through
  `/api/v1/system/capabilities`.

## Event persistence

The event journal stores:

```text
sequence
event_id
schema_version
event_type
occurred_at
actor_type
actor_id
aggregate_type
aggregate_id
aggregate_revision
project_id
objective_id
correlation_id
causation_id
redacted_data_json
```

Retention must be explicit.

Before pruning events, the Controller must ensure clients can rebuild current
state from query endpoints. Pruning never deletes the authoritative domain
records or required audit history.

## Console reconciliation algorithm

1. load initial HTTP snapshots;
2. record snapshot sequence from response metadata;
3. open event stream after that sequence;
4. apply events in sequence order;
5. deduplicate by event ID;
6. fetch a resource when an event revision skips an expected revision;
7. request replay on a sequence gap;
8. refresh snapshots if replay is unavailable;
9. show degraded synchronization state while reconciling;
10. never treat connection state as domain state.

## Failure cases

### Event stream unavailable

The Console:

- continues read-only polling with bounded intervals;
- displays a degraded real-time status;
- does not disable the HTTP command API solely because WebSocket is down;
- refreshes affected resources after successful commands.

### Duplicate event

Ignore after verifying the same event ID.

### Same sequence with different event ID

Treat as a Controller integrity incident and force full refresh.

### Invalid event schema

Stop applying events, preserve diagnostics, and show an incompatible-controller
error.

### Client falls too far behind

Use snapshot refresh and restart from the new snapshot sequence.

## Security

- event stream requires authentication;
- origin validation is mandatory;
- authorization applies to each event;
- project filtering occurs server-side;
- secret redaction occurs before persistence and fan-out;
- connection limits and replay limits are enforced;
- malformed client protocol messages close the connection with an auditable
  reason;
- the browser cannot request arbitrary database event tables.

## Implemented local transport

Orchestra 2J implements this contract at `ws://127.0.0.1:8765/api/v1/events`
using only the Python standard library. The handshake requires the Controller
session cookie and the exact configured Console origin. Credentials and replay
cursors are forbidden in the URL. `Last-Event-Sequence` may start an immediate
all-topic subscription; otherwise the first client text frame must be the
normative `subscribe` object. Session rotation closes existing streams.

### Reviewer assignment lifecycle

```text
review.assignment_created
review.assignment_claimed
review.assignment_completed
review.assignment_failed
```

These events contain bounded identifiers, status, assignment number, role and
failure code metadata only. They never contain prompts, logs, paths, raw error
messages or credentials.


### Blueprint source lifecycle

The current Blueprint lifecycle reuses the known `sandbox.created` and
`sandbox.updated` events for profile creation and immutable source revision
changes. Their redacted data is limited to profile name, source/resource
revision numbers, and canonical SHA-256. Raw source, canonical JSON,
diagnostics, idempotency keys,
CSRF values, and secret-like rejected input are never stored in the event
journal.
