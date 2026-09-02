# Controller and Console Boundary

Status: **Accepted design contract for Milestone 2A**

## Summary

Orchestra Console is an unprivileged operator client.

Orchestra Controller is the authoritative control-plane service.

The Console never connects directly to SQLite, Hermes Agent, the host Docker
daemon, or the sandbox Docker daemon. Every read and command crosses the
Controller API, where authentication, authorization, validation, idempotency,
audit, and confirmation policy are applied.

## Trust zones

```text
┌───────────────────────────────────────────────────────────────────┐
│ Operator workstation                                              │
│                                                                   │
│  Browser                                                          │
│  └── Orchestra Console                                            │
└───────────────────────┬───────────────────────────────────────────┘
                        │ HTTPS / WebSocket
                        │ authenticated, same-origin
┌───────────────────────▼───────────────────────────────────────────┐
│ Orchestra control plane                                           │
│                                                                   │
│  Controller API                                                   │
│  Command service                                                  │
│  Query service                                                    │
│  Event journal                                                    │
│  Project / objective / run services                               │
│  Review and recovery services                                     │
│  Hermes Agent adapter                                             │
│  Sandbox manager                                                  │
│  Audit and confirmation services                                  │
│  SQLite                                                           │
└───────────────┬───────────────────────┬───────────────────────────┘
                │                       │
                │ controlled adapter    │ controlled engine API
┌───────────────▼──────────────┐  ┌────▼────────────────────────────┐
│ Hermes Agent                │  │ Dedicated sandbox engine        │
│ upstream AI execution       │  │ immutable images               │
│ no control-plane ownership  │  │ temporary worker containers    │
└──────────────────────────────┘  └─────────────────────────────────┘
```

## Component responsibilities

### Orchestra Console

The Console may:

- authenticate an operator;
- display projects, objectives, tasks, runs, reviews, recovery cases, sandbox
  profiles, backups, notifications, and system health;
- submit validated commands to the Controller;
- subscribe to Controller events;
- retain non-sensitive presentation preferences in browser storage;
- prompt for explicit confirmation when the Controller requires it;
- render logs and diffs received from approved Controller endpoints.

The Console must not:

- open SQLite files;
- read or write files under `/opt/orchestra`;
- call Hermes Agent directly;
- call Docker or the sandbox engine directly;
- construct shell commands for execution on the server;
- store provider credentials, `auth.json`, API keys, or secret values in
  browser storage;
- infer successful state transitions from optimistic UI state;
- mark a command complete before authoritative Controller state is received.

### Orchestra Controller

The Controller must:

- be the authoritative API for the Console;
- own control-plane state transitions;
- validate every command against current state and policy;
- assign request IDs and correlation IDs;
- enforce idempotency for mutations;
- enforce optimistic concurrency where stale operator state is dangerous;
- persist audit records before reporting completion;
- emit replayable events after durable state changes;
- redact secrets and sensitive paths;
- mediate Hermes Agent execution through a stable adapter;
- mediate sandbox operations through the sandbox manager;
- create confirmation requests for dangerous actions;
- return structured errors without leaking credentials or raw internal
  exceptions.

### Hermes Agent

Hermes Agent may:

- execute model sessions requested by the Controller;
- use the tools and runtime capabilities granted to a role;
- return structured or streamed output to the Controller;
- report session usage and transport errors.

Hermes Agent must not become the source of truth for:

- project registration;
- objective or task state;
- integration approval;
- recovery decisions;
- sandbox profile activation;
- operator identity;
- audit history.

### Sandbox manager

The sandbox manager may:

- validate a Blueprint;
- build an image in the dedicated sandbox engine;
- run validation commands;
- assign an immutable image digest;
- activate or roll back a profile;
- create and remove task containers;
- collect logs, resource metrics, and exit status.

It must not:

- expose the host Docker socket to the Console, Agent, or workers;
- permit arbitrary host-path mounts from a Blueprint;
- run privileged containers;
- activate an unvalidated mutable tag;
- delete the last known-good image during a failed activation;
- return secret values in logs or API responses.

## Allowed communication paths

| Caller | Callee | Allowed | Notes |
| --- | --- | ---: | --- |
| Console | Controller API | Yes | Only supported browser-to-server path |
| Console | Controller event stream | Yes | Authenticated and replayable |
| Console | Hermes Agent | No | Must use Controller |
| Console | SQLite | No | Never exposed |
| Console | Docker | No | Never exposed |
| Controller | SQLite | Yes | Through persistence layer |
| Controller | Hermes Agent | Yes | Through Agent adapter |
| Controller | sandbox engine | Yes | Through sandbox manager |
| Hermes Agent | sandbox task container | Yes | Only when a run policy grants it |
| Hermes Agent | Controller database | No | No direct access |
| Worker | host Docker socket | No | Forbidden |
| Worker | project workspace | Conditional | Scoped mount or clone only |
| Supervisor | Controller internal command service | Yes | Recovery-safe operations |
| Supervisor | Console | No | Events are emitted through Controller |

## Initial network posture

For local-first deployments:

- Controller binds to `127.0.0.1` by default.
- Console is served on loopback or behind a trusted reverse proxy.
- Browser access uses an SSH tunnel or operator-managed TLS reverse proxy.
- Cross-origin requests are disabled by default.
- WebSocket origin must match the configured Console origin.
- No public unauthenticated health details beyond a minimal liveness response.
- Administrative endpoints are never exposed directly on the sandbox engine.

A future remote-access profile may be added, but it must not weaken the
loopback default.

## Browser authentication contract

The first Console release targets a single local operator, but it still uses an
authenticated session boundary.

Required properties:

- secure, HTTP-only session cookie;
- `SameSite=Strict` unless a documented deployment requires otherwise;
- short-lived CSRF token delivered separately from the session cookie;
- CSRF header required for mutating requests;
- session rotation after authentication;
- no bearer token in URLs;
- no credentials in WebSocket query strings;
- logout invalidates the server-side session;
- failed authentication attempts are rate-limited and audited.

Multi-user roles and external identity providers are out of scope for the first
beta, but the API must not hard-code an unauthenticated trust model.

## Command execution contract

The Console sends intent, not implementation details.

Good:

```json
{
  "decision": "ROLLBACK_SAFE",
  "reason": "The interrupted run owns the complete dirty diff."
}
```

Forbidden:

```json
{
  "shell": "git reset --hard HEAD~1 && rm -rf /some/path"
}
```

The Controller selects the implementation, validates policy, records the
command, and emits the result.

Every mutating request requires:

- `Idempotency-Key`;
- authenticated session;
- CSRF token;
- request body matching the contract;
- current resource revision when the operation can conflict;
- a durable audit record.

## Confirmation boundary

A confirmation is required when a command can:

- discard uncommitted work;
- delete a project, sandbox profile, backup, or image;
- force a rollback over ambiguous state;
- expose network access beyond the active policy;
- enable secret injection;
- change a project's main branch or remote;
- enable automatic push;
- activate a sandbox with broader permissions;
- stop all project activity;
- perform a destructive uninstall or data purge.

The initial command returns HTTP `409` with a
`confirmation_required` problem and a short-lived `confirmation_id`.

The Console shows:

- the exact action;
- affected resources;
- risk classification;
- expected consequences;
- expiration time;
- any required typed confirmation phrase.

The Console then submits the confirmation ID. It never reconstructs or alters
the original command.

## Secret boundary

Secret metadata may be displayed:

```json
{
  "id": "openai-codex-auth",
  "type": "provider-auth",
  "configured": true,
  "updated_at": "2026-07-18T19:00:00Z"
}
```

Secret values must never be returned:

```json
{
  "value": "forbidden"
}
```

Write operations accept a value only over an authenticated request and must:

1. write it to the dedicated secret store;
2. redact it from logs and errors;
3. return metadata only;
4. emit an audit event without the value;
5. avoid persisting it in the command payload or event data.

## Failure behavior

When the Controller is unavailable, the Console enters read-only degraded mode
using only already-loaded non-sensitive state. It must clearly mark data as
stale and must not queue destructive commands in browser storage.

When Hermes Agent is unavailable:

- project browsing remains available;
- new AI work is rejected with a dependency-unavailable error;
- existing durable state remains authoritative;
- recovery and operator decisions that do not require the Agent may continue.

When the sandbox engine is unavailable:

- the Controller rejects new sandbox runs and builds;
- running-state reconciliation begins;
- no run is silently marked failed or complete;
- recovery receives an explicit infrastructure incident.

## Non-goals

The boundary does not require:

- microservices for every component;
- distributed transactions;
- browser access to raw system paths;
- direct database queries from plugins;
- a public plugin API in the first beta;
- replacement of Hermes Agent internals.

The first implementation may be one Controller process with clear internal
modules, provided these boundaries are preserved.
