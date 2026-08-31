# HermesOps Console Information Architecture

Status: **Design contract**
Target: `v0.2.0-beta`
Current implementation: **Not started**

## Product intent

HermesOps Console is the dedicated WebUI for operating HermesOps after the
initial terminal installation.

The Console must make daily project operations possible without requiring the
operator to understand internal SQLite tables, systemd units, Docker commands,
or Hermes Agent session details.

Terminal use remains appropriate for:

- first installation;
- major upgrades;
- emergency repair;
- low-level diagnostics;
- conservative or destructive uninstall.

Normal project operation should be WebUI-first.

## Navigation

Primary navigation:

```text
Dashboard
Projects
Objectives
Runs
Reviews
Recovery
Sandboxes
Backups
Notifications
Settings
```

Contextual project navigation:

```text
Overview
Objectives
Tasks
Runs
Reviews
Memory
Decisions
Files and Git
Backups
Policy
Settings
```

## Global application shell

The shell includes:

- HermesOps logo and current environment;
- global project selector;
- system health indicator;
- active-run count;
- unresolved blocker count;
- notifications;
- operator session menu;
- connection and synchronization state.

The shell must visibly distinguish:

```text
healthy
degraded
offline
read-only
reconciling
```

A green browser connection indicator must never imply that all workers or
dependencies are healthy.

## Dashboard

Purpose: answer “What needs my attention now?”

Sections:

### Attention queue

- human confirmations;
- blocked objectives;
- recovery cases;
- reviewer verdicts requiring action;
- failed sandbox builds;
- failing infrastructure components;
- disk or memory pressure;
- expired credentials or missing authentication.

### Active work

For each active run:

- project;
- objective and task;
- role;
- current phase;
- progress;
- last heartbeat;
- CPU and memory;
- elapsed time;
- current sandbox profile;
- safe available actions.

### Project portfolio

- enabled projects;
- current milestone;
- health;
- active objective;
- latest accepted integration;
- pending technical debt;
- last backup.

### System capacity

- host CPU, RAM, disk;
- sandbox-engine availability;
- Hermes Agent availability;
- queue depth;
- current writer locks;
- notification backlog.

## Projects

### Project list

Columns/cards:

- name;
- status;
- repository;
- default branch;
- active objective;
- health;
- blocked state;
- last activity;
- policy;
- default sandbox.

Actions:

```text
Create project
Import repository
Enable
Disable
Archive
```

Delete is hidden behind project detail and confirmation.

### Create project wizard

Steps:

1. identity;
2. repository source;
3. workspace and data policy;
4. default branch and Git protection;
5. HermesOps policy;
6. default sandbox;
7. optional existing project documentation import;
8. validation preview;
9. create.

The wizard submits intent to the Controller. It does not construct server shell
commands.

### Project overview

Shows:

- vision and summary;
- repository health;
- objective status;
- task DAG summary;
- latest runs and reviews;
- unresolved blockers;
- technical-debt register;
- decision log;
- backup status;
- sandbox and role bindings.

## Objectives

### Objective list

Filters:

```text
draft
planning
planned
running
paused
blocked
succeeded
failed
cancelled
archived
```

Columns:

- title;
- project;
- priority;
- state;
- progress;
- current task;
- blockers;
- created time;
- last activity.

### Create objective

Fields:

- title;
- detailed outcome;
- constraints;
- acceptance criteria;
- priority;
- maximum autonomy level;
- optional due date;
- optional sandbox or role overrides;
- human approval checkpoints.

The operator sees a preview of:

- selected project;
- policy;
- estimated scope class;
- permissions;
- network access;
- whether automatic integration is enabled.

### Objective detail

Tabs:

```text
Summary
Plan
Task graph
Runs
Reviews
Decisions
Memory
Artifacts
Audit
```

Primary actions depend on state:

```text
Plan
Start
Pause
Resume
Replan
Cancel
Archive
```

The UI never offers an impossible transition.

## Task graph

The graph shows:

- dependencies;
- role assignment;
- sandbox profile;
- state;
- writer/read-only classification;
- review requirement;
- retry count;
- blockers.

The same information must be available as an accessible table.

Selecting a task opens:

- description;
- inputs and expected outputs;
- dependencies;
- run attempts;
- current evidence;
- produced commits;
- review history;
- recovery history.

## Runs

### Run list

Filters:

- project;
- objective;
- role;
- state;
- sandbox;
- date;
- review status.

### Run detail

Header:

- run ID;
- task;
- role;
- model/provider;
- sandbox image digest;
- start and elapsed time;
- state;
- heartbeat;
- resource usage.

Panels:

```text
Timeline
Live logs
Progress
Git changes
Tests
Artifacts
Agent session
Review
Recovery
Audit
```

### Live logs

Requirements:

- virtualized rendering;
- bounded pagination;
- sequence markers;
- pause and resume;
- filter by stream or phase;
- secret redaction indicator;
- download only through a controlled artifact endpoint;
- no HTML interpretation of worker output.

### Git changes

Shows:

- baseline commit;
- produced commit;
- changed files;
- diff summary;
- clean/dirty status;
- transaction snapshot;
- integration target.

Dangerous Git actions are never offered as raw commands.

## Reviews

### Review queue

Categories:

```text
waiting
running
PASS
PASS_WITH_DEBT
FIX
SECURITY
PERFORMANCE
ARCHITECTURE
HUMAN
```

### Review detail

Shows:

- reviewed run and commit;
- reviewer role/model;
- evidence;
- test results;
- findings by severity;
- verdict;
- required fixes;
- technical debt;
- architecture and security notes;
- transport status.

A failed reviewer transport must be visibly different from a completed review.

Actions:

```text
Rerun review
Acknowledge debt
Request human review
Open related recovery
```

## Recovery

### Recovery queue

Shows:

- affected project/run;
- incident type;
- detected time;
- current evidence confidence;
- recommended decision;
- destructive risk;
- required human action.

### Recovery detail

Evidence sections:

```text
Durable Controller state
Git state
Worktree ownership
Transaction snapshots
Container state
Heartbeats
Logs
Backups
Previous recovery attempts
```

Allowed decisions:

```text
RESUME_SAFE
ROLLBACK_SAFE
BLOCK_HUMAN
```

The UI displays why a decision is or is not allowed.

A destructive confirmation screen includes:

- affected repository and branch;
- files or commits at risk;
- available backup;
- expected rollback target;
- typed phrase;
- expiration;
- audit warning.

## Sandboxes

### Sandbox profile list

Columns:

- profile name;
- schema version;
- source revision;
- status;
- active image digest;
- last build;
- last validation;
- projects/roles using it;
- security summary.

Actions:

```text
Create
Duplicate
Edit
Validate
Build
Test
Activate
Roll back
Archive
```

### Blueprint editor

The editor provides:

- syntax highlighting;
- schema-aware completion;
- inline diagnostics;
- documentation links;
- immutable-digest helper;
- security-policy summary;
- canonical diff;
- build-plan preview;
- source revision history.

The operator can edit source text, but activation remains a separate command.

### Build view

Shows:

- source revision;
- canonical hash;
- base image and digest;
- build plan;
- network access used;
- resolved packages;
- logs;
- validation commands;
- image ID/digest;
- warnings and errors;
- duration and resource use.

### Activation view

Shows:

- current active digest;
- candidate digest;
- security-policy difference;
- package and tool difference;
- validation results;
- projects and roles affected;
- rollback target.

Broader permissions require explicit confirmation.

## Memory and decisions

Project memory is presented as structured records, not one unbounded prompt.

Categories:

```text
Architecture decisions
Project conventions
Known bugs and traps
Assumptions
Rejected approaches
Operational procedures
Technical debt
Glossary
Milestone summaries
```

Each record shows:

- source;
- author/agent;
- confidence;
- project scope;
- creation and update time;
- related objective/task/run;
- superseded state;
- audit history.

The Console must distinguish operator-authored truth from model-generated
hypotheses.

## Backups

Views:

- backup inventory;
- project vs Controller backup type;
- creation reason;
- verification status;
- size and retention;
- restore compatibility;
- related transaction/run.

Actions:

```text
Create backup
Verify
Restore
Delete
```

Restore and delete require confirmation.

The Console must not claim a backup is usable until verification passes.

## Notifications

Shows:

- channel;
- queued/sent/retrying/failed state;
- redacted destination;
- related project/run;
- attempts;
- next retry;
- final failure reason.

Notification failure does not change objective success.

## Settings

Sections:

```text
General
Hermes Agent
Models and roles
Projects defaults
Sandbox defaults
Network policy
Secrets
Notifications
Backups and retention
Authentication
Audit
Maintenance
```

### Secrets

The Console displays metadata only:

- configured;
- type;
- last updated;
- last successful test;
- used by.

When a value is entered:

- it is sent once;
- it is never displayed again;
- it is not stored in browser state;
- it is cleared immediately after submission;
- the response returns metadata only.

## Global search

Searchable entities:

- projects;
- objectives;
- tasks;
- runs;
- reviews;
- recovery cases;
- sandbox profiles;
- decisions;
- memory records;
- audit references.

Search results must respect authorization and secret redaction.

## Command UX

### Optimistic UI

Allowed only for reversible presentation state such as:

- expanded panels;
- selected filters;
- local draft text.

Not allowed for domain state transitions.

After a command:

1. show request accepted;
2. link to operation;
3. wait for authoritative response/event;
4. update resource revision;
5. show failure or confirmation requirements explicitly.

### Idempotency

The Console generates one idempotency key per operator intent.

Retrying due to network failure reuses the same key.

Editing the command creates a new key.

### Conflicts

On `resource_conflict`, the UI:

- fetches current state;
- shows what changed;
- preserves the operator's unsent input;
- requires deliberate resubmission.

It must not silently overwrite newer state.

## Degraded modes

### Controller offline

- show offline banner;
- display cached non-sensitive data as stale;
- disable commands;
- do not queue commands locally;
- offer connection diagnostics.

### Event stream offline

- show degraded real-time banner;
- continue HTTP operation;
- poll bounded status endpoints;
- reconcile after reconnect.

### Hermes Agent offline

- allow browsing and non-Agent administration;
- disable new AI runs;
- show dependency status;
- preserve durable queues.

### Sandbox engine offline

- disable new runs/builds;
- display affected operations;
- allow safe browsing and recovery evidence;
- avoid marking work complete.

## Accessibility and safety

Requirements:

- keyboard navigation;
- visible focus;
- accessible tables for every graph;
- status not conveyed by color alone;
- confirmation dialogs readable by screen readers;
- logs use monospace but remain selectable;
- reduced-motion support;
- destructive actions separated from primary actions;
- no auto-confirm;
- no secret copied to clipboard automatically.

## Responsive behavior

Primary target is desktop administration.

Tablet support should preserve all actions.

Mobile support may prioritize:

- dashboard;
- notifications;
- run status;
- confirmations;
- pause/cancel;
- recovery decisions.

The Blueprint editor may require desktop width.

## Initial beta route map

```text
/
 /projects
 /projects/new
 /projects/:projectId
 /projects/:projectId/objectives
 /objectives/:objectiveId
 /tasks/:taskId
 /runs
 /runs/:runId
 /reviews
 /reviews/:reviewId
 /recovery
 /recovery/:recoveryId
 /sandboxes
 /sandboxes/new
 /sandboxes/:sandboxId
 /sandbox-builds/:buildId
 /backups
 /notifications
 /settings
 /audit/:auditId
```

## First implementation slice

The first usable Console slice should include:

1. authenticated shell;
2. system status;
3. project list/detail;
4. objective list/detail;
5. task graph table;
6. run list/detail;
7. replayable event connection;
8. read-only logs;
9. no destructive mutations.

Write commands are added after Controller idempotency, confirmation, and audit
tests pass.

## Non-goals for first beta

- public SaaS multi-tenancy;
- arbitrary third-party UI plugins;
- direct SSH terminal in the browser;
- raw Docker management;
- raw SQLite explorer;
- generic shell execution;
- mobile-first Blueprint editing;
- hidden autonomous destructive actions.
