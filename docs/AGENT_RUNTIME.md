# Agent runtime boundary

Orchestra separates control-plane decisions from execution of a bounded AI
role. Planner, worker, and reviewer code create a runtime-neutral
`RuntimeRequest` and invoke the `AgentRuntime` contract. The returned text is
input to the existing domain validation; it is never treated
as authoritative lifecycle state by the runtime.

## Responsibilities

The control plane continues to own objectives, scheduling, task DAGs, SQLite
state, approvals, retries, recovery decisions, reviewer verdict policy, Git
transactions and integration, project registration, durable transcripts, and
sandbox policy. Worker and reviewer create and audit a generic sandbox before
execution. They decide its workspace, access mode, network policy, CPU, and
memory without encoding how any runtime discovers it. Plan JSON and reviewer
JSON are parsed and validated by their domain components after execution.

The runtime boundary owns one bounded role invocation: prompt delivery,
logical runtime-configuration selection, backend-specific execution
supervision and timeout behavior, textual output, and a stable failure
classification. A
returned `RuntimeResult` always denotes runtime success and therefore contains
only output text; non-zero process exits are `execution_failed` errors. The
same textual output field is available on a `RuntimeError` so the control plane
can persist partial diagnostics without giving the runtime a durable path. The
failure vocabulary is deliberately small:
unavailable runtime, failed execution, timeout, invalid result, and
cancellation.

The public request carries a typed role, prompt, opaque `runtime_config_id`,
neutral request identifier, timeout, completion marker, optional sandbox
facts, and one optional runtime-event sink. `RuntimeSandboxContext`
contains only an absolute workspace, image identity, CPU and memory limits,
read-only and network policy, the control-plane task identity, and an opaque
sandbox handle, plus the explicit numeric runtime UID/GID. The task identity is
a generic authorization binding, not a runtime discovery protocol. The context
contains no Hermes, Compose, profile, container-name, or discovery-label field.

`RuntimeEvent` is the runtime-fact side of the boundary. Its envelope is
limited to a strict `RuntimeEventKind`, the request and role bindings, and an
aware UTC timestamp. The contract has exactly two event kinds: `STARTED`
reports that runtime pre-invocation validation has completed and backend
invocation is beginning, and `HEARTBEAT` lets the current worker and reviewer
consumers refresh their durable liveness. The
dispatcher rejects a wrong request, wrong role, duplicate `STARTED`, or a
`HEARTBEAT` before `STARTED`. A sink exception becomes a normalized
`execution_failed` error with a stable, controlled message; no detail from the
secondary sink exception is propagated. Normal process/container/profile
cleanup still runs. Events are emitted synchronously during `execute`; none
can be emitted after it returns. They contain no prompt, environment,
credentials, arbitrary payload, task completion, review verdict, or lifecycle
instruction.
The event timestamp is the UTC time reported for the runtime fact and is
informational, not an authority for durable liveness. When an accepted
`HEARTBEAT` is received, worker and reviewer persist their control-plane local
time at reception; a future, stale, or regressing runtime timestamp cannot
move that durable heartbeat.

The runtime has no durable transcript path. `HermesRuntime` captures output in
a private temporary file and returns it; the control plane persists success or
partial failure output to its internally derived journal path using a
no-follow open. This removes arbitrary path and symlink writes from the public
runtime contract and makes `FakeRuntime` and `HermesRuntime` obey the same
output semantics.

Schema 25 stores the selected `runtime_kind` on synchronized roles and snapshots
it onto planner, worker, and reviewer execution rows. Runtime callbacks are
appended to `runtime_events`, while the existing execution row remains the
authority for terminal success or failure (`exit_code`, `failure_reason`, and
`finished_at`). This lets restart/readback recover both runtime identity and
terminal state without retaining an in-memory runtime object.

The existing database columns named `runtime_profile` and
`outer_container_name` remain unchanged for schema and recovery compatibility.
They now contain the same runtime-neutral request identity. That identity is
also the cleanup key consumed by the current adapter; launchers no longer
fabricate Hermes role/profile/container names.

## Implementations

The boundary currently has three implementations:

```text
AgentRuntime
|- HermesRuntime
|- FakeRuntime
`- NativeRuntime -> ModelProvider
```

`HermesRuntime` is the current Hermes integration adapter. It maps the neutral
request to an exact immutable Hermes Agent image and creates the bounded
one-off container directly in Orchestra's private DIND daemon. It preserves
planner, worker, and reviewer limits, sandbox mounts, numeric runtime identity,
and ephemeral profiles without using the host Docker daemon or outer Compose
one-offs. Hermes-specific command construction and process management belong
here. For worker and reviewer requests it passes the opaque handle to the
private Orchestra entrypoint. At adoption, that entrypoint re-inspects and verifies
the exact full container ID, generic owner/task/request bindings, running
state, image identity, `/workspace` source and access mode, effective network
mode and attached networks, CPU/RAM/PID limits, non-privileged mode,
`no-new-privileges`, dropped capabilities, and runtime user. Any mismatch is a
hard refusal; there is no alternate discovery fallback. Hermes discovery
labels are neither created nor understood by worker or reviewer.

Outer containers created by the adapter carry
`orchestra-runtime-container=1` and a matching
`orchestra-runtime-request-id`. Recovery discovery uses positive labels, then
re-inspects identity and checks durable execution bindings. Nested cleanup
requires `orchestra-sandbox=1`, coherent task/request labels, and the durable
SQLite binding. Retired Hermes ownership labels are not accepted. A
Hermes-looking name alone is never ownership and is ignored.

Sandbox names are used only for creation. A collision fails closed; worker and
reviewer never pre-delete the existing name. The worker's before/after Docker
snapshot is only a discovery signal: every cleanup candidate is re-inspected
and must match the generic owner, task, request, image, workspace and mount mode
before removal by its full immutable ID. Outer runtime IDs are captured while
the launched process is live, then re-inspected before stop or removal; there
is no same-name fallback if the original ID disappears.

Cleanup is best-effort across independent process, container, and profile
phases. A primary runtime error is preserved if cleanup also fails; cleanup
failures are recorded as bounded secondary error types. A cleanup failure after
otherwise successful execution is itself `execution_failed`. Invalid Hermes
profile YAML, including a non-mapping document root, is an `invalid_result`
because the adapter cannot construct its completion protocol.

Planner, worker, and reviewer use the shared `RuntimeFailureRecord` projection
for the stable `runtime_error[<kind>]: <message>` journal reason, known exit
code, and partial output. The projection accepts a control-plane-owned output
sink; it does not choose task state, retry, rollback, plan policy, or verdict.
The primary runtime kind, message, exit status, and output are projected before
that sink is called. If the sink raises an ordinary exception, the primary
record is preserved and receives only the fixed safe marker
`transcript_persistence_failed`; no secondary exception detail is persisted.

Timeout and exceptional supervision paths terminate and reap the process while
the private capture file is still open, then recover partial output. The
control plane persists the stable kind in the existing `failure_reason` column
as `runtime_error[<kind>]: <message>`; no provider string and no schema change
is needed.

`FakeRuntime` returns configured deterministic outcomes without invoking a
process. It strictly validates outcome and event types, emits scripted events
with deterministic UTC timestamps through the same binding/order dispatcher,
and uses the same request, sandbox handle, output, completion, and
normalized-error contract. Tests use it to exercise success, failure, timeout,
invalid output, cancellation, worker/reviewer heartbeat consumption, real
worker Git checks, and real reviewer immutability checks at the runtime seam.

`NativeRuntime` is the first native execution backend. One instance receives
exactly one explicitly injected `ModelProvider` and one fixed opaque model ID.
For every role it mechanically maps the runtime prompt to one `USER`
`ModelMessage`, preserves the request timeout exactly, validates the resulting
`ModelRequest`, emits one request/role-bound `STARTED` fact, invokes
`ModelProvider.generate` synchronously once, and maps the complete
`ModelResult` text to `RuntimeResult`. It does not add a
system prompt, select a model from the role, inspect sandbox or lifecycle
metadata, inspect the completion marker, validate planner/worker/reviewer
payloads, decide task completion, retry, fall back, or construct a provider.
Empty and non-JSON model output remain successful runtime output for the
control plane to interpret later. Completion-marker handling is specific to
`HermesRuntime`; business completion remains a control-plane decision.

The model-provider timeout contract is limited to 600 seconds. A runtime
request above that limit cannot be represented exactly and therefore fails
closed before `STARTED` and before provider invocation; the timeout is never
clamped or coerced. Provider failures are mapped to stable runtime kinds and
controlled messages without retaining or copying provider exception data.
Malformed provider results fail as `invalid_result`.

The provider-to-runtime failure mapping is explicit:

| Model provider kind | Runtime kind |
|---|---|
| `unavailable` | `runtime_unavailable` |
| `timeout` | `timeout` |
| `request_rejected` | `execution_failed` |
| `invalid_response` | `invalid_result` |
| `provider_failed` | `execution_failed` |

An unexpected provider exception is also `execution_failed`; its type and
message are neither retained nor copied. Provider failures have no fabricated
partial output or process exit status.

NativeRuntime emits no synthetic `HEARTBEAT` while synchronous
`ModelProvider.generate` is blocked and creates no thread or background task.
The current `RuntimeRequest` has no cancellation primitive, so NativeRuntime
does not claim to interrupt an in-flight synchronous provider call. Advanced
liveness and cancellation are future contract work.

## Runtime selection

Runtime selection uses the existing role/profile configuration. Each role may
contain exactly one bounded selector:

```toml
runtime = { kind = "hermes" }
```

The accepted values are `hermes` and `native`. Omitting `runtime` preserves the
v0.1 behavior and selects Hermes; invalid values and unknown runtime fields are
rejected. Role synchronization persists the selected kind and fixed existing
top-level model ID. Every launch resolves its role snapshot and calls the same
runtime factory before executing the same `RuntimeRequest` flow.

For NativeRuntime, the factory constructs the existing OpenAI-compatible
provider from `ORCHESTRA_NATIVE_ENDPOINT_URL` and the optional
`ORCHESTRA_NATIVE_API_KEY`. The endpoint is required only when a native role is
selected. Tests inject `FakeModelProvider` through the provider abstraction;
no controller path switches on a concrete provider implementation. There is no
model router, fallback, retry, worker pool, or multi-model policy here.

`AgentRuntime` is the application boundary for an AI task. `HermesRuntime`
executes it through external Hermes Agent, while `NativeRuntime` executes it
directly through `ModelProvider`. The similarly named `orchestra-runtime`
container is different: it is the sole privileged infrastructure service for
the private sandbox engine. NativeRuntime selection does not grant privileges,
mount the host Docker socket, or replace that service.

## Future direction

Native worker pooling, richer liveness and cancellation, recovery loops, and
model routing remain later roadmap work.
