from __future__ import annotations

import base64
import http.client
import json
import os
import socket
import sqlite3
import struct
import time
import unittest
from contextlib import closing
from unittest import mock

from controller_api.event_journal import EventJournal
from controller_api.websocket_probe import _read_server_frame
from controller_api.websocket_transport import (
    encode_client_frame,
    encode_server_frame,
    websocket_accept_value,
)
from tests.test_controller_api import APIFixture, TOKEN

ORIGIN = "http://127.0.0.1:8787"


class RawWebSocketClient:
    def __init__(
        self,
        fixture: APIFixture,
        *,
        token: str | None = TOKEN,
        origin: str | None = ORIGIN,
        last_sequence: int | None = 0,
        path: str = "/api/v1/events",
        version: str = "13",
        extensions: str | None = None,
    ) -> None:
        self.fixture = fixture
        self.socket = socket.create_connection(("127.0.0.1", fixture.port), timeout=2)
        self.socket.settimeout(2)
        self.stream = self.socket.makefile("rb")
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        headers = [
            f"GET {path} HTTP/1.1",
            f"Host: 127.0.0.1:{fixture.port}",
            "Upgrade: websocket",
            "Connection: keep-alive, Upgrade",
            f"Sec-WebSocket-Version: {version}",
            f"Sec-WebSocket-Key: {key}",
        ]
        if origin is not None:
            headers.append(f"Origin: {origin}")
        if token is not None:
            headers.append(f"Cookie: orchestra_session={token}")
        if last_sequence is not None:
            headers.append(f"Last-Event-Sequence: {last_sequence}")
        if extensions is not None:
            headers.append(f"Sec-WebSocket-Extensions: {extensions}")
        self.socket.sendall(("\r\n".join(headers) + "\r\n\r\n").encode("ascii"))
        status_line = self.stream.readline().decode("iso-8859-1").rstrip("\r\n")
        self.status = int(status_line.split(" ", 2)[1])
        self.headers: dict[str, str] = {}
        while True:
            line = self.stream.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            name, value = line.decode("iso-8859-1").rstrip("\r\n").split(":", 1)
            self.headers[name.lower()] = value.strip()
        if self.status == 101:
            self.assert_accept = self.headers.get("sec-websocket-accept") == websocket_accept_value(key)
        else:
            self.assert_accept = False

    def send_json(self, value: dict[str, object]) -> None:
        self.socket.sendall(
            encode_client_frame(
                0x1,
                json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8"),
            )
        )

    def read_frame(self) -> tuple[int, bytes]:
        return _read_server_frame(self.stream)

    def read_json(self) -> dict[str, object]:
        opcode, payload = self.read_frame()
        if opcode != 0x1:
            raise AssertionError(f"expected text frame, received opcode {opcode}")
        value = json.loads(payload.decode("utf-8"))
        if not isinstance(value, dict):
            raise AssertionError("expected JSON object")
        return value

    def close(self) -> None:
        try:
            if self.status == 101:
                self.socket.sendall(encode_client_frame(0x8, struct.pack("!H", 1000)))
        except OSError:
            pass
        try:
            self.stream.close()
        finally:
            self.socket.close()


class ControllerWebSocketTransportTest(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = APIFixture()
        self.clients: list[RawWebSocketClient] = []

    def tearDown(self) -> None:
        for client in self.clients:
            client.close()
        self.fixture.close()

    def client(self, **kwargs: object) -> RawWebSocketClient:
        client = RawWebSocketClient(self.fixture, **kwargs)
        self.clients.append(client)
        return client

    def emit(self, aggregate_type: str, aggregate_id: str, event_type: str) -> int:
        with closing(sqlite3.connect(self.fixture.database)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            event = EventJournal.emit(
                connection,
                event_type=event_type,
                actor_type="system",
                actor_id="websocket-test",
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                correlation_id="corr_" + (aggregate_id.encode().hex() + "0" * 32)[:32],
                project_id="alpha",
                data={"state": "test"},
            )
            connection.commit()
            return int(event["sequence"])

    def test_authenticated_upgrade_and_capability(self) -> None:
        status, _, payload = self.fixture.request(
            "GET", "/api/v1/system/capabilities", authenticated=True
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["features"]["websocket_events"])

        client = self.client()
        self.assertEqual(client.status, 101)
        self.assertTrue(client.assert_accept)
        subscribed = client.read_json()
        self.assertEqual(subscribed["type"], "subscribed")
        self.assertEqual(subscribed["replay_from"], 1)
        self.assertEqual(subscribed["latest_sequence"], 0)

    def test_handshake_rejects_unsafe_requests(self) -> None:
        cases = (
            ({"token": None}, 401),
            ({"origin": None}, 400),
            ({"origin": "http://127.0.0.1:9999"}, 403),
            ({"path": "/api/v1/events?token=secret"}, 400),
            ({"version": "12"}, 426),
            ({"extensions": "permessage-deflate"}, 400),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                client = self.client(**arguments)
                self.assertEqual(client.status, expected)

    def test_subscribe_replays_ordered_filtered_events(self) -> None:
        first = self.emit("objective", "objective-alpha", "objective.created")
        self.emit("run", "run-alpha", "run.started")
        third = self.emit("review", "review-alpha", "review.debt_acknowledged")
        client = self.client(last_sequence=None)
        self.assertEqual(client.status, 101)
        client.send_json(
            {
                "type": "subscribe",
                "after_sequence": 0,
                "topics": ["objectives", "reviews"],
            }
        )
        subscribed = client.read_json()
        self.assertEqual(subscribed["topics"], ["objectives", "reviews"])
        one = client.read_json()
        two = client.read_json()
        self.assertEqual([one["sequence"], two["sequence"]], [first, third])
        self.assertEqual([one["aggregate"]["type"], two["aggregate"]["type"]], ["objective", "review"])

    def test_replay_unavailable_when_client_is_ahead(self) -> None:
        client = self.client(last_sequence=99)
        message = client.read_json()
        self.assertEqual(message["type"], "replay_unavailable")
        self.assertEqual(message["required_action"], "refresh_snapshot")
        opcode, payload = client.read_frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1000)

    def test_unmasked_client_frame_is_closed(self) -> None:
        client = self.client(last_sequence=None)
        client.socket.sendall(
            encode_server_frame(
                0x1,
                b'{"after_sequence":0,"topics":[],"type":"subscribe"}',
            )
        )
        opcode, payload = client.read_frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1002)

    def test_session_rotation_closes_open_connection(self) -> None:
        client = self.client()
        self.assertEqual(client.read_json()["type"], "subscribed")
        self.fixture.session_file.write_text("b" * 64 + "\n", encoding="ascii")
        opcode, payload = client.read_frame()
        self.assertEqual(opcode, 0x8)
        self.assertEqual(struct.unpack("!H", payload[:2])[0], 1008)

    def test_heartbeat_and_http_remain_available(self) -> None:
        with mock.patch(
            "controller_api.websocket_transport.HEARTBEAT_SECONDS", 0.05
        ):
            client = self.client()
            self.assertEqual(client.read_json()["type"], "subscribed")
            connection = http.client.HTTPConnection(
                "127.0.0.1", self.fixture.port, timeout=2
            )
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
            finally:
                connection.close()
            heartbeat = client.read_json()
            self.assertEqual(heartbeat["type"], "heartbeat")
            self.assertIn("occurred_at", heartbeat)

    def test_websocket_connection_slots_are_bounded(self) -> None:
        acquired = 0
        try:
            for _ in range(self.fixture.settings.max_websocket_connections):
                self.assertTrue(self.fixture.server.acquire_websocket_slot())
                acquired += 1
            self.assertFalse(self.fixture.server.acquire_websocket_slot())
        finally:
            for _ in range(acquired):
                self.fixture.server.release_websocket_slot()


if __name__ == "__main__":
    unittest.main(verbosity=2)
