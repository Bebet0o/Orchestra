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
