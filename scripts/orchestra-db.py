#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


ROOT = Path(
    os.environ.get(
        "ORCHESTRA_ROOT",
        "/opt/orchestra",
    )
)

REPO = ROOT / "repo"
DATABASE = ROOT / "state" / "controller" / "orchestra.db"
MIGRATIONS = REPO / "migrations"


def connect() -> sqlite3.Connection:
    DATABASE.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(
        DATABASE,
        timeout=10,
    )

    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")

    return connection


def migration_files() -> list[tuple[int, Path]]:
    result: list[tuple[int, Path]] = []

    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        prefix = path.name.split("_", 1)[0]
        result.append((int(prefix), path))

    return result


def applied_versions(
    connection: sqlite3.Connection,
) -> set[int]:
    exists = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'schema_migrations'
        """
    ).fetchone()

    if exists is None:
        return set()

    return {
        int(row[0])
        for row in connection.execute(
            "SELECT version FROM schema_migrations"
        )
    }


def command_migrate() -> None:
    with connect() as connection:
        applied = applied_versions(connection)

        for version, path in migration_files():
            if version in applied:
                print(
                    f"Migration {version:03d}: "
                    f"already applied"
                )
                continue

            script = path.read_text(encoding="utf-8")

            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + script
                    + "\nCOMMIT;\n"
                )
            except Exception:
                if connection.in_transaction:
                    connection.rollback()
                raise

            print(
                f"Migration {version:03d}: "
                f"applied ({path.name})"
            )

    os.chmod(DATABASE, 0o640)

    print(f"Database: {DATABASE}")
    print("Database migration: PASS")


def command_integrity() -> None:
    with connect() as connection:
        quick_check = [
            row[0]
            for row in connection.execute(
                "PRAGMA quick_check"
            )
        ]

        foreign_keys = list(
            connection.execute(
                "PRAGMA foreign_key_check"
            )
        )

        journal_mode = connection.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]

    if quick_check != ["ok"]:
        raise SystemExit(
            f"SQLite quick_check failed: {quick_check!r}"
        )

    if foreign_keys:
        raise SystemExit(
            f"Foreign-key violations: {foreign_keys!r}"
        )

    if journal_mode.lower() != "wal":
        raise SystemExit(
            f"Unexpected journal mode: {journal_mode}"
        )

    print("SQLite quick_check: ok")
    print("SQLite foreign keys: ok")
    print("SQLite journal mode: wal")
    print("Database integrity: PASS")


def command_status() -> None:
    with connect() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                  AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            )
        ]

        migrations = []

        if "schema_migrations" in tables:
            migrations = [
                int(row[0])
                for row in connection.execute(
                    """
                    SELECT version
                    FROM schema_migrations
                    ORDER BY version
                    """
                )
            ]

        journal_mode = connection.execute(
            "PRAGMA journal_mode"
        ).fetchone()[0]

        project_count = connection.execute(
            "SELECT COUNT(*) FROM projects"
        ).fetchone()[0]

    print(f"Database: {DATABASE}")
    print(f"Journal mode: {journal_mode}")
    print(f"Migrations: {migrations}")
    print(f"Registered projects: {project_count}")
    print("Tables:")

    for table in tables:
        print(f"  - {table}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Orchestra database manager"
    )

    parser.add_argument(
        "command",
        choices=(
            "migrate",
            "integrity",
            "status",
        ),
    )

    arguments = parser.parse_args()

    if arguments.command == "migrate":
        command_migrate()
    elif arguments.command == "integrity":
        command_integrity()
    elif arguments.command == "status":
        command_status()


if __name__ == "__main__":
    main()
