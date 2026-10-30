"""Chamber API <-> agent SDK over real pipes, against an in-memory fake sandbox."""

import textwrap
import time
from pathlib import Path

import pytest

from valve.chamber_api import ChamberApi
from valve.errors import ChamberStopped, InfraError
from valve.launchers import HostLauncher
from valve.sandbox import ExecResult
from valve.specs import AgentSpec


class FakeSandbox:
    id, shell = "fake", ["sh", "-c"]

    def __init__(self, output_bytes: int = 0):
        self.files: dict[str, bytes] = {}
        self.cmds: list[str] = []
        self.output_bytes = output_bytes

    def alive(self):
        return True

    def exec(self, cmd, *, timeout, cwd="/workspace", env=None, stdin=None):
        if cmd == "kill 1":
            raise ChamberStopped("sandbox stopped from the inside during exec (init exited with 143)")
        self.cmds.append(cmd)
        out = "x" * self.output_bytes if self.output_bytes else f"ran:{cmd}"
        return ExecResult(output=out, returncode=0, timed_out=False, truncated=False, duration_s=0.0)

    def put(self, path, data):
        self.files[path] = data

    def get(self, path):
        if path not in self.files:
            raise FileNotFoundError(path)
        return self.files[path]


class ListRecorder:
    def __init__(self):
        self.events, self.entries, self.artifacts = [], [], {}

    def event(self, type, payload):
        self.events.append((type, payload))

    def transcript(self, entry):
        self.entries.append(entry)

    def artifact(self, name, data):
        self.artifacts[name] = data


def _serve(tmp_path: Path, body: str, timeout_s: float = 20, output_bytes: int = 0):
    folder = tmp_path / "agent"
    folder.mkdir(parents=True)
    (folder / "agent.py").write_text("from valve_agent_sdk.client import AgentClient\n" + textwrap.dedent(body))
    spec = AgentSpec(name="t", placement="host", command=["python", "agent.py"], root=folder)
    sb, rec = FakeSandbox(output_bytes), ListRecorder()
    session = HostLauncher().launch(spec, tmp_path / "stderr.log", {})
    api = ChamberApi(sb, rec, trial_id="trial-1", command_timeout_s=30)
    start = {"instruction": "do it", "config": {"model": "m"}, "task": {"id": "t1"}, "workspace": "/workspace"}
    outcome = api.serve(session, start, deadline=time.monotonic() + timeout_s)
    return outcome, sb, rec


def test_round_trip(tmp_path):
    outcome, sb, rec = _serve(tmp_path, """
        c = AgentClient("t", "1")
        print("stray print must not corrupt the channel")
        assert c.instruction == "do it" and c.task["id"] == "t1" and c.trial_id == "trial-1"
        r = c.exec("echo hi")
        assert r["output"] == "ran:echo hi"
        c.put("a.txt", "x")
        assert c.get("a.txt") == b"x"
        c.event("note", k=1)
        c.artifact("log.txt", "abc")
        c.finish("submitted", steps=3, cost_usd=0.5)
    """)
    assert outcome.exit_status == "submitted", outcome.message
    assert (outcome.steps, outcome.cost_usd, outcome.n_commands) == (3, 0.5, 1)
    assert sb.files == {"/workspace/a.txt": b"x"}
    assert rec.artifacts == {"log.txt": b"abc"}
    assert ("agent.note", {"k": 1}) in rec.events


def test_exit_without_finish_is_agent_error(tmp_path):
    outcome, _, _ = _serve(tmp_path, """
        c = AgentClient("t", "1")
        raise SystemExit(3)
    """)
    assert outcome.exit_status == "agent_error"


def test_deadline_cancels_then_kills(tmp_path):
    t0 = time.monotonic()
    outcome, _, _ = _serve(tmp_path, """
        import time
        c = AgentClient("t", "1")
        time.sleep(60)
    """, timeout_s=1)
    assert outcome.exit_status == "timeout"
    assert time.monotonic() - t0 < 30


def test_missing_file_is_a_clean_error(tmp_path):
    outcome, _, _ = _serve(tmp_path, """
        from valve_agent_sdk.client import ChamberError
        c = AgentClient("t", "1")
        try:
            c.get("nope.txt")
        except ChamberError as e:
            c.finish("gave_up", message=str(e.code))
    """)
    assert outcome.exit_status == "gave_up" and outcome.message == "-32001"


def test_malformed_params_get_errors_and_never_crash_the_session(tmp_path):
    outcome, sb, _ = _serve(tmp_path, """
        from valve_agent_sdk.client import ChamberError
        c = AgentClient("t", "1")
        codes = []
        for method, params in [("exec", {"cmd": 42}), ("exec", {"cmd": "x", "env": ["a"]}),
                               ("exec", {"cmd": "x", "stdin": {}}), ("put", {"path": "p", "data_b64": "!!"}),
                               ("get", {"path": ""}), ("nope", {})]:
            try:
                c._request(method, params)
            except ChamberError as e:
                codes.append(e.code)
        c.exec("still works")
        c.finish("submitted", message=",".join(map(str, codes)))
    """)
    assert outcome.exit_status == "submitted"
    assert outcome.message == "-32602,-32602,-32602,-32602,-32602,-32601"
    assert sb.cmds == ["still works"]


def test_finish_with_garbage_numbers_is_parsed_leniently(tmp_path):
    outcome, _, _ = _serve(tmp_path, """
        c = AgentClient("t", "1")
        c.finish("weird-status", steps="lots", cost_usd=None, input_tokens=-5, output_tokens=float("nan"))
    """)
    assert (outcome.exit_status, outcome.steps, outcome.cost_usd, outcome.input_tokens) == ("agent_error", 0, 0.0, 0)


def test_agent_that_stops_reading_cannot_block_the_worker(tmp_path, monkeypatch):
    """Big replies fill the pipe; the worker's send blocks; the watchdog must still end it."""
    import valve.chamber_api as ca
    monkeypatch.setattr(ca, "CANCEL_GRACE_S", 1)
    monkeypatch.setattr(ca, "WATCHDOG_SLACK_S", 1)
    t0 = time.monotonic()
    outcome, _, _ = _serve(tmp_path, """
        import json, time
        c = AgentClient("t", "1")
        for i in range(50):   # fire requests, never read the (200 KB) replies
            c._send({"jsonrpc": "2.0", "id": 1000 + i, "method": "exec", "params": {"cmd": "x"}})
        time.sleep(120)
    """, timeout_s=1, output_bytes=200_000)
    assert outcome.exit_status == "timeout"
    assert time.monotonic() - t0 < 20


def test_agent_stopping_its_chamber_ends_the_session_as_agent_error(tmp_path):
    t0 = time.monotonic()
    outcome, sb, rec = _serve(tmp_path, """
        from valve_agent_sdk.client import ChamberError
        c = AgentClient("t", "1")
        codes = []
        for cmd in ("kill 1", "echo after"):
            try:
                c.exec(cmd)
            except ChamberError as e:
                codes.append(e.code)
        c.finish("submitted", cost_usd=0.4, input_tokens=9, message=",".join(map(str, codes)))
    """)
    assert outcome.exit_status == "agent_error" and "stopped its chamber" in outcome.message
    assert (outcome.cost_usd, outcome.input_tokens) == (0.4, 9)   # reported spend is kept
    assert sb.cmds == []                                            # nothing ran after the stop
    assert any(t == "chamber_stopped" for t, _ in rec.events)
    assert time.monotonic() - t0 < 10


def test_finish_during_the_cancel_grace_keeps_reported_spend(tmp_path):
    outcome, _, _ = _serve(tmp_path, """
        import time
        c = AgentClient("t", "1")
        while not c.cancelled:
            try:
                c.exec("work")
            except Exception:
                break
            time.sleep(0.05)
        c.finish("submitted", steps=7, cost_usd=1.25, input_tokens=1000, output_tokens=50)
    """, timeout_s=1)
    assert outcome.exit_status == "timeout"
    assert (outcome.steps, outcome.cost_usd, outcome.input_tokens, outcome.output_tokens) == (7, 1.25, 1000, 50)


def test_model_error_is_honoured_only_before_any_chamber_command(tmp_path):
    before, _, _ = _serve(tmp_path / "a", """
        c = AgentClient("t", "1")
        c.finish("model_error", message="AuthenticationError: invalid x-api-key")
    """)
    assert before.exit_status == "model_error"
    after, _, _ = _serve(tmp_path / "b", """
        c = AgentClient("t", "1")
        c.exec("ls")
        c.finish("model_error", message="AuthenticationError")
    """)
    assert after.exit_status == "agent_error" and "after 1 command" in after.message


@pytest.mark.parametrize("rc,infra", [(125, True), (126, True), (127, True), (1, False)])
def test_launch_failure_before_hello_is_infra(tmp_path, rc, infra):
    """`docker run` failing (125-127: daemon error, name conflict, missing command) is the
    harness's; any other exit before hello stays the agent's (it never saw the task)."""
    spec = AgentSpec(name="t", placement="host", command=["sh", "-c", f"exit {rc}"], root=tmp_path)
    session = HostLauncher().launch(spec, tmp_path / "stderr.log", {})
    api = ChamberApi(FakeSandbox(), ListRecorder(), trial_id="t", command_timeout_s=30)
    if infra:
        with pytest.raises(InfraError, match=f"docker exit {rc}"):
            api.serve(session, {"instruction": "x"}, deadline=time.monotonic() + 20)
    else:
        assert api.serve(session, {"instruction": "x"}, deadline=time.monotonic() + 20).exit_status == "agent_error"
