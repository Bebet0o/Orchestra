#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import signal
import socket
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

VERSION = "notifier-v1"
ROOT = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")).resolve()
REPO = ROOT / "repo"
DATABASE = Path(
    os.environ.get(
        "ORCHESTRA_DB",
        str(ROOT / "state/controller/orchestra.db"),
    )
).resolve()
CONFIG_PATH = Path(
    os.environ.get(
        "ORCHESTRA_NOTIFIER_CONFIG",
        str(REPO / "config/notifier.toml"),
    )
).resolve()
RUNTIME = Path(
    os.environ.get(
        "ORCHESTRA_NOTIFIER_RUNTIME",
        str(ROOT / "runtime/notifier"),
    )
).resolve()
STATUS_PATH = RUNTIME / "status.json"
LOCK_PATH = RUNTIME / "notifier.lock"
DEFAULT_SECRET_PATH = ROOT / "secrets/notifications.env"
STOP_REQUESTED = False


class NotifierError(RuntimeError):
    pass


def fail(message: str) -> NoReturn:
    raise NotifierError(message)


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def utc_now() -> str:
    return (
        utc_now_dt()
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def to_utc(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DATABASE, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.is_file():
        fail(f"Notifier configuration missing: {CONFIG_PATH}")
    with CONFIG_PATH.open("rb") as stream:
        payload = tomllib.load(stream)
    if payload.get("schema_version") != 1:
        fail("Unsupported notifier configuration schema")
    return payload


def load_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    result: dict[str, str] = {}
    for raw in path.read_text(
        encoding="utf-8",
        errors="strict",
    ).splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def telegram_credentials(
    config: dict[str, Any],
) -> tuple[str, str, str] | None:
    channel = config.get("channels", {}).get("telegram", {})
    secret_path = Path(
        os.environ.get(
            "ORCHESTRA_NOTIFIER_SECRETS",
            str(channel.get("secrets_file") or DEFAULT_SECRET_PATH),
        )
    ).resolve()
    secrets = load_env_file(secret_path)
    token = str(
        os.environ.get("ORCHESTRA_TELEGRAM_BOT_TOKEN")
        or secrets.get("ORCHESTRA_TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()
    chat_id = str(
        os.environ.get("ORCHESTRA_TELEGRAM_CHAT_ID")
        or secrets.get("ORCHESTRA_TELEGRAM_CHAT_ID")
        or ""
    ).strip()
    api_base = str(
        os.environ.get("ORCHESTRA_TELEGRAM_API_BASE")
        or channel.get("api_base")
        or "https://api.telegram.org"
    ).strip().rstrip("/")
    if token and chat_id:
        return token, chat_id, api_base
    return None


def channel_enabled(
    config: dict[str, Any],
    channel: str,
) -> bool:
    if channel == "FILE":
        return bool(
            config.get("channels", {})
            .get("file", {})
            .get("enabled", True)
        )
    if channel == "TELEGRAM":
        mode = str(
            config.get("channels", {})
            .get("telegram", {})
            .get("mode", "auto")
        ).strip().lower()
        if mode == "disabled":
            return False
        if mode in {"auto", "required"}:
            configured = telegram_credentials(config) is not None
            if mode == "required" and not configured:
                fail("Telegram channel is required but credentials are absent")
            return configured
        fail(f"Unsupported Telegram mode: {mode}")
    return False


def write_status(payload: dict[str, Any]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    temporary = STATUS_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o640)
    temporary.replace(STATUS_PATH)


def get_cursor(
    connection: sqlite3.Connection,
    source_name: str,
) -> int:
    row = connection.execute(
        """
        SELECT last_rowid
        FROM notification_cursors
        WHERE source_name = ?
        """,
        (source_name,),
    ).fetchone()
    return int(row["last_rowid"]) if row else 0


def set_cursor(
    connection: sqlite3.Connection,
    source_name: str,
    rowid: int,
) -> None:
    connection.execute(
        """
        INSERT INTO notification_cursors (
            source_name,
            last_rowid,
            updated_at
        )
        VALUES (?, ?, ?)
        ON CONFLICT(source_name) DO UPDATE SET
            last_rowid = excluded.last_rowid,
            updated_at = excluded.updated_at
        """,
        (source_name, rowid, utc_now()),
    )


def objective_message(
    row: sqlite3.Row,
) -> tuple[str, str, str] | None:
    event = row["event_type"]
    mapping = {
        "OBJECTIVE_SUBMITTED": (
            "INFO",
            "Objectif ajouté",
        ),
        "OBJECTIVE_PAUSED": (
            "WARNING",
            "Objectif en pause",
        ),
        "OBJECTIVE_RESUMED": (
            "INFO",
            "Objectif repris",
        ),
        "OBJECTIVE_CANCELLED": (
            "WARNING",
            "Objectif annulé",
        ),
        "OBJECTIVE_COMPLETED": (
            "INFO",
            "Objectif terminé",
        ),
        "OBJECTIVE_FAILED": (
            "CRITICAL",
            "Objectif en échec",
        ),
        "OBJECTIVE_PLANNING_ABANDONED": (
            "WARNING",
            "Planification interrompue puis reprise",
        ),
    }
    if event not in mapping:
        return None
    severity, title = mapping[event]
    objective = str(row["objective"] or "").strip()
    if len(objective) > 600:
        objective = objective[:597] + "..."
    projects = str(row["project_scope_json"] or "[]")
    text = (
        f"{title}\n"
        f"ID: {row['objective_id']}\n"
        f"État: {row['old_status'] or '-'} -> {row['new_status'] or '-'}\n"
        f"Projets: {projects}\n"
        f"Objectif: {objective}"
    )
    return severity, title, text


def enqueue(
    connection: sqlite3.Connection,
    *,
    dedupe_key: str,
    event_kind: str,
    severity: str,
    subject_type: str,
    subject_id: str,
    channel: str,
    title: str,
    text: str,
    metadata: dict[str, Any] | None = None,
    max_attempts: int = 5,
) -> str | None:
    identifier = "notification-" + uuid.uuid4().hex
    now = utc_now()
    payload = {
        "title": title,
        "text": text,
        "metadata": metadata or {},
    }
    cursor = connection.execute(
        """
        INSERT OR IGNORE INTO notification_outbox (
            notification_id,
            dedupe_key,
            event_kind,
            severity,
            subject_type,
            subject_id,
            channel,
            destination,
            payload_json,
            status,
            attempt_count,
            max_attempts,
            next_attempt_at,
            lease_owner,
            lease_expires_at,
            last_error,
            created_at,
            updated_at,
            delivered_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, 'default', ?, 'PENDING',
                0, ?, ?, NULL, NULL, NULL, ?, ?, NULL)
        """,
        (
            identifier,
            dedupe_key,
            event_kind,
            severity,
            subject_type,
            subject_id,
            channel,
            canonical_json(payload),
            max_attempts,
            now,
            now,
            now,
        ),
    )
    return identifier if cursor.rowcount == 1 else None


def enabled_channels(config: dict[str, Any]) -> list[str]:
    result = []
    for channel in ("FILE", "TELEGRAM"):
        if channel_enabled(config, channel):
            result.append(channel)
    return result


def collect_objective_events(
    connection: sqlite3.Connection,
    config: dict[str, Any],
) -> int:
    source = "objective_events"
    cursor = get_cursor(connection, source)
    rows = connection.execute(
        """
        SELECT
            event.rowid AS source_rowid,
            event.*,
            objective.objective,
            objective.project_scope_json
        FROM objective_events AS event
        JOIN objective_queue AS objective
          ON objective.objective_id = event.objective_id
        WHERE event.rowid > ?
        ORDER BY event.rowid
        """,
        (cursor,),
    ).fetchall()
    created = 0
    last = cursor
    channels = enabled_channels(config)
    for row in rows:
        last = max(last, int(row["source_rowid"]))
        rendered = objective_message(row)
        if rendered is None:
            continue
        severity, title, text = rendered
        for channel in channels:
            if enqueue(
                connection,
                dedupe_key=(
                    f"objective-event:{row['objective_event_id']}:{channel}"
                ),
                event_kind=row["event_type"],
                severity=severity,
                subject_type="OBJECTIVE",
                subject_id=row["objective_id"],
                channel=channel,
                title=title,
                text=text,
                metadata={
                    "objective_event_id": row["objective_event_id"],
                    "created_at": row["created_at"],
                },
            ):
                created += 1
    if rows:
        set_cursor(connection, source, last)
    return created


def collect_control_events(
    connection: sqlite3.Connection,
    config: dict[str, Any],
) -> int:
    source = "events"
    cursor = get_cursor(connection, source)
    rows = connection.execute(
        """
        SELECT rowid AS source_rowid, *
        FROM events
        WHERE rowid > ?
        ORDER BY rowid
        """,
        (cursor,),
    ).fetchall()
    selected = {
        "INTEGRATION_BLOCKED_HUMAN": (
            "CRITICAL",
            "Intervention humaine requise",
        ),
        "RECOVERY_BLOCKED_HUMAN": (
            "CRITICAL",
            "Récupération bloquée",
        ),
        "ORCHESTRATION_TASK_BLOCKED": (
            "WARNING",
            "Tâche d'orchestration bloquée",
        ),
    }
    channels = enabled_channels(config)
    created = 0
    last = cursor
    for row in rows:
        last = max(last, int(row["source_rowid"]))
        if row["event_type"] not in selected:
            continue
        severity, title = selected[row["event_type"]]
        text = (
            f"{title}\n"
            f"Événement: {row['event_type']}\n"
            f"Run: {row['run_id'] or '-'}\n"
            f"Tâche: {row['task_id'] or '-'}\n"
            f"Gravité: {row['severity']}\n"
            f"Détails: {row['payload_json']}"
        )
        for channel in channels:
            if enqueue(
                connection,
                dedupe_key=f"event:{row['event_id']}:{channel}",
                event_kind=row["event_type"],
                severity=severity,
                subject_type="RUN",
                subject_id=str(row["run_id"] or row["event_id"]),
                channel=channel,
                title=title,
                text=text,
                metadata={"event_id": row["event_id"]},
            ):
                created += 1
    if rows:
        set_cursor(connection, source, last)
    return created


def collect_approvals(
    connection: sqlite3.Connection,
    config: dict[str, Any],
) -> int:
    rows = connection.execute(
        """
        SELECT *
        FROM approvals
        ORDER BY created_at
        """
    ).fetchall()
    channels = enabled_channels(config)
    created = 0
    for row in rows:
        status = row["status"]
        severity = (
            "CRITICAL"
            if status == "PENDING"
            else "WARNING"
            if status in {"REJECTED", "EXPIRED", "CANCELLED"}
            else "INFO"
        )
        title = (
            "Décision humaine requise"
            if status == "PENDING"
            else f"Approbation {status.lower()}"
        )
        text = (
            f"{title}\n"
            f"Approval: {row['approval_id']}\n"
            f"Run: {row['run_id']}\n"
            f"État: {status}\n"
            f"Question: {row['question']}\n"
            f"Options: {row['options_json']}"
        )
        for channel in channels:
            if enqueue(
                connection,
                dedupe_key=(
                    f"approval:{row['approval_id']}:{status}:{channel}"
                ),
                event_kind=f"APPROVAL_{status}",
                severity=severity,
                subject_type="APPROVAL",
                subject_id=row["approval_id"],
                channel=channel,
                title=title,
                text=text,
                metadata={
                    "run_id": row["run_id"],
                    "status": status,
                },
            ):
                created += 1
    return created


def collect_events(config: dict[str, Any]) -> dict[str, int]:
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = {
            "objective_events": collect_objective_events(
                connection,
                config,
            ),
            "control_events": collect_control_events(
                connection,
                config,
            ),
            "approvals": collect_approvals(
                connection,
                config,
            ),
        }
        connection.commit()
    return result


def reconcile_abandoned_deliveries(
    connection: sqlite3.Connection,
) -> int:
    now = utc_now()
    cursor = connection.execute(
        """
        UPDATE notification_outbox
        SET status = 'RETRY',
            lease_owner = NULL,
            lease_expires_at = NULL,
            next_attempt_at = ?,
            updated_at = ?,
            last_error = COALESCE(
                last_error,
                'delivery lease expired'
            )
        WHERE status = 'DELIVERING'
          AND (
              lease_expires_at IS NULL
              OR lease_expires_at <= ?
          )
        """,
        (now, now, now),
    )
    return int(cursor.rowcount)


def claim_one(
    owner: str,
    lease_seconds: int,
    *,
    channel: str | None = None,
    notification_id: str | None = None,
) -> sqlite3.Row | None:
    now_dt = utc_now_dt()
    now = to_utc(now_dt)
    expires = to_utc(now_dt + timedelta(seconds=lease_seconds))
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        reconcile_abandoned_deliveries(connection)
        clauses = [
            "status IN ('PENDING', 'RETRY')",
            "next_attempt_at <= ?",
        ]
        parameters: list[Any] = [now]
        if channel:
            clauses.append("channel = ?")
            parameters.append(channel)
        if notification_id:
            clauses.append("notification_id = ?")
            parameters.append(notification_id)
        row = connection.execute(
            f"""
            SELECT *
            FROM notification_outbox
            WHERE {' AND '.join(clauses)}
            ORDER BY
                CASE severity
                    WHEN 'CRITICAL' THEN 0
                    WHEN 'WARNING' THEN 1
                    ELSE 2
                END,
                created_at
            LIMIT 1
            """,
            tuple(parameters),
        ).fetchone()
        if row is None:
            connection.commit()
            return None
        connection.execute(
            """
            UPDATE notification_outbox
            SET status = 'DELIVERING',
                lease_owner = ?,
                lease_expires_at = ?,
                updated_at = ?
            WHERE notification_id = ?
            """,
            (owner, expires, now, row["notification_id"]),
        )
        connection.commit()
    with connect() as connection:
        return connection.execute(
            """
            SELECT *
            FROM notification_outbox
            WHERE notification_id = ?
            """,
            (row["notification_id"],),
        ).fetchone()


def file_destination(config: dict[str, Any]) -> Path:
    raw = str(
        config.get("channels", {})
        .get("file", {})
        .get(
            "path",
            str(ROOT / "runtime/notifications/delivered.jsonl"),
        )
    )
    return Path(raw).resolve()


def deliver_file(
    config: dict[str, Any],
    row: sqlite3.Row,
    payload: dict[str, Any],
) -> dict[str, Any]:
    destination = file_destination(config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "notification_id": row["notification_id"],
        "event_kind": row["event_kind"],
        "severity": row["severity"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "channel": row["channel"],
        "payload": payload,
        "delivered_at": utc_now(),
    }
    with destination.open("a", encoding="utf-8") as stream:
        stream.write(canonical_json(record) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(destination, 0o640)
    return {
        "path": str(destination),
        "bytes": len(canonical_json(record).encode()),
    }


def deliver_telegram(
    config: dict[str, Any],
    payload: dict[str, Any],
) -> dict[str, Any]:
    credentials = telegram_credentials(config)
    if credentials is None:
        fail("Telegram credentials are not configured")
    token, chat_id, api_base = credentials
    url = f"{api_base}/bot{token}/sendMessage"
    encoded = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": payload["text"],
            "disable_web_page_preview": "true",
        }
    ).encode()
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read(262_144)
            status = int(response.status)
    except urllib.error.HTTPError as error:
        body = error.read(4096)
        raise NotifierError(
            f"Telegram HTTP {error.code}: "
            f"{body.decode('utf-8', errors='replace')[:300]}"
        ) from error
    decoded = json.loads(body.decode("utf-8"))
    if status != 200 or decoded.get("ok") is not True:
        fail(f"Telegram rejected notification: {decoded!r}")
    result = decoded.get("result") or {}
    return {
        "http_status": status,
        "message_id": result.get("message_id"),
        "chat_id": str(
            (result.get("chat") or {}).get("id", chat_id)
        ),
    }


def finish_delivery(
    row: sqlite3.Row,
    *,
    success: bool,
    response: dict[str, Any] | None = None,
    error: str | None = None,
    base_backoff_seconds: int = 15,
) -> None:
    started = utc_now()
    attempt_number = int(row["attempt_count"]) + 1
    finished = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            INSERT INTO notification_deliveries (
                delivery_id,
                notification_id,
                attempt_number,
                status,
                transport_status,
                response_json,
                failure_reason,
                started_at,
                finished_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "delivery-" + uuid.uuid4().hex,
                row["notification_id"],
                attempt_number,
                "DELIVERED" if success else "FAILED",
                "ok" if success else "error",
                canonical_json(response or {}),
                error,
                started,
                finished,
            ),
        )
        if success:
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = 'DELIVERED',
                    attempt_count = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = NULL,
                    updated_at = ?,
                    delivered_at = ?
                WHERE notification_id = ?
                """,
                (
                    attempt_number,
                    finished,
                    finished,
                    row["notification_id"],
                ),
            )
        else:
            terminal = attempt_number >= int(row["max_attempts"])
            delay = min(
                3600,
                base_backoff_seconds * (2 ** max(0, attempt_number - 1)),
            )
            next_attempt = to_utc(
                utc_now_dt() + timedelta(seconds=delay)
            )
            connection.execute(
                """
                UPDATE notification_outbox
                SET status = ?,
                    attempt_count = ?,
                    next_attempt_at = ?,
                    lease_owner = NULL,
                    lease_expires_at = NULL,
                    last_error = ?,
                    updated_at = ?
                WHERE notification_id = ?
                """,
                (
                    "DEAD_LETTER" if terminal else "RETRY",
                    attempt_number,
                    next_attempt,
                    (error or "delivery failed")[:1000],
                    finished,
                    row["notification_id"],
                ),
            )
        connection.commit()


def deliver_row(
    config: dict[str, Any],
    row: sqlite3.Row,
) -> bool:
    payload = json.loads(row["payload_json"])
    try:
        if row["channel"] == "FILE":
            response = deliver_file(config, row, payload)
        elif row["channel"] == "TELEGRAM":
            response = deliver_telegram(config, payload)
        else:
            fail(f"Unsupported channel: {row['channel']}")
    except Exception as error:
        finish_delivery(
            row,
            success=False,
            error=f"{type(error).__name__}: {error}",
            base_backoff_seconds=int(
                config.get("notifier", {})
                .get("base_backoff_seconds", 15)
            ),
        )
        return False
    finish_delivery(row, success=True, response=response)
    return True


def deliver_pending(
    config: dict[str, Any],
    *,
    owner: str,
    limit: int,
    channel: str | None = None,
    notification_id: str | None = None,
) -> dict[str, int]:
    delivered = 0
    failed = 0
    for _ in range(max(0, limit)):
        row = claim_one(
            owner,
            int(
                config.get("notifier", {})
                .get("lease_seconds", 120)
            ),
            channel=channel,
            notification_id=notification_id,
        )
        if row is None:
            break
        if deliver_row(config, row):
            delivered += 1
        else:
            failed += 1
        if notification_id:
            break
    return {"delivered": delivered, "failed": failed}


def status_payload(
    config: dict[str, Any],
    *,
    lock_held: bool,
) -> dict[str, Any]:
    with connect() as connection:
        counts = {
            row["status"]: int(row["count"])
            for row in connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM notification_outbox
                GROUP BY status
                ORDER BY status
                """
            )
        }
        instance = connection.execute(
            """
            SELECT *
            FROM notifier_instances
            ORDER BY started_at DESC
            LIMIT 1
            """
        ).fetchone()
    return {
        "version": VERSION,
        "instance": dict(instance) if instance else None,
        "lock_held": lock_held,
        "outbox_counts": counts,
        "telegram_configured": telegram_credentials(config) is not None,
        "runtime_status_path": str(STATUS_PATH),
    }


def register_instance(owner: str) -> str:
    instance_id = "notifier-instance-" + uuid.uuid4().hex
    now = utc_now()
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            UPDATE notifier_instances
            SET status = 'ABANDONED',
                stopped_at = ?,
                last_error = COALESCE(
                    last_error,
                    'notifier process restarted'
                )
            WHERE status = 'RUNNING'
            """,
            (now,),
        )
        connection.execute(
            """
            INSERT INTO notifier_instances (
                instance_id,
                owner,
                hostname,
                pid,
                version,
                status,
                started_at,
                heartbeat_at,
                stopped_at,
                last_error
            )
            VALUES (?, ?, ?, ?, ?, 'RUNNING', ?, ?, NULL, NULL)
            """,
            (
                instance_id,
                owner,
                socket.gethostname(),
                os.getpid(),
                VERSION,
                now,
                now,
            ),
        )
        reconcile_abandoned_deliveries(connection)
        connection.commit()
    return instance_id


def heartbeat(instance_id: str) -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE notifier_instances
            SET heartbeat_at = ?
            WHERE instance_id = ?
              AND status = 'RUNNING'
            """,
            (utc_now(), instance_id),
        )
        connection.commit()


def stop_instance(
    instance_id: str,
    *,
    status: str = "STOPPED",
    error: str | None = None,
) -> None:
    with connect() as connection:
        connection.execute(
            """
            UPDATE notifier_instances
            SET status = ?,
                stopped_at = ?,
                heartbeat_at = ?,
                last_error = ?
            WHERE instance_id = ?
            """,
            (
                status,
                utc_now(),
                utc_now(),
                error,
                instance_id,
            ),
        )
        connection.commit()


def signal_handler(signum: int, _frame: Any) -> None:
    global STOP_REQUESTED
    STOP_REQUESTED = True
    print(
        canonical_json({"event": "signal", "signal": signum}),
        flush=True,
    )


def daemon() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    lock_stream = LOCK_PATH.open("a+")
    try:
        fcntl.flock(
            lock_stream.fileno(),
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    except BlockingIOError as error:
        fail("Another notifier instance already owns the lock")
    os.chmod(LOCK_PATH, 0o640)

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    config = load_config()
    owner = f"ops-notifier:{socket.gethostname()}"
    instance_id = register_instance(owner)
    poll_seconds = int(
        config.get("notifier", {}).get("poll_seconds", 5)
    )
    batch_size = int(
        config.get("notifier", {}).get("batch_size", 20)
    )
    try:
        while not STOP_REQUESTED:
            heartbeat(instance_id)
            collection = collect_events(config)
            delivery = deliver_pending(
                config,
                owner=owner,
                limit=batch_size,
            )
            payload = status_payload(config, lock_held=True)
            payload["collection"] = collection
            payload["delivery"] = delivery
            write_status(payload)
            for _ in range(max(1, poll_seconds * 10)):
                if STOP_REQUESTED:
                    break
                time.sleep(0.1)
        stop_instance(instance_id)
    except Exception as error:
        stop_instance(
            instance_id,
            status="FAILED",
            error=f"{type(error).__name__}: {error}",
        )
        raise
    finally:
        write_status(status_payload(config, lock_held=False))


def command_run_once(arguments: argparse.Namespace) -> None:
    config = load_config()
    owner = arguments.owner or f"ops-notifier-once:{socket.gethostname()}"
    collection = collect_events(config)
    delivery = deliver_pending(
        config,
        owner=owner,
        limit=arguments.limit,
        channel=arguments.channel,
        notification_id=arguments.notification,
    )
    print(
        json.dumps(
            {
                "collection": collection,
                "delivery": delivery,
            },
            indent=2,
            sort_keys=True,
        )
    )


def command_collect(_: argparse.Namespace) -> None:
    print(
        json.dumps(
            collect_events(load_config()),
            indent=2,
            sort_keys=True,
        )
    )


def command_status(_: argparse.Namespace) -> None:
    config = load_config()
    lock_held = False
    RUNTIME.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as stream:
        try:
            fcntl.flock(
                stream.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            lock_held = True
    print(
        json.dumps(
            status_payload(config, lock_held=lock_held),
            indent=2,
            sort_keys=True,
        )
    )


def command_list(arguments: argparse.Namespace) -> None:
    query = """
        SELECT
            notification_id,
            event_kind,
            severity,
            subject_type,
            subject_id,
            channel,
            status,
            attempt_count,
            max_attempts,
            next_attempt_at,
            created_at,
            delivered_at,
            last_error
        FROM notification_outbox
    """
    parameters: list[Any] = []
    clauses = []
    if arguments.status:
        clauses.append("status = ?")
        parameters.append(arguments.status)
    if arguments.channel:
        clauses.append("channel = ?")
        parameters.append(arguments.channel)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC LIMIT ?"
    parameters.append(arguments.limit)
    with connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(query, tuple(parameters))
        ]
    print(json.dumps(rows, indent=2, sort_keys=True))


def command_test_message(arguments: argparse.Namespace) -> None:
    config = load_config()
    channel = arguments.channel
    if not channel_enabled(config, channel):
        fail(f"Channel is not enabled/configured: {channel}")
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        identifier = enqueue(
            connection,
            dedupe_key=(
                arguments.dedupe_key
                or "test:" + uuid.uuid4().hex + ":" + channel
            ),
            event_kind="TEST_MESSAGE",
            severity=arguments.severity,
            subject_type="TEST",
            subject_id=arguments.subject_id or "manual-test",
            channel=channel,
            title="Orchestra test",
            text=arguments.text,
            metadata={"source": "test-message"},
            max_attempts=arguments.max_attempts,
        )
        connection.commit()
    if identifier is None:
        with connect() as connection:
            row = connection.execute(
                """
                SELECT notification_id
                FROM notification_outbox
                WHERE dedupe_key = ?
                """,
                (arguments.dedupe_key,),
            ).fetchone()
        identifier = row["notification_id"] if row else None
    result: dict[str, Any] = {"notification_id": identifier}
    if arguments.deliver and identifier:
        result["delivery"] = deliver_pending(
            config,
            owner=f"ops-notifier-test:{socket.gethostname()}",
            limit=1,
            notification_id=identifier,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


def command_self_test(_: argparse.Namespace) -> None:
    expected = {
        "OBJECTIVE_COMPLETED": "INFO",
        "OBJECTIVE_FAILED": "CRITICAL",
        "OBJECTIVE_CANCELLED": "WARNING",
        "APPROVAL_PENDING": "CRITICAL",
    }
    if set(expected.values()) != {"INFO", "WARNING", "CRITICAL"}:
        fail("Notification severity matrix is incomplete")
    if VERSION != "notifier-v1":
        fail("Unexpected notifier version")
    print("Orchestra durable notification outbox: PASS")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Orchestra durable notification outbox"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    daemon_parser = sub.add_parser("daemon")
    daemon_parser.set_defaults(function=lambda _: daemon())

    once = sub.add_parser("run-once")
    once.add_argument("--owner")
    once.add_argument("--limit", type=int, default=50)
    once.add_argument(
        "--channel",
        choices=("FILE", "TELEGRAM"),
    )
    once.add_argument("--notification")
    once.set_defaults(function=command_run_once)

    collect = sub.add_parser("collect")
    collect.set_defaults(function=command_collect)

    status = sub.add_parser("status")
    status.set_defaults(function=command_status)

    listing = sub.add_parser("list")
    listing.add_argument(
        "--status",
        choices=(
            "PENDING",
            "DELIVERING",
            "RETRY",
            "DELIVERED",
            "DEAD_LETTER",
            "SUPPRESSED",
        ),
    )
    listing.add_argument(
        "--channel",
        choices=("FILE", "TELEGRAM"),
    )
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(function=command_list)

    test = sub.add_parser("test-message")
    test.add_argument(
        "--channel",
        choices=("FILE", "TELEGRAM"),
        required=True,
    )
    test.add_argument("--text", required=True)
    test.add_argument("--dedupe-key")
    test.add_argument("--subject-id")
    test.add_argument(
        "--severity",
        choices=("INFO", "WARNING", "CRITICAL"),
        default="INFO",
    )
    test.add_argument("--max-attempts", type=int, default=3)
    test.add_argument("--deliver", action="store_true")
    test.set_defaults(function=command_test_message)

    self_test = sub.add_parser("self-test")
    self_test.set_defaults(function=command_self_test)

    return parser


def main() -> None:
    arguments = build_parser().parse_args()
    try:
        arguments.function(arguments)
    except NotifierError as error:
        print(f"Notifier error: {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
