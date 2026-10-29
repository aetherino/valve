"""Installed agents: an off-the-shelf CLI (e.g. Claude Code) runs inside the chamber (DESIGN.md §5).

This is the Harbor / Inspect SWE pattern, used because such CLIs' tools act on
the machine they run on. The cost is that the chamber is no longer the bare task
image, so:
  - the runtime is a separate image layer built FROM the pinned task image
    (cached by content hash, pinned by id in the manifest), never installed live;
  - the chamber needs egress for model calls (v2: model proxy only);
  - secrets reach the CLI's environment via `docker exec -e NAME`, never argv.
The CLI is driven from the worker with Sandbox.exec; its own transcript is parsed
into our transcript and its logs are kept as artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from vals_harness.chamber_api import AgentOutcome, Recorder
from vals_harness.errors import InfraError
from vals_harness.sandbox import Sandbox
from vals_harness.sandbox.docker import _docker
from vals_harness.specs import AgentSpec, InstalledRuntime, TrialSpec

WORK = "/tmp/vals"           # outside /workspace, so none of this is part of the submission
INSTRUCTION = f"{WORK}/instruction.txt"
OUTPUT = f"{WORK}/output.jsonl"
STDERR = f"{WORK}/stderr.log"
WORKSPACE = "/workspace"


class InstalledRunner:
    def prepare(self, agent: AgentSpec, task_image_id: str) -> str:
        """Build (once) task image + agent runtime; return the new image id."""
        rt = _runtime(agent)
        recipe = (agent.root / rt.dockerfile).read_bytes()
        base = f"vals/base:{task_image_id.removeprefix('sha256:')[:16]}"
        h = hashlib.sha256(recipe + task_image_id.encode()).hexdigest()[:12]
        tag = f"vals/installed-{agent.name}:{h}"
        if _docker(["image", "inspect", tag], check=False).returncode != 0:
            # FROM needs a name, so give the pinned id a tag that can't move.
            _docker(["tag", task_image_id, base])
            _docker(["build", "-q", "--build-arg", f"BASE={base}", "-t", tag,
                     "-f", str(agent.root / rt.dockerfile), str(agent.root)], timeout=1800, kind="image")
        return _docker(["image", "inspect", "--format", "{{.Id}}", tag]).stdout.decode().strip()

    def run(self, spec: TrialSpec, chamber: Sandbox, rec: Recorder, deadline: float) -> AgentOutcome:
        rt = _runtime(spec.agent)
        chamber.put(INSTRUCTION, spec.task.instruction.encode())
        cmd = rt.run.format(model=shlex.quote(spec.model or ""), max_turns=int(spec.limits.step_limit))
        script = f"mkdir -p {rt.logs_dir} && {cmd} < {INSTRUCTION} > {OUTPUT} 2> {STDERR}"
        timeout = max(1, int(deadline - time.monotonic()))
        r = chamber.exec(script, timeout=timeout, cwd=WORKSPACE, env=_public_env(rt), secret_env=_secret_env(spec.agent, rt))
        rec.event("installed_agent_exit", {"returncode": r.returncode, "timed_out": r.timed_out,
                                           "duration_s": r.duration_s})

        output = _get_text(chamber, OUTPUT)
        rec.artifact("output.jsonl", output.encode())
        rec.artifact("stderr.log", _get_text(chamber, STDERR).encode())
        self._collect_logs(chamber, rt.logs_dir, rec)

        outcome = PARSERS[rt.output_parser](output, rec)
        if r.timed_out:
            outcome.exit_status = "timeout"
            outcome.message = "agent wall-clock limit reached"
        return outcome

    @staticmethod
    def _collect_logs(chamber: Sandbox, logs_dir: str, rec: Recorder) -> None:
        """The CLI's own session logs, kept as one tar artifact (best effort)."""
        with tempfile.TemporaryDirectory() as d:
            tar = Path(d) / "logs.tar"
            try:
                chamber.snapshot(logs_dir, tar)
            except InfraError:
                return
            rec.artifact("agent_logs.tar", tar.read_bytes())


def _runtime(agent: AgentSpec) -> InstalledRuntime:
    if agent.installed is None:
        raise InfraError("adapter", f"agent {agent.name} has no installed runtime")
    return agent.installed


def _public_env(rt: InstalledRuntime) -> dict[str, str]:
    return {k: v for k, v in rt.env.items() if "${" not in v}


def _secret_env(agent: AgentSpec, rt: InstalledRuntime) -> dict[str, str]:
    """Declared secrets, plus env entries that reference worker variables ("${VAR}").
    An entry whose variables aren't set is skipped rather than sent half-filled."""
    out = {k: os.environ[k] for k in agent.secrets if k in os.environ}
    for key, template in rt.env.items():
        refs = re.findall(r"\$\{(\w+)\}", template)
        if refs and all(r in os.environ for r in refs):
            out[key] = re.sub(r"\$\{(\w+)\}", lambda m: os.environ[m.group(1)], template)
    return out


def _get_text(chamber: Sandbox, path: str) -> str:
    try:
        return chamber.get(path).decode("utf-8", errors="replace")
    except (FileNotFoundError, ValueError):
        return ""


# ---------------------------------------------------------------- output parsers


def parse_claude_stream_json(output: str, rec: Recorder) -> AgentOutcome:
    """Claude Code `--output-format stream-json`: one JSON event per line, ending with
    a `result` event (subtype, num_turns, total_cost_usd, usage). Tool calls become
    transcript entries; anything unparseable is skipped (it's the agent's output)."""
    n_tools, result = 0, None
    for line in output.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            for block in (ev.get("message") or {}).get("content") or []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    n_tools += 1
                    rec.transcript({"type": "tool_use", "name": block.get("name"), "input": block.get("input")})
        elif ev.get("type") == "result":
            result = ev
    if result is None:
        return AgentOutcome("agent_error", n_commands=n_tools, message="no result event in agent output")
    usage = result.get("usage") or {}
    subtype = result.get("subtype", "")
    status = ("submitted" if subtype == "success" and not result.get("is_error")
              else "step_limit" if subtype == "error_max_turns" else "agent_error")
    return AgentOutcome(
        exit_status=status,  # type: ignore[arg-type]
        steps=int(result.get("num_turns") or 0),
        input_tokens=sum(int(usage.get(k) or 0) for k in
                         ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(result.get("total_cost_usd") or 0.0),
        n_commands=n_tools,
        message=str(result.get("result") or subtype)[:2000],
    )


PARSERS: dict[str, Callable[[str, Recorder], AgentOutcome]] = {
    "claude_stream_json": parse_claude_stream_json,
}
