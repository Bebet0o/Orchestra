# Structured Project Memory

Status: **0.3-A implemented**

Structured Project Memory is Orchestra’s canonical durable project-knowledge authority. It is separate from legacy/runtime `shared_context_entries`; `context_snapshots` remain the historical authority for what an agent actually received. Runtime context selection does not consume canonical memory automatically until 0.3-B.

## Data model

Records are project- or objective-scoped and use one of `FACT`, `CONSTRAINT`, `DECISION`, `ASSUMPTION`, `FINDING`, `RESULT`, `REFERENCE`, or `NOTE`. Authority is one of `OPERATOR_DECLARED`, `CONTROL_PLANE_OBSERVED`, `REVIEW_ACCEPTED`, `AGENT_PROPOSED`, or `LEGACY_UNVERIFIED`.

Edits create immutable revisions. Exact deterministic duplicates may add provenance without semantic merging. Normal lifecycle is `ACTIVE → RETRACTED`; security containment may additionally move `ACTIVE` or `RETRACTED` to terminal `REDACTED`. Delete and reactivation are not public operations.

## Controller surface

0.3-A2 exposes bounded authenticated reads and idempotent writes under `/api/v1/projects/{project_id}/memories` and `/api/v1/memories/{memory_id}`. Revisions, retract/redact commands, immutable command audit and replayable `memory.*` EventJournal events are part of the same Controller transaction boundary. Browser writes derive `OPERATOR_DECLARED`; clients cannot submit privileged authority or provenance.

## Console

0.3-A3 adds `/memory` to Orchestra Console. The view provides a project selector, kind/state filters with a Decisions shortcut, bounded first-page listing, detail, provenance/link readback, immutable revision history, create/revise forms, retract and redaction actions. It uses only the Console’s closed same-origin Controller gateway; it never reads SQLite directly and stores no memory in browser persistence.

## Security and redaction

Credential-like content is rejected before canonical persistence. Canonical redaction scrubs payload text across every structured-memory revision while retaining hashes/audit metadata. This is not forensic erasure: legacy source rows, WAL/database free pages, backups, and previously materialized context snapshots may retain older bytes. Any exposed credential must therefore be rotated or revoked.

## Migration

Migration 032 creates the canonical memory authority and imports historical `memory_records` and `shared_context_entries` 1:1 as `LEGACY_UNVERIFIED` without fabricating semantic grouping or hashes. Migration 033 adds Controller operation/idempotency/audit tables and extends EventJournal with the `memory` aggregate while preserving existing event sequence history.
