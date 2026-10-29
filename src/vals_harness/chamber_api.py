"""Chamber API: the worker side of one agent session (DESIGN.md §5.1–5.2).

Serves vals-agent/1 for one trial: handshake, task delivery, then exec/put/get
against the chamber sandbox until the agent sends `finish`, disconnects, or runs
out of time. Every action is recorded. The agent never touches the sandbox itself.
"""

from __future__ import annotations

import base64
import math
import threading
import time
from dataclasses import asdict, dataclass
from typing import Any, Literal, Protocol

from vals_harness.errors import InfraError
from vals_harness.launchers import AgentSession
from vals_harness.protocol import (CANCELLED, CHAMBER_ERROR, INVALID_PARAMS, METHOD_NOT_FOUND,
                                   NOT_FOUND, PROTOCOL, ProtocolError, TransportClosed, error,
                                   notification, result)
from vals_harness.sandbox import Sandbox

HELLO_TIMEOUT_S = 30
CANCEL_GRACE_S = 10
EXIT_GRACE_S = 10
WATCHDOG_SLACK_S = 5   # past deadline + grace, the agent process is killed unconditionally
WORKSPACE = "/workspace"

ExitStatus = Literal["submitted", "step_limit", "cost_limit", "timeout", "agent_error", "gave_up"]


@dataclass
class AgentOutcome:
    exit_status: ExitStatus
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    n_commands: int = 0         # counted by the harness, not reported by the agent
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Recorder(Protocol):
    """Where the session's observations go (DB events, transcript, artifacts)."""

    def event(self, type: str, payload: dict[str, Any]) -> None: ...
    def transcript(self, entry: dict[str, Any]) -> None: ...
    def artifact(self, name: str, data: bytes) -> None: ...


class ChamberApi:
    def __init__(self, sandbox: Sandbox, recorder: Recorder, *, trial_id: str, command_timeout_s: int):
        self.sandbox = sandbox
        self.rec = recorder
        self.trial_id = trial_id
        self.command_timeout_s = command_timeout_s
        self.n_commands = 0
        self._cancelling = False
        self._watchdog_fired = threading.Event()

    # ------------------------------------------------------------------ session

    def serve(self, session: AgentSession, start: dict[str, Any], deadline: float) -> AgentOutcome:
        """Run one agent session to completion. Never blocks past the deadline.

        Deadline handling has two layers: recv() times out at the deadline (then
        `cancel` + grace period), and a watchdog kills the agent process outright a bit
        later, which also unblocks a send() stuck on an agent that stopped reading.
        """
        def _fire() -> None:
            self._watchdog_fired.set()
            session.kill()

        watchdog = threading.Timer(max(0.0, deadline - time.monotonic()) + CANCEL_GRACE_S + WATCHDOG_SLACK_S, _fire)
        watchdog.daemon = True
        watchdog.start()
        t = session.transport
        try:
            self._handshake(session)
            t.send(notification("start", start))
            outcome = self._loop(session, deadline)
        except TransportClosed as e:
            outcome = AgentOutcome("agent_error", message=f"agent exited without finish: {e}")
        except ProtocolError as e:
            outcome = AgentOutcome("agent_error", message=f"protocol violation: {e}")
        finally:
            watchdog.cancel()
            if session.wait(EXIT_GRACE_S) is None:
                session.kill()
            else:
                session.transport.close()
        if self._watchdog_fired.is_set():
            outcome = AgentOutcome("timeout", message="agent killed at the wall-clock limit")
        outcome.n_commands = self.n_commands
        self.rec.event("agent_finished", outcome.to_dict())
        return outcome

    def _handshake(self, session: AgentSession) -> None:
        try:
            msg = session.transport.recv(timeout=HELLO_TIMEOUT_S)
        except TimeoutError:
            raise ProtocolError(f"no hello within {HELLO_TIMEOUT_S}s") from None
        if msg.get("method") != "hello":
            raise ProtocolError(f"expected hello, got {msg.get('method')!r}")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        theirs = str(params.get("protocol", ""))
        if theirs.split(".")[0] != PROTOCOL:
            # The agent's adapter is wrong: an agent failure (graded 0), not the harness's.
            session.transport.send(error(msg.get("id"), INVALID_PARAMS, f"unsupported protocol {theirs!r}"))
            raise ProtocolError(f"agent speaks {theirs!r}, we speak {PROTOCOL!r}")
        self.rec.event("agent_hello", {"agent": params.get("agent", {}),
                                       "capabilities": params.get("capabilities", [])})
        session.transport.send(result(msg.get("id"), {"protocol": PROTOCOL, "trial_id": self.trial_id}))

    def _loop(self, session: AgentSession, deadline: float) -> AgentOutcome:
        t = session.transport
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._cancel(session)
            try:
                msg = t.recv(timeout=remaining)
            except TimeoutError:
                return self._cancel(session)
            done = self._dispatch(session, msg)
            if done is not None:
                return done

    def _cancel(self, session: AgentSession) -> AgentOutcome:
        """Deadline passed: ask nicely, give a short grace period to `finish`, then kill.

        During the grace period the agent may only report (finish / event / artifact);
        it can't keep working on the chamber.
        """
        self._cancelling = True
        try:
            session.transport.send(notification("cancel", {"reason": "deadline"}))
            grace_end = time.monotonic() + CANCEL_GRACE_S
            while (left := grace_end - time.monotonic()) > 0:
                msg = session.transport.recv(timeout=left)
                if self._dispatch(session, msg) is not None:
                    break
        except (TimeoutError, TransportClosed, ProtocolError):
            pass
        session.kill()
        return AgentOutcome("timeout", message="agent wall-clock limit reached")

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self, session: AgentSession, msg: dict[str, Any]) -> AgentOutcome | None:
        """Handle one message. Malformed input from the agent is answered with an error
        and never raised: only a broken chamber (InfraError) escapes."""
        method, id_ = msg.get("method"), msg.get("id")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        t = session.transport

        if method == "event":
            data = params.get("data") if isinstance(params.get("data"), dict) else {}
            self.rec.event(f"agent.{str(params.get('type', 'event'))[:64]}", data)
            return None
        if method == "finish":
            t.send(result(id_, {}))
            return self._outcome_from(params)

        handler = {"exec": self._exec, "put": self._put, "get": self._get,
                   "artifact": self._artifact}.get(method if isinstance(method, str) else "")
        if handler is None:
            t.send(error(id_, METHOD_NOT_FOUND, f"unknown method {method!r}"))
            return None
        if self._cancelling and method != "artifact":
            t.send(error(id_, CANCELLED, "trial cancelled: finish now"))
            return None
        try:
            value = handler(params)
        except InfraError:
            t.send(error(id_, CHAMBER_ERROR, "chamber unavailable"))
            raise
        except FileNotFoundError as e:
            t.send(error(id_, NOT_FOUND, str(e)))
        except Exception as e:  # anything else the agent sent us is its problem, not ours
            t.send(error(id_, INVALID_PARAMS, f"bad params for {method}: {type(e).__name__}: {e}"))
        else:
            t.send(result(id_, value))
        return None

    @staticmethod
    def _path(p: Any) -> str:
        if not isinstance(p, str) or not p:
            raise ValueError("path must be a non-empty string")
        return p if p.startswith("/") else f"{WORKSPACE}/{p}"

    def _exec(self, p: dict[str, Any]) -> dict[str, Any]:
        cmd, env, stdin, cwd = p["cmd"], p.get("env"), p.get("stdin"), p.get("cwd") or WORKSPACE
        if not isinstance(cmd, str):
            raise TypeError("cmd must be a string")
        if env is not None and not (isinstance(env, dict) and all(isinstance(k, str) and isinstance(v, str)
                                                                   for k, v in env.items())):
            raise TypeError("env must be an object of strings")
        if stdin is not None and not isinstance(stdin, str):
            raise TypeError("stdin must be a string")
        if not isinstance(cwd, str):
            raise TypeError("cwd must be a string")
        timeout = min(int(p.get("timeout_s") or self.command_timeout_s), self.command_timeout_s)
        r = self.sandbox.exec(cmd, timeout=max(1, timeout), cwd=cwd, env=env,
                              stdin=stdin.encode() if stdin is not None else None)
        self.n_commands += 1
        self.rec.transcript({"type": "exec", "cmd": cmd, **r.to_dict()})
        self.rec.event("exec", {"cmd": cmd[:500], "returncode": r.returncode,
                                "duration_s": r.duration_s, "timed_out": r.timed_out})
        return r.to_dict()

    def _put(self, p: dict[str, Any]) -> dict[str, Any]:
        path = self._path(p["path"])
        data = base64.b64decode(p["data_b64"], validate=True)
        self.sandbox.put(path, data)
        self.rec.transcript({"type": "put", "path": path, "bytes": len(data)})
        return {}

    def _get(self, p: dict[str, Any]) -> dict[str, Any]:
        path = self._path(p["path"])
        data = self.sandbox.get(path)
        self.rec.transcript({"type": "get", "path": path, "bytes": len(data)})
        return {"data_b64": base64.b64encode(data).decode()}

    def _artifact(self, p: dict[str, Any]) -> dict[str, Any]:
        self.rec.artifact(str(p["name"]), base64.b64decode(p["data_b64"], validate=True))
        return {}

    @staticmethod
    def _outcome_from(p: dict[str, Any]) -> AgentOutcome:
        """Agent-reported numbers are untrusted: anything malformed becomes 0, never a crash."""
        def num(key: str, cast: type) -> Any:
            try:
                v = cast(p.get(key) or 0)
                return v if math.isfinite(v) and v >= 0 else cast(0)
            except (TypeError, ValueError, OverflowError):
                return cast(0)

        status = p.get("exit_status")
        allowed = {"submitted", "step_limit", "cost_limit", "timeout", "agent_error", "gave_up"}
        return AgentOutcome(
            exit_status=status if status in allowed else "agent_error",
            steps=num("steps", int),
            input_tokens=num("input_tokens", int),
            output_tokens=num("output_tokens", int),
            cost_usd=num("cost_usd", float),
            message=str(p.get("message") or "")[:2000],
        )
