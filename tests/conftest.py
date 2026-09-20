"""Shared pytest fixtures."""

from __future__ import annotations

import asyncio
import socket
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from netpilot.core import App  # noqa: E402
from netpilot.db import Store  # noqa: E402
from netpilot.models import Credential, Device  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> Store:
    store = Store(data_dir=tmp_path / "npdata")
    yield store
    store.close()


@pytest.fixture()
def app(tmp_path: Path) -> App:
    """Core app with the monitor *not* started (tests drive the engine explicitly)."""
    instance = App(data_dir=tmp_path / "appdata", start_monitor=False)
    yield instance
    instance.store.close()


@pytest.fixture()
def device(store: Store) -> Device:
    return store.add_device(
        Device(name="test-router", host="192.0.2.10", vendor="mikrotik", tags=["lab", "core"], ssh_port=22)
    )


@pytest.fixture()
def credential(store: Store) -> Credential:
    return store.add_credential(
        Credential(name="lab", username="admin", password="s3cr3t", enable_password="en4ble")
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class TcpEchoServer:
    """Minimal TCP server used to exercise the tcp/ssh probes for real."""

    def __init__(self, banner: str | None = None) -> None:
        self.banner = banner
        self.port = free_port()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(8)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    def start(self) -> "TcpEchoServer":
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                self._sock.settimeout(0.4)
                conn, _addr = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                if self.banner:
                    conn.sendall((self.banner + "\r\n").encode())
                conn.close()
            except OSError:
                pass

    def stop(self) -> None:
        self._stop.set()
        self._sock.close()


@pytest.fixture()
def tcp_server():
    servers: list[TcpEchoServer] = []

    def factory(banner: str | None = None) -> TcpEchoServer:
        server = TcpEchoServer(banner).start()
        servers.append(server)
        return server

    yield factory
    for server in servers:
        server.stop()
