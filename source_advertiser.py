"""GarStream source discovery and receiver lease handling for a TX device."""

from __future__ import annotations

from dataclasses import dataclass
import json
import socket
import threading
import time
from typing import Callable, Iterable


PROTOCOL = "gar-stream/1"
DEFAULT_DISCOVERY_PORT = 5601
DEFAULT_ANNOUNCE_INTERVAL = 2.0
DEFAULT_LEASE_SECONDS = 7.0
MAX_PACKET_SIZE = 4096


@dataclass(frozen=True, order=True)
class StreamClient:
    """A receiver that currently owns a valid stream lease."""

    receiver_id: str
    host: str
    port: int


def _safe_identifier(value: object, *, maximum: int = 96) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > maximum:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    return value


def _decode_packet(payload: bytes) -> dict | None:
    try:
        message = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict) or message.get("protocol") != PROTOCOL:
        return None
    return message


class SourceAdvertiser:
    """Advertise one TX and turn RX requests into renewable stream clients."""

    def __init__(
        self,
        source_id: str,
        source_name: str,
        *,
        control_port: int = DEFAULT_DISCOVERY_PORT,
        announce_targets: Iterable[tuple[str, int]] | None = None,
        announce_interval: float = DEFAULT_ANNOUNCE_INTERVAL,
        default_lease_seconds: float = DEFAULT_LEASE_SECONDS,
        on_clients_changed: Callable[[tuple[StreamClient, ...]], None] | None = None,
    ) -> None:
        checked_id = _safe_identifier(source_id)
        checked_name = _safe_identifier(source_name)
        if checked_id is None or checked_name is None:
            raise ValueError("source_id and source_name must be printable non-empty strings")
        if not 0 <= control_port <= 65535:
            raise ValueError("control_port must be in the range 0..65535")

        self.source_id = checked_id
        self.source_name = checked_name
        self.announce_interval = max(0.1, float(announce_interval))
        self.default_lease_seconds = max(1.0, float(default_lease_seconds))
        self.on_clients_changed = on_clients_changed
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._clients: dict[str, tuple[StreamClient, float]] = {}
        self._last_notified: tuple[StreamClient, ...] = ()

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._socket.bind(("", control_port))
        self._socket.settimeout(0.2)
        self.control_port = int(self._socket.getsockname()[1])
        self.announce_targets = tuple(
            announce_targets
            if announce_targets is not None
            else (("255.255.255.255", self.control_port),)
        )
        self._thread = threading.Thread(
            target=self._run,
            name=f"gar-stream-source-{self.source_id}",
            daemon=True,
        )

    @property
    def clients(self) -> tuple[StreamClient, ...]:
        with self._lock:
            return tuple(sorted(entry[0] for entry in self._clients.values()))

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._socket.close()

    def _announcement(self) -> bytes:
        message = {
            "protocol": PROTOCOL,
            "type": "source_announce",
            "source_id": self.source_id,
            "source_name": self.source_name,
            "control_port": self.control_port,
            "transport": "rtp/udp",
            "encoding": "JPEG",
            "payload": 26,
            "lease_seconds": self.default_lease_seconds,
        }
        return json.dumps(message, separators=(",", ":"), ensure_ascii=True).encode("ascii")

    def _send_announcement(self, targets: Iterable[tuple[str, int]]) -> None:
        payload = self._announcement()
        for target in targets:
            try:
                self._socket.sendto(payload, target)
            except OSError:
                if not self._stop.is_set():
                    continue

    def _handle_request(self, message: dict, address: tuple[str, int]) -> None:
        if message.get("source_id") != self.source_id:
            return
        receiver_id = _safe_identifier(message.get("receiver_id"))
        if receiver_id is None:
            return

        if message.get("type") == "stream_stop":
            with self._lock:
                current = self._clients.get(receiver_id)
                if current is not None and current[0].host == address[0]:
                    self._clients.pop(receiver_id, None)
            self._notify_if_changed()
            return
        if message.get("type") != "stream_request":
            return

        try:
            stream_port = int(message["stream_port"])
            lease_seconds = float(message.get("lease_seconds", self.default_lease_seconds))
        except (KeyError, TypeError, ValueError):
            return
        if not 1 <= stream_port <= 65535:
            return
        lease_seconds = min(30.0, max(1.0, lease_seconds))
        client = StreamClient(receiver_id=receiver_id, host=address[0], port=stream_port)
        with self._lock:
            self._clients[receiver_id] = (client, time.monotonic() + lease_seconds)
        self._notify_if_changed()

    def _expire_clients(self) -> None:
        now = time.monotonic()
        with self._lock:
            expired = [key for key, (_client, expires) in self._clients.items() if expires <= now]
            for key in expired:
                self._clients.pop(key, None)
        if expired:
            self._notify_if_changed()

    def _notify_if_changed(self) -> None:
        clients = self.clients
        if clients == self._last_notified:
            return
        self._last_notified = clients
        if self.on_clients_changed is not None:
            self.on_clients_changed(clients)

    def _run(self) -> None:
        next_announcement = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_announcement:
                self._send_announcement(self.announce_targets)
                next_announcement = now + self.announce_interval
            self._expire_clients()
            try:
                payload, address = self._socket.recvfrom(MAX_PACKET_SIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            message = _decode_packet(payload)
            if message is None:
                continue
            if message.get("type") == "source_query":
                self._send_announcement((address,))
            else:
                self._handle_request(message, address)
