# Orchestra

Local-first orchestration platform for durable, multi-agent software projects.

> [!IMPORTANT]
> **Orchestra is the successor to HermesOps.** This repository was created from
> the complete HermesOps Git history, and its initial snapshot is the final
> HermesOps 0.2.0 architecture. Some infrastructure identifiers still use
> `HermesOps` or `hermesops-*` and will migrate in dedicated milestones. The
> declarative sandbox authority has completed its product migration to
> **Orchestra Blueprint**; historical documents and database migrations retain
> the names that shipped in their original releases.

Orchestra is under active development. It already provides a durable control
plane, isolated execution, review and recovery workflows, a dedicated Console,
an agent-runtime boundary, and the first native runtime primitive. It is not
yet a complete autonomous multi-agent system, and `NativeRuntime` is not the
default execution backend.

## Contents

- [Vision](#vision)
- [From HermesOps to Orchestra](#from-hermesops-to-orchestra)
- [Current project state](#current-project-state)
- [Release direction](#release-direction)
- [Architecture](#architecture)
- [Capabilities](#capabilities)
- [Orchestra Blueprint](#orchestra-blueprint)
- [Console and CLI](#console-and-cli)
- [Installation](#installation)
- [Quick start](#quick-start)
- [Runtime model](#runtime-model)
- [Security model](#security-model)
- [Current limitations](#current-limitations)
- [Roadmap](#roadmap)
- [Project history and versioning](#project-history-and-versioning)
- [Documentation](#documentation)
- [License](#license)

## Vision

Orchestra aims to become a local platform that turns a complex software
objective into durable, auditable, isolated work distributed across specialized
agents.

The design emphasizes:

- durable state rather than session-only context;
- explicit objective and task lifecycles;
- deterministic boundaries around probabilistic model output;
- isolated Git and sandbox workspaces;
- independent review before integration;
- bounded recovery when execution is interrupted or evidence is ambiguous;
- multiple model backends behind stable contracts;
- local-first operation and operator-owned data;
- human supervision whenever policy or uncertainty requires it.

This is a direction, not a claim of complete autonomy today.

## From HermesOps to Orchestra

HermesOps began as an orchestration layer built around
[Hermes Agent](https://github.com/NousResearch/hermes-agent). As it evolved, the
project gained:

- durable objectives;
- persistent task DAGs;
- lifecycle management;
- sandboxed workers;
- planner, worker, reviewer, and recovery roles;
- Git-isolated execution and controlled integration;
- human approval gates;
- runtime-neutral execution contracts;
- a model-provider abstraction;
- the first `NativeRuntime`.

The control-plane architecture is therefore no longer tied to one agent
backend. That evolution motivates the new product name: **Orchestra**.

Hermes Agent remains a supported backend and retains its own name. The existing
adapter remains `HermesRuntime`:

```text
AgentRuntime
├── HermesRuntime
│   └── Hermes Agent
└── NativeRuntime
    └── ModelProvider
```

HermesOps does not replace Hermes Agent, and neither does Orchestra. Orchestra
owns the project orchestration product; Hermes Agent remains an execution
backend reached through its adapter.

## Current project state

### Available today

- a persistent SQLite control plane for projects, objectives, plans, tasks,
  runs, reviews, approvals, recovery records, events, and notifications;
- a durable objective queue with planning, priority, bounded concurrency,
  pause, resume, and cancellation at safe boundaries;
- a persistent task-DAG orchestrator with dependency ordering and restart
  reconciliation;
- planner, isolated worker, independent reviewer, integrator, supervisor,
  notifier, and deterministic recovery components;
- project-scoped Git snapshots, branches, worktrees or clones, writer locks,
  review gates, and controlled local integration;
- a dedicated Docker sandbox engine and ephemeral worker/reviewer containers;
- an authenticated, loopback-only Console for operational summaries, project
  lifecycle, Blueprint lifecycle, and bounded objective lifecycle;
- legacy operator CLIs and user-level systemd services;
- `AgentRuntime`, `HermesRuntime`, `NativeRuntime`, and deterministic fake
  implementations used at the test boundary;
- `ModelProvider`, a fake provider, and the minimal
  `OpenAICompatibleProvider` adapter;
- strict Blueprint v1 parsing, validation, canonicalization, fingerprinting,
  persistence, source revision history, and Console editing.

### Being built next

- the technical rename from HermesOps to Orchestra, without breaking current
  installations or history;
- selection and configuration of native runtimes in the control plane;
- a native worker pool, parallel specialized workers, shared context, routing,
  stronger independent judging, dynamic decomposition, retry/replan, and
  multi-agent recovery;
- richer Console views and controls where the current same-origin API boundary
  is deliberately narrow.

## Release direction

The repository's source history contains older release language that remains
visible to validation and installation tooling. The historical README title was
`# HermesOps`, and retained tooling still reports:

```text
Current status: `v0.1.0-alpha` — foundation release
```

That string is a legacy technical marker, not a newly selected Orchestra
release. The certified Orchestra bootstrap instead starts from the final
HermesOps 0.2.0 architecture.

### `v0.2.0-beta` — long-term product milestone (historical label)

This was the historical HermesOps roadmap label under which the Controller,
Console, Hermesfile lifecycle, runtime boundary, model-provider boundary, and
first native runtime were developed. It is not an Orchestra tag.
It has no committed release date.

No `v0.3.0`, `v1.0.0`, or other Orchestra release is declared by this README.

## Architecture

```text
User
 │
 ├── Orchestra Console (current technical service: hermesops-console)
 └── CLI (current commands: hermesops-*)
       │
       ▼
 Orchestra Control Plane
       │
       ├── Projects
       ├── Objectives / tasks / DAG
       ├── Lifecycle and durable SQLite state
       ├── Planner / Worker / Reviewer / Recovery
       ├── Human approval
       ├── Git isolation / integration
       └── AgentRuntime
             ├── HermesRuntime
             │      └── Hermes Agent
             │
             └── NativeRuntime
                    └── ModelProvider
                         └── OpenAI-compatible backend
```

The control plane owns project state, scheduling, lifecycle transitions, retry
policy, approvals, Git operations, persistence, and interpretation of agent
output. `AgentRuntime` owns one bounded role invocation; it does not decide that
an objective is complete or that a change may be integrated.

The current default factory still constructs `HermesRuntime` for planner,
worker, and reviewer execution. `NativeRuntime` exists and is tested as a
runtime primitive, but milestone 2Z does not wire it into those default launch
paths.

The historical Hermes WebUI is separate from the diagram's control-plane
Console: it connects directly to Hermes Agent as a temporary compatibility interface
on port `8787`. The dedicated Console connects through a narrow
same-origin gateway to the Controller and is served on port `8788`.

See [Architecture](docs/ARCHITECTURE.md) and
[Agent runtime boundary](docs/AGENT_RUNTIME.md) for the current contracts.

## Roles

The current logical roles retain their historical identifiers:

| Role | Current responsibility |
| --- | --- |
| `ops-orchestrator` | Plan objectives and produce validated task DAGs. |
| `ops-worker-code` | Make code changes in an isolated writable workspace. |
| `ops-worker-tests` | Implement or run bounded validation work. |
| `ops-worker-docs` | Produce project documentation. |
| `ops-reviewer` | Review independently in a read-only clone with no remotes or network. |
| `ops-recovery` | Support deterministic resume, rollback, or human blocking decisions. |

Roles describe behavior and permissions. They do not replace sandbox policy,
project policy, or the runtime boundary.

## Capabilities

### Durable projects and objectives

Projects can be created from an existing managed repository, initialized, or
cloned through the Console. Operators can edit bounded metadata and then enable,
disable, rescan, or archive a project. Project deletion, automatic push, remote
mutation, and default-branch mutation are not available.

Objectives are durable queue entries. They can move through planning and
running states to completion or failure. Pause and cancel requests take effect
at safe transaction boundaries rather than killing active writes. The Console
supports objective creation, detail, pause, resume, and cancel; the CLI also
supports queue inspection and the same lifecycle controls.

### Planning, DAG execution, review, and recovery

The AI planner emits a strictly validated DAG for enabled projects. The
orchestrator persists dependencies and attempts, enforces global limits and one
writer per project, and reconciles interrupted work after restart.

Pipeline tasks reserve a Git transaction and snapshot, run an isolated worker,
verify its commit, run an independent reviewer, and pass through a controlled
integration gate. Reviewer decisions are `APPROVE`, `REJECT`, or
`BLOCK_HUMAN`, with more specific stored verdicts.

Recovery is Controller-owned and fail-closed. Its bounded decisions are:

```text
RESUME_SAFE
ROLLBACK_SAFE
BLOCK_HUMAN
```

Ambiguous or corrupted evidence preserves the project lock and creates a human
approval instead of guessing.

### Persistence and operations

SQLite in WAL mode is the transactional source of truth, complemented by Git
and verified snapshots. User-level systemd units keep the supervisor,
orchestrator, notifier, Controller API, and Console running across sessions and
reboots when linger is enabled.

The notifier maintains a durable outbox with file delivery and optional
Telegram delivery. Neither the Console, the legacy WebUI, Telegram, nor a model
session is treated as the source of truth.

## Worker image, archive, engine, and containers

The current execution plane retains these historical artifacts:

```text
hermesops-worker-sandbox-0.2.tar.gz
        ↓ imported as
hermesops-worker-sandbox:0.2
        ↓ stored inside
hermesops-sandbox-engine
        ↓ used to create
ephemeral worker and reviewer containers
```

The archive is a distributable copy of the pinned image, not a running worker.
The dedicated engine is separate from the host Docker daemon. One image can
create multiple containers, but current policy and project writer locks bound
safe concurrency.

The matching checksum asset is
`hermesops-worker-sandbox-0.2.tar.gz.sha256`. These names remain unchanged
during the bootstrap.

## Orchestra Blueprint

**Orchestra Blueprint** is the current product name for Orchestra's declarative
sandbox specification. A Blueprint is one strict declarative YAML source for a
`SandboxProfile`; it is not a project definition, objective, task DAG, role
prompt, or orchestration policy. Its executable v1 contract uses:

```yaml
apiVersion: hermesops.dev/v1
kind: SandboxProfile
```

The persisted source format is `blueprint-v1`; the Controller API authority is
`/api/v1/blueprints`, and the Console product route is `/blueprints`, labeled
**Blueprints** in navigation.

The source declares a digest-pinned base image, declarative package inputs,
workspace identity and source mode, resource limits, network policy, mandatory
security invariants, logical mounts, and validation command vectors. It cannot
contain arbitrary host mounts, shell pass-through, secret values, privileged
mode, added capabilities, Docker socket access, or device access.

The current tree can strictly parse, validate, canonicalize and fingerprint
Blueprint v1 sources:

```bash
scripts/hermesops-blueprint.py validate config/examples/Blueprint
scripts/hermesops-blueprint.py fingerprint config/examples/Blueprint --json
scripts/hermesops-blueprint.py canonicalize config/examples/Blueprint
```

Operators can import a valid source into durable sandbox-profile storage:

```bash
scripts/hermesops-sandbox-profile.py import config/examples/Blueprint
scripts/hermesops-sandbox-profile.py list
```

The Console can load the official template, validate without persistence,
create and update a Blueprint with optimistic concurrency, retain immutable
source revisions, preview canonical/runtime projections, inspect history, and
compare canonical paths. A project may reference a persisted sandbox profile;
project identity and objective scheduling remain separate Controller concerns.

Source and canonical SHA-256 values are both retained. Formatting, comments,
mapping order, and equivalent normalized quantities do not change the canonical
fingerprint; array order remains significant.

Image construction, package resolution, validation-container execution,
activation, rollback, secret binding, revision deletion, and profile deletion
are not implemented by the current lifecycle.

See the executable [Blueprint v1 specification](docs/blueprint/SPECIFICATION_V1.md).
The [Hermesfile v0 specification](docs/hermesfile/SPECIFICATION_V0.md) is an
experimental historical design contract, not the current executable format.

## Console and CLI

The dedicated Console is an existing interface, not a future placeholder. It
is an unprivileged, authenticated, loopback-only browser client with these
current product routes:

Historical documentation and service descriptions may still call it the
**HermesOps Console**; the product-facing name is now Orchestra Console.

- `/dashboard`: bounded operational summaries and an attention queue;
- `/projects`: create/import and manage the bounded project lifecycle;
- `/blueprints`: validate, create, edit, version, inspect, and compare sources;
- `/objectives`: create, inspect, pause, resume, and cancel objectives;
- navigation shells for executions, reviews, events, and administration, whose
  richer workflows remain limited or deferred.

The Console does not access SQLite, Docker, workspaces, Hermes Agent, or host
paths directly. It uses an allowlisted same-origin gateway to the loopback
Controller. It has no browser storage or generic API proxy, and it does not yet
offer objective task detail, live polling, human review actions, Blueprint
build/activation, or arbitrary Controller commands.

The legacy CLI remains the broader administration and recovery interface:

```bash
/opt/docker/hermesops/repo/scripts/hermesopsctl --help
```

Legacy `hermesops-*` technical identifiers remain during the transition.

## Installation

### Current supported host contract

The retained public installer currently validates:

- Debian 12 Bookworm on amd64;
- a service user with UID/GID `1000:1000` and `sudo` access;
- Docker Engine and the Docker Compose plugin;
- user-level systemd with linger;
- the fixed installation root `/opt/docker/hermesops`;
- network access unless the required dependencies and worker archive are
  supplied for offline installation.

The installer can install its pinned official Docker packages when Docker is
absent. It refuses to remove conflicting Docker packages automatically.
`auth.json` is optional at install time; without it, infrastructure and the
empty project registry can start, but AI objectives are unavailable until
authentication is configured.

### Install from Orchestra

```bash
git clone https://github.com/Bebet0o/Orchestra.git
cd Orchestra

./preflight.sh
./install.sh --user "$USER"
```

`preflight.sh` is read-only. A successful preflight ends with:

```text
HERMESOPS_PREFLIGHT_PASS
```

The online installer retrieves the retained worker asset from the historical
HermesOps release when the image is not already present. To provide a verified
archive explicitly:

```bash
./install.sh \
  --user "$USER" \
  --worker-image-archive \
  "$HOME/hermesops-worker-sandbox-0.2.tar.gz"
```

A successful installation ends with:

```text
HERMESOPS_INSTALL_PASS
```

If the installer adds the user to the `docker` group, it reports
`RELOGIN_REQUIRED`; end the login session, reconnect, and rerun the same
command. Installation is designed to resume without replacing preserved state.

The technical installation paths, service units, container names, environment
variables, success markers, and release-asset names still use HermesOps. This
is intentional during the bootstrap; no renamed Orchestra services are claimed
here.

### Services and local endpoints

| Component | Current endpoint or unit |
| --- | --- |
| Hermes Agent gateway | `127.0.0.1:8642` |
| Controller API | `127.0.0.1:8765` |
| Legacy Hermes WebUI | `127.0.0.1:8787` |
| Dedicated Console | `127.0.0.1:8788` |
| Durable services | `hermesops-supervisor`, `hermesops-orchestrator`, `hermesops-notifier`, `hermesops-controller-api`, `hermesops-console` |

All published endpoints bind to loopback. Use an operator-managed SSH tunnel or
properly secured reverse proxy for remote access; do not expose them directly.

### Authentication after deferred installation

Place an existing Hermes Agent authentication file at:

```text
/opt/docker/hermesops/state/hermes-home/auth.json
```

Protect it, restart the Agent, and verify the retained role profiles:

```bash
chmod 0600 /opt/docker/hermesops/state/hermes-home/auth.json
docker restart hermesops-agent

HERMESOPS_ROOT=/opt/docker/hermesops \
  /opt/docker/hermesops/repo/scripts/hermesops-roles.py verify-profiles
```

Provider-specific authentication remains the responsibility of Hermes Agent
and the selected provider.

### Conservative uninstall

```bash
./uninstall.sh --user "$USER"
```

The default uninstall stops services and containers while preserving the
installed repository, SQLite state, secrets, project workspaces, project data,
and backups. Review `./uninstall.sh --help` before requesting destructive
removal.

## Quick start

After installation:

1. Tunnel the dedicated Console from the operator workstation:

   ```bash
   ssh -L 8788:127.0.0.1:8788 user@server
   ```

2. Open `http://127.0.0.1:8788`, authenticate, and use `/projects` to create,
   initialize, or clone a managed project. Enable the project when its bounded
   configuration is valid.

3. If the project needs a custom sandbox specification, use `/blueprints` to
   load the current template, validate it, save it, and attach the persisted
   sandbox profile to the project. This manages source and revisions only; it
   does not build or activate an image.

4. Submit an objective from `/objectives`, or use the installed CLI:

   ```bash
   /opt/docker/hermesops/repo/scripts/hermesopsctl submit \
     --project my-project \
     --text "Describe the bounded software objective"
   ```

5. Follow durable state in the Console or CLI:

   ```bash
   /opt/docker/hermesops/repo/scripts/hermesopsctl queue --active
   /opt/docker/hermesops/repo/scripts/hermesopsctl show OBJECTIVE_ID
   /opt/docker/hermesops/repo/scripts/hermesopsctl approvals
   ```

   The supported objective controls are:

   ```bash
   /opt/docker/hermesops/repo/scripts/hermesopsctl pause OBJECTIVE_ID
   /opt/docker/hermesops/repo/scripts/hermesopsctl resume OBJECTIVE_ID
   /opt/docker/hermesops/repo/scripts/hermesopsctl cancel OBJECTIVE_ID
   ```

Objective progress is persisted. Pause and cancellation become effective at a
safe boundary when active transactional work must first settle.

## Runtime model

`AgentRuntime` and `ModelProvider` are separate boundaries.

### AgentRuntime

`AgentRuntime` is the control plane's execution boundary for one bounded
planner, worker, or reviewer invocation. A `RuntimeRequest` carries a typed
role, prompt, opaque runtime configuration identifier, request identity,
timeout, completion marker, optional neutral sandbox facts, and an optional
event sink. Runtime output returns to domain code for validation; the runtime
cannot approve integration or mutate lifecycle state.

`HermesRuntime` adapts that contract to Hermes Agent and preserves the current
Compose/CLI execution, sandbox auditing, timeout, output capture, and cleanup
behavior.

`NativeRuntime` mechanically translates a `RuntimeRequest` into one
`ModelRequest` and translates the result or normalized provider failure back to
the runtime contract.

### ModelProvider

`ModelProvider` is the backend-neutral boundary for one complete model
generation. It does not know about projects, roles, tasks, Git, sandboxes,
review policy, or lifecycle state.

`OpenAICompatibleProvider` is the first minimal concrete adapter. It implements
one non-streaming chat-completions request, strict bounded response parsing,
controlled error normalization, optional HTTPS-only bearer authentication, no
redirect following, and no environment credential discovery.

See [Agent runtime boundary](docs/AGENT_RUNTIME.md) and
[Model provider boundary](docs/MODEL_PROVIDER.md).

### NativeRuntime 2Z limitations

The first native runtime is deliberately small:

- execution is synchronous;
- each instance has one injected provider;
- each instance has one fixed model identifier;
- each request maps to one user message and one provider generation;
- it emits `STARTED` but no synthetic `HEARTBEAT`;
- it has no automatic retry or fallback;
- it has no role-to-model routing or model router;
- it has no worker pool;
- it has no streaming;
- it has no tool or function calling;
- it has no background cancellation primitive for an in-flight synchronous
  provider call;
- it is not wired as the full control-plane default.

Empty or non-JSON model text is still successful runtime output; planner,
worker, and reviewer domain code remains responsible for interpreting and
validating it. `NativeRuntime` is a foundation for the next milestones, not a
native multi-agent pipeline by itself.

## Security model

Orchestra inherits layered safety mechanisms from HermesOps. They reduce risk;
they do not constitute an absolute security guarantee or certification.

- Hermes Agent and workers do not receive the host Docker socket. Sandboxes use
  a dedicated Docker engine with no published engine port.
- Project writes occur in controlled worktrees or standalone clones. The main
  project worktree must be clean, and one writer lock is enforced per project.
- Write transactions create verified snapshots before changes. Worker results
  require a clean committed descendant before review and local integration.
- The independent reviewer receives a read-only clone, no Git remotes, and no
  network; it cannot be the worker that produced the result.
- Ambiguous recovery and policy-sensitive outcomes can stop at human approval.
- Secrets, local environment files, authentication material, SQLite state, and
  project registrations remain outside Git. Secrets are not passed to workers
  or reviewers by default.
- Sandbox identity and cleanup rely on explicit ownership labels, durable task
  bindings, immutable IDs, and re-inspection; a HermesOps-looking name alone is
  not proof of ownership.
- Runtime and provider failures are normalized into bounded error classes.
  Provider exception details, endpoints, credentials, prompts, raw bodies, and
  responses are not copied into normalized error messages.
- Automatic Git push is disabled.

Operators remain responsible for host hardening, network exposure, provider
credentials, project backups, policy review, and evaluating generated changes.
See [Security](docs/SECURITY.md), [Transactions](docs/TRANSACTIONS.md), and
[Recovery](docs/RECOVERY.md).

## Current limitations

- The infrastructure rebrand is incomplete; some interfaces still expose
  HermesOps technical names.
- `HermesRuntime` remains the default planner/worker/reviewer backend.
- `NativeRuntime` is the synchronous 2Z primitive described above, not a native
  worker fleet.
- The Console exposes bounded workflows, not every Controller read or command;
  task detail, rich execution views, human review actions, live WebSocket
  reconciliation, and offline queues are not available there.
- Blueprint source lifecycle exists, but image build, validation-container
  execution, activation, rollback, secret binding, and revision deletion do not.
- The pinned default worker image still comes from a historical HermesOps
  release asset.
- Public installation currently targets Debian 12 amd64 and UID/GID
  `1000:1000`, with the fixed `/opt/docker/hermesops` root.
- AI workflows require a valid Hermes Agent authentication setup.
- Safe concurrency is bounded, and there is still only one active writer per
  project.
- Operators should understand Git, Docker, systemd, credentials, backups, and
  recovery policy before using the system on valuable repositories.

## Roadmap

The next sequence is planned direction, not current functionality:

| Milestone | Direction |
| --- | --- |
| 3A — Native Worker Pool | Introduce a managed pool above the native runtime primitive. |
| 3B — Parallel 4B Workers | Execute four-billion-parameter-class workers in bounded parallel lanes. |
| 3C — Shared Context | Provide controlled context shared across specialized agents. |
| 3D — Planner Routing | Route planning work to an appropriate model/backend. |
| 3E — Independent Reviewer | Strengthen native separation between production and review. |
| 3F — 35B Judge | Add a larger independent judging role. |
| 3G — Dynamic Decomposition | Adapt task decomposition from evidence gathered during execution. |
| 3H — Retry / Replan | Add bounded native retry and replanning policy. |
| 3I — Multi-Agent Recovery | Recover coordinated native work across agent failures. |
| 3J — Autonomous Orchestra | Compose the mature native workflow under durable control-plane policy. |

The overall goal is to move from the `NativeRuntime` primitive to native
multi-agent orchestration that is parallel, routed, independently reviewed,
and recoverable.

## Project history and versioning

Orchestra preserves the complete Git history of HermesOps. Its initial `main`
snapshot is:

```text
11ff2a3b9cf797a5dbc7992ff65e3ddf6a6534be
```

That snapshot closes the HermesOps 0.2.0 technical architecture after milestone
2Z, First NativeRuntime. Future Orchestra development continues from this
history; this bootstrap does not create or imply a new release tag.

The original [HermesOps repository](https://github.com/Bebet0o/HermesOps)
remains available as the historical home of the HermesOps generation and will
be documented as such.

## Documentation

Core current contracts:

- [Architecture](docs/ARCHITECTURE.md)
- [Agent runtime boundary](docs/AGENT_RUNTIME.md)
- [Model provider boundary](docs/MODEL_PROVIDER.md)
- [Control plane](docs/CONTROL_PLANE.md)
- [Objective lifecycle](docs/OBJECTIVES.md)
- [Orchestration DAG](docs/ORCHESTRATION.md)
- [Console foundation](docs/console/FOUNDATION.md)
- [Console project lifecycle](docs/console/PROJECT_LIFECYCLE.md)
- [Console objective lifecycle](docs/console/OBJECTIVE_LIFECYCLE.md)
- [Blueprint v1 specification](docs/blueprint/SPECIFICATION_V1.md)
- [Security](docs/SECURITY.md)
- [Recovery](docs/RECOVERY.md)

Some documents retain historical HermesOps naming or milestone framing. That is
expected during the transition and does not mean the technical rebrand is
complete.

## Contributing

Orchestra is an early open-source project under active development. Preserve
the runtime-neutral boundaries and fail-closed lifecycle rules when changing
the system. Do not commit authentication files, real environment files,
secrets, private project registrations, SQLite databases, workspaces, or
generated runtime state.

Security-sensitive findings should follow [SECURITY.md](SECURITY.md) and should
not include reproduction secrets in public reports.

## License

Orchestra retains the repository's **Apache License 2.0** license. See
[LICENSE](LICENSE).

Third-party components, including Hermes Agent and the historical Hermes WebUI,
retain their own licenses and copyrights.
