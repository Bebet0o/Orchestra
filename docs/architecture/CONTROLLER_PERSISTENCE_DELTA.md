# Controller Persistence Delta

Status: **Design contract after adversarial review**

## Purpose

API v1 is a stable domain projection over the existing HermesOps runtime. It is
not a direct exposure of SQLite tables. This document records which existing
semantics must be preserved and which durable structures are still required
before write endpoints can be implemented.

## Existing sources of truth

| API concept | Current durable source |
| --- | --- |
| Project | `projects` plus local project TOML |
| Objective | `objective_queue` and `objective_events` |
| Task | `orchestration_tasks`, dependencies, and attempts |
| Run | `runs`, worker/reviewer executions, transactions, attempts |
| Review | `review_results`, `reviewer_executions` |
| Recovery | recovery evaluations/executions and approvals |
| Notification | notification outbox |
| Legacy event | `events` and `objective_events` |

The Controller must adapt these records. It must not silently replace existing
semantics with a cleaner but incompatible model.

## Required compatibility mappings

### Objectives

The current objective queue supports:

- one or more project IDs in `project_scope_json`;
- numeric priority from `-1000` to `1000`, lower values first;
- `not_before` scheduling;
- `max_parallel_tasks` from 1 to 16;
- `planning_max_attempts` from 1 to 5;
- request states such as `PAUSE_REQUESTED` and `CANCEL_REQUESTED`.

API v1 preserves those values. Friendly priority labels are a Console-only
presentation. The Controller may expose normalized lowercase states, but the
mapping must be total, tested, and reversible where a command is accepted.

### Tasks and runs

An API task projects an `orchestration_task`. Its objective is resolved through
`orchestration_tasks.plan_id -> objective_queue.plan_id`.

An API run projects an orchestration attempt plus its linked transaction,
worker, reviewer, and integration executions. A future migration may add a
materialized read model, but it must not create a second independent workflow
state machine.

## Durable structures required before writes

The next persistence migration is expected to add or deliberately replace the
following concepts:

- API idempotency records bound to actor, operation, and normalized body hash;
- durable long-running operations;
- sealed actor-bound confirmation records;
- a Controller-wide append-only event journal with monotonic sequence;
- resource revisions or an equivalent optimistic-concurrency read model;
- sandbox profiles, source revisions, builds, image digests, and activations;
- backup metadata and verification state;
- secret metadata only, never secret values in the control database;
- immutable audit records;
- optional objective title/description projection fields without discarding the
  original objective text.

No public write endpoint is implemented until its idempotency, revision,
audit, event, and failure-recovery persistence has a migration and tests.

## Event migration

The existing `events` and `objective_events` tables do not yet form the v1
global replay stream. The Controller journal must:

1. allocate one globally increasing sequence in the same transaction as state;
2. retain stable event IDs;
3. store a redacted payload;
4. preserve correlation and causation IDs;
5. support bounded replay and snapshot fallback;
6. project legacy events without rewriting historical evidence.

## Read-only Controller milestone

Milestone 2B may expose read-only status and project queries using adapters over
current tables. It must report unsupported capabilities through
`/system/capabilities`. Mutations remain disabled until the persistence delta
is implemented.

## Milestone 2O implemented delta

Schema version 20 adds `sandbox_profiles` and
`sandbox_profile_revisions`. Source revisions are immutable and retain only
Hermesfile v1 sources that pass validation and persistence eligibility checks.
The Controller exposes redacted authenticated profile reads. Build, image,
activation, rollback, deletion and HTTP mutation persistence remain future
deltas.

## Milestone 2S implemented delta

Schema version 21 extends `projects` with an explicit public resource revision,
default branch, optional sandbox profile, archive state, and repository mode. It
adds dedicated project operations, actor-bound idempotency, and immutable command
audit tables. Project create/update/enable/disable/rescan/archive writes emit the
Controller event journal transactionally and keep the compatibility TOML
projection synchronized. Delete remains unavailable.

## Milestone 2T implemented delta

Schema version 22 adds dedicated Hermesfile command operations, session-bound
idempotency, and immutable command audit. Hermesfile source and canonical bytes
remain owned by the existing immutable `sandbox_profile_revisions`; the current
pointer and resource revision remain owned by `sandbox_profiles`. Accepted
create/update transactions also append redacted sandbox events. No audit,
idempotency, operation, or event record stores raw source, canonical content,
secret-like input, or diagnostics containing the rejected value.

## Milestone 3B implemented delta

Schema version 23 promotes Orchestra Blueprint as the current source authority.
Persisted current source formats become `blueprint-v1`, and current lifecycle,
idempotency, operation, and audit table names use Blueprint terminology. The
migration deliberately retains historical routes, request hashes, key hashes,
and the historical HMAC domain so existing integrity material remains valid.
Projects continue to reference `sandbox_profile_id`; no parallel Blueprint
resource identity is introduced.
