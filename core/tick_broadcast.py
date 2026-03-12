"""Tick broadcasting over Unix domain sockets.

The supervisor process runs a TickBroadcaster that publishes each tick as a
JSON line to connected clients. Bot worker processes use TickSubscriber to
receive the tick stream.

Message types:
  session_start  — new session beginning (includes metadata)
  tick           — a price sample
  session_end    — session finished (includes outcome, filepath)
  waiting        — between sessions (informational)
"""

import json
import logging
import os
import select
import socket
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ENCODING = "utf-8"


class TickBroadcaster:
    """Server-side tick broadcaster over a Unix domain socket.

    Accepts multiple client connections and broadcasts tick messages
    to all of them. Non-blocking sends — slow/dead clients get dropped.
    """

    def __init__(self, socket_path):
        # type: (str) -> None
        self.socket_path = socket_path
        self._server = None  # type: Optional[socket.socket]
        self._clients = []  # type: List[socket.socket]
        self._lock = threading.Lock()
        self._thread = None  # type: Optional[threading.Thread]
        self._running = False
        self._last_session_start = None  # type: Optional[Dict[str, Any]]

    def start(self):
        # type: () -> None
        # Remove stale socket file
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)

        self._server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(self.socket_path)
        self._server.listen(16)
        self._server.setblocking(False)
        self._running = True

        self._thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._thread.start()
        logger.info("TickBroadcaster listening on %s", self.socket_path)

    def stop(self):
        # type: () -> None
        self._running = False
        with self._lock:
            for c in self._clients:
                try:
                    c.close()
                except Exception:
                    pass
            self._clients.clear()
        if self._server:
            try:
                self._server.close()
            except Exception:
                pass
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass

    def broadcast(self, message):
        # type: (Dict[str, Any]) -> None
        """Send a message dict to all connected clients as a JSON line."""
        line = json.dumps(message, separators=(',', ':')) + "\n"
        data = line.encode(_ENCODING)
        msg_type = message.get("type")
        if msg_type == "session_start":
            self._last_session_start = message
        elif msg_type == "session_end":
            self._last_session_start = None
        with self._lock:
            dead = []
            for c in self._clients:
                try:
                    c.sendall(data)
                except (BrokenPipeError, ConnectionResetError, OSError):
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass
            if dead:
                logger.debug(
                    "TickBroadcaster: dropped %d dead client(s), %d remaining",
                    len(dead), len(self._clients),
                )

    def _accept_loop(self):
        # type: () -> None
        sweep_interval = 30.0  # seconds between dead-client sweeps
        last_sweep = time.time()
        while self._running:
            try:
                readable, _, _ = select.select([self._server], [], [], 1.0)
                if readable:
                    conn, _ = self._server.accept()
                    # Replay session_start so mid-session joiners can trade
                    replay = self._last_session_start
                    if replay is not None:
                        replayed_msg = dict(replay, replayed=True)
                        replay_data = (json.dumps(replayed_msg, separators=(',', ':')) + "\n").encode(_ENCODING)
                        try:
                            conn.sendall(replay_data)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            try:
                                conn.close()
                            except Exception:
                                pass
                            continue
                    with self._lock:
                        self._clients.append(conn)
                    logger.info(
                        "TickBroadcaster: client connected (%d total%s)",
                        len(self._clients),
                        ", replayed session_start" if replay else "",
                    )

                # Periodic sweep: detect dead clients even when no broadcasts occur
                now = time.time()
                if now - last_sweep >= sweep_interval:
                    last_sweep = now
                    self._sweep_dead_clients()
            except Exception:
                if self._running:
                    time.sleep(0.1)

    def _sweep_dead_clients(self):
        # type: () -> None
        """Probe clients with zero-length send to detect dead sockets."""
        with self._lock:
            dead = []
            for c in self._clients:
                try:
                    # select for writability with zero timeout
                    _, _, errored = select.select([], [c], [c], 0)
                    if errored:
                        dead.append(c)
                except Exception:
                    dead.append(c)
            for c in dead:
                self._clients.remove(c)
                try:
                    c.close()
                except Exception:
                    pass
            if dead:
                logger.debug(
                    "TickBroadcaster: swept %d dead client(s), %d remaining",
                    len(dead), len(self._clients),
                )

    @property
    def client_count(self):
        # type: () -> int
        with self._lock:
            return len(self._clients)


class TickSubscriber:
    """Client-side tick subscriber. Connects to the broadcaster's Unix socket
    and yields messages as dicts.

    Handles reconnection automatically if the supervisor restarts.
    """

    def __init__(self, socket_path, reconnect_delay=2.0):
        # type: (str, float) -> None
        self.socket_path = socket_path
        self.reconnect_delay = reconnect_delay
        self._sock = None  # type: Optional[socket.socket]
        self._buffer = b""

    def connect(self):
        # type: () -> None
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self.socket_path)
        self._buffer = b""
        logger.info("TickSubscriber connected to %s", self.socket_path)

    def close(self):
        # type: () -> None
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def messages(self, is_running=None):
        # type: (Optional[Any]) -> Any
        """Generator that yields message dicts. Reconnects on disconnect.

        Args:
            is_running: optional callable returning False to stop iteration
        """
        while is_running is None or is_running():
            try:
                if self._sock is None:
                    self.connect()

                data = self._sock.recv(65536)
                if not data:
                    raise ConnectionResetError("Server closed connection")

                self._buffer += data
                while b"\n" in self._buffer:
                    line, self._buffer = self._buffer.split(b"\n", 1)
                    if line.strip():
                        yield json.loads(line.decode(_ENCODING))

            except (ConnectionRefusedError, FileNotFoundError):
                self.close()
                logger.info("TickSubscriber: waiting for broadcaster...")
                time.sleep(self.reconnect_delay)
            except (ConnectionResetError, BrokenPipeError, OSError) as e:
                self.close()
                logger.warning(
                    "TickSubscriber disconnected: %s. Reconnecting...", e,
                )
                time.sleep(self.reconnect_delay)
