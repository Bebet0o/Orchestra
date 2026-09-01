from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import sqlite3
import struct
import unittest
from contextlib import closing
from unittest import mock

from controller_api.event_journal import EventJournal
from controller_api.websocket_probe import _read_server_frame
from controller_api.websocket_transport import encode_client_frame, websocket_accept_value
from tests.test_controller_api import APIFixture, TOKEN

ORIGIN = "http://127.0.0.1:8787"


class RawClient:
    def __init__(
        self,
        fixture: APIFixture,
        *,
        path: str = "/api/v1/events",
        http_version: str = "HTTP/1.1",
        last_sequence: int | None = 0,
        key: str | None = None,
    ) -> None:
        self.socket = socket.create_connection(("127.0.0.1", fixture.port), timeout=3)
        self.socket.settimeout(3)
        self.stream = self.socket.makefile("rb")
        key = key or base64.b64encode(os.urandom(16)).decode("ascii")
        headers = [
            f"GET {path} {http_version}",
            f"Host: 127.0.0.1:{fixture.port}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            "Sec-WebSocket-Version: 13",
            f"Sec-WebSocket-Key: {key}",
            f"Origin: {ORIGIN}",
            f"Cookie: orchestra_session={TOKEN}",
        ]
        if last_sequence is not None:
            headers.append(f"Last-Event-Sequence: {last_sequence}")
        self.socket.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("iso-8859-1"))
        status_line = self.stream.readline().decode("iso-8859-1").rstrip("\r\n")
        self.status = int(status_line.split(" ", 2)[1])
        self.headers: dict[str, str] = {}
        while True:
            line = self.stream.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            name, value = line.decode("iso-8859-1").rstrip("\r\n").split(":", 1)
            self.headers[name.lower()] = value.strip()
        self.accept_valid = (
            self.status == 101
            and self.headers.get("sec-websocket-accept") == websocket_accept_value(key)
        )

    def send(self, opcode: int, payload: bytes) -> None:
        self.socket.sendall(encode_client_frame(opcode, payload))

    def send_json(self, value: object) -> None:
        self.send(0x1, json.dumps(value, separators=(",", ":")).encode("utf-8"))

    def frame(self) -> tuple[int, bytes]:
        return _read_server_frame(self.stream)

    def json(self) -> dict[str, object]:
        opcode, payload = self.frame()
        if opcode != 0x1:
            raise AssertionError(f"expected text frame, got {opcode}")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise AssertionError("expected object")
        return value

    def close(self) -> None:
        try:
            self.stream.close()
        finally:
            self.socket.close()


class WebSocketAdversarialTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = APIFixture()
        self.clients: list[RawClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        self.fixture.close()

    def client(self, **kwargs: object) -> RawClient:
        client = RawClient(self.fixture, **kwargs)
        self.clients.append(client)
        return client

    def emit_many(self, count: int) -> None:
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            for index in range(count):
                aggregate_id = f"objective-adversarial-{index:04d}"
                EventJournal.emit(
                    connection,
                    event_type="objective.created",
                    actor_type="system",
                    actor_id="websocket-adversarial-test",
                    aggregate_type="objective",
                    aggregate_id=aggregate_id,
                    correlation_id="corr_" + hashlib.sha256(aggregate_id.encode()).hexdigest()[:32],
                    project_id="alpha",
                    data={"index": index},
                )
            connection.commit()

    def test_http_10_upgrade_is_rejected(self) -> None:
        client = self.client(http_version="HTTP/1.0")
        self.assertEqual(client.status, 400)

    def test_rejected_query_values_are_redacted_from_logs(self) -> None:
        marker = "query-secret-must-not-be-logged"
        with mock.patch("controller_api.server.LOGGER.info") as logged:
            client = self.client(path=f"/api/v1/events?token={marker}")
            self.assertEqual(client.status, 400)
        rendered = " ".join(str(part) for call in logged.call_args_list for part in call.args)
        self.assertNotIn(marker, rendered)
        self.assertIn("[query-redacted]", rendered)

    def test_replay_window_is_bounded(self) -> None:
        self.emit_many(501)
        client = self.client(last_sequence=0)
        self.assertEqual(client.status, 101)
        message = client.json()
        self.assertEqual(message["type"], "replay_unavailable")
        self.assertEqual(message["latest_sequence"], 501)
        opcode, payload = client.frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)

    def test_replay_yields_to_session_invalidation_between_batches(self) -> None:
        self.emit_many(20)
        with mock.patch("controller_api.websocket_transport.MAX_REPLAY_BATCH", 1), mock.patch(
            "controller_api.websocket_transport.POLL_SECONDS", 0.5
        ):
            client = self.client(last_sequence=0)
            self.assertEqual(client.json()["type"], "subscribed")
            first = client.json()
            self.assertEqual(first["sequence"], 1)
            self.fixture.session_file.write_text("b" * 64 + "\n", encoding="ascii")
            opcode, payload = client.frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1008)

    def test_websocket_releases_general_http_request_slot(self) -> None:
        client = self.client(last_sequence=0)
        self.assertEqual(client.json()["type"], "subscribed")
        acquired = 0
        try:
            for _ in range(self.fixture.settings.max_concurrent_requests):
                self.assertTrue(self.fixture.server._request_slots.acquire(blocking=False))
                acquired += 1
        finally:
            for _ in range(acquired):
                self.fixture.server._request_slots.release()

    def test_invalid_close_code_is_protocol_error(self) -> None:
        client = self.client(last_sequence=0)
        self.assertEqual(client.json()["type"], "subscribed")
        client.send(0x8, struct.pack("!H", 1005))
        opcode, payload = client.frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1002)

    def test_invalid_close_reason_is_invalid_text(self) -> None:
        client = self.client(last_sequence=0)
        self.assertEqual(client.json()["type"], "subscribed")
        client.send(0x8, struct.pack("!H", 1000) + b"\xff")
        opcode, payload = client.frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1007)

    def test_valid_close_payload_is_echoed(self) -> None:
        client = self.client(last_sequence=0)
        self.assertEqual(client.json()["type"], "subscribed")
        close_payload = struct.pack("!H", 1001) + b"leaving"
        client.send(0x8, close_payload)
        opcode, payload = client.frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(payload, close_payload)

    def test_deep_subscription_is_closed_with_policy_error(self) -> None:
        nested: object = "objectives"
        for _ in range(20):
            nested = [nested]
        client = self.client(last_sequence=None)
        client.send_json({"type": "subscribe", "after_sequence": 0, "topics": nested})
        opcode, payload = client.frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1008)


if __name__ == "__main__":
    unittest.main(verbosity=2)
