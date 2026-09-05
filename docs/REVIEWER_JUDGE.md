# Reviewer / Judge — v0.2.0 milestone 0.2-E

Reviewer evaluates evidence. Judge is deterministic control-plane logic that turns
validated evidence into an immutable orchestration disposition. Recovery is a
future consumer of that disposition, not a Reviewer or Judge responsibility.

## Enable required task acceptance

A task in a declarative or generated schema-1 plan may specify:

```json
"review": {"required": true, "role_id": "reviewer"}
```

The default is `required: false`. Existing PIPELINE Git review/integration remains
unchanged for that default. Required review is supported for PIPELINE tasks and
uses one enabled reviewer role with read-only workspace, no network, commit or
push authority. The existing role's `runtime_kind`, profile and model select
HermesRuntime or NativeRuntime. These identifiers are frozen when review is
requested. Changes before dispatch fail closed. There is no dynamic model router.

For required tasks, worker submission ends the worker attempt successfully and
releases its pool slot. The task is `BLOCKED` with `review_state=PENDING`; the
separate state avoids changing historical task-status vocabulary. The existing
read-only reviewer clone, sandbox audit, runtime adapter, durable assignments and
reviewer execution tables are reused. This replaces the legacy reviewer/retry
path for this task; it does not run two reviewers.

## Authorities and provenance

Schema 29 adds `task_reviews`, `review_context_snapshots`,
`structured_review_results`, `judge_decisions` and `review_events`.

A review subject binds project, objective when present, plan, logical task,
worker assignment, completed attempt, worker runtime execution when present,
worker snapshot and worker result reference/hash. A production worker result is
`worker_execution_result:<execution_id>`; controller fixtures without a runtime
execution use `orchestration_attempt_result:<attempt_id>`. Neither authority is
copied into the review result table.

`ContextProjector.review_input` checks ownership and reads the actual immutable
worker snapshot. It does not re-project current context. The reviewer snapshot
records schema 1, objective/task identities and instructions, subject IDs, source
IDs, worker snapshot reference/hash and worker result reference/hash. Canonical
JSON uses sorted keys, compact separators, UTF-8 and no NaN; SHA-256 hashes those
bytes. No creation timestamp is included in hashed projection content.

`RuntimeRequest.context` contains this structured input. The prompt resolves its
immutable references to the exact historical worker projection and result, so
both runtimes receive the same evidence bytes. Dependency evidence and explicit
shared context come from that worker projection. Unrelated runtime history and
current sibling outputs are never queried. The compatibility `for_reviewer`
preview remains available but is not used to construct execution evidence.

Reviewer and worker snapshots are separate immutable authorities. SQLite guards
reject updates, deletes and replacement of review history; reviewed attempt and
worker result bodies and assignment identities cannot change. One attempt is one
review revision. A future new worker attempt may receive another review; this
milestone does not provide automatic rereview or retry.

## Structured output and bounds

```json
{
  "schema_version": 1,
  "assessment": "needs_fix",
  "summary": "A required condition is missing.",
  "findings": [
    {"code": "MISSING_CHECK", "severity": "error", "message": "Add the missing check.", "evidence": []}
  ],
  "required_changes": ["Add the missing check and provide its result."]
}
```

The exact JSON object is enclosed in the documented structured-review delimiters
and followed by the completion marker. Bounded transport log lines may surround
the block; duplicate delimiters or markers are rejected. Unknown fields, duplicate JSON keys,
unknown assessments, duplicate finding codes, invalid types and invalid evidence
references are rejected. Bounds are:

- 64 KiB total output; summary 4,000 UTF-8 bytes.
- 32 findings; code 64 ASCII characters, message 4,000 UTF-8 bytes.
- Severity is `info`, `warning` or `error`.
- 16 unique evidence references per finding, restricted to supplied source IDs.
- 32 required changes, each at most 2,000 UTF-8 bytes.
- 240,000 bytes of resolved mandatory input evidence; overflow fails closed.
- Runtime context retains the existing 64 KiB contract. Runtime prompt retains
  its 256 KiB contract. Required review runtime timeout is at most 600 seconds.

Evidence IDs cannot reference unrelated projects, objectives or tasks. An
explicit upstream dependency may already be present in the historical worker
projection, including in a cross-project graph; it is not newly disclosed to the
reviewer. Model-generated evidence references to other projects are rejected.
Findings describe changes; they are not executable recovery plans.

## Judge dispositions and graph acceptance

| Disposition | Meaning | Graph effect |
| --- | --- | --- |
| `PASS` | Supplied evidence satisfies the requested work | Existing guarded Git integration may run; only a completed run releases the task |
| `NEEDS_FIX` | Work needs actionable corrections | Result/findings retained, dependencies held, no retry |
| `BLOCKED` | Evaluation cannot safely proceed, such as unavailable required evidence | Distinct durable non-acceptance; no automatic human gate or retry |
| `HUMAN_REVIEW` | A human decision is required | Existing `approvals` pending gate; no continuation until resolved |

Judge maps the validated assessment to its corresponding disposition, retaining
review hash, reason, decision identity and timestamp separately from Reviewer
output. The legacy review record is an adapter for existing Git checks; it cannot
overrule required Judge evidence. The integrator checks required Judge authority
both on entry and under its integration transaction, and continues to enforce
snapshot/commit freshness and cancellation.

The task becomes `COMPLETED` only after acceptance and, when a run exists,
successful integration. The task's reviewed worker result remains unchanged.
Downstream readiness consumes that accepted task status. Pending/rejected review
does not propagate a terminal dependency failure. A plan with no runnable work
and held review becomes `BLOCKED`; its objective remains nonterminal. Independent
accepted branches survive rejection. A newly ready task reactivates a plan held
only for required review.

`HUMAN_REVIEW` creates a row in the existing `approvals` table. Operators inspect
it with `orchestra-control.py approvals --json` and resolve with the existing
`resolve --approval <id> --decision APPROVE|REJECT` command. This command recognizes
Judge gates and records the bounded decision without invoking legacy recovery.
Human approval authorizes integration of the same reviewed result; rejection
retains the hold. Original Judge `HUMAN_REVIEW` evidence is never overwritten.

## Claims, failures and restart

SQLite `BEGIN IMMEDIATE`, unique attempt IDs and conditional `PENDING -> RUNNING`
claims allow one reviewer execution per subject. Runtime work occurs outside DB
transactions, in a bounded background scheduler job, leaving worker capacity and
controller heartbeats available. Completed decisions and acceptance events are
idempotent. Integration has a separate one-shot durable claim; a failure does
not start a retry loop.

A malformed result or runtime failure leaves a terminal failed review and an
unaccepted task. Invalid subject provenance holds the task with `review_state=FAILED`
without inserting an invalid review subject. Failure codes are bounded; exception/output bodies are not
copied into new review events. Events contain review identity and lifecycle kind;
query joins supply hashes and provenance without duplicating bodies.

After acquiring controller ownership, startup marks running reviews interrupted,
uses existing ownership-checked reviewer resource cleanup and fails active
legacy reviewer assignments. Pending reviews remain dispatchable. Completed
reviews are not recreated. A committed Judge PASS with a completed run can finish
graph acceptance after restart. An interrupted integration claim whose run did
not complete remains held for future recovery, rather than replaying side effects.
The legacy automatic recovery sweep excludes these held review dispositions.

Internal query surfaces are `ReviewStore.get`, `list(pending=True)`,
`acceptance_candidates`, `evidence`, and the `reviews` collection returned by the
existing task-graph snapshot. They expose snapshots, schema/hash, findings, Judge
and pending human state. No Console UI is added.

## Validation and deferrals

`tests/test_reviewer_judge.py` provides deterministic diamonds: B and C own two
worker slots; D remains pending after B PASS alone; both PASS decisions release D
exactly once. B NEEDS_FIX with C PASS retains C and blocks D without another B
attempt. Tests also cover runtime boundaries, the production reviewer command's
durable legacy/structured writes, human gates, strict parsing, ownership,
concurrent claims, restart, migration preservation and SQLite integrity.

Hermes tests use deterministic process transport and verify its command mapping;
Native tests execute NativeRuntime with FakeModelProvider. These are not live
provider or Docker end-to-end runs.

Automatic repair, reassignment, replanning, fix tasks, dependency changes,
reviewer consensus, model routing and Console visualization remain deferred to
0.2-F, 0.2-G and 0.2-H. No release, tag or publication workflow is created.
