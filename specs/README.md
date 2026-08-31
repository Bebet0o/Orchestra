# HermesOps Machine-Readable Contracts

This directory contains design contracts for the long-term `v0.2.0-beta`
architecture.

- `controller-api-v1.openapi.json`: OpenAPI 3.1 Controller HTTP contract.
- `events-v1.schema.json`: JSON Schema for persisted event envelopes.
- `hermesfile-v0.schema.json`: JSON Schema for parsed Hermesfile v0 data.

They are validated by:

```text
tests/test-controller-contracts.sh
```

The schemas are not proof that the runtime exists. Implementation milestones
must claim support only after corresponding integration and security tests pass.

- `controller-events-v1.asyncapi.json`: authenticated replayable WebSocket transport.
- `blueprint-v1.schema.json`: executable source contract for Orchestra Blueprint v1 sandbox profiles.
