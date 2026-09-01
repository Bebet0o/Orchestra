from __future__ import annotations

import http.client
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .service_support import (
    MAX_RESPONSE_BYTES,
    ServiceSupportError,
    _request,
    _validated_base_url,
    read_session,
)


@dataclass(frozen=True)
class ObjectiveCommandProbeResult:
    csrf_status: int
    create_status: int
    pause_status: int
    resume_status: int
    cancel_status: int
    operation_status: int
    objective_status: int


def _post(
    host: str,
    port: int,
    path: str,
    *,
    token: str,
    idempotency_key: str,
    body: dict[str, Any],
    csrf_token: str | None = None,
    timeout: float,
) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        "Cookie": f"orchestra_session={token}",
        "Idempotency-Key": idempotency_key,
    }
    if csrf_token is not None:
        headers["X-CSRF-Token"] = csrf_token
    try:
        connection.request("POST", path, body=raw, headers=headers)
        response = connection.getresponse()
        payload_raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(payload_raw) > MAX_RESPONSE_BYTES:
            raise ServiceSupportError(f"Controller mutation response is too large for {path}.")
        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise ServiceSupportError(
                f"Controller mutation probe received invalid JSON from {path}."
            ) from error
        if not isinstance(payload, dict):
            raise ServiceSupportError(
                f"Controller mutation probe received a non-object response from {path}."
            )
        return response.status, payload
    finally:
        connection.close()


def _data_object(payload: dict[str, object], label: str) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ServiceSupportError(f"{label} did not return an object.")
    return data


def probe_objective_commands(
    base_url: str,
    session_file: Path,
    *,
    wait_seconds: float = 10,
) -> ObjectiveCommandProbeResult:
    if not 0 < wait_seconds <= 300:
        raise ServiceSupportError("Objective command probe wait must be 0..300 seconds.")
    host, port = _validated_base_url(base_url)
    token = read_session(session_file)
    nonce = uuid.uuid4().hex

    project_status, project_payload = _request(
        host,
        port,
        "/api/v1/projects?limit=100",
        token=token,
        timeout=wait_seconds,
    )
    projects = project_payload.get("data")
    if project_status != 200 or not isinstance(projects, list):
        raise ServiceSupportError("Project discovery did not return HTTP 200.")
    project_id: str | None = None
    for item in projects:
        if isinstance(item, dict) and item.get("state") == "enabled" and isinstance(item.get("id"), str):
            project_id = str(item["id"])
            break
    if project_id is None:
        raise ServiceSupportError("No enabled project is available for the command probe.")

    csrf_status, csrf_payload = _post(
        host,
        port,
        "/api/v1/auth/csrf",
        token=token,
        idempotency_key=f"probe-csrf-{nonce}",
        body={},
        timeout=wait_seconds,
    )
    csrf_data = _data_object(csrf_payload, "CSRF probe")
    csrf_token = csrf_data.get("token")
    if csrf_status != 200 or not isinstance(csrf_token, str):
        raise ServiceSupportError("CSRF probe did not return HTTP 200.")

    create_status, create_payload = _post(
        host,
        port,
        "/api/v1/objectives",
        token=token,
        csrf_token=csrf_token,
        idempotency_key=f"probe-create-{nonce}",
        body={
            "project_ids": [project_id],
            "title": "Orchestra Controller command probe",
            "description": "Safe future-dated objective created by the installed-service probe.",
            "priority": 1000,
            "not_before": "2099-01-01T00:00:00Z",
            "max_parallel_tasks": 1,
            "planning_max_attempts": 1,
            "constraints": ["Probe only; do not execute before cancellation."],
        },
        timeout=wait_seconds,
    )
    create_data = _data_object(create_payload, "Objective create probe")
    target = create_data.get("target")
    operation_id = create_data.get("id")
    if (
        create_status != 202
        or not isinstance(target, dict)
        or not isinstance(target.get("id"), str)
        or not isinstance(operation_id, str)
    ):
        raise ServiceSupportError("Objective create probe did not return HTTP 202.")
    objective_id = str(target["id"])

    pause_status, _ = _post(
        host,
        port,
        f"/api/v1/objectives/{objective_id}/commands/pause",
        token=token,
        csrf_token=csrf_token,
        idempotency_key=f"probe-pause-{nonce}",
        body={"reason": "installed-service probe"},
        timeout=wait_seconds,
    )
    if pause_status != 202:
        raise ServiceSupportError("Objective pause probe did not return HTTP 202.")

    resume_status, _ = _post(
        host,
        port,
        f"/api/v1/objectives/{objective_id}/commands/resume",
        token=token,
        csrf_token=csrf_token,
        idempotency_key=f"probe-resume-{nonce}",
        body={"reason": "installed-service schedule preservation probe"},
        timeout=wait_seconds,
    )
    if resume_status != 202:
        raise ServiceSupportError("Objective resume probe did not return HTTP 202.")

    resumed_status, resumed_payload = _request(
        host,
        port,
        f"/api/v1/objectives/{objective_id}",
        token=token,
        timeout=wait_seconds,
    )
    resumed = resumed_payload.get("data")
    if (
        resumed_status != 200
        or not isinstance(resumed, dict)
        or resumed.get("raw_state") != "QUEUED"
        or resumed.get("not_before") != "2099-01-01T00:00:00.000Z"
    ):
        raise ServiceSupportError(
            "Objective resume probe did not preserve its future schedule."
        )

    cancel_status, _ = _post(
        host,
        port,
        f"/api/v1/objectives/{objective_id}/commands/cancel",
        token=token,
        csrf_token=csrf_token,
        idempotency_key=f"probe-cancel-{nonce}",
        body={"reason": "installed-service probe cleanup"},
        timeout=wait_seconds,
    )
    if cancel_status != 202:
        raise ServiceSupportError("Objective cancel probe did not return HTTP 202.")

    operation_status, operation_payload = _request(
        host,
        port,
        f"/api/v1/operations/{operation_id}",
        token=token,
        timeout=wait_seconds,
    )
    if operation_status != 200 or not isinstance(operation_payload.get("data"), dict):
        raise ServiceSupportError("Controller operation probe did not return HTTP 200.")

    objective_status, objective_payload = _request(
        host,
        port,
        f"/api/v1/objectives/{objective_id}",
        token=token,
        timeout=wait_seconds,
    )
    objective = objective_payload.get("data")
    if objective_status != 200 or not isinstance(objective, dict) or objective.get("state") != "cancelled":
        raise ServiceSupportError("Cancelled objective probe did not return HTTP 200.")

    return ObjectiveCommandProbeResult(
        csrf_status=csrf_status,
        create_status=create_status,
        pause_status=pause_status,
        resume_status=resume_status,
        cancel_status=cancel_status,
        operation_status=operation_status,
        objective_status=objective_status,
    )
