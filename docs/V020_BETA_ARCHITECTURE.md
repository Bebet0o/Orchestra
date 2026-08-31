# v0.2.0-beta Architecture Contracts

The `v0.2.0-beta` line is a long-term product milestone with no committed
release date.

Milestone 2A defines the first implementation contracts before the Controller
API, HermesOps Console, and Hermesfile builder are developed.

## Start here

- [Milestone 2A scope and acceptance](milestones/2A_CONTROLLER_CONTRACTS.md)
- [Controller and Console trust boundary](architecture/CONTROLLER_CONSOLE_BOUNDARY.md)
- [Controller internal components](architecture/CONTROLLER_COMPONENTS.md)
- [Controller API v1](api/CONTROLLER_API_V1.md)
- [Replayable events v1](api/EVENTS_V1.md)
- [Current Orchestra Blueprint v1](blueprint/SPECIFICATION_V1.md)
- [Historical Hermesfile v0 design](hermesfile/SPECIFICATION_V0.md)
- [Console information architecture](console/INFORMATION_ARCHITECTURE.md)
- [Console foundation](console/FOUNDATION.md)
- [Console browser session and Controller client](console/CONTROLLER_CLIENT.md)
- [Milestone 2Q scope](milestones/2Q_BROWSER_SESSION_CONTROLLER_CLIENT.md)

## Machine-readable contracts

- [Controller OpenAPI 3.1 design contract](../specs/controller-api-v1.openapi.json)
- [Event envelope JSON Schema](../specs/events-v1.schema.json)
- [Current Orchestra Blueprint v1 JSON Schema](../specs/blueprint-v1.schema.json)

These files are design contracts. They do not imply that the corresponding
runtime features are already implemented.

## Architecture decisions

- [ADR 0001 — Controller owns control-plane writes](adr/0001-controller-owns-control-plane-writes.md)
- [ADR 0002 — Console is an unprivileged API client](adr/0002-console-is-an-unprivileged-api-client.md)
- [ADR 0003 — Hermes Agent is behind an adapter](adr/0003-hermes-agent-is-behind-an-adapter.md)
- [ADR 0004 — Hermesfiles compile to immutable images](adr/0004-hermesfiles-compile-to-immutable-images.md)
- [ADR 0005 — Controller events are replayable](adr/0005-controller-events-are-replayable.md)
- [ADR 0006 — Dangerous actions require confirmation](adr/0006-dangerous-actions-require-confirmation.md)

## Contract update rule

A change affecting API behavior, event semantics, Hermesfile fields, trust
boundaries, or dangerous-action policy must update:

1. the human-readable contract;
2. the machine-readable schema when applicable;
3. the relevant ADR or a new ADR;
4. `tests/test-controller-contracts.sh`;
5. the changelog.

The current release remains `v0.1.0-alpha`. These contracts guide future
development and do not change the released runtime.
