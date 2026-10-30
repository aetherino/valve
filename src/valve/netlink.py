"""Chamber API over the network: one listener per trial, one authenticated connection.

The agent box (phantom, or any remote agent) dials in instead of talking over a
container's stdio, so it can run anywhere that can reach the worker: a container on
this machine, or a vendor's own infrastructure. The protocol on the connection is the
same newline-delimited valve-agent/1, so ChamberApi doesn't change.

    worker: listen on BIND:0 -> token -> hand the box (address, token) -> accept one
            connection whose first line is {"token": ...} -> StdioTransport on the socket

Defaults are local: bind 127.0.0.1 (never exposed to the LAN; Docker Desktop still
routes host.docker.internal to it) and tell the box to dial host.docker.internal.
For a remote box set VALVE_CHAMBER_API_BIND / VALVE_CHAMBER_API_PUBLIC and put TLS in
front (a reverse proxy); the token alone authenticates, it doesn't encrypt.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import time
from typing import Callable

from valve.errors import InfraError
from valve.protocol import StdioTransport

AUTH_TIMEOUT_S = 10


class TrialListener:
    def __init__(self) -> None:
        self.bind = os.environ.get("VALVE_CHAMBER_API_BIND", "127.0.0.1")
        self.public_host = os.environ.get("VALVE_CHAMBER_API_PUBLIC", "host.docker.internal")
        self.token = secrets.token_hex(24)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind((self.bind, 0))
        self._sock.listen(4)
        self.port = self._sock.getsockname()[1]

    @property
    def address(self) -> str:
        return f"{self.public_host}:{self.port}"

    def accept(self, timeout: float, alive: Callable[[], bool] = lambda: True) -> tuple[socket.socket, StdioTransport]:
        """Wait for the box to dial in. Wrong or missing tokens are dropped; the first
        correct one wins and the listener closes, so the trial has exactly one peer.
        `alive` lets a local launcher stop waiting as soon as its box died."""
        deadline = time.monotonic() + timeout
        try:
            while (left := deadline - time.monotonic()) > 0:
                if not alive():
                    raise InfraError("adapter", "agent box exited before connecting to the Chamber API")
                self._sock.settimeout(min(left, 1.0))
                try:
                    conn, _ = self._sock.accept()
                except TimeoutError:
                    continue
                if self._authenticated(conn):
                    conn.settimeout(None)
                    return conn, StdioTransport(conn.makefile("rb"), conn.makefile("wb"))
                conn.close()
        finally:
            self.close()
        raise InfraError("adapter", f"agent box did not connect to the Chamber API within {timeout:.0f}s")

    def _authenticated(self, conn: socket.socket) -> bool:
        conn.settimeout(AUTH_TIMEOUT_S)
        try:
            line = conn.makefile("rb").readline(4096)
            got = json.loads(line).get("token", "")
        except (OSError, ValueError, AttributeError):
            return False
        ok = isinstance(got, str) and hmac.compare_digest(got, self.token)
        try:
            conn.sendall(b'{"auth": "ok"}\n' if ok else b'{"auth": "denied"}\n')
        except OSError:
            return False
        return ok

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
