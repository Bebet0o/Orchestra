# Console Project Lifecycle

Status: **implemented by milestone 2S**

## Scope

The authenticated Console project page manages the bounded project lifecycle
through the Controller. It never edits SQLite, project TOML files, repositories,
or host paths directly.

Implemented browser intents:

- list and inspect projects;
- create a disabled project using `existing`, `initialize`, or `clone` repository mode;
- update the display name, policy, and optional sandbox profile;
- enable, disable, rescan, and archive a project;
- display accepted Controller operation identifiers.

Deletion, remote mutation, default-branch mutation, Blueprint editing, automatic
push, and background polling are not implemented.

## Same-origin routes

The Console gateway exposes only these additional routes:

```text
GET   /api/v1/projects/{project_id}
POST  /api/v1/projects
PATCH /api/v1/projects/{project_id}
POST  /api/v1/projects/{project_id}/commands/{enable|disable|rescan|archive}
```

Project identifiers must match the public project identifier grammar. Query
strings, encoded paths, nested resources, `delete`, and arbitrary commands fail
closed before reaching the Controller.

## Mutation safety

Each browser mutation obtains a fresh CSRF challenge and sends a new
cryptographically random `Idempotency-Key`. Updates and commands require the
ETag returned by the project detail read as `If-Match`.

The Controller validates repository state, managed paths, policy and sandbox
references, active work, idempotency, and resource revision. Project state,
compatibility TOML, operations, audit, and events are committed as one bounded
intent with filesystem cleanup or rollback on failure.

Archive is explicitly confirmed in the browser. Delete remains unavailable.
No command or credential is persisted in browser storage.
