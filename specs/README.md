# Orchestra Machine-Readable Contracts

This directory contains the maintained machine-readable contracts for the
current Orchestra architecture.

- `controller-api-v1.openapi.json`: OpenAPI 3.1 Controller HTTP contract.
- `controller-events-v1.asyncapi.json`: authenticated replayable WebSocket
  transport contract.
- `events-v1.schema.json`: JSON Schema for persisted event envelopes.
- `blueprint-v1.schema.json`: executable source contract for Orchestra
  Blueprint v1 sandbox profiles.

They are validated by:

```text
tests/test-controller-contracts.sh
```

The schemas define maintained interfaces. Runtime-support claims additionally
depend on the corresponding integration and security tests.
