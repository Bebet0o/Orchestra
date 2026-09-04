# Shared project context

Shared project context is durable selected knowledge, not a transcript or a
copy of runtime history. Schema 28 separates three responsibilities:

- `SharedContextStore` owns append-only structured knowledge in SQLite.
- `ContextProjector` owns deterministic selection, ordering, and bounds.
- `ContextSnapshot` records exactly what one execution attempt received.

Task Graph still owns dependency and readiness decisions. WorkerPool still owns
capacity and queuing. AgentRuntime still owns execution.

## Entry and scope model

An entry is either `PROJECT` scoped or `OBJECTIVE` scoped and has a kind, stable
key, content, timestamp, and source type. The deliberately small kind set is
fact, constraint, decision, finding, note, and reference. Control-plane entries
cannot claim task provenance. Task-result entries require a matching completed
task, assignment, and attempt in the same project; objective entries also
require the source task to belong to that objective.

Project entries are visible to objectives using that project. Objective entries
are visible only to that objective. The store rejects objective/project scope
mismatches and task-result provenance from another project or objective.
Repeated additions remain distinct append-only facts with distinct IDs; there
is no semantic deduplication or revision engine.

## Projection schema and selection

Every projection declares `context_schema_version: 1`. A planner receives the
current objective plus eligible project and objective entries. A worker receives
project/objective identity, the complete current task identity and instruction,
eligible explicit entries, and results from direct completed dependencies only.
A dependency result remains authoritative in `orchestration_tasks.result_json`;
the projection carries a stable result reference, SHA-256, and bounded excerpt
instead of copying an unbounded artifact. Unrelated tasks, ancestor traversal,
prompts, logs, and runtime/event journals are excluded.

Ordering is explicit: project IDs are sorted for planner projections, objective
entries precede project entries, entry sequence plus ID breaks ties, and direct
dependencies use graph position plus task ID. Canonical compact JSON is the hash
input, so unchanged durable state produces identical bytes and SHA-256.

Default bounds are:

- 16 KiB per explicit entry;
- 64 explicit entries per projection;
- 8 KiB excerpt per direct dependency result;
- 64 KiB for the complete canonical projection.

Core objective/task data is mandatory. The projector omits lower-priority
project entries first, then objective entries, then dependency results, and
reports `budget_exhausted` plus `omitted_count`. If mandatory data itself cannot
fit, projection fails and the worker assignment releases its slot without an
empty-context execution.

## Snapshot timing and queries

Planner context freezes after the objective attempt is running and before its
runtime launch. Worker context freezes after WorkerPool has claimed a slot and
the assignment is bound to a running task attempt, also before runtime launch.
Queued tasks have no snapshot and may observe newer eligible entries at their
eventual start.

`context_snapshots` stores the canonical projection, schema version, hash,
bounding metadata, consumer, and execution linkage. Ordered
`context_snapshot_sources` rows identify each included entry or dependency
result. Entries, snapshots, and source rows are immutable. Store queries expose
project/objective entries and snapshots by task attempt or objective attempt,
including sources, bounds, and dependency references. A repeated freeze for an
already-bound attempt returns the original snapshot, including after restart.

Events contain only entry/snapshot IDs, hashes, counts, and timestamps; they do
not contain context text or credentials. Provider keys and environment secrets
are never automatically copied into the store, snapshot events, or projections.

## Deliberate limits

This milestone adds no embeddings, vector database, semantic retrieval,
automatic memory extraction, intelligent summarization, contradiction handling,
context aging, or peer-worker chat. It does not redesign Reviewer/Judge policy,
add a Recovery Loop, route models, or expose a Multi-Agent Console. The common
reviewer projection method is only the reusable input seam for the next
milestone.
