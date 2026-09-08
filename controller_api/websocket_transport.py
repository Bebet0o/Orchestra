from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import queue
import select
import socket
import struct
import threading
import time
import uuid
from contextlib import closing
from dataclasses import dataclass
from typing import Any, BinaryIO

from .core import ControllerError, ControllerService
from .event_journal import EventJournal, canonical_json, utc_now

WEBSOCKET_PATH = "/api/v1/events"
WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_CLIENT_FRAME_BYTES = 65_536
MAX_REPLAY_BATCH = 100
MAX_REPLAY_WINDOW = 500
MAX_INBOUND_FRAMES = 16
MAX_SUBSCRIBE_DEPTH = 8
MAX_SUBSCRIBE_NODES = 128
POLL_SECONDS = 0.25
HEARTBEAT_SECONDS = 15.0
SUBSCRIBE_TIMEOUT_SECONDS = 5.0
SEND_TIMEOUT_SECONDS = 2.0
MAX_SEQUENCE = 9_223_372_036_854_775_807

TOPIC_AGGREGATES: dict[str, frozenset[str]] = {
    "system": frozenset({"system"}),
    "projects": frozenset({"project"}),
    "objectives": frozenset({"objective"}),
    "tasks": frozenset({"task"}),
    "runs": frozenset({"run"}),
    "reviews": frozenset({"review"}),
    "recoveries": frozenset({"recovery"}),
    "sandboxes": frozenset({"sandbox", "sandbox_build"}),
    "backups": frozenset({"backup"}),
    "notifications": frozenset({"notification"}),
    "confirmations": frozenset({"confirmation"}),
    "audit": frozenset({"audit"}),
    "memories": frozenset({"memory"}),
}
SUPPORTED_TOPICS = frozenset(TOPIC_AGGREGATES) | {"all"}
ALL_AGGREGATE_TYPES = frozenset().union(*TOPIC_AGGREGATES.values())


class WebSocketProtocolError(RuntimeError):
    """A post-upgrade protocol failure that maps to a close frame."""

    def __init__(self, close_code: int, reason: str) -> None:
        super().__init__(reason)
        self.close_code = close_code
        self.reason = reason


@dataclass(frozen=True)
class ClientFrame:
    opcode: int
    payload: bytes


def header_has_token(value: str | None, token: str) -> bool:
    if value is None:
        return False
    expected = token.lower()
    return any(part.strip().lower() == expected for part in value.split(","))


def websocket_accept_value(key: str) -> str:
    if not isinstance(key, str) or not key.isascii() or len(key) != 24:
        raise ControllerError(400, "invalid_websocket_key", "Invalid WebSocket key")
    try:
        decoded = base64.b64decode(key, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ControllerError(
            400,
            "invalid_websocket_key",
            "Invalid WebSocket key",
        ) from error
    if (
        len(decoded) != 16
        or base64.b64encode(decoded).decode("ascii") != key
    ):
        raise ControllerError(400, "invalid_websocket_key", "Invalid WebSocket key")
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_last_event_sequence(value: str | None) -> int | None:
    if value is None:
        return None
    if not value or not value.isascii() or not value.isdigit():
        raise ControllerError(
            400,
            "invalid_last_event_sequence",
            "Invalid Last-Event-Sequence",
        )
    parsed = int(value)
    if parsed > MAX_SEQUENCE:
        raise ControllerError(
            400,
            "invalid_last_event_sequence",
            "Invalid Last-Event-Sequence",
        )
    return parsed


def _read_exact(stream: BinaryIO, count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise EOFError("websocket peer closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_client_frame(
    stream: BinaryIO,
    *,
    maximum_payload: int = MAX_CLIENT_FRAME_BYTES,
) -> ClientFrame:
    first, second = _read_exact(stream, 2)
    final = bool(first & 0x80)
    reserved = first & 0x70
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    payload_length = second & 0x7F

    if reserved or not final:
        raise WebSocketProtocolError(1002, "fragmented or reserved frame")
    if not masked:
        raise WebSocketProtocolError(1002, "client frame must be masked")
    if opcode not in {0x1, 0x2, 0x8, 0x9, 0xA}:
        raise WebSocketProtocolError(1002, "unsupported opcode")

    if payload_length == 126:
        payload_length = struct.unpack("!H", _read_exact(stream, 2))[0]
        if payload_length < 126:
            raise WebSocketProtocolError(1002, "non-canonical frame length")
    elif payload_length == 127:
        encoded = _read_exact(stream, 8)
        if encoded[0] & 0x80:
            raise WebSocketProtocolError(1002, "invalid frame length")
        payload_length = struct.unpack("!Q", encoded)[0]
        if payload_length < 65_536:
            raise WebSocketProtocolError(1002, "non-canonical frame length")

    if opcode >= 0x8 and payload_length > 125:
        raise WebSocketProtocolError(1002, "oversized control frame")
    if payload_length > maximum_payload:
        raise WebSocketProtocolError(1009, "frame too large")

    mask = _read_exact(stream, 4)
    payload = bytearray(_read_exact(stream, payload_length))
    for index in range(payload_length):
        payload[index] ^= mask[index % 4]

    if opcode == 0x8:
        if payload_length == 1:
            raise WebSocketProtocolError(1002, "invalid close payload")
        if payload_length >= 2:
            close_code = struct.unpack("!H", payload[:2])[0]
            if not (
                close_code in {1000, 1001, 1002, 1003, 1007, 1008, 1009, 1010, 1011}
                or 3000 <= close_code <= 4999
            ):
                raise WebSocketProtocolError(1002, "invalid close code")
            try:
                payload[2:].decode("utf-8")
            except UnicodeDecodeError as error:
                raise WebSocketProtocolError(1007, "invalid close reason") from error
    return ClientFrame(opcode=opcode, payload=bytes(payload))


def encode_server_frame(opcode: int, payload: bytes = b"") -> bytes:
    if opcode not in {0x1, 0x2, 0x8, 0x9, 0xA}:
        raise ValueError("unsupported server opcode")
    length = len(payload)
    first = bytes((0x80 | opcode,))
    if length < 126:
        return first + bytes((length,)) + payload
    if length <= 0xFFFF:
        return first + bytes((126,)) + struct.pack("!H", length) + payload
    return first + bytes((127,)) + struct.pack("!Q", length) + payload


def encode_client_frame(opcode: int, payload: bytes = b"", *, mask: bytes | None = None) -> bytes:
    """Small RFC 6455 client encoder used by probes and tests."""
    if mask is None:
        mask = uuid.uuid4().bytes[:4]
    if len(mask) != 4:
        raise ValueError("mask must contain four bytes")
    length = len(payload)
    first = bytes((0x80 | opcode,))
    if length < 126:
        header = first + bytes((0x80 | length,))
    elif length <= 0xFFFF:
        header = first + bytes((0x80 | 126,)) + struct.pack("!H", length)
    else:
        header = first + bytes((0x80 | 127,)) + struct.pack("!Q", length)
    masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
    return header + mask + masked


def _strict_json_object(raw: bytes) -> dict[str, Any]:
    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON member")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON value: {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise WebSocketProtocolError(1007, "invalid JSON text") from error
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if depth > MAX_SUBSCRIBE_DEPTH or nodes > MAX_SUBSCRIBE_NODES:
            raise WebSocketProtocolError(1008, "subscription structure exceeds limits")
        if isinstance(current, dict):
            stack.extend((item, depth + 1) for item in current.values())
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
    if not isinstance(value, dict):
        raise WebSocketProtocolError(1008, "subscription must be an object")
    return value


def parse_subscribe(payload: bytes) -> tuple[int, tuple[str, ...]]:
    message = _strict_json_object(payload)
    if set(message) != {"type", "after_sequence", "topics"}:
        raise WebSocketProtocolError(1008, "invalid subscription fields")
    if message.get("type") != "subscribe":
        raise WebSocketProtocolError(1008, "subscribe message required")
    after_sequence = message.get("after_sequence")
    topics = message.get("topics")
    if (
        type(after_sequence) is not int
        or after_sequence < 0
        or after_sequence > MAX_SEQUENCE
    ):
        raise WebSocketProtocolError(1008, "invalid subscription sequence")
    if (
        not isinstance(topics, list)
        or len(topics) > 64
        or any(not isinstance(topic, str) for topic in topics)
        or len(set(topics)) != len(topics)
        or any(topic not in SUPPORTED_TOPICS for topic in topics)
        or ("all" in topics and len(topics) != 1)
    ):
        raise WebSocketProtocolError(1008, "invalid subscription topics")
    return after_sequence, tuple(topics)


def aggregates_for_topics(topics: tuple[str, ...]) -> frozenset[str]:
    if not topics or topics == ("all",):
        return ALL_AGGREGATE_TYPES
    selected: set[str] = set()
    for topic in topics:
        selected.update(TOPIC_AGGREGATES[topic])
    return frozenset(selected)


class _FramePump(threading.Thread):
    def __init__(self, stream: BinaryIO) -> None:
        super().__init__(name="controller-websocket-reader", daemon=True)
        self.stream = stream
        self.frames: queue.Queue[ClientFrame | None] = queue.Queue(
            maxsize=MAX_INBOUND_FRAMES
        )
        self.failure: BaseException | None = None
        self.finished = threading.Event()

    def run(self) -> None:
        try:
            while True:
                frame = read_client_frame(self.stream)
                try:
                    self.frames.put(frame, timeout=0.1)
                except queue.Full as error:
                    raise WebSocketProtocolError(1008, "inbound queue limit") from error
                if frame.opcode == 0x8:
                    return
        except EOFError:
            try:
                self.frames.put_nowait(None)
            except queue.Full:
                pass
        except BaseException as error:  # transported to the owning request thread
            self.failure = error
        finally:
            self.finished.set()


class WebSocketSession:
    """Authenticated replay/fan-out session for a single RFC 6455 connection."""

    def __init__(
        self,
        *,
        connection: socket.socket,
        stream: BinaryIO,
        service: ControllerService,
        authenticated_session: str,
        initial_after_sequence: int | None,
    ) -> None:
        self.connection = connection
        self.stream = stream
        self.service = service
        self.authenticated_session = authenticated_session
        self.initial_after_sequence = initial_after_sequence
        self.connection_id = "conn_" + uuid.uuid4().hex
        self._close_sent = False

    def _send_bytes(self, payload: bytes) -> None:
        view = memoryview(payload)
        deadline = time.monotonic() + SEND_TIMEOUT_SECONDS
        flags = getattr(socket, "MSG_DONTWAIT", 0) | getattr(
            socket, "MSG_NOSIGNAL", 0
        )
        while view:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("websocket send timeout")
            _, writable, _ = select.select([], [self.connection], [], remaining)
            if not writable:
                raise TimeoutError("websocket send timeout")
            try:
                count = self.connection.send(view, flags)
            except BlockingIOError:
                continue
            if count <= 0:
                raise ConnectionError("websocket send failed")
            view = view[count:]

    def send_json(self, value: dict[str, Any]) -> None:
        payload = canonical_json(value).encode("utf-8")
        self._send_bytes(encode_server_frame(0x1, payload))

    def send_pong(self, payload: bytes) -> None:
        self._send_bytes(encode_server_frame(0xA, payload))

    def send_close(self, code: int, reason: str = "") -> None:
        if self._close_sent:
            return
        encoded = reason.encode("utf-8", errors="replace")[:123]
        while True:
            try:
                encoded.decode("utf-8")
                break
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        self._close_sent = True
        try:
            self._send_bytes(encode_server_frame(0x8, struct.pack("!H", code) + encoded))
        except (OSError, TimeoutError, ConnectionError):
            pass

    def _session_is_current(self) -> bool:
        return self.service.session_is_current(self.authenticated_session)

    @staticmethod
    def _frame_from_queue(pump: _FramePump, timeout: float) -> ClientFrame | None:
        try:
            frame = pump.frames.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            if pump.failure is not None:
                raise pump.failure
            if pump.finished.is_set():
                return None
            raise
        if pump.failure is not None:
            raise pump.failure
        return frame

    def _receive_subscription(self, pump: _FramePump) -> tuple[int, tuple[str, ...]]:
        deadline = time.monotonic() + SUBSCRIBE_TIMEOUT_SECONDS
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WebSocketProtocolError(1008, "subscription timeout")
            try:
                frame = self._frame_from_queue(pump, min(POLL_SECONDS, remaining))
            except queue.Empty:
                continue
            if frame is None:
                raise EOFError("peer closed before subscription")
            if frame.opcode == 0x9:
                self.send_pong(frame.payload)
                continue
            if frame.opcode == 0x8:
                raise EOFError("peer closed before subscription")
            if frame.opcode != 0x1:
                raise WebSocketProtocolError(1003, "text subscription required")
            return parse_subscribe(frame.payload)

    def _replay_bounds(self, after_sequence: int) -> tuple[int | None, int, bool]:
        with closing(self.service.database.connect()) as connection:
            oldest, latest = EventJournal.bounds(connection)
        unavailable = (
            after_sequence > latest
            or (oldest is not None and after_sequence < oldest - 1)
            or latest - after_sequence > MAX_REPLAY_WINDOW
        )
        return oldest, latest, unavailable

    def _send_replay_unavailable(self, oldest: int | None, latest: int) -> None:
        self.send_json(
            {
                "type": "replay_unavailable",
                "oldest_available_sequence": oldest if oldest is not None else 1,
                "latest_sequence": latest,
                "required_action": "refresh_snapshot",
            }
        )

    def _drain_event_batch(
        self,
        *,
        cursor: int,
        aggregate_types: frozenset[str],
    ) -> int:
        with closing(self.service.database.connect()) as connection:
            events = EventJournal.read_after(
                connection,
                after_sequence=cursor,
                limit=MAX_REPLAY_BATCH,
            )
        for event in events:
            sequence = int(event["sequence"])
            if sequence <= cursor:
                raise ControllerError(
                    503,
                    "event_stream_order_invalid",
                    "Event stream ordering is invalid",
                )
            cursor = sequence
            aggregate = event.get("aggregate")
            aggregate_type = (
                aggregate.get("type") if isinstance(aggregate, dict) else None
            )
            if aggregate_type in aggregate_types:
                self.send_json(event)
        return cursor

    def run(self) -> None:
        self.connection.settimeout(None)
        pump = _FramePump(self.stream)
        pump.start()
        try:
            if self.initial_after_sequence is None:
                after_sequence, topics = self._receive_subscription(pump)
            else:
                after_sequence = self.initial_after_sequence
                topics = ("all",)

            aggregate_types = aggregates_for_topics(topics)
            oldest, latest, unavailable = self._replay_bounds(after_sequence)
            if unavailable:
                self._send_replay_unavailable(oldest, latest)
                self.send_close(1000, "snapshot refresh required")
                return

            self.send_json(
                {
                    "type": "subscribed",
                    "connection_id": self.connection_id,
                    "replay_from": after_sequence + 1,
                    "latest_sequence": latest,
                    "topics": list(topics),
                }
            )
            cursor = after_sequence
            next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS

            while True:
                if not self._session_is_current():
                    self.send_close(1008, "session invalidated")
                    return

                oldest, latest, unavailable = self._replay_bounds(cursor)
                if unavailable:
                    self._send_replay_unavailable(oldest, latest)
                    self.send_close(1000, "snapshot refresh required")
                    return

                cursor = self._drain_event_batch(
                    cursor=cursor,
                    aggregate_types=aggregate_types,
                )

                now = time.monotonic()
                if now >= next_heartbeat:
                    with closing(self.service.database.connect()) as connection:
                        _, latest = EventJournal.bounds(connection)
                    self.send_json(
                        {
                            "type": "heartbeat",
                            "latest_sequence": latest,
                            "occurred_at": utc_now(),
                        }
                    )
                    next_heartbeat = now + HEARTBEAT_SECONDS

                wait = min(POLL_SECONDS, max(0.001, next_heartbeat - time.monotonic()))
                try:
                    frame = self._frame_from_queue(pump, wait)
                except queue.Empty:
                    continue
                if frame is None:
                    return
                if frame.opcode == 0x8:
                    if self._close_sent:
                        return
                    self._close_sent = True
                    try:
                        self._send_bytes(encode_server_frame(0x8, frame.payload))
                    except (OSError, TimeoutError, ConnectionError):
                        pass
                    return
                if frame.opcode == 0x9:
                    self.send_pong(frame.payload)
                    continue
                if frame.opcode == 0xA:
                    continue
                raise WebSocketProtocolError(1008, "unexpected client message")
        except WebSocketProtocolError as error:
            self.send_close(error.close_code, error.reason)
        except (EOFError, BrokenPipeError, ConnectionResetError, OSError, TimeoutError):
            pass
        except ControllerError:
            self.send_close(1011, "event stream unavailable")
        except Exception:
            self.send_close(1011, "internal stream error")
        finally:
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
