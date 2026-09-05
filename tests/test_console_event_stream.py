from __future__ import annotations

import base64
import hashlib
import importlib.util
import socket
import socketserver
import threading
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    __import__("sys").modules[name] = module
    spec.loader.exec_module(module)
    return module


service = load_module("orchestra_console_event_service", REPO / "scripts/orchestra-console.py")


class EventStreamConsoleSourceTest(unittest.TestCase):
    def test_event_route_is_exact_and_not_an_http_proxy_route(self) -> None:
        self.assertFalse(service._controller_route_exposed("GET", "/api/v1/events"))
        self.assertFalse(service._controller_route_exposed("GET", "/api/v1/events?after=1"))
        source = (REPO / "scripts/orchestra-console.py").read_text(encoding="utf-8")
        self.assertIn('parsed.path != "/api/v1/events"', source)
        self.assertIn('Sec-WebSocket-Protocol', source)
        self.assertIn('Sec-WebSocket-Extensions', source)
        self.assertIn('self.settings.controller_origin', source)

    def test_events_route_has_bounded_replay_ui(self) -> None:
        html = (REPO / "console/src/index.html").read_text(encoding="utf-8")
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        client = (REPO / "console/src/controller-client.js").read_text(encoding="utf-8")
        for marker in ('id="event-panel"', 'id="event-list"', 'id="event-connection-state"', 'id="event-facts"'):
            self.assertIn(marker, html)
        for marker in ("connectEvents", "disconnectEvents", "scheduleEventReconnect", "replay_unavailable", "eventMessages.length > 100", "reconcileEventReplay", "refreshEventReconciliationSnapshot"):
            self.assertIn(marker, app)
        for marker in ("new WebSocket", '/api/v1/events', 'after_sequence', 'topics: selectedTopics'):
            self.assertIn(marker, client)


    def test_replay_gap_requires_successful_http_snapshot_before_cursor_advance_and_reconnect(self) -> None:
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        replay_block = app.split('if (payload.type === "replay_unavailable") {', 1)[1].split('if (Number.isSafeInteger(payload.sequence)', 1)[0]
        self.assertIn("eventReplayTargetSequence = payload.latest_sequence", replay_block)
        self.assertNotIn("eventLastSequence =", replay_block)
        self.assertIn("void reconcileEventReplay()", replay_block)

        reconcile = app.split("async function reconcileEventReplay() {", 1)[1].split("function connectEvents() {", 1)[0]
        snapshot_call = reconcile.index("await refreshEventReconciliationSnapshot()")
        cursor_advance = reconcile.index("eventLastSequence = targetSequence")
        target_clear = reconcile.index("eventReplayTargetSequence = null")
        reconnect = reconcile.index("connectEvents()")
        self.assertLess(snapshot_call, cursor_advance)
        self.assertLess(cursor_advance, target_clear)
        self.assertLess(target_clear, reconnect)
        self.assertIn("if (!snapshotReady", reconcile)
        self.assertIn("reconnexion bloquée", reconcile)

        connect_guard = app.split("function connectEvents() {", 1)[1].split("setEventConnection", 1)[0]
        self.assertIn("eventReplayReconciling", connect_guard)
        self.assertIn("eventReplayTargetSequence !== null", connect_guard)
        self.assertIn("eventReplayBlocked", connect_guard)
        schedule_guard = app.split("function scheduleEventReconnect() {", 1)[1].split("eventReconnectAttempts", 1)[0]
        self.assertIn("eventReplayReconciling", schedule_guard)
        self.assertIn("eventReplayTargetSequence !== null", schedule_guard)
        self.assertIn("eventReplayBlocked", schedule_guard)


    def test_invalid_replay_metadata_fails_closed_without_reconnect(self) -> None:
        app = (REPO / "console/src/app.js").read_text(encoding="utf-8")
        replay_block = app.split('if (payload.type === "replay_unavailable") {', 1)[1].split('if (Number.isSafeInteger(payload.sequence)', 1)[0]
        invalid = replay_block.split('if (!Number.isSafeInteger(payload.latest_sequence)', 1)[1].split('eventLatestSequence = payload.latest_sequence', 1)[0]
        self.assertIn("eventReplayBlocked = true", invalid)
        self.assertIn("reconnexion bloquée", invalid)
        close_guard = app.split('eventStream = null;', 2)[2].split('scheduleEventReconnect();', 1)[0]
        self.assertIn("!eventReplayBlocked", close_guard)

    def test_event_browser_surface_has_no_persistence_or_privileged_paths(self) -> None:
        source = "\n".join((REPO / "console/src" / name).read_text(encoding="utf-8") for name in ("app.js", "controller-client.js", "index.html"))
        for forbidden in ("localStorage", "sessionStorage", "indexedDB", "orchestra.db", "/var/run/docker.sock", "/run/orchestra-docker", "innerHTML", "eval(", "new Function"):
            self.assertNotIn(forbidden, source)


class FakeWebSocketControllerHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        data = bytearray()
        while b"\r\n\r\n" not in data and len(data) < 16384:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data.extend(chunk)
        head = bytes(data).split(b"\r\n\r\n", 1)[0].decode("ascii")
        self.server.requests.append(head)  # type: ignore[attr-defined]
        headers = {}
        for line in head.split("\r\n")[1:]:
            name, value = line.split(":", 1)
            headers[name.lower()] = value.strip()
        key = headers["sec-websocket-key"]
        accept = base64.b64encode(hashlib.sha1((key + service.WEBSOCKET_GUID).encode("ascii")).digest()).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n"
            "\r\n"
        ).encode("ascii") + b"\x81\x02{}"
        self.request.sendall(response)


class FakeWebSocketController(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self) -> None:
        self.requests: list[str] = []
        super().__init__(("127.0.0.1", 0), FakeWebSocketControllerHandler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.shutdown()
        self.server_close()
        self.thread.join(timeout=5)


class EventStreamRelayIntegrationTest(unittest.TestCase):
    def _upgrade_request(self, port: int, key: str) -> bytes:
        return (
            "GET /api/v1/events HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Origin: http://127.0.0.1:{port}\r\n"
            "Cookie: unrelated=ignored; orchestra_session=test-session-cookie\r\n"
            "\r\n"
        ).encode("ascii")

    def test_unavailable_controller_returns_bounded_503(self) -> None:
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        unused_port = probe.getsockname()[1]
        probe.close()
        settings = service.Settings.from_root(
            REPO / "console/dist", host="127.0.0.1", port=0, max_connections=4,
            controller_host="127.0.0.1", controller_port=unused_port,
            controller_origin="http://127.0.0.1:8787", controller_timeout=0.25,
        )
        server = service.create_server(settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            key = base64.b64encode(b"0123456789abcdef").decode("ascii")
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as connection:
                connection.sendall(self._upgrade_request(server.server_port, key))
                data = connection.recv(4096)
                self.assertIn(b"HTTP/1.1 503", data)
                self.assertIn(b"controller_unavailable", data)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

    def test_exact_websocket_upgrade_is_translated_and_relayed(self) -> None:
        controller = FakeWebSocketController()
        settings = service.Settings.from_root(
            REPO / "console/dist",
            host="127.0.0.1",
            port=0,
            max_connections=4,
            controller_host="127.0.0.1",
            controller_port=controller.server_address[1],
            controller_origin="http://127.0.0.1:8787",
            controller_timeout=2.0,
        )
        server = service.create_server(settings)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            key = base64.b64encode(b"0123456789abcdef").decode("ascii")
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as connection:
                connection.sendall(self._upgrade_request(server.server_port, key))
                response = bytearray()
                while b"\r\n\r\n" not in response:
                    response.extend(connection.recv(4096))
                head, tail = bytes(response).split(b"\r\n\r\n", 1)
                self.assertIn(b"HTTP/1.1 101", head)
                if not tail:
                    tail = connection.recv(4)
                self.assertEqual(tail[:4], b"\x81\x02{}")
            self.assertEqual(len(controller.requests), 1)
            upstream = controller.requests[0]
            self.assertIn("GET /api/v1/events HTTP/1.1", upstream)
            self.assertIn("Origin: http://127.0.0.1:8787", upstream)
            self.assertIn("Cookie: orchestra_session=test-session-cookie", upstream)
            self.assertNotIn("unrelated=ignored", upstream)
            self.assertNotIn("Sec-WebSocket-Protocol", upstream)
            self.assertNotIn("Sec-WebSocket-Extensions", upstream)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
            controller.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
