# Multi-Agent Console — v0.2.0 milestone 0.2-H completion report

## Scope

Milestone 0.2-H turns the existing Console foundation into the bounded operational
surface for Orchestra's multi-agent control plane. The browser remains an
unprivileged same-origin API client: it consumes Controller projections and the
Controller event stream through the Console service, without direct SQLite,
Docker-socket, runtime, prompt/output-file, or provider-secret access.

The milestone deliberately does **not** add a second orchestration authority,
direct database administration, arbitrary Controller proxying, raw evidence or
runtime payload exposure, browser-side durable state, or privileged host controls.

## Delivered contract

- Controller-backed `/executions` view for plans, tasks, dependencies and attempts;
- Controller-backed `/reviews` view for Reviewer/Judge results, redacted evidence
  metadata, reviewer assignments and Recovery decisions/outcomes;
- replayable `/events` WebSocket view with bounded in-memory display, topic
  selection, reconnect backoff and explicit replay-gap handling;
- bounded read-only `/administration` diagnostics backed only by exact Controller
  system health/status projections;
- strict same-origin Console proxy allowlists for the required dynamic read routes;
- deterministic committed `console/dist` generated from `console/src`;
- explicit first-page/truncation reporting rather than inference about unseen data;
- browser rendering through safe DOM operations without `innerHTML`, `eval`, local
  storage, session storage or IndexedDB;
- replay reconciliation serialized as
  `replay_unavailable -> complete HTTP snapshot -> cursor advance -> reconnect`;
- incomplete reconciliation snapshots keep the event stream disconnected;
- invalid replay metadata fails closed and blocks automatic/manual reconnect until
  the session is reloaded.

## Security and authority boundary

The Console remains outside the orchestration authority. The Controller is the
source of public operational projections; the browser does not read Orchestra's
SQLite database, `/run/orchestra-docker`, `/var/run/docker.sock`, prompt/output
paths, raw review evidence, `result_json`, or failure payloads. Dynamic proxy routes
validate exact resource identifiers and reject unsupported methods, queries and
suffixes before reaching the upstream Controller.

The event transport is similarly bounded to the existing authenticated Controller
`/api/v1/events` contract. Event messages are retained only in bounded in-memory UI
state. A replay gap no longer advances the local cursor merely because the
Controller reports a latest sequence: the Console first completes every required
HTTP dashboard projection, advances to the reported sequence only after that
snapshot succeeds, and only then reconnects. A failed or incomplete snapshot leaves
the stream degraded and disconnected.

## Milestone commit history before final publication preparation

| Commit | Tree | Subject |
| --- | --- | --- |
| `eeb439ce119f7116801984621d6a93c2d4ba15a6` | `fe28ed4ac7387ec294cbab24d71458206968c809` | `feat: activate multi-agent execution console` |
| `ade01486187f7902fe4eb0155480b7a32a9b7dd5` | `e42ecc1505227aea8b98e6003fb1cad033a968d0` | `feat: activate reviewer recovery console` |
| `161d2148f5fdcdf93fb71d6c291a0343181200f1` | `f259eac492b2864ad5c238d390194e064fc91e7b` | `feat: activate replayable event console` |
| `9b12e171d658cf47bc5732efa1b85b441356815c` | `2fd74375d58e5f3858e573a0118b22d18de0e41e` | `feat: activate bounded administration console` |
| `6a869f0005f76903f8b26160b04538c379643e87` | `958384fa0d04ba6362dd56cef916729aa7f7464e` | `fix: serialize event replay reconciliation` |

The final publication-preparation documentation commit/tree is reported in the
completion response; a commit cannot include its own content-derived identity.

## Validation and adversarial review

The final code state was revalidated after the replay reconciliation fix and its
secondary fail-closed hardening:

- Console regression suite: **106 tests PASS**;
- focused Event Stream suite: **7 tests PASS**;
- deterministic Console distribution build/check: **PASS**;
- JavaScript syntax checks for source and distribution: **PASS**;
- static validation: **PASS**;
- migration sequence through schema 31: **PASS**;
- secret scan: **PASS**;
- milestone range and working-tree `git diff --check`: **PASS**;
- final adversarial review: **0 critical, 0 major, 0 blocking medium findings**.

The adversarial review found one replay-reconciliation defect before closure: on
`replay_unavailable`, the Console could move its cursor to `latest_sequence` before
proving that the HTTP snapshot used to replace the missing event range had
completed. That could silently skip an unobserved interval. The final fix introduces
an explicit reconciliation state that blocks automatic and manual reconnects,
requires every HTTP snapshot projection to succeed, advances the cursor only after
success, and then reconnects. Review of that fix found one secondary malformed
`latest_sequence` path that could still enter normal reconnect backoff; it was also
closed fail-closed before the final validation run.

No live external provider or privileged Docker end-to-end claim is made by this
report. The milestone validates the Console/Controller contracts and deterministic
test transports present in the repository.

## Changed surface

Relative to the merged 0.2-G base, the milestone changes the Console source and
deterministic distribution, the Console service/proxy, Console documentation and
focused Console tests. It introduces no database migration; the schema remains 31.

## Intentional deferrals

No arbitrary administration shell, database editor, Docker control surface,
provider-secret management UI, raw prompt/output browser, direct runtime control,
or alternate browser persistence layer is introduced. Future Console capabilities
must continue to cross the Controller boundary explicitly rather than bypass it.

## Acceptance markers

```text
MULTI_AGENT_EXECUTION_CONSOLE=PASS
REVIEWER_JUDGE_RECOVERY_CONSOLE=PASS
REPLAYABLE_EVENT_CONSOLE=PASS
BOUNDED_ADMINISTRATION_CONSOLE=PASS
CONSOLE_CONTROLLER_ONLY_AUTHORITY=YES
DIRECT_SQLITE_BROWSER_ACCESS=NO
HOST_DOCKER_SOCKET_BROWSER_ACCESS=NO
RAW_REVIEW_EVIDENCE_EXPOSED=NO
RAW_RUNTIME_PAYLOAD_EXPOSED=NO
BROWSER_DURABLE_STATE_INTRODUCED=NO
EVENT_STREAM_BOUNDED_IN_MEMORY=YES
EVENT_REPLAY_RECONCILIATION_SERIALIZED=YES
EVENT_CURSOR_ADVANCE_REQUIRES_HTTP_SNAPSHOT=YES
EVENT_INVALID_REPLAY_FAILS_CLOSED=YES
CONSOLE_DIST_REPRODUCIBLE=YES
CONSOLE_TEST_SUITE=PASS
CONSOLE_TEST_COUNT=106
EVENT_STREAM_TEST_COUNT=7
STATIC_VALIDATION=PASS
MIGRATIONS_1_TO_31=PASS
SECRET_SCAN=PASS
DIFF_CHECK=PASS
ADVERSARIAL_BLOCKING_FINDINGS=0
DB_SCHEMA_VERSION=31
CONTROL_PLANE_PRIVILEGE_INCREASE=NO
ORCHESTRA_V020_H_MULTI_AGENT_CONSOLE_READY
```
