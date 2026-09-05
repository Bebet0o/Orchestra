# Orchestra 0.2-E milestone report

Branch: `milestone/0.2e-reviewer-judge`.
Base: `e9d1f1fbbab114f0a49044440d8df7dce059921a`, verified against fetched
`origin/main` before branching from a clean worktree.

## Implemented behavior

The previous reviewer already persisted read-only Git-transaction execution and
legacy `APPROVE/REJECT/BLOCK_HUMAN` evidence. The missing boundary was task-graph
acceptance with historical worker-context provenance, bounded structured findings,
and a separate durable Judge decision.

Required PIPELINE tasks now stop after worker submission and release worker-pool
capacity. One structured review reuses the existing reviewer assignment,
AgentRuntime and sandbox execution path; it bypasses legacy review retries.
No-review policy remains the default and preserves the existing pipeline.

Review subjects bind project, objective, plan, task, worker assignment, attempt,
runtime execution, exact worker snapshot, and result reference/hash where those
identities exist. Separate immutable reviewer snapshots contain schema-1 input,
source IDs and the worker evidence references/hashes. The runtime prompt resolves
these authorities to the actual historical bytes. It never substitutes current
shared context or unrelated runtime history.

Structured output has schema version, bounded assessment, summary, findings
(code/severity/message/evidence) and required changes. Limits: 64 KiB output,
32 findings, 16 evidence references per finding, 32 changes, 4,000-byte summaries
and finding messages, 2,000-byte changes, and 64-character finding codes.
Duplicate keys/codes/frames, unknown fields and out-of-scope references fail closed.
Validated canonical JSON and SHA-256 are immutable.

Judge stores one separately identified decision with the review hash, reason and
timestamp. `PASS` permits existing guarded integration; graph acceptance follows
only a completed run. `NEEDS_FIX` retains actionable findings and blocks downstream
work without retry. `BLOCKED` means evaluation cannot safely proceed and is
distinct from needed corrections. `HUMAN_REVIEW` creates an existing `approvals`
gate; the existing bounded CLI resolves it. Human approval authorizes the same
reviewed result without rewriting the Judge's original disposition.

SQLite transactional claims prevent duplicate review dispatch, decisions and
acceptance. An additional one-shot integration claim prevents integration retry
loops. Pending reviews survive restart; running reviews become interrupted, with
existing ownership-checked cleanup and assignment failure; completed evidence is
not recreated. A completed run can finish graph acceptance after restart. Held
review states are excluded from the legacy automatic recovery sweep.

In the diamond, B and C own concurrent worker slots. D stays pending after B PASS
alone; both accepted branches release D exactly once. With B NEEDS_FIX and C PASS,
B's result remains durable, C stays accepted, D stays pending and B is not retried.
The graph/objective remains nonterminal and suitable for future recovery.

Internal query surfaces expose pending subjects, worker/reviewer snapshots,
structured findings, hashes/schema, Judge decisions and human state. Full details
and policy examples are in [Reviewer / Judge lifecycle](REVIEWER_JUDGE.md).

## Validation

- Focused Reviewer/Judge coverage: **40 tests**, all passing in the final suite.
  The two final bounded fixes passed **6 directly affected tests** before that suite.
- Shared Context regressions: **10 passed**.
- Task Graph regressions: **10 passed**.
- WorkerPool regressions: **9 passed**.
- AgentRuntime regressions: **107 passed**.
- NativeRuntime regressions: **35 passed**.
- NativeRuntime activation regressions: **7 passed**.
- Existing reviewer-assignment regressions: **10 passed**.
- Combined targeted regressions: **188 passed**.
- Final full unittest suite: **874 passed**, 190.422 seconds, exit status 0.
- Fresh migration sequence **1..29 passed**; schema/user version **29**.
- Foreign-key check: no violations; SQLite quick check: `ok`; integrity check: `ok`.
- Migration test also preserves schema-28 historical reviews/completed tasks and
  creates no fabricated historical task reviews.
- `PYTHONDONTWRITEBYTECODE=1 ./validate.sh --static --quiet`: **PASS**, exit status 0.
- `git diff --check origin/main...HEAD`: **PASS**.
- Milestone-only diff reviewed: **19 intended files**; no accidental artifacts.
- Final worktree: **clean** after the documentation commit.

The final suite was run once after the final code state. Earlier runs exposed
schema expectation updates and the bounded framing/integration-check defects;
those were fixed and directly tested before the final run.

Runtime proofs are deterministic: NativeRuntime executes FakeModelProvider;
HermesRuntime command mapping and the production reviewer persistence path use
controlled process transport. No live provider or Docker end-to-end claim is made.

## Implementation commits and trees

| Commit | Tree | Change |
| --- | --- | --- |
| `828e65cecab14c3e3cae410f88f6a671243591b4` | `706d5e042dc9584a16728cc672410eb33608c448` | Durable structured review, snapshots and Judge authorities |
| `864396635ff84791c1ba5270a204326242ad075e` | `1f912f4d1d6946de3eaff0a0034f74f401d11d69` | Orchestration acceptance, runtime, human gate and recovery exclusion |
| `e62ec30e81e276ad4d8171216a61d11184e86e08` | `1439c880d4b40198960ce21a227536b1dc290ec5` | Focused coverage and schema expectation updates |

The final documentation commit/tree is reported in the completion response;
a commit cannot include its own content-derived identity.

## Changed files

- `migrations/029_reviewer_judge.sql`
- `scripts/reviewer_judge.py`
- `scripts/shared_context.py`
- `scripts/orchestra-orchestrator.py`
- `scripts/orchestra-reviewer.py`
- `scripts/orchestra-integrator.py`
- `scripts/orchestra-control.py`
- `scripts/orchestra-recovery.py`
- `tests/test_reviewer_judge.py`
- `tests/test-controller-blueprint-lifecycle.sh`
- `tests/test_appliance_distribution.py`
- `tests/test_controller_blueprint_lifecycle.py`
- `tests/test_controller_event_journal_adversarial.py`
- `tests/test_controller_project_lifecycle.py`
- `tests/test_sandbox_profiles.py`
- `tests/test_shared_context.py`
- `ROADMAP.md`
- `docs/REVIEWER_JUDGE.md`
- `docs/REVIEWER_JUDGE_REPORT.md`

The existing regression-file edits only update current schema/ledger expectations
from 28 to 29. The milestone diff introduces no image, Compose, UI or publication
changes.

## Intentional deferrals

No automatic repair/retry, alternate worker, replan, fix task, dependency rewrite,
reviewer consensus, dynamic model routing or Console UI. Those remain future
0.2-F/G/H work. No push, merge, tag, release or publication was performed.

## Acceptance markers

```text
REVIEWER_JUDGE_IMPLEMENTED=YES
STRUCTURED_REVIEW_DURABLE=YES
REVIEW_SUBJECT_PROVENANCE_COMPLETE=YES
REVIEWER_USES_WORKER_CONTEXT_SNAPSHOT=YES
REVIEWER_CONTEXT_SNAPSHOT_IMMUTABLE=YES
STRUCTURED_REVIEW_VALIDATED=YES
REVIEW_RESULT_HASH_DETERMINISTIC=YES
JUDGE_DISPOSITION_DURABLE=YES
JUDGE_DECISIONS_BOUNDED=YES
REVIEW_PASS_RELEASES_DOWNSTREAM=YES
REVIEW_NEEDS_FIX_BLOCKS_DOWNSTREAM=YES
REVIEW_NEEDS_FIX_AUTO_RETRY=NO
HUMAN_REVIEW_EXISTING_GATE_INTEGRATION=PASS
REVIEW_BLOCKED_DISTINCT=YES
DUPLICATE_REVIEW_DISPATCH=NO
DUPLICATE_JUDGE_DECISION=NO
REVIEW_RECONCILIATION_IDEMPOTENT=YES
DIAMOND_REVIEW_PASS_FLOW=PASS
DIAMOND_REVIEW_REJECTION_FLOW=PASS
HERMES_RUNTIME_REVIEW_EXECUTION=PASS
NATIVE_RUNTIME_REVIEW_EXECUTION=PASS
FAKE_MODEL_PROVIDER_REVIEW_EXECUTION=PASS
MALFORMED_REVIEW_FAILS_CLOSED=YES
REVIEW_RUNTIME_FAILURE_FAILS_CLOSED=YES
EXISTING_REVIEW_DEFAULT_PRESERVED=YES
PROVIDER_SECRET_PERSISTED_IN_REVIEW=NO
HOST_DOCKER_SOCKET_INTRODUCED=NO
CONTROL_PLANE_PRIVILEGE_INCREASE=NO
DB_SCHEMA_VERSION=29
RECOVERY_LOOP_IMPLEMENTED_EARLY=NO
MODEL_ROUTER_IMPLEMENTED_EARLY=NO
MULTI_AGENT_CONSOLE_IMPLEMENTED_EARLY=NO
FULL_TEST_SUITE=PASS
STATIC_VALIDATION=PASS
DIFF_CHECK=PASS
WORKTREE_CLEAN=YES
V020_RELEASE_CREATED=NO
ORCHESTRA_V020_E_REVIEWER_JUDGE_READY
```
