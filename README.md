# Orchestra

Orchestra is a local-first orchestration and control-plane project for durable,
multi-agent software work. It persists projects and objectives, coordinates
bounded planner, worker, reviewer, and recovery roles, and runs worker tasks in
verified environments rather than treating a chat session as the source of
truth.

Orchestra is under active development. Its control-plane, Blueprint, runtime,
Console, persistence, and worker-distribution foundations are implemented and
tested, but it is not yet a production-ready autonomous agent platform.

## What is Orchestra?

Orchestra separates durable orchestration policy from the agent or model used
for one execution:

- **Orchestra** owns projects, objectives, task state, review, recovery,
  persistence, and environment policy.
- An **Orchestra Blueprint** is the declarative source for a versioned runtime
  `SandboxProfile`.
- **AgentRuntime** is the boundary for one planner, worker, or reviewer call.
- **HermesRuntime** adapts that boundary to the external
  [Hermes Agent](https://github.com/NousResearch/hermes-agent) project.
- **NativeRuntime** is an explicitly selectable provider-backed execution path;
  Hermes remains the backward-compatible default.
- **ModelProvider** isolates model-generation adapters from Orchestra domain
  state and policy.
- **Worker environments** resolve to immutable, verified OCI artifacts before
  sandbox execution.

Hermes Agent is an upstream integration, not a component owned by Orchestra.
Current Orchestra commands, services, paths, labels, and resources use the
Orchestra namespace; Hermes names remain only at the upstream integration
boundary.

## Why Orchestra?

Long-running software work needs more than a prompt and a model response.
Orchestra is designed around:

- durable project, objective, task, review, and recovery state;
- explicit planner, worker, reviewer, and human authority boundaries;
- dependency-aware task orchestration and bounded concurrency;
- isolated Git workspaces and controlled integration;
- reproducible worker environments;
- fail-closed recovery when state or evidence is ambiguous;
- local and self-hosted operation with replaceable runtime and model adapters.

The goal is increasing autonomy without making lifecycle, security, or artifact
identity implicit.

## Current status

Implemented foundations include:

- a SQLite-backed control plane for projects, objectives, plans, tasks, runs,
  reviews, recovery records, events, operations, and audit data;
- project and objective lifecycle commands, persistent task-DAG execution,
  review gates, human approvals, and restart reconciliation;
- an authenticated loopback-only Console and a Controller HTTP/WebSocket API;
- isolated worker and reviewer execution through a dedicated sandbox engine;
- `AgentRuntime`, `HermesRuntime`, `NativeRuntime`, `ModelProvider`, and an
  OpenAI-compatible provider adapter;
- Blueprint v1 parsing, canonicalization, persistence, immutable revision
  history, API/CLI/Console lifecycle operations, schema migration 22 to 23,
  the Blueprint namespace migration, runtime selection, worker-pool schema 26,
  planner task-graph schema 27, and durable shared-context schema 28;
- a trusted two-phase worker publication process and an accepted default worker
  environment identified by an immutable OCI digest.

Foundation-only or planned work includes richer model routing, stronger judge
workflows, automatic context intelligence, and broader Console operations.
Existing components should not be read as a claim that these capabilities are
complete.

## Architecture

```text
Orchestra Console / CLI
          |
          v
Orchestra Control Plane
  +-- Projects / Objectives / task DAG
  +-- SharedContextStore --> ContextProjector --> immutable ContextSnapshot
  +-- Blueprint and SandboxProfile lifecycle
  +-- Review / recovery / human approval
  +-- durable SQLite state and Git isolation
  |
  +-- AgentRuntime
  |     +-- HermesRuntime --> Hermes Agent
  |     +-- NativeRuntime --> ModelProvider
  |
  +-- EnvironmentSpec
        --> EnvironmentResolver
        --> ResolvedEnvironment
        --> SandboxBackend.materialize()
        --> PreparedEnvironment
```

For a published environment, the authority is an immutable OCI reference of
the form `repository@sha256:digest`. A Docker image configuration ID is only
daemon-local evidence and is never substituted for cross-host artifact
identity. Worker and reviewer preparation share the same materialization and
verification path.

See [Architecture](docs/ARCHITECTURE.md),
[Agent runtime](docs/AGENT_RUNTIME.md),
[shared project context](docs/SHARED_CONTEXT.md), and
[worker distribution](docs/distribution/WORKER_IMAGE.md).

## Orchestra Blueprint

A Blueprint declares the source inputs and policy for a runtime sandbox
profile: base image, packages, workspace mode, resource limits, networking,
mounts, and validation commands. The conventional filename is `Blueprint`, and
the current source format is `blueprint-v1`.

Blueprint lifecycle operations produce and version the existing
`SandboxProfile` identity; they do not introduce a separate `blueprint_id`.
Source bytes and canonical bytes have distinct fingerprints, while equivalent
normalized content shares a canonical identity.

The current implementation can validate, fingerprint, and canonicalize a
Blueprint:

```bash
scripts/orchestra-blueprint.py validate config/examples/Blueprint
scripts/orchestra-blueprint.py fingerprint config/examples/Blueprint --json
scripts/orchestra-blueprint.py canonicalize config/examples/Blueprint
```

References:

- [Blueprint v1 specification](docs/blueprint/SPECIFICATION_V1.md)
- [Blueprint v1 JSON Schema](specs/blueprint-v1.schema.json)
- [Example Blueprint](config/examples/Blueprint)

## Getting started

The public installer uses Docker Compose on Debian 12+ or Ubuntu 22.04+ on amd64.
It creates the host data directory at `/opt/orchestra/data`, downloads the
canonical Compose and accepted release-manifest assets, and starts the exact
immutable application and runtime image references recorded by that manifest.
It does not clone this repository, build an application image, or install
Python on the host. No application systemd units, user DBus session, or
lingering are used.

The default worker is the already accepted immutable OCI artifact recorded in
`config/environments/default-worker.toml`. Installation pulls that exact
`repository@sha256:digest` into Orchestra's private sandbox Docker daemon and
verifies its exact RepoDigest; it does not build, import, or select a local
worker tag.

The canonical self-hosted contract contains two services: the unprivileged
`orchestra` control plane and the privileged `orchestra-runtime` private
container authority. Neither service mounts the host Docker socket. Both use
the same persistent container data root, `/var/lib/orchestra`.

At release, a normal installation starts with the standalone installer:

```bash
curl --fail --location --output install.sh \
  https://github.com/Bebet0o/Orchestra/releases/download/v0.1.0/install.sh
chmod 0755 install.sh
./install.sh
```

The release images and assets do not exist until the separate trusted
publication gate completes. During development, explicit image and Compose
overrides are available. Authentication may be deferred, but objectives that
need Hermes Agent remain unavailable until provider authentication is
configured. See [public installation](docs/PUBLIC_INSTALLATION.md), the
[appliance architecture](docs/distribution/APPLIANCE.md), and the
[release manifest contract](docs/distribution/RELEASE_MANIFEST.md).

For repository development, clone the project and run the validation commands
below. Python tests depend on the modules available on the supported host/CI
environment; static CI installs `python3-yaml`, `rsync`, `sqlite3`, and
`util-linux` on Ubuntu 24.04.

## Validation and tests

Run the repository validation entry point:

```bash
./validate.sh
```

For the CI-equivalent static checks:

```bash
PYTHONDONTWRITEBYTECODE=1 ./validate.sh --static --quiet
```

The full Python unit suite is:

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD" \
  python3 -m unittest discover -s tests -p 'test_*.py'
```

## Repository layout

| Path | Purpose |
| --- | --- |
| `controller_api/` | Controller reads, commands, lifecycle services, and transport boundaries |
| `scripts/` | orchestration services, CLIs, runtimes, providers, environment resolution, and sandbox preparation |
| `migrations/` | forward-only SQLite schema history |
| `console/src/` | Console source assets |
| `console/dist/` | deterministic committed Console distribution |
| `config/` | examples, environment distribution data, roles, and policies |
| `specs/` | machine-readable API, event, and Blueprint contracts |
| `docs/` | architecture, API, operational, historical, and milestone documentation |
| `tests/` | unit, integration, contract, migration, and adversarial tests |
| `.github/workflows/` | CI and manually dispatched trusted worker publication workflows |

## Security

Orchestra handles repositories, credentials, model boundaries, Docker images,
and persistent control-plane state. Review [SECURITY.md](SECURITY.md) before
deploying or reporting a vulnerability. Do not expose the host Docker socket to
workers or publish sensitive vulnerability details in a public issue.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development, migration, generated
asset, validation, and pull-request guidance.

## Roadmap

The product roadmap is maintained in [ROADMAP.md](ROADMAP.md).

Completed distribution and Blueprint foundations are recorded in
[CHANGELOG.md](CHANGELOG.md). Historical milestone documents remain in the
repository as an accurate record and are not current product authority.

## License

Orchestra is licensed under the [Apache License 2.0](LICENSE).
