# Console Operational Dashboard

Status: **implemented by milestone 2R**

## Purpose

The dashboard answers a bounded version of “what needs attention now?” using
only authenticated Controller projections. The Console never opens SQLite,
workspaces, Docker, Hermes Agent, or host paths.

## Same-origin read boundary

Milestone 2R adds exactly six query-free GET routes to the existing Console
gateway:

- `GET /api/v1/projects`;
- `GET /api/v1/objectives`;
- `GET /api/v1/reviews`;
- `GET /api/v1/recoveries`;
- `GET /api/v1/plans`;
- `GET /api/v1/reviewer-assignments`.

The routes are existing redacted Controller collections. Query strings, nested
resources, writes, event transport, arbitrary tasks, run logs, evidence, and
artifacts remain unavailable through the Console gateway.

## Presentation

The dashboard renders:

- enabled-project and active-objective counts from the first bounded pages;
- an attention queue from explicit blocked, failed, rejected, pending, or
  recovery-related public states;
- active orchestration plans and their already-redacted task counts;
- a bounded project portfolio;
- partial-data and pagination warnings when collections are unavailable or
  advertise a continuation cursor.

All untrusted Controller strings are inserted through DOM `textContent`. The
application does not use HTML injection, browser storage, WebSocket, background
polling, or offline data. Refresh is explicit and bounded.

## Degradation

A `401` during dashboard reads invalidates the local authenticated presentation
and requires a new login. Other collection failures produce a partial dashboard
without extrapolating missing counts. If all collections fail, no stale value is
retained as authoritative.

## Deferred work

Project, Blueprint, and objective lifecycles are implemented. Detailed
orchestration/execution views, human review/recovery actions, and WebSocket
reconciliation remain deferred.
