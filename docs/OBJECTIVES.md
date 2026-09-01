# Orchestra objective queue

Milestone 4B adds a durable queue above the 4A orchestration DAG engine.
Operators submit an objective once; the persistent Orchestrator plans, orders,
dispatches and reconciles it across restarts.

## Objective lifecycle

`QUEUED -> PLANNING -> RUNNING -> COMPLETED`

Control states are `PAUSE_REQUESTED`, `PAUSED`, `CANCEL_REQUESTED` and
`CANCELLED`. Planning or plan failure produces `FAILED`. A running task is
never killed merely to pause or cancel: the request becomes effective after
the active transaction reaches a safe boundary.

## Priority and concurrency

Lower integer priority values run first. Ordering is global across AI and
declarative objectives. `global_parallel_objectives` bounds active objectives,
while `global_parallel_tasks` bounds task execution. Project affinity preserves
one writer slot per project across different plans and objectives. Different
projects may advance concurrently.

## Planning and retry

AI objectives are planned by the isolated `ops-orchestrator` profile. Planner
attempts are durable in SQLite. An interrupted or failed planning attempt is
retried only within its configured budget and after `not_before`; retries never
bypass strict DAG validation. Unfinished planner containers and executions are
reconciled after daemon restart.

## Commands

```bash
orchestra-objectives.py submit \
  --objective-file objective.txt \
  --project PROJECT_ID \
  --priority 100

orchestra-objectives.py list
orchestra-objectives.py status --objective OBJECTIVE_ID
orchestra-objectives.py pause --objective OBJECTIVE_ID
orchestra-objectives.py resume --objective OBJECTIVE_ID
orchestra-objectives.py cancel --objective OBJECTIVE_ID
```

Declarative DAGs may be queued with `submit-plan`. `--allow-test-actions` is
reserved for controller validation fixtures.


## Task-scoped worker markers

An AI or declarative plan may define a completion marker specific to its task.
The worker launcher remains authoritative: successful execution requires the
exact expected marker and persists `marker_found=true`. Compatibility tests do
not assume the historical 3B marker literal.


## Readiness observation

Objective-queue validation may overlap the permanent Supervisor sweep. A
`RUNNING` latest sweep is transient; readiness is accepted only after a
bounded wait yields a terminal healthy `COMPLETED` or `SKIPPED` snapshot.


## Historical-plan compatibility

Foundation tests for earlier milestones identify their own immutable audit
artifacts instead of assuming that the latest AI execution belongs to them.
Completed autonomous objectives can therefore be appended without changing
the meaning of prior evidence.


## Pre-commit repository validation

The objective-queue foundation test can execute before or after the milestone
commit. Before commit it validates an exact allowlist of 4B-owned modifications
and rejects every foreign or unexpected Git status. After commit it accepts
only the clean repository state.
