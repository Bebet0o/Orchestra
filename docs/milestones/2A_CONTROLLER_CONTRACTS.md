# Milestone 2A — Controller, Console, and Hermesfile Contracts

Status: **Design baseline**
Branch: `milestone/2a-controller-contract`
Target line: `v0.2.0-beta`
Runtime changes: **Out of scope**

## Purpose

Milestone 2A freezes the first public contracts for the next HermesOps
architecture before a new controller service or WebUI is implemented.

The milestone defines:

- the boundary between HermesOps Console and HermesOps Controller;
- the boundary between HermesOps Controller and Hermes Agent;
- the boundary between HermesOps Controller and the sandbox engine;
- the first HTTP API contract;
- the first replayable event-stream contract;
- the first declarative Hermesfile contract;
- the information architecture of HermesOps Console;
- the decisions that future implementation milestones must preserve.

The contracts are intentionally conservative. They favor recoverability,
auditability, explicit state transitions, and local-first operation over
premature distributed-system features.

## Deliverables

Human-readable contracts:

- [`../architecture/CONTROLLER_CONSOLE_BOUNDARY.md`](../architecture/CONTROLLER_CONSOLE_BOUNDARY.md)
- [`../architecture/CONTROLLER_COMPONENTS.md`](../architecture/CONTROLLER_COMPONENTS.md)
- [`../api/CONTROLLER_API_V1.md`](../api/CONTROLLER_API_V1.md)
- [`../api/EVENTS_V1.md`](../api/EVENTS_V1.md)
- Historical Hermesfile v0 specification (retained in Git history)
- [`../console/INFORMATION_ARCHITECTURE.md`](../console/INFORMATION_ARCHITECTURE.md)

Machine-readable contracts:

- [`../../specs/controller-api-v1.openapi.json`](../../specs/controller-api-v1.openapi.json)
- [`../../specs/events-v1.schema.json`](../../specs/events-v1.schema.json)
- Historical Hermesfile v0 schema (retained in Git history)

Accepted architecture decisions:

- [`../adr/0001-controller-owns-control-plane-writes.md`](../adr/0001-controller-owns-control-plane-writes.md)
- [`../adr/0002-console-is-an-unprivileged-api-client.md`](../adr/0002-console-is-an-unprivileged-api-client.md)
- [`../adr/0003-hermes-agent-is-behind-an-adapter.md`](../adr/0003-hermes-agent-is-behind-an-adapter.md)
- [`../adr/0004-hermesfiles-compile-to-immutable-images.md`](../adr/0004-hermesfiles-compile-to-immutable-images.md)
- [`../adr/0005-controller-events-are-replayable.md`](../adr/0005-controller-events-are-replayable.md)
- [`../adr/0006-dangerous-actions-require-confirmation.md`](../adr/0006-dangerous-actions-require-confirmation.md)

## Product boundary

HermesOps remains an independent layer around Hermes Agent.

```text
HermesOps Console
        ↓ authenticated HTTP / WebSocket
HermesOps Controller
        ├── durable state and audit log
        ├── orchestration and review
        ├── recovery and confirmation gates
        ├── Hermes Agent adapter
        └── sandbox manager
                ↓
        dedicated sandbox engine
                ↓
        immutable images and temporary containers
```

Hermes Agent remains the upstream AI execution engine. It is not the system of
record for projects, objectives, tasks, runs, reviews, recovery decisions,
sandbox profiles, or audit records.

## Scope

This milestone includes:

- resource and command names;
- state ownership;
- trust boundaries;
- API envelopes and error format;
- idempotency and optimistic concurrency rules;
- human-confirmation rules;
- event ordering and replay semantics;
- sandbox-profile source format;
- WebUI routes and primary operator workflows;
- compatibility expectations for future migrations.

## Explicitly out of scope

This milestone does not add:

- a running Controller HTTP service;
- a new database migration;
- a WebUI application;
- a Hermesfile builder;
- automatic image pulling;
- changes to the worker runtime;
- changes to Hermes Agent;
- public multi-user authentication;
- distributed controller replicas;
- remote worker nodes.

Those implementation tasks start only after this contract milestone is
accepted.

## Acceptance criteria

Milestone 2A is complete when:

1. all listed documents and schemas exist;
2. all machine-readable files parse successfully;
3. every mutating API operation requires an idempotency key;
4. dangerous commands can return a confirmation requirement;
5. the Console has no direct database, Hermes Agent, or Docker connection;
6. Hermes Agent is accessed only through a Controller adapter;
7. the event stream has stable event IDs, persisted sequence numbers, and
   replay semantics;
8. the Hermesfile schema rejects privileged mode, arbitrary host mounts, and unsupported secret eligibility;
9. base images require immutable SHA-256 digests;
10. the repository's static validation runs the contract regression test;
11. no runtime behavior changes as part of this milestone;
12. the branch remains clean and all existing runtime validation still passes;
13. HTTP documentation and OpenAPI expose exactly the same endpoint surface;
14. WebSocket transport has a machine-readable AsyncAPI contract;
15. the API preserves current multi-project objective and numeric-priority semantics;
16. forward-compatible response fields and event types do not contradict the schemas.

## Compatibility rules

- The API namespace is `/api/v1`.
- Design-time breaking changes are allowed while the target remains
  unreleased, but changes must update the human-readable and machine-readable
  contracts in the same commit.
- Once `v0.2.0-beta` is released, incompatible API changes require a new major
  API namespace.
- Hermesfile v0 remains experimental until a builder executes it successfully
  in a later milestone.
- Unknown fields are rejected in Hermesfile v0 to prevent silent security
  policy drift.
- Event consumers must preserve unknown well-formed event types but must never
  ignore a schema-version mismatch.

## Next milestone

The planned successor is:

```text
Milestone 2B — Controller Core Skeleton
```

It should create the first installable `hermesops-controller` service with:

- a health endpoint;
- read-only status and project queries;
- the standard problem-details envelope;
- request IDs;
- an append-only event journal;
- no write commands until persistence and idempotency are tested.
