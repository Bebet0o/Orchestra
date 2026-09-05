# Recovery Loop — v0.2.0 milestone 0.2-F

The recovery loop turns a durable `NEEDS_FIX` Judge decision into a bounded
corrective attempt for the same logical task. `RecoveryCoordinator` is the
single authority for that decision. It does not run a synchronous retry loop,
change the task graph, select a different role or model, or own worker capacity.

The deterministic path is:

```text
worker attempt -> structured review -> Judge NEEDS_FIX
    -> RecoveryAction -> WorkerPool assignment -> new worker attempt
    -> new immutable context snapshot -> new review and Judge decision
```

## Policy and dispositions

`[recovery].max_retries` is the number of additional corrective attempts after
the original attempt. The accepted range is `0..3`, and the default is `0` so
existing configurations retain the 0.2-E behavior. The effective value and a
canonical policy hash are copied to each review-required logical task when its
plan is inserted. Later configuration changes do not alter an active chain,
and model output cannot change or bypass this policy.

| Judge disposition | Recovery behavior |
| --- | --- |
| `PASS` | No recovery. Existing acceptance and integration rules apply. |
| `NEEDS_FIX` | One action if budget remains; otherwise `EXHAUSTED`. |
| `BLOCKED` | No automatic recovery. |
| `HUMAN_REVIEW` | No recovery or budget use; existing human authority applies. |

Reviewer runtime failure and malformed output remain fail closed. They do not
provide valid corrective evidence or consume worker recovery budget. Existing
worker runtime retries controlled by task `max_attempts` remain the bounded
infrastructure-failure policy. A corrective attempt itself is not blindly
retried after runtime interruption or failure; only a later valid review and
`NEEDS_FIX` decision can authorize another corrective attempt.

## Durable authority and accounting

`recovery_actions` binds the project, objective, plan, logical task, source
attempt, source review, source Judge decision, retry sequence, snapshotted
maximum, canonical reason and hash, target WorkerPool assignment, target
attempt, and terminal outcome. Its lifecycle is:

```text
PENDING -> DISPATCHED -> ATTEMPT_CREATED -> COMPLETED
       \-> CANCELLED       \-> COMPLETED

NEEDS_FIX with no budget -> EXHAUSTED
```

The unique source-decision constraint and `BEGIN IMMEDIATE` transaction create
at most one action for a Judge decision. WorkerPool creates the assignment,
transitions the action to `DISPATCHED`, and increments the task's
`recovery_retry_count` in one transaction. The count means corrective
assignments durably created. Reconciliation can run repeatedly or after a
restart without consuming budget again.

Exhaustion is an explicit terminal action and creates an approval through the
existing `approvals` table when the attempt owns a run. `ACKNOWLEDGE` records
that an operator saw the exhaustion; it never accepts the task or creates
another attempt. Without a run, exhaustion remains directly inspectable rather
than creating another approval subsystem.

## Corrective context and immutable lineage

The frozen recovery reason contains the previous attempt identity, previous
result reference and SHA-256, triggering review identity and SHA-256, Judge
decision and disposition, review summary, findings, and required changes.

`ContextProjector` adds this task-local overlay only while freezing the target
corrective attempt. The snapshot stores its `recovery_action_id`; unrelated
tasks and global project/objective context are unchanged. Every corrective
attempt receives a new context snapshot. Previous attempt results, worker
executions, worker snapshots, review inputs, structured reviews, and Judge
decisions remain protected by immutable history tables and triggers. The task
row remains a current-result projection and may advance only from a linked
corrective attempt.

Pipeline attempts continue to use independent transaction worktrees and
runtime sandboxes. Recovery does not share writable sandboxes across attempts.
Prior evidence reaches the new sandbox through the durable context overlay.

## Graph, pool, runtime, and restart behavior

The logical task remains blocked after `NEEDS_FIX`, so its dependants remain
unsatisfied. Recovery submits through WorkerPool using the task's existing role
and runtime kind. WorkerPool remains the capacity and FIFO queue authority. The
Task Graph alone releases dependants after the corrective result receives
`PASS` and the existing acceptance path completes.

RecoveryCoordinator is runtime neutral. HermesRuntime and NativeRuntime,
including NativeRuntime with FakeModelProvider in tests, receive the frozen
context through the normal worker execution contract.

At startup, WorkerPool reconstructs queued work and interrupts previously
running assignments under its existing policy. Task attempt reconciliation
then finishes interrupted state, and RecoveryCoordinator reconciles durable
actions. A persisted `NEEDS_FIX`, `PENDING` action, queued assignment, or linked
attempt is discoverable without creating a duplicate. An interrupted
corrective execution consumes the assignment already created and does not
authorize another attempt by itself.

The task graph snapshot includes `recoveries`.
`RecoveryCoordinator.lineage(task_id)` returns the task, every attempt with
snapshot/review/Judge linkage, and every recovery action.
