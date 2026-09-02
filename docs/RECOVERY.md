# Deterministic recovery manager

Orchestra recovery is fail-closed and Controller-owned. The `ops-recovery`
identity is recorded for every recovery execution, but Git and SQLite state
transitions are decided by deterministic policy rather than probabilistic
model output.

## Decisions

- `RESUME_SAFE` keeps a coherent `RUNNING` or `REVIEWING` transaction ready
  for a fresh worker/reviewer attempt, or completes a proven interrupted
  `COMMITTING` fast-forward.
- `ROLLBACK_SAFE` removes a missing/abandoned transaction worktree only after
  the snapshot artifacts and Git bundle are verified.
- `BLOCK_HUMAN` preserves the project lock, creates a pending approval, and
  performs no Git integration when evidence is ambiguous or corrupted.

## Evidence

Each decision stores a canonical evidence document and SHA-256 digest in
`recovery_executions`. Evidence includes the run, lock, snapshot hashes,
default branch and worktree state, integration record, unfinished tasks, and
resource identities.

## Crash cleanup

Abandoned worker/reviewer executions are marked failed. Their host containers,
nested DIND sandboxes, runtime profiles, and standalone clones are removed.
The orphan sweep only targets Orchestra-prefixed resources that are not
referenced by an active run.

## Restart usage

A Controller startup or watchdog invokes:

```text
orchestra-recovery.py sweep --owner controller-recovery --stale-seconds 300
```

The sweep ignores fresh heartbeats, recovers stale runs, and then cleans
unreferenced temporary resources.


## Clone-tree hygiene

Removing a worker or reviewer clone also prunes empty run/project parent
directories. The clone roots themselves are preserved when present, and path
containment is checked before every removal.
