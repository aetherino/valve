"""valve-agent/1: newline-delimited JSON-RPC 2.0 between the worker and an agent (DESIGN.md §5.2).

Transport is an interface: stdio pipes today (local process or `docker run -i`),
a WebSocket for remote agents later. Messages never contain transport details.
"""

from __future__ import annotations

import json
import queue
import threading
from typing import IO, Any, Protocol

PROTOCOL = "valve-agent/1"

# JSON-RPC error codes
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
CHAMBER_ERROR = -32000      # the chamber is broken -> infra_error
NOT_FOUND = -32001          # e.g. `get` on a missing file
CANCELLED = -32002          # deadline passed; only finish/event/artifact accepted
CHAMBER_STOPPED = -32003    # the agent stopped its own chamber; session ends, still graded


class TransportClosed(Exception):
    """The other side went away (process exited, pipe closed)."""


class ProtocolError(Exception):
    """A message that isn't valid valve-agent/1."""


class Transport(Protocol):
    def send(self, msg: dict[str, Any]) -> None: ...
    def recv(self, timeout: float | None) -> dict[str, Any]:
        """Next message. Raises TimeoutError, TransportClosed or ProtocolError."""
        ...
    def close(self) -> None: ...


def request(id_: int, method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params}


def notification(method: str, params: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "method": method, "params": params}


def result(id_: Any, value: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "result": value}


def error(id_: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


MAX_MESSAGE_BYTES = 32_000_000  # one JSON line; artifacts are the big ones (base64)

_EOF = object()
_TOO_BIG = object()


class StdioTransport:
    """Worker side of a pipe pair. A reader thread feeds a queue so recv() can time out.

    Sends can still block if the agent stops reading; ChamberApi's watchdog bounds
    that by killing the agent process at the deadline.
    """

    def __init__(self, reader: IO[bytes], writer: IO[bytes]):
        self._writer = writer
        self._q: queue.Queue[object] = queue.Queue()
        threading.Thread(target=self._pump, args=(reader,), daemon=True).start()

    def _pump(self, reader: IO[bytes]) -> None:
        try:
            while line := reader.readline(MAX_MESSAGE_BYTES + 1):
                if len(line) > MAX_MESSAGE_BYTES:
                    self._q.put(_TOO_BIG)   # never hold more than one capped line in memory
                    return
                if line.strip():
                    self._q.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._q.put(_EOF)

    def send(self, msg: dict[str, Any]) -> None:
        try:
            self._writer.write(json.dumps(msg).encode() + b"\n")
            self._writer.flush()
        except (BrokenPipeError, ValueError, OSError) as e:
            raise TransportClosed(str(e)) from e

    def recv(self, timeout: float | None) -> dict[str, Any]:
        try:
            item = self._q.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError from None
        if item is _EOF:
            self._q.put(_EOF)  # stay closed for later calls
            raise TransportClosed("agent closed its stdout")
        if item is _TOO_BIG:
            self._q.put(_EOF)
            raise ProtocolError(f"message larger than {MAX_MESSAGE_BYTES} bytes")
        try:
            msg = json.loads(item)  # type: ignore[arg-type]
        except json.JSONDecodeError as e:
            raise ProtocolError(f"not JSON: {item[:200]!r}") from e  # type: ignore[index]
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            raise ProtocolError(f"not JSON-RPC 2.0: {str(msg)[:200]}")
        return msg

    def close(self) -> None:
        try:
            self._writer.close()
        except OSError:
            pass
