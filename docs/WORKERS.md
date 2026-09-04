# Controlled worker lifecycle

The terms are deliberately distinct:

- A worker role is durable behavior and runtime configuration.
- A worker-pool assignment is queued or active Orchestra-owned capacity.
- A worker execution is one concrete attempt.
- An `AgentRuntime` performs that execution through HermesRuntime or
  NativeRuntime.
- A sandbox is the dedicated isolated workspace for an execution.
- `orchestra-runtime` is the separate privileged DIND infrastructure service.

For each pool-dispatched pipeline task:

1. The Controller durably queues and claims a worker-pool assignment.
2. The transaction pipeline reserves and binds a concrete attempt.
3. The Controller freezes its immutable task-specific context snapshot.
4. The Controller reserves the run and creates a standalone Git clone.
5. The Controller starts and audits a dedicated DIND sandbox.
6. The selected AgentRuntime receives the structured context and executes.
7. The Controller verifies and imports the resulting commit.
8. The Controller removes the sandbox in `finally` and releases capacity.

Parallel assignments do not share writable clones or sandboxes. The pool does
not mount the host Docker socket and does not change the privilege boundary.
Context is frozen only after an assignment owns a slot, so queued work may see
new eligible project/objective entries without mutating any started attempt.
