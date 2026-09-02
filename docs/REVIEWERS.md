# Independent reviewers

Orchestra reviewers are separate from workers and operate only after a run
enters `REVIEWING`.

The Controller creates an audited DIND sandbox before Hermes starts. The
transaction clone is mounted at `/workspace` as read-only, with no remotes and
no network access. The reviewer may copy content into the sandbox `/tmp` for
isolated checks, but cannot modify the transaction branch.

Every successful review records:

- high-level decision: `APPROVE`, `REJECT`, or `BLOCK_HUMAN`;
- database verdict compatible with the original control-plane taxonomy;
- structured findings and checks;
- sandbox audit evidence;
- proof that repository references, worktree HEAD, clone HEAD, and clean state
  remained unchanged.

Decision mapping:

| Decision | Stored verdicts |
| --- | --- |
| `APPROVE` | `PASS`, `PASS_WITH_DEBT` |
| `REJECT` | `FIX`, `SECURITY`, `PERFORMANCE`, `ARCHITECTURE` |
| `BLOCK_HUMAN` | `HUMAN` |

A worker cannot review its own result because reviewer launches require a role
whose kind is `reviewer` and whose workspace mode is `read_only`.


## Heartbeat and schema compatibility

The reviewer uses the same liveness contract as the validated controlled
worker: `runs.heartbeat_at`, `project_locks.heartbeat_at`, and
`tasks.heartbeat_at`. It never assumes an unsupported `runs.updated_at`
column. The live Controller schema is checked before task reservation.


## Regression validation

The inherited controlled-worker foundation test is schema-version agnostic.
This allows later migrations to coexist with the already validated 3B worker
without weakening any worker isolation or execution checks.

## Durable reviewer assignments

Before every controlled reviewer transport attempt, the orchestrator creates a
`reviewer_assignments` row. The reviewer claims it atomically with its task and
execution reservation. A successful verdict completes it; a bounded execution
or transport failure marks it failed; Recovery closes abandoned active rows.
Historical executions created before migration 019 are not backfilled.
