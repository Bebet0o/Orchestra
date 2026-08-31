# Controller Components

Status: **Accepted design contract for Milestone 2A**

## Objective

This document defines the internal component model of the future
`hermesops-controller` service.

The model maps the existing HermesOps scripts into a coherent Controller
without requiring an immediate rewrite. The first implementation may wrap
existing logic, but core state transitions must progressively move into shared
Python modules rather than being implemented through shell subprocess chains.

## High-level component model

```text
HTTP / WebSocket adapters
        │
        ▼
Authentication and request context
        │
        ├── Query service ───────────────► read models
        │
        └── Command service
                │
                ├── policy engine
                ├── idempotency store
                ├── confirmation service
                ├── domain services
                │     ├── projects
                │     ├── objectives and plans
                │     ├── tasks and runs
                │     ├── reviews
                │     ├── recovery
                │     ├── integration and transactions
                │     └── sandboxes
                ├── persistence unit of work
                ├── audit service
                └── event journal
                       │
                       └── WebSocket fan-out and replay
```

External adapters:

```text
Hermes Agent adapter
Sandbox engine adapter
Git adapter
Filesystem adapter
Notification adapter
System health adapter
Secret store adapter
```

## Process model

The target beta may run as one Controller process plus the existing independent
supervisor and notifier services.

```text
hermesops-controller
├── HTTP API
├── event stream
├── command/query services
├── orchestration scheduler
└── adapter coordination

hermesops-supervisor
└── health and stale-run detection through internal Controller commands

hermesops-notifier
└── durable outbox delivery

hermesops-agent
└── upstream AI execution

hermesops-sandbox-engine
└── isolated container execution
```

The process boundary is an operational choice. The domain boundary is the
contract. Splitting or combining processes must not permit multiple independent
writers to bypass Controller invariants.

## Request context

Every API request receives an immutable request context:

```text
request_id
correlation_id
actor_id
actor_type
session_id
source_ip
user_agent
received_at
idempotency_key
resource_revision
confirmation_id
```

Internal scheduled work uses the same command path with an actor such as:

```text
system:orchestrator
system:supervisor
system:notifier
```

There must be no privileged “internal shortcut” that skips validation or audit.

## Authentication service

Responsibilities:

- authenticate the local operator session;
- rotate and invalidate sessions;
- validate CSRF protection;
- enforce request rate limits;
- provide actor identity to the request context;
- audit authentication events without recording credentials.

The authentication service does not own provider credentials used by Hermes
Agent. Those belong to the secret store and Agent adapter.

## Query service

The query service:

- exposes stable read models;
- never changes domain state;
- enforces project visibility and secret redaction;
- supports pagination, filtering, and bounded log retrieval;
- reads from SQLite through repository interfaces;
- may use cached projections when their revision is explicit.

Read models are API contracts, not direct database row serialization. Database
schema changes must not silently alter API responses.

## Command service

The command service is the only entry point for state-changing intent.

Pipeline:

```text
authenticate
→ authorize
→ validate body
→ check idempotency
→ load current aggregate
→ verify revision
→ evaluate policy
→ require confirmation when necessary
→ execute domain transition
→ persist state + audit + events atomically
→ schedule external side effects
→ return accepted/result envelope
```

External side effects such as starting a container or calling Hermes Agent must
use an outbox or resumable operation record when they cannot be committed in
the same SQLite transaction.

## Idempotency service

Every mutating API request requires `Idempotency-Key`.

The service stores:

- actor;
- endpoint and command name;
- normalized request hash;
- first-seen time;
- final response status;
- final response body or accepted operation ID;
- expiration policy.

Reusing a key with the same normalized request returns the original response.
Reusing a key with a different request returns `409 idempotency_conflict`.

Idempotency records are scoped to the authenticated actor and operation.

## Confirmation service

The confirmation service stores a sealed representation of the original
command.

A confirmation record contains:

- confirmation ID;
- actor ID;
- command type;
- target resource;
- normalized command hash;
- risk class;
- human-readable consequences;
- required phrase when applicable;
- creation and expiration time;
- single-use state.

The follow-up confirmation request references the ID and cannot change the
original command payload.

## Policy engine

The policy engine combines:

- global defaults;
- project policy;
- role policy;
- sandbox policy;
- current runtime health;
- actor permissions;
- requested command risk;
- confirmation state.

It returns one of:

```text
ALLOW
DENY
REQUIRE_CONFIRMATION
BLOCK_DEPENDENCY
BLOCK_HUMAN
```

Policy decisions are persisted in the audit trail.

## Project service

Responsibilities:

- create and register projects;
- validate repository and data paths;
- manage project metadata and enabled state;
- bind project policy and default sandbox profile;
- protect the main branch and remote settings;
- expose project health and blockers.

A project creation workflow may clone a repository, but the filesystem action
must be represented as a durable operation with rollback or cleanup semantics.

## Objective and planning service

Responsibilities:

- accept durable user objectives;
- preserve the original operator text;
- manage objective lifecycle;
- request planning through the Hermes Agent adapter;
- validate planner output;
- create a task DAG;
- reject cycles and impossible dependencies;
- record assumptions, constraints, and unresolved questions;
- schedule only tasks whose dependencies and policies are satisfied.

The planner cannot directly write task rows. Its output is untrusted input
validated by the Controller.

## Task and run service

Responsibilities:

- claim ready tasks;
- enforce one active writer per project by default;
- select role and sandbox profile;
- create immutable run configuration;
- start a transaction snapshot;
- start and monitor sandbox execution;
- persist heartbeats and progress;
- classify completion, failure, timeout, and interruption;
- hand results to review and integration services.

A task is durable intent. A run is one execution attempt.

## Transaction and integration service

Responsibilities:

- create pre-run snapshots;
- track baseline commit and worktree state;
- verify worker ownership of changes;
- require a clean committed result;
- prevent direct worker writes to protected branches;
- apply approved commits through a controlled integration path;
- detect ambiguous or divergent Git state;
- create rollback evidence.

Existing logic in:

```text
scripts/hermesops-transaction.py
scripts/hermesops-integrator.py
```

is the starting implementation source, not a permanent process boundary.

## Review service

Responsibilities:

- create an independent review run;
- provide a read-only project view;
- prevent access to worker secrets;
- validate tests, diffs, architecture, security, and policy;
- persist evidence and verdict;
- reject transport failures as approvals.

Normalized verdicts:

```text
PASS
PASS_WITH_DEBT
FIX
SECURITY
PERFORMANCE
ARCHITECTURE
HUMAN
```

The review service never integrates code itself.

## Recovery service

Responsibilities:

- reconcile interrupted runs;
- inspect durable state, Git state, container state, and snapshots;
- classify ownership of dirty changes;
- propose or execute only safe actions;
- request human confirmation for ambiguous or destructive actions;
- preserve incident evidence.

Normalized decisions:

```text
RESUME_SAFE
ROLLBACK_SAFE
BLOCK_HUMAN
```

Recovery must be deterministic from persisted evidence where possible.

## Sandbox service

Responsibilities:

- store Blueprint source and parsed canonical form;
- validate schema and security policy;
- create build records;
- compile Blueprint into a deterministic build plan;
- build in the dedicated sandbox engine;
- run declared validation commands;
- record image ID and immutable digest;
- activate, deactivate, and roll back profiles;
- enforce retention of the last known-good image;
- create task containers with immutable run configuration;
- collect exit status, logs, and resource data.

The sandbox service never trusts a mutable image tag as execution identity.

## Hermes Agent adapter

The adapter isolates Controller code from upstream Hermes Agent details.

Responsibilities:

- create and track sessions;
- select provider, model, and Hermes profile;
- send structured prompts and context;
- receive streamed output;
- normalize tool/session errors;
- report usage and health;
- cancel or time out sessions;
- avoid leaking provider-specific payloads into domain records.

The adapter returns normalized results such as:

```text
SUCCEEDED
FAILED_MODEL
FAILED_TRANSPORT
FAILED_TOOL
CANCELLED
TIMED_OUT
```

A future provider adapter can be added without changing objective, task, or
review state machines.

## Event journal

The event journal:

- assigns a globally increasing sequence number;
- stores immutable event envelopes;
- commits events in the same transaction as domain changes;
- supports replay after a sequence number;
- fans committed events out to connected clients;
- preserves correlation and causation IDs;
- never stores secret values.

Events are facts, not commands.

## Audit service

Audit records answer:

- who requested the action;
- what resource was affected;
- what command was evaluated;
- what policy decision was made;
- whether confirmation was required;
- what durable state changed;
- which external operation was scheduled;
- what request and correlation IDs connect the evidence.

Audit records are operator-facing and may contain more detail than public event
payloads, but still exclude secret values.

## Notification service

The Controller writes notification intent to a durable outbox.

The independent notifier:

- reads due messages;
- sends through Telegram or future channels;
- records attempts and redacted failure information;
- retries with bounded backoff;
- does not own objective or run state.

Notification delivery failure does not roll back a successful domain command.

## Secret store adapter

The adapter exposes:

```text
put secret
delete secret
test configured state
provide scoped secret reference to an approved runtime
```

It never exposes:

```text
list plaintext values
return plaintext through query API
place plaintext in events or audit payloads
persist plaintext in idempotency records
```

## Existing-script migration map

| Existing entry point | Future Controller component |
| --- | --- |
| `hermesops-db.py` | persistence and migrations |
| `hermesops-registry.py` | project service |
| `hermesops-objectives.py` | objective service |
| `hermesops-planner.py` | planning service |
| `hermesops-orchestrator.py` | scheduler and run coordination |
| `hermesops-worker.py` | task/run service and sandbox adapter |
| `hermesops-reviewer.py` | review service |
| `hermesops-recovery.py` | recovery service |
| `hermesops-transaction.py` | transaction service |
| `hermesops-integrator.py` | integration service |
| `hermesops-supervisor.py` | system health adapter and internal commands |
| `hermesops-notifier.py` | notification outbox consumer |
| `hermesops-roles.py` | role policy repository |
| `hermesops-control.py` | temporary operator adapter |
| `hermesopsctl` | temporary CLI client of Controller API |

## Implementation sequence

Recommended order:

1. extract reusable repositories and domain types from scripts;
2. implement Controller health and request context;
3. implement read-only queries;
4. implement event journal;
5. implement idempotent project/objective commands;
6. adapt orchestrator and supervisor to internal commands;
7. implement Agent adapter;
8. implement sandbox profile storage and validation;
9. implement Blueprint build operations;
10. retire direct database writes from CLI scripts.

## Invariants

The implementation must preserve:

- one authoritative state writer;
- no Console access to privileged infrastructure;
- no worker access to host Docker;
- no secret values in events or logs;
- no unreviewed integration;
- no unsafe automatic recovery;
- no mutable image identity;
- no successful mutation without an audit record;
- no emitted state-change event before durable commit.
