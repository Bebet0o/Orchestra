# Reviewed integration gate

Orchestra integrates a transaction only through the Controller-owned
`orchestra-integrator.py` gate. Workers and reviewers never receive this
script, the Controller database, or the project default-branch checkout.

## APPROVE

`APPROVE` with `PASS` or `PASS_WITH_DEBT` triggers a verified fast-forward.
Immediately before integration, the gate verifies the snapshot, lock owner,
review identity, reviewed commit, worktree cleanliness, default-branch HEAD,
and reviewer isolation evidence. The run transitions through `COMMITTING` to
`COMPLETED`, the worktree and temporary branch are removed, and the project
lock is released.

## REJECT

`REJECT` with a corrective verdict records a rejected integration attempt and
leaves the reviewed transaction unchanged. No Git reference is moved. An
orchestrator can later choose correction or rollback.

## BLOCK_HUMAN

`BLOCK_HUMAN/HUMAN` moves the run to `WAITING_HUMAN`, records
`recovery_decision=BLOCK_HUMAN`, creates a pending approval, and preserves the
project lock and worktree. Transaction rollback supports this state and
cancels unresolved approvals.

## Fail-closed guarantees

- main must still equal the transaction base;
- the worktree must still equal the reviewed result and be clean;
- the review must be the completed independent read-only review for that exact
  result commit;
- snapshot hashes and bundle are reverified immediately before action;
- integration is `git merge --ff-only`;
- no push or remote operation is performed.
