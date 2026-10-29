"""Agent side of vals-agent/1. Stdlib only, so any agent image can vendor it.

Usage:
    client = AgentClient("my-agent", "1.0")      # handshake + waits for the task
    r = client.exec("ls -la")                     # runs in the chamber
    client.finish("submitted", steps=3)

The real stdout is reserved for protocol messages. Anything the agent prints
(including its child processes) is redirected to stderr, so a stray print()
cannot corrupt the channel.
"""

from __future__ import annotations

import base64
import json
import os
import sys
from typing import Any

PROTOCOL = "vals-agent/1"


class ChamberError(Exception):
    """The worker returned a JSON-RPC error for a request."""

    def __init__(self, code: int, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


class AgentClient:
    def __init__(self, name: str, version: str, capabilities: list[str] | None = None):
        # Keep a private handle on the real stdout, then point fd 1 at stderr.
        self._out = os.fdopen(os.dup(1), "wb", buffering=0)
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        self._in = sys.stdin.buffer
        self._next_id = 0
        self.cancelled = False

        hello = self._request("hello", {"protocol": PROTOCOL, "agent": {"name": name, "version": version},
                                        "capabilities": capabilities or ["exec", "files", "events"]})
        self.trial_id: str = hello["trial_id"]
        start = self._read()
        if start.get("method") != "start":
            raise RuntimeError(f"expected start, got {start}")
        p = start["params"]
        self.instruction: str = p["instruction"]
        self.config: dict[str, Any] = p.get("config", {})
        self.task: dict[str, Any] = p.get("task", {})
        self.workspace: str = p.get("workspace", "/workspace")
        # Facts about the chamber the harness already knows (`uname`), so an agent needn't
        # spend a chamber command on them before its model has done anything.
        self.chamber: dict[str, Any] = p.get("chamber") or {}

    # ------------------------------------------------------------------ wire

    def _send(self, msg: dict[str, Any]) -> None:
        self._out.write(json.dumps(msg).encode() + b"\n")

    def _read(self) -> dict[str, Any]:
        line = self._in.readline()
        if not line:
            raise EOFError("worker closed the connection")
        return json.loads(line)

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        my_id = self._next_id
        self._send({"jsonrpc": "2.0", "id": my_id, "method": method, "params": params})
        while True:
            msg = self._read()
            if msg.get("method") == "cancel":       # notification can arrive at any time
                self.cancelled = True
                continue
            if msg.get("id") != my_id:
                continue
            if "error" in msg:
                raise ChamberError(msg["error"]["code"], msg["error"]["message"])
            return msg.get("result", {})

    # ------------------------------------------------------------------ API

    def exec(self, cmd: str, *, cwd: str | None = None, timeout_s: int | None = None,
             env: dict[str, str] | None = None, stdin: str | None = None) -> dict[str, Any]:
        """-> {output, returncode, timed_out, truncated, duration_s}"""
        params: dict[str, Any] = {"cmd": cmd}
        if cwd: params["cwd"] = cwd
        if timeout_s: params["timeout_s"] = timeout_s
        if env: params["env"] = env
        if stdin is not None: params["stdin"] = stdin
        return self._request("exec", params)

    def put(self, path: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        self._request("put", {"path": path, "data_b64": base64.b64encode(raw).decode()})

    def get(self, path: str) -> bytes:
        return base64.b64decode(self._request("get", {"path": path})["data_b64"])

    def event(self, type: str, **data: Any) -> None:
        self._send({"jsonrpc": "2.0", "method": "event", "params": {"type": type, "data": data}})

    def artifact(self, name: str, data: bytes | str) -> None:
        raw = data.encode() if isinstance(data, str) else data
        self._request("artifact", {"name": name, "data_b64": base64.b64encode(raw).decode()})

    def finish(self, exit_status: str, **usage: Any) -> None:
        """exit_status: submitted | step_limit | cost_limit | timeout | agent_error | gave_up
        | model_error (the model provider failed; only honoured before any chamber command).
        usage: steps, input_tokens, output_tokens, cost_usd, message."""
        try:
            self._request("finish", {"exit_status": exit_status, **usage})
        except EOFError:
            pass
