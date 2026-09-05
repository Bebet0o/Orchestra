# Orchestra orchestration DAG

Milestone 4A adds a durable multi-task scheduler above the transactional
worker/reviewer/integrator pipeline.

## Plan contract

A plan contains an objective, a bounded parallelism value, and an acyclic set
of tasks. Production tasks use `PIPELINE` and specify one enabled project, one
committing worker role, precise instructions, acceptance criteria and a
completion marker. Same-project write tasks must be dependency-ordered because
project writer concurrency is one.

## Execution lifecycle

For each `PIPELINE` task the orchestrator:

1. reserves a Git transaction and verified snapshot;
2. launches the selected isolated worker;
3. submits the exact result commit;
4. launches the independent read-only reviewer;
5. calls the controlled integration gate;
6. persists the worker, review, integration and run identifiers;
7. marks the task complete only after reviewed local integration.

Dependencies become `READY` only after every parent is `COMPLETED`. A failed
parent blocks descendants. Tasks from different projects or controller-only
test actions can run concurrently, bounded by both plan and global limits.

## Planner task graph

The existing planner produces one bounded schema-v1 JSON plan with 1–32
logical tasks and at most 256 dependency edges. Each task has a stable key,
human-readable title, graph position, instruction, existing worker-role
binding, and dependency keys. Planner output is untrusted: Orchestra rejects
unknown or duplicate keys and edges, unsupported roles, self-dependencies,
cross-plan edges, excessive graphs, and cycles before activation.

The normalized `orchestration_dependencies` rows are scheduler authority; the
JSON plan remains provenance rather than the only graph representation. A
logical task is distinct from its `orchestration_attempts` and from a
`worker_pool_assignments` capacity claim. Schema 27 records graph activation
metadata and runtime-neutral task-graph lifecycle events, while query snapshots
join tasks to assignments and attempts without copying runtime output.

Readiness reconciliation runs in one immediate SQLite transaction to a stable
fixpoint. A PENDING task becomes READY only when every parent is COMPLETED. A
FAILED, CANCELLED, or BLOCKED parent makes the task BLOCKED, including
transitive descendants. Concurrent parent completions cannot create duplicate
work because WorkerPool has one active assignment per logical task and graph
dispatch linkage is idempotent.

The responsibility boundary is:

1. Planner creates the validated durable DAG.
2. Task Graph owns dependency, readiness, blocking, and graph terminality.
3. The scheduler submits READY work.
4. WorkerPool alone owns bounded capacity and durable queuing.
5. AgentRuntime selects HermesRuntime or NativeRuntime for execution.

Graph activation is immutable; this milestone does not rewrite a live graph.
Advanced reviewer/judge behavior, automated recovery and retries, model
routing, and Console graph visualization remain deferred.

## Shared project context

Schema 28 adds append-only `PROJECT` and `OBJECTIVE` knowledge through
`SharedContextStore`. `ContextProjector` constructs one deterministic,
runtime-neutral schema-v1 projection for planner, worker, and future reviewer
consumers. Worker projections contain objective identity and instruction, the
current logical task, eligible explicit entries, and bounded references plus
excerpts for direct completed dependency results. They never traverse all DAG
ancestors or copy runtime/event history.

The projection limit is 64 KiB by default, with at most 64 explicit entries,
16 KiB per entry, and an 8 KiB excerpt per dependency result. Mandatory
objective/task identity is never truncated; if it cannot fit, dispatch fails.
When lower-priority data is omitted, the projection reports
`budget_exhausted` and `omitted_count`. Priority is core task data, direct
dependency results, objective entries, then project entries. Stable graph
positions and context sequence IDs define ordering.

A canonical JSON projection and SHA-256 hash are frozen into an immutable
`context_snapshots` row after WorkerPool claims capacity and the concrete task
attempt is reserved and bound, but before AgentRuntime starts. A task that is
only queued can therefore see newer eligible entries; a started attempt can
never have its historical snapshot rewritten. Snapshot source rows retain the
exact context-entry and dependency-task IDs used. Planner snapshots follow the
same rule when their objective attempt starts. Restart readback reuses the
attempt-bound snapshot rather than recomputing it.

Task Graph remains dependency/readiness authority, WorkerPool remains capacity
authority, and AgentRuntime remains execution authority. Shared context adds no
worker-to-worker transport.

Review-required tasks may enter the bounded corrective flow documented in
[Recovery Loop](RECOVERY_LOOP.md). RecoveryCoordinator translates only a
durable Judge `NEEDS_FIX` decision into a new WorkerPool assignment; it does not
alter dependencies or directly release downstream tasks.

## Native worker pool

Pipeline tasks enter an Orchestra-owned `WorkerPool`. This is bounded execution
capacity, not a NativeRuntime-only scheduler: every assignment snapshots its
role and runtime, then reaches the existing worker launch and common
`AgentRuntime` boundary. HermesRuntime and NativeRuntime use the same pool.

```toml
[worker_pool]
max_concurrency = 1
```

The default is one for serialized compatibility; valid values are 1 through
16. Plan limits and the existing one-writer-per-project rule may reduce actual
parallelism further. CPU and memory quota scheduling are not part of this
milestone.

`orchestration_tasks` remains the durable eligibility authority. Eligible
pipeline work receives a durable FIFO `worker_pool_assignments` record, ordered
by a transactionally allocated queue sequence. Slot claims use an immediate SQLite
transaction and count durable RUNNING assignments before dispatch, so an
in-memory executor cannot exceed the configured limit. Completion, failure,
queued cancellation, slot release, and restart interruption are recorded in
runtime-neutral `worker_pool_events` without prompts, provider details, or
credentials.

## Persistence and restart

The Compose-owned `orchestrator` service owns one exclusive lock. Plans, tasks,
dependencies, attempts and daemon instances are durable in SQLite. On restart, interrupted
non-pipeline attempts become `ABANDONED` and are retried within their attempt
budget. Active transactional pipelines are never duplicated; the existing
Recovery Manager remains authoritative for their safe reconciliation.

After controller restart, QUEUED pool assignments remain dispatchable.
Formerly RUNNING assignments become `INTERRUPTED` and release capacity; they
are not blindly duplicated. No ephemeral Future or thread handle is persisted.

The pool coordinates capacity only. Task-graph redesign, shared worker
context, automatic recovery policy, and model routing remain separate roadmap
milestones.

## AI planner

`orchestra-planner.py` runs the `ops-orchestrator` profile without a project
workspace. It accepts a high-level objective and a fixed set of enabled
projects, then emits a strictly validated JSON DAG. AI plans start as `DRAFT`
unless explicitly activated.

## Operations

```bash
/opt/orchestra/repo/scripts/orchestra-compose.sh ps orchestrator
/opt/orchestra/repo/scripts/orchestra-orchestrator.py daemon-status
/opt/orchestra/repo/scripts/orchestra-orchestrator.py list
/opt/orchestra/repo/scripts/orchestra-orchestrator.py status --plan PLAN_ID
```


## Compose lifecycle

The Orchestrator runs in the canonical Compose application with a read-only
root filesystem, dropped capabilities, `no-new-privileges`, and the selected
operator's numeric identity. It receives only the dedicated private DIND
socket required for sandbox/runtime operations. Readiness requires the
exclusive lock, a healthy Supervisor, and the matching SQLite instance in
`RUNNING`.

## Reviewer transport resilience

Reviewer transport failures and reviewer decisions are separate states. A
missing marker is not retryable by itself. Orchestra reads the failed reviewer
execution log and retries only recognized provider/stream failures, including
the observed Codex `no SSE events` condition. Retries are bounded, audited and
reuse the same immutable transaction result; the worker is not rerun.

After every reviewer invocation, controller-owned runtime containers, profiles
and clones are removed using exact audited identifiers. A real `REJECT` or
`BLOCK_HUMAN` decision is never retried.


## Active sandbox protection

Worker and reviewer execution rows are reserved before their nested Docker
sandboxes are created. The sandbox ID is finalized later, which previously
left a race with the permanent orphan cleaner. Recovery now preserves any
sandbox whose `orchestra-task-id` belongs to a SQLite task in `RUNNING` under an
active run. The normal sandbox ID remains the primary reference after it is
persisted.

## Reviewer assignment boundary

Reviewer transport retries are distinct durable assignments. At most one
assignment may be active for a run. The assignment captures the selected role
and profile before launch and cannot be reassigned or rewritten after it reaches
a terminal state.

## Public read boundary

The Controller exposes a redacted plan graph, attempts and reviewer assignments
for the Orchestra Console. Public reads never reuse the internal CLI status
payload because that payload contains task definitions, raw result objects and
failure text. Cursor pagination is authenticated and all mutation authority
remains outside these routes.
