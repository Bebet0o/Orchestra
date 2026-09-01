from __future__ import annotations

import http.client
import json
from dataclasses import dataclass

from .service_support import read_session


@dataclass(frozen=True)
class SandboxProfileProbeResult:
    list_status: int
    capabilities_status: int
    profile_count: int


def _request_json(
    *,
    host: str,
    port: int,
    token: str,
    path: str,
) -> tuple[int, dict]:
    connection = http.client.HTTPConnection(host, port, timeout=5)
    host_header = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    connection.request(
        "GET",
        path,
        headers={
            "Accept": "application/json",
            "Cookie": f"orchestra_session={token}",
            "Host": host_header,
        },
    )
    response = connection.getresponse()
    body = response.read()
    status = response.status
    connection.close()
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{path} returned invalid JSON") from error
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} returned an invalid response shape")
    return status, payload


def _assert_public(value) -> None:
    forbidden_keys = {
        "source",
        "source_text",
        "canonical",
        "canonical_json",
        "repo_path",
        "data_path",
        "host_path",
        "secret",
        "secret_value",
        "token",
        "password",
        "credential",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in forbidden_keys:
                raise RuntimeError(
                    "sandbox profile response exposes a private field"
                )
            _assert_public(item)
    elif isinstance(value, list):
        for item in value:
            _assert_public(item)


def probe_sandbox_profiles(
    *,
    host: str,
    port: int,
    session_file,
) -> SandboxProfileProbeResult:
    token = read_session(session_file)
    list_status, payload = _request_json(
        host=host,
        port=port,
        token=token,
        path="/api/v1/sandboxes?limit=10",
    )
    if list_status != 200:
        raise RuntimeError(
            f"sandbox profile list returned HTTP {list_status}"
        )
    data = payload.get("data")
    if not isinstance(data, list):
        raise RuntimeError("sandbox profile list has invalid shape")
    _assert_public(payload)

    capabilities_status, capabilities = _request_json(
        host=host,
        port=port,
        token=token,
        path="/api/v1/system/capabilities",
    )
    if capabilities_status != 200:
        raise RuntimeError(
            "sandbox profile capabilities returned "
            f"HTTP {capabilities_status}"
        )
    features = capabilities.get("data", {}).get("features")
    if not isinstance(features, dict):
        raise RuntimeError("sandbox profile capabilities have invalid shape")
    expected = {
        "sandbox_profile_reads": True,
        "sandbox_profile_operator_import": True,
        "sandbox_profile_http_writes": True,
        "sandbox_profile_http_validation": True,
        "blueprint_builds": False,
    }
    for key, value in expected.items():
        if features.get(key) is not value:
            raise RuntimeError(f"sandbox profile capability drift: {key}")

    return SandboxProfileProbeResult(
        list_status=list_status,
        capabilities_status=capabilities_status,
        profile_count=len(data),
    )
