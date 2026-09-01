#!/usr/bin/env python3
from __future__ import annotations

import argparse
import http.client
import ipaddress
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit


FORBIDDEN_KEYS = {
    "instruction",
    "acceptance_json",
    "marker",
    "plan_json",
    "last_error",
    "result_json",
    "failure_reason",
    "assigned_by",
    "claim_owner",
    "executor_instance_id",
    "internal_run_id",
    "transaction_owner",
    "worktree_path",
    "prompt_path",
    "output_path",
}


def fail(message: str) -> None:
    raise SystemExit(f"orchestration_read_probe_failed: {message}")


def base_url(value: str) -> tuple[str, int]:
    parsed = urlsplit(value)
    if parsed.scheme != "http" or parsed.username or parsed.password:
        fail("base URL must be plain HTTP without credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        fail("base URL must not include a path, query, or fragment")
    host = parsed.hostname or ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        fail("base URL host must be a literal IP address")
    if not address.is_loopback:
        fail("base URL must use a loopback address")
    return host, parsed.port or 80


def session_token() -> str:
    root = Path(os.environ.get("ORCHESTRA_ROOT", "/opt/orchestra")).resolve()
    path = Path(
        os.environ.get(
            "ORCHESTRA_CONTROLLER_SESSION_FILE",
            str(root / "secrets/controller-session"),
        )
    ).resolve()
    try:
        token = path.read_text(encoding="ascii").strip()
    except OSError as error:
        fail(f"unable to read Controller session: {error}")
    if not 32 <= len(token) <= 256:
        fail("Controller session has an invalid length")
    return token


def request(
    host: str,
    port: int,
    path: str,
    *,
    token: str,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection(host, port, timeout=timeout)
    try:
        connection.request(
            "GET",
            path,
            headers={
                "Accept": "application/json",
                "Cookie": f"orchestra_session={token}",
            },
        )
        response = connection.getresponse()
        body = response.read()
    finally:
        connection.close()
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        fail(f"{path} returned invalid JSON: {error}")
    if not isinstance(payload, dict):
        fail(f"{path} returned a non-object payload")
    return response.status, payload


def assert_safe(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in FORBIDDEN_KEYS:
                fail(f"forbidden key {key!r} exposed at {path}")
            assert_safe(item, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            assert_safe(item, path=f"{path}[{index}]")
    elif isinstance(value, str):
        lowered = value.lower()
        for fragment in (
            "/opt/docker/",
            "/home/trader/",
            "controller-session",
            "auth.json",
            "api_key",
            "bearer ",
        ):
            if fragment in lowered:
                fail(f"sensitive value fragment {fragment!r} exposed at {path}")


def data_list(payload: dict[str, Any], *, route: str) -> list[dict[str, Any]]:
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, list) or not isinstance(meta, dict):
        fail(f"{route} did not return a collection envelope")
    for item in data:
        if not isinstance(item, dict):
            fail(f"{route} returned a non-object collection member")
    return data


def data_object(payload: dict[str, Any], *, route: str) -> dict[str, Any]:
    data = payload.get("data")
    meta = payload.get("meta")
    if not isinstance(data, dict) or not isinstance(meta, dict):
        fail(f"{route} did not return a resource envelope")
    return data


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe Orchestra public orchestration read routes"
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8765")
    parser.add_argument("--timeout", type=float, default=10.0)
    arguments = parser.parse_args()
    host, port = base_url(arguments.base_url)
    token = session_token()

    status, capabilities_payload = request(
        host, port, "/api/v1/system/capabilities",
        token=token, timeout=arguments.timeout,
    )
    if status != 200:
        fail(f"capabilities returned HTTP {status}")
    capabilities = data_object(capabilities_payload, route="capabilities")
    features = capabilities.get("features")
    if not isinstance(features, dict):
        fail("capabilities features are absent")
    for feature in (
        "orchestration_plan_reads",
        "orchestration_graph_reads",
        "orchestration_attempt_reads",
        "reviewer_assignment_reads",
    ):
        if features.get(feature) is not True:
            fail(f"capability {feature} is not enabled")

    plan_route = "/api/v1/plans?limit=2"
    status, payload = request(
        host, port, plan_route, token=token, timeout=arguments.timeout
    )
    if status != 200:
        fail(f"plan list returned HTTP {status}")
    plans = data_list(payload, route=plan_route)
    assert_safe(payload)

    task_count = 0
    dependency_count = 0
    attempt_count = 0
    nested_assignment_count = 0

    if plans:
        plan_id = plans[0].get("id")
        if not isinstance(plan_id, str):
            fail("plan list returned an invalid identifier")
        encoded_plan = quote(plan_id, safe="")
        detail_route = f"/api/v1/plans/{encoded_plan}"
        status, detail_payload = request(
            host, port, detail_route, token=token, timeout=arguments.timeout
        )
        if status != 200:
            fail(f"plan detail returned HTTP {status}")
        plan = data_object(detail_payload, route=detail_route)
        if plan.get("id") != plan_id:
            fail("plan detail identifier mismatch")
        assert_safe(detail_payload)

        for suffix, counter_name in (
            ("tasks", "tasks"),
            ("dependencies", "dependencies"),
            ("attempts", "attempts"),
        ):
            route = f"/api/v1/plans/{encoded_plan}/{suffix}?limit=2"
            status, collection_payload = request(
                host, port, route, token=token, timeout=arguments.timeout
            )
            if status != 200:
                fail(f"{suffix} list returned HTTP {status}")
            collection = data_list(collection_payload, route=route)
            assert_safe(collection_payload)
            if counter_name == "tasks":
                task_count = len(collection)
            elif counter_name == "dependencies":
                dependency_count = len(collection)
            else:
                attempt_count = len(collection)
                if collection:
                    run_id = collection[0].get("run_id")
                    if isinstance(run_id, str):
                        nested_route = (
                            f"/api/v1/runs/{quote(run_id, safe='')}"
                            "/reviewer-assignments?limit=2"
                        )
                        nested_status, nested_payload = request(
                            host, port, nested_route,
                            token=token, timeout=arguments.timeout,
                        )
                        if nested_status != 200:
                            fail(
                                "nested reviewer assignment list returned "
                                f"HTTP {nested_status}"
                            )
                        nested = data_list(nested_payload, route=nested_route)
                        nested_assignment_count = len(nested)
                        assert_safe(nested_payload)

    assignment_route = "/api/v1/reviewer-assignments?limit=2"
    status, assignment_payload = request(
        host, port, assignment_route, token=token, timeout=arguments.timeout
    )
    if status != 200:
        fail(f"reviewer assignment list returned HTTP {status}")
    assignments = data_list(assignment_payload, route=assignment_route)
    assert_safe(assignment_payload)

    if assignments:
        assignment_id = assignments[0].get("id")
        if not isinstance(assignment_id, str):
            fail("reviewer assignment list returned an invalid identifier")
        detail_route = (
            "/api/v1/reviewer-assignments/"
            + quote(assignment_id, safe="")
        )
        detail_status, detail_payload = request(
            host, port, detail_route,
            token=token, timeout=arguments.timeout,
        )
        if detail_status != 200:
            fail(f"reviewer assignment detail returned HTTP {detail_status}")
        detail = data_object(detail_payload, route=detail_route)
        if detail.get("id") != assignment_id:
            fail("reviewer assignment detail identifier mismatch")
        assert_safe(detail_payload)

    print(
        "Controller orchestration reads probe: PASS "
        f"plans={len(plans)} "
        f"tasks={task_count} "
        f"dependencies={dependency_count} "
        f"attempts={attempt_count} "
        f"assignments={len(assignments)} "
        f"nested_assignments={nested_assignment_count}"
    )


if __name__ == "__main__":
    main()
