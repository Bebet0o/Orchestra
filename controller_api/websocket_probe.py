from __future__ import annotations

import base64
import json
import os
import socket
import struct
from dataclasses import dataclass
from typing import BinaryIO

from .websocket_transport import encode_client_frame, websocket_accept_value

MAX_PROBE_HEADER_BYTES = 16_384
MAX_PROBE_FRAME_BYTES = 65_536


class WebSocketProbeError(RuntimeError):
    """Expected local WebSocket probe failure."""


@dataclass(frozen=True)
class WebSocketProbeResult:
    status: int
    connection_id: str
    replay_from: int
    latest_sequence: int


def _readline(stream: BinaryIO, total: list[int]) -> bytes:
    line = stream.readline(MAX_PROBE_HEADER_BYTES + 1)
    total[0] += len(line)
    if not line or total[0] > MAX_PROBE_HEADER_BYTES or len(line) > MAX_PROBE_HEADER_BYTES:
        raise WebSocketProbeError("WebSocket probe received invalid HTTP headers.")
    return line


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise WebSocketProbeError("WebSocket probe connection closed early.")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _read_server_frame(stream: BinaryIO) -> tuple[int, bytes]:
    first, second = _read_exact(stream, 2)
    if first & 0x70 or not first & 0x80:
        raise WebSocketProbeError("WebSocket probe received an invalid frame.")
    opcode = first & 0x0F
    if second & 0x80:
        raise WebSocketProbeError("WebSocket server frames must not be masked.")
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _read_exact(stream, 2))[0]
    elif length == 127:
        encoded = _read_exact(stream, 8)
        if encoded[0] & 0x80:
            raise WebSocketProbeError("WebSocket probe received an invalid length.")
        length = struct.unpack("!Q", encoded)[0]
    if length > MAX_PROBE_FRAME_BYTES:
        raise WebSocketProbeError("WebSocket probe frame is too large.")
    return opcode, _read_exact(stream, length)


def probe_websocket(
    host: str,
    port: int,
    *,
    token: str,
    origin: str,
    timeout: float = 3.0,
) -> WebSocketProbeResult:
    if host not in {"127.0.0.1", "::1"}:
        raise WebSocketProbeError("WebSocket probe accepts a loopback literal only.")
    if not 1 <= port <= 65_535 or not 0.1 <= timeout <= 30.0:
        raise WebSocketProbeError("WebSocket probe settings are invalid.")
    if not token or any(character in token for character in "\r\n"):
        raise WebSocketProbeError("WebSocket probe session is invalid.")
    if not origin or any(character in origin for character in "\r\n"):
        raise WebSocketProbeError("WebSocket probe origin is invalid.")

    key = base64.b64encode(os.urandom(16)).decode("ascii")
    authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    request = (
        "GET /api/v1/events HTTP/1.1\r\n"
        f"Host: {authority}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Origin: {origin}\r\n"
        f"Cookie: orchestra_session={token}\r\n"
        "Last-Event-Sequence: 0\r\n"
        "\r\n"
    ).encode("ascii")

    connection = socket.create_connection((host, port), timeout=timeout)
    connection.settimeout(timeout)
    stream = connection.makefile("rb")
    try:
        connection.sendall(request)
        total = [0]
        status_line = _readline(stream, total).decode("iso-8859-1").rstrip("\r\n")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[1].isdigit():
            raise WebSocketProbeError("WebSocket probe received an invalid status line.")
        status = int(parts[1])
        headers: dict[str, str] = {}
        while True:
            line = _readline(stream, total)
            if line in {b"\r\n", b"\n"}:
                break
            decoded = line.decode("iso-8859-1").rstrip("\r\n")
            name, separator, value = decoded.partition(":")
            if not separator:
                raise WebSocketProbeError("WebSocket probe received a malformed header.")
            normalized = name.strip().lower()
            if normalized in headers:
                raise WebSocketProbeError("WebSocket probe received duplicate headers.")
            headers[normalized] = value.strip()

        if status != 101:
            raise WebSocketProbeError(f"WebSocket upgrade failed with HTTP {status}.")
        if headers.get("upgrade", "").lower() != "websocket":
            raise WebSocketProbeError("WebSocket upgrade response is missing Upgrade.")
        if "upgrade" not in {
            token.strip().lower()
            for token in headers.get("connection", "").split(",")
        }:
            raise WebSocketProbeError("WebSocket upgrade response is missing Connection.")
        if headers.get("sec-websocket-accept") != websocket_accept_value(key):
            raise WebSocketProbeError("WebSocket accept value is invalid.")

        opcode, payload = _read_server_frame(stream)
        if opcode != 0x1:
            raise WebSocketProbeError("WebSocket probe expected a text frame.")
        try:
            message = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise WebSocketProbeError("WebSocket probe received invalid JSON.") from error
        if (
            not isinstance(message, dict)
            or message.get("type") != "subscribed"
            or not isinstance(message.get("connection_id"), str)
            or type(message.get("replay_from")) is not int
            or type(message.get("latest_sequence")) is not int
        ):
            raise WebSocketProbeError("WebSocket subscribed frame is invalid.")

        connection.sendall(encode_client_frame(0x8, struct.pack("!H", 1000)))
        return WebSocketProbeResult(
            status=status,
            connection_id=message["connection_id"],
            replay_from=message["replay_from"],
            latest_sequence=message["latest_sequence"],
        )
    except (OSError, socket.timeout) as error:
        raise WebSocketProbeError(f"WebSocket probe failed: {error}") from error
    finally:
        try:
            stream.close()
        finally:
            connection.close()
