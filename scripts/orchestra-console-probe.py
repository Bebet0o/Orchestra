#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import json
import time
from urllib.parse import urlsplit

ROUTES = (
    "/",
    "/dashboard",
    "/projects",
    "/blueprints",
    "/objectives",
    "/executions",
    "/reviews",
    "/events",
    "/administration",
)
REQUIRED_HEADERS = {
    "content-security-policy": "default-src 'none'",
    "cross-origin-resource-policy": "same-origin",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


class ProbeError(RuntimeError):
    pass


def request(
    host: str,
    port: int,
    path: str,
    *,
    method: str = "GET",
    timeout: float = 3.0,
    headers: dict[str, str] | None = None,
    body: bytes | None = None,
):
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    request_headers = {"Host": f"{host}:{port}"}
    request_headers.update(headers or {})
    try:
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        payload = response.read(1_100_000)
        return response.status, {name.lower(): value for name, value in response.getheaders()}, payload
    finally:
        connection.close()


def validate(base_url: str, wait_seconds: float) -> None:
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "http"
        or parsed.hostname not in {"127.0.0.1", "::1"}
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ProbeError("Console probe requires one canonical loopback HTTP origin")
    port = parsed.port or 80
    deadline = time.monotonic() + wait_seconds
    last_error: BaseException | None = None
    while True:
        try:
            status, headers, body = request(parsed.hostname, port, "/health")
            if status != 200:
                raise ProbeError(f"Console health returned HTTP {status}")
            payload = json.loads(body)
            if payload != {"service": "orchestra-console", "status": "ok"}:
                raise ProbeError("Console health payload is invalid")
            break
        except (OSError, ValueError, ProbeError) as error:
            last_error = error
            if time.monotonic() >= deadline:
                raise ProbeError(f"Console did not become ready: {last_error}") from error
            time.sleep(0.25)

    for route in ROUTES:
        status, headers, body = request(parsed.hostname, port, route)
        if status != 200 or b"Orchestra Console" not in body:
            raise ProbeError(f"Console route failed: {route}")
        for name, fragment in REQUIRED_HEADERS.items():
            if fragment not in headers.get(name, ""):
                raise ProbeError(f"Console security header missing on {route}: {name}")
        csp = headers.get("content-security-policy", "")
        if "connect-src 'self'" not in csp or "form-action 'self'" not in csp:
            raise ProbeError(f"Console Controller-client CSP is invalid on {route}")
        if headers.get("cache-control") != "no-store":
            raise ProbeError(f"Console cache policy is invalid on {route}")
        if not headers.get("x-request-id", "").startswith("req_"):
            raise ProbeError(f"Console request ID is invalid on {route}")

    status, _, _ = request(parsed.hostname, port, "/hermesfiles")
    if status != 404:
        raise ProbeError("Legacy Console route remains available")

    status, _, body = request(parsed.hostname, port, "/assets/app.js")
    if (
        status != 200
        or b"createControllerClient" not in body
        or b"refreshDashboard" not in body
        or b"Promise.allSettled" not in body
        or b"refreshProjects" not in body
        or b"client.createProject" not in body
        or b"client.updateProject" not in body
        or b"client.commandProject" not in body
        or b"refreshBlueprints" not in body
        or b"client.validateBlueprint" not in body
        or b"client.createBlueprint" not in body
        or b"client.updateBlueprint" not in body
        or b"client.compareBlueprintRevisions" not in body
        or b"refreshObjectives" not in body
        or b"client.createObjective" not in body
        or b"client.commandObjective" not in body
        or b"client.operation" not in body
        or b"fetch(" in body
        or b"innerHTML" in body
    ):
        raise ProbeError("Console application script does not isolate the operational dashboard client")

    status, _, body = request(parsed.hostname, port, "/assets/controller-client.js")
    if (
        status != 200
        or b"fetch(" not in body
        or b'credentials: "same-origin"' not in body
        or b"127.0.0.1:8765" in body
        or b"localStorage" in body
        or b"sessionStorage" in body
        or b"WebSocket(" in body
        or b'path: "/api/v1/projects"' not in body
        or b'path: "/api/v1/objectives"' not in body
        or b'path: "/api/v1/reviews"' not in body
        or b'path: "/api/v1/recoveries"' not in body
        or b'path: "/api/v1/plans"' not in body
        or b'path: "/api/v1/reviewer-assignments"' not in body
        or b"createProject" not in body
        or b"updateProject" not in body
        or b"commandProject" not in body
        or b"validateBlueprint" not in body
        or b"createBlueprint" not in body
        or b"updateBlueprint" not in body
        or b"compareBlueprintRevisions" not in body
        or b"createObjective" not in body
        or b"commandObjective" not in body
        or b"async operation" not in body
        or b'"delete"' in body
        or b"buildBlueprint" in body
        or b"activateBlueprint" in body
    ):
        raise ProbeError("Console Controller client violates the 2U browser boundary")

    status, headers, body = request(parsed.hostname, port, "/", method="HEAD")
    if status != 200 or body or int(headers.get("content-length", "0")) <= 0:
        raise ProbeError("Console HEAD contract failed")

    status, headers, body = request(parsed.hostname, port, "/api/v1/auth/session")
    if status != 401:
        raise ProbeError(f"Unauthenticated same-origin session probe returned HTTP {status}")
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ProbeError("Controller proxy returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise ProbeError("Controller proxy payload is invalid")
    if "access-control-allow-origin" in headers or "access-control-allow-credentials" in headers:
        raise ProbeError("Controller proxy leaked cross-origin response headers")
    if "connect-src 'self'" not in headers.get("content-security-policy", ""):
        raise ProbeError("Controller proxy response lacks Console security headers")
    if not headers.get("x-request-id", "").startswith("req_"):
        raise ProbeError("Controller proxy request ID is invalid")

    for path in (
        "/api/v1/projects",
        "/api/v1/objectives",
        "/api/v1/reviews",
        "/api/v1/recoveries",
        "/api/v1/plans",
        "/api/v1/reviewer-assignments",
    ):
        status, _, _ = request(parsed.hostname, port, path)
        if status != 401:
            raise ProbeError(f"Unauthenticated dashboard proxy returned HTTP {status}: {path}")

    status, _, _ = request(parsed.hostname, port, "/api/v1/projects/probe-project")
    if status != 401:
        raise ProbeError(f"Unauthenticated project detail returned HTTP {status}")

    probe_objective = "objective-" + "a" * 32
    probe_operation = "operation-" + "a" * 32
    for path in (
        f"/api/v1/objectives/{probe_objective}",
        f"/api/v1/operations/{probe_operation}",
    ):
        status, _, _ = request(parsed.hostname, port, path)
        if status != 401:
            raise ProbeError(f"Unauthenticated objective proxy returned HTTP {status}: {path}")

    probe_sandbox = "sandbox-" + "a" * 32
    status, _, _ = request(parsed.hostname, port, "/api/v1/hermesfiles")
    if status != 404:
        raise ProbeError("Legacy Controller proxy remains available")
    for path in (
        "/api/v1/blueprints",
        "/api/v1/blueprints/template",
        f"/api/v1/blueprints/{probe_sandbox}",
        f"/api/v1/blueprints/{probe_sandbox}/revisions",
        f"/api/v1/blueprints/{probe_sandbox}/revisions/1",
        f"/api/v1/blueprints/{probe_sandbox}/diff?from=1&to=2",
    ):
        status, _, _ = request(parsed.hostname, port, path)
        if status != 401:
            raise ProbeError(f"Unauthenticated Blueprint proxy returned HTTP {status}: {path}")

    mutation_headers = {
        "Origin": base_url,
        "Content-Type": "application/json",
        "Idempotency-Key": "console-probe-project-0001",
        "X-CSRF-Token": "csrf1.probe",
        "If-Match": '"1"',
    }
    for method, path in (
        ("POST", "/api/v1/projects"),
        ("PATCH", "/api/v1/projects/probe-project"),
        ("POST", "/api/v1/projects/probe-project/commands/enable"),
        ("POST", "/api/v1/blueprints/validate"),
        ("POST", "/api/v1/blueprints"),
        ("PATCH", f"/api/v1/blueprints/{probe_sandbox}"),
        ("POST", "/api/v1/objectives"),
        ("POST", f"/api/v1/objectives/{probe_objective}/commands/pause"),
        ("POST", f"/api/v1/objectives/{probe_objective}/commands/resume"),
        ("POST", f"/api/v1/objectives/{probe_objective}/commands/cancel"),
    ):
        status, _, _ = request(
            parsed.hostname,
            port,
            path,
            method=method,
            headers=mutation_headers,
            body=b"{}",
        )
        if status != 401:
            raise ProbeError(f"Unauthenticated project mutation returned HTTP {status}: {method} {path}")

    for method, path in (
        ("GET", "/api/v1/tasks"),
        ("POST", "/api/v1/projects/probe-project/commands/delete"),
        ("POST", f"/api/v1/blueprints/{probe_sandbox}/builds"),
        ("POST", f"/api/v1/blueprints/{probe_sandbox}/activate"),
        ("POST", f"/api/v1/objectives/{probe_objective}/commands/start"),
        ("POST", f"/api/v1/objectives/{probe_objective}/commands/replan"),
        ("POST", f"/api/v1/objectives/{probe_objective}/commands/archive"),
        ("GET", f"/api/v1/objectives/{probe_objective}/tasks"),
    ):
        status, _, _ = request(
            parsed.hostname,
            port,
            path,
            method=method,
            headers=mutation_headers if method == "POST" else None,
            body=b"{}" if method == "POST" else None,
        )
        if status != 404:
            raise ProbeError(f"Console exposes an out-of-scope Controller route: {method} {path}")

    print(
        f"Orchestra Console probe: PASS routes={len(ROUTES)} port={port} "
        "session_proxy=401 dashboard_proxy=401 project_lifecycle_proxy=401 "
        "blueprint_lifecycle_proxy=401 objective_lifecycle_proxy=401"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the Orchestra Console browser client")
    parser.add_argument("--base-url", default="http://127.0.0.1:8788")
    parser.add_argument("--wait-seconds", type=float, default=10.0)
    arguments = parser.parse_args()
    if not 0 <= arguments.wait_seconds <= 60:
        parser.error("--wait-seconds must be between 0 and 60")
    try:
        validate(arguments.base_url, arguments.wait_seconds)
    except ProbeError as error:
        print(f"Console probe failed: {error}", file=__import__("sys").stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
