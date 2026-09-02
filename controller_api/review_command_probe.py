from __future__ import annotations

import http.client
import json
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
class ReviewCommandProbeResult:
    csrf_status: int
    command_status: int
    operation_status: int
    review_status: int


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
                f"Controller review command probe received invalid JSON from {path}."
            ) from error
        if not isinstance(payload, dict):
            raise ServiceSupportError(
                f"Controller review command probe received a non-object response from {path}."
            )
        return response.status, payload
    finally:
        connection.close()


def _data(payload: dict[str, object], label: str) -> dict[str, object]:
    value = payload.get("data")
    if not isinstance(value, dict):
        raise ServiceSupportError(f"{label} did not return an object.")
    return value


def probe_review_commands(
    base_url: str,
    session_file: Path,
    *,
    wait_seconds: float = 10,
) -> ReviewCommandProbeResult:
    if not 0 < wait_seconds <= 300:
        raise ServiceSupportError("Review command probe wait must be 0..300 seconds.")
    host, port = _validated_base_url(base_url)
    token = read_session(session_file)
    nonce = uuid.uuid4().hex

    list_status, list_payload = _request(
        host,
        port,
        "/api/v1/reviews?limit=50",
        token=token,
        timeout=wait_seconds,
    )
    reviews = list_payload.get("data")
    if list_status != 200 or not isinstance(reviews, list) or not reviews:
        raise ServiceSupportError("No review is available for the review command probe.")

    csrf_status, csrf_payload = _post(
        host,
        port,
        "/api/v1/auth/csrf",
        token=token,
        idempotency_key=f"probe-review-csrf-{nonce}",
        body={},
        timeout=wait_seconds,
    )
    csrf_data = _data(csrf_payload, "CSRF probe")
    csrf_token = csrf_data.get("token")
    if csrf_status != 200 or not isinstance(csrf_token, str):
        raise ServiceSupportError("Review command CSRF probe did not return HTTP 200.")

    command_status = 0
    command_payload: dict[str, object] | None = None
    review_id: str | None = None
    for index, item in enumerate(reviews):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        candidate = str(item["id"])
        status, payload = _post(
            host,
            port,
            f"/api/v1/reviews/{candidate}/commands/request-human-review",
            token=token,
            csrf_token=csrf_token,
            idempotency_key=f"probe-review-command-{nonce}-{index}",
            body={"reason": "installed-service bounded human review probe"},
            timeout=wait_seconds,
        )
        if status == 202:
            command_status = status
            command_payload = payload
            review_id = candidate
            break
        code = payload.get("code")
        if status == 409 and code in {
            "human_review_already_required",
            "review_action_already_recorded",
        }:
            continue
        raise ServiceSupportError(
            f"Review command probe failed for {candidate}: HTTP {status} code={code}."
        )
    if command_status != 202 or command_payload is None or review_id is None:
        raise ServiceSupportError("No eligible review accepted request-human-review.")

    operation = _data(command_payload, "Review command probe")
    operation_id = operation.get("id")
    if not isinstance(operation_id, str):
        raise ServiceSupportError("Review command probe did not return an operation id.")
    if operation.get("kind") != "review.request-human-review":
        raise ServiceSupportError("Review command probe returned an unexpected operation kind.")

    operation_status, operation_payload = _request(
        host,
        port,
        f"/api/v1/operations/{operation_id}",
        token=token,
        timeout=wait_seconds,
    )
    operation_data = operation_payload.get("data")
    if (
        operation_status != 200
        or not isinstance(operation_data, dict)
        or operation_data.get("target") != {"type": "review", "id": review_id}
    ):
        raise ServiceSupportError("Review Controller operation probe did not return HTTP 200.")

    review_status, review_payload = _request(
        host,
        port,
        f"/api/v1/reviews/{review_id}",
        token=token,
        timeout=wait_seconds,
    )
    review_data = review_payload.get("data")
    if review_status != 200 or not isinstance(review_data, dict):
        raise ServiceSupportError("Historical review no longer projects after the command.")

    return ReviewCommandProbeResult(
        csrf_status=csrf_status,
        command_status=command_status,
        operation_status=operation_status,
        review_status=review_status,
    )
