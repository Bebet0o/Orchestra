#!/usr/bin/env bash
set -Eeuo pipefail
export LC_ALL=C
export PYTHONDONTWRITEBYTECODE=1

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

python3 -m unittest -v tests.test_controller_blueprint_lifecycle

python3 - <<'PY'
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

root = Path.cwd()
historical_migration = root / "migrations/023_blueprint_migration.sql"
source = historical_migration.read_text(encoding="utf-8")
for phrase in (
    "CREATE TABLE controller_blueprint_operations",
    "CREATE TABLE controller_blueprint_idempotency",
    "CREATE TABLE controller_blueprint_command_audit",
    "historical request authority: preserve exactly",
    "historical integrity authority: preserve exactly",
    "controller Blueprint audit is immutable",
    "PRAGMA user_version = 23",
):
    if phrase not in source:
        raise SystemExit(f"Blueprint lifecycle migration contract missing: {phrase}")
for forbidden in (
    "create table sandbox_builds",
    "docker build",
    "active_image_digest =",
):
    if forbidden in source.lower():
        raise SystemExit(f"2T migration exceeds scope: {forbidden}")

migration = root / "migrations/024_blueprint_apiversion.sql"
source = migration.read_text(encoding="utf-8")
for phrase in (
    "api_version IN ('hermesops.dev/v1', 'orchestra.dev/v1')",
    "sandbox_profile_revision_api_version_insert_guard",
    "new Blueprint revisions require orchestra.dev/v1",
    "PRAGMA user_version = 24",
):
    if phrase not in source:
        raise SystemExit(f"Blueprint API namespace migration contract missing: {phrase}")

with tempfile.TemporaryDirectory() as directory:
    database = Path(directory) / "fresh.db"
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        for item in sorted((root / "migrations").glob("[0-9][0-9][0-9]_*.sql")):
            connection.executescript(item.read_text(encoding="utf-8"))
        if connection.execute("PRAGMA user_version").fetchone()[0] != 31:
            raise SystemExit("fresh migration did not reach schema 31")
        if connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] != 31:
            raise SystemExit("fresh migration ledger did not reach 31")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SystemExit("fresh migration quick_check failed")
        required = {
            "controller_blueprint_operations",
            "controller_blueprint_idempotency",
            "controller_blueprint_command_audit",
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not required <= tables:
            raise SystemExit(f"Blueprint lifecycle tables missing: {sorted(required-tables)}")

        try:
            connection.executescript(migration.read_text(encoding="utf-8"))
        except sqlite3.Error:
            connection.rollback()
        else:
            raise SystemExit("Blueprint lifecycle migration rerun unexpectedly succeeded")

        if connection.execute("PRAGMA user_version").fetchone()[0] != 31:
            raise SystemExit("migration rerun changed schema version")
        if connection.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise SystemExit("migration rerun damaged database")

print("ORCHESTRA_3B_BLUEPRINT_MIGRATION_FRESH_PASS")
print("ORCHESTRA_3B_BLUEPRINT_MIGRATION_RERUN_FAIL_CLOSED_PASS")
PY

echo "ORCHESTRA_3B_CONTROLLER_BLUEPRINT_LIFECYCLE_PASS"
