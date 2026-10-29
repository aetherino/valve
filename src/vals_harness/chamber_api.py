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
from pathlib import Path
from typing import Any, Literal, Protocol

from vals_harness.errors import ChamberStopped, InfraError
from vals_harness.launchers import AgentSession
from vals_harness.protocol import (CANCELLED, CHAMBER_ERROR, CHAMBER_STOPPED, INVALID_PARAMS,
                                   METHOD_NOT_FOUND, NOT_FOUND, PROTOCOL, ProtocolError,
                                   TransportClosed, error, notification, result)
from vals_harness.sandbox import Sandbox

HELLO_TIMEOUT_S = 30
CANCEL_GRACE_S = 10
EXIT_GRACE_S = 10
WATCHDOG_SLACK_S = 5   # past deadline + grace, the agent process is killed unconditionally
# `docker run` itself failed before the agent said hello (125: daemon error / name
# conflict, 126: cannot invoke, 127: not found). Before hello the agent hasn't seen the
# task, so treating these as harness failures can't be used to dodge a hard task.
LAUNCH_FAILURE_CODES = (125, 126, 127)

# "model_error": the agent's adapter reports that its model provider failed (auth, rate
# limit, overload, unknown model). Honoured as an infra error (kind model_api) only if
# the agent never touched the chamber; otherwise it becomes agent_error (see serve()).
ExitStatus = Literal["submitted", "step_limit", "cost_limit", "timeout", "agent_error", "gave_up", "model_error"]
EXIT_STATUSES = {"submitted", "step_limit", "cost_limit", "timeout", "agent_error", "gave_up", "model_error"}


def safe_num(value: Any, cast: type) -> Any:
    """Agent-reported numbers are untrusted: anything malformed, negative or non-finite is 0."""
    try:
        v = cast(value or 0)
        return v if math.isfinite(v) and v >= 0 else cast(0)
    except (TypeError, ValueError, OverflowError):
        return cast(0)


@dataclass
class AgentOutcome:
    exit_status: ExitStatus
    steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    n_commands: int = 0         # counted by the harness, not reported by the agent
    n_not_found: int = 0        # commands that exited 127 ("command not found"): an env-mismatch signal
    message: str = ""
    answer: str = ""            # the agent's final answer, for graders that need it (Q&A, proofs)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["answer"] = d["answer"][:2000]   # full answer goes to the grader, a preview to results
        return d


class Recorder(Protocol):
    """Where the session's observations go (DB events, transcript, artifacts)."""

    def event(self, type: str, payload: dict[str, Any]) -> None: ...
    def transcript(self, entry: dict[str, Any]) -> None: ...
    def artifact(self, name: str, data: bytes) -> None: ...
    def artifact_path(self, name: str) -> Path: ...   # for artifacts streamed to disk


class ChamberApi:
    def __init__(self, sandbox: Sandbox, recorder: Recorder, *, trial_id: str, command_timeout_s: int,
                 workspace: str = "/workspace"):
        self.workspace = workspace
        self.sandbox = sandbox
        self.rec = recorder
        self.trial_id = trial_id
        self.command_timeout_s = command_timeout_s
        self.n_commands = 0
        self.n_not_found = 0
        self._cancelling = False
        self._hello_done = False
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
            if not self._hello_done and (rc := session.wait(5)) in LAUNCH_FAILURE_CODES:
                watchdog.cancel()
                raise InfraError("adapter", f"agent container failed to start (docker exit {rc}); "
                                            "see agent_stderr.log") from e
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
            # Keep any numbers the agent reported (a finish during the grace period).
            outcome.exit_status, outcome.message = "timeout", "agent killed at the wall-clock limit"
        outcome.n_commands = self.n_commands
        outcome.n_not_found = self.n_not_found
        if outcome.exit_status == "model_error" and self.n_commands:
            # Once the agent has acted on the chamber, a claimed provider failure could be a
            # way to drop a failing trial from the accuracy denominator: graded instead.
            outcome.exit_status = "agent_error"
            outcome.message = f"model_error after {self.n_commands} command(s): {outcome.message}"[:2000]
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
        self._hello_done = True
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
        """Deadline passed. The agent's reported numbers (tokens, cost) are kept if it
        finishes during the grace period: that spend is real."""
        outcome = self._wind_down(session, "deadline") or AgentOutcome("timeout")
        outcome.exit_status, outcome.message = "timeout", "agent wall-clock limit reached"
        return outcome

    def _chamber_stopped(self, session: AgentSession, e: ChamberStopped) -> AgentOutcome:
        """The agent stopped its own chamber (`kill 1` ...): nothing left to serve. The
        session ends like a cancel; the workspace is still snapshotted and graded."""
        self.rec.event("chamber_stopped", {"reason": str(e)})
        outcome = self._wind_down(session, "chamber_stopped") or AgentOutcome("agent_error")
        outcome.exit_status, outcome.message = "agent_error", f"agent stopped its chamber: {e}"
        return outcome

    def _wind_down(self, session: AgentSession, reason: str) -> AgentOutcome | None:
        """Send `cancel`, give a short grace period to `finish`, then kill. During the
        grace period the agent may only report (finish / event / artifact); it can't keep
        working on the chamber. Returns the agent's finish, if it sent one."""
        self._cancelling = True
        finished = None
        try:
            session.transport.send(notification("cancel", {"reason": reason}))
            grace_end = time.monotonic() + CANCEL_GRACE_S
            while (left := grace_end - time.monotonic()) > 0:
                msg = session.transport.recv(timeout=left)
                if (finished := self._dispatch(session, msg)) is not None:
                    break
        except (TimeoutError, TransportClosed, ProtocolError):
            pass
        session.kill()
        return finished

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
        except ChamberStopped as e:
            t.send(error(id_, CHAMBER_STOPPED, f"chamber stopped: {e}"))
            return self._chamber_stopped(session, e)
        except FileNotFoundError as e:
            t.send(error(id_, NOT_FOUND, str(e)))
        except Exception as e:  # anything else the agent sent us is its problem, not ours
            t.send(error(id_, INVALID_PARAMS, f"bad params for {method}: {type(e).__name__}: {e}"))
        else:
            t.send(result(id_, value))
        return None

    def _path(self, p: Any) -> str:
        if not isinstance(p, str) or not p:
            raise ValueError("path must be a non-empty string")
        return p if p.startswith("/") else f"{self.workspace}/{p}"

    def _exec(self, p: dict[str, Any]) -> dict[str, Any]:
        cmd, env, stdin, cwd = p["cmd"], p.get("env"), p.get("stdin"), p.get("cwd") or self.workspace
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
        if r.returncode == 127:
            self.n_not_found += 1
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
        status = p.get("exit_status")
        return AgentOutcome(
            exit_status=status if isinstance(status, str) and status in EXIT_STATUSES else "agent_error",
            steps=safe_num(p.get("steps"), int),
            input_tokens=safe_num(p.get("input_tokens"), int),
            output_tokens=safe_num(p.get("output_tokens"), int),
            cost_usd=safe_num(p.get("cost_usd"), float),
            message=str(p.get("message") or "")[:2000],
            answer=str(p.get("answer") or "")[:100000],
        )
