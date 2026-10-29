"""Installed agents: an off-the-shelf CLI (e.g. Claude Code) runs inside the chamber (DESIGN.md §5).

This is the Harbor / Inspect SWE pattern, used because such CLIs' tools act on
the machine they run on. The cost is that the chamber is no longer the bare task
image, so:
  - the runtime is a separate image layer built FROM the pinned task image
    (cached by content hash, pinned by id in the manifest), never installed live;
  - the chamber needs egress for model calls (v2: model proxy only);
  - secrets reach the CLI's environment via `docker exec -e NAME`, never argv.
The CLI is driven from the worker with Sandbox.exec. Its stdout (stream-json) is
the exec's own output, streamed to a host file as it is produced, so the agent
can't rewrite it afterwards; it is parsed into our transcript and outcome, and
the CLI's own logs are kept as artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Callable

from vals_harness.chamber_api import AgentOutcome, Recorder, safe_num
from vals_harness.errors import ChamberStopped, InfraError
from vals_harness.sandbox import Sandbox
from vals_harness.sandbox.docker import _docker
from vals_harness.specs import AgentSpec, InstalledRuntime, TrialSpec, folder_digest

WORK = "/tmp/vals"           # outside /workspace, so none of this is part of the submission
INSTRUCTION = f"{WORK}/instruction.txt"
STDERR = f"{WORK}/stderr.log"
WORKSPACE = "/workspace"
INSTALLED_BUILD_TIMEOUT_S = 1800   # see specs.PREPARE_SLACK_S


class InstalledRunner:
    @staticmethod
    def base_tag(task_image_id: str) -> str:
        """FROM needs a name, so the pinned task image id gets a tag that can't move."""
        return f"vals/base:{task_image_id.removeprefix('sha256:')[:16]}"

    @staticmethod
    def tag_for(agent: AgentSpec, task_image_id: str) -> str:
        """Tag = hash of the whole build context (the agent folder) + the task image id."""
        h = hashlib.sha256((folder_digest(agent.root) + task_image_id).encode()).hexdigest()[:12]
        return f"vals/installed-{agent.name}:{h}"

    def prepare(self, agent: AgentSpec, task_image_id: str) -> str:
        """Build (once) task image + agent runtime; return the new image id."""
        rt = _runtime(agent)
        base, tag = self.base_tag(task_image_id), self.tag_for(agent, task_image_id)
        if _docker(["image", "inspect", tag], check=False).returncode != 0:
            _docker(["tag", task_image_id, base])
            _docker(["build", "-q", "--build-arg", f"BASE={base}", "-t", tag,
                     "-f", str(agent.root / rt.dockerfile), str(agent.root)],
                    timeout=INSTALLED_BUILD_TIMEOUT_S, kind="image")
        return _docker(["image", "inspect", "--format", "{{.Id}}", tag], kind="image").stdout.decode().strip()

    def run(self, spec: TrialSpec, chamber: Sandbox, rec: Recorder, deadline: float) -> AgentOutcome:
        rt = _runtime(spec.agent)
        chamber.put(INSTRUCTION, spec.task.instruction.encode())
        cmd = rt.run.format(model=shlex.quote(spec.model or ""), max_turns=int(spec.limits.step_limit))
        # No `> file`: stdout goes to the docker exec client and streams into the artifact folder.
        script = f"mkdir -p {rt.logs_dir} && {cmd} < {INSTRUCTION} 2> {STDERR}"
        output_path = rec.artifact_path("output.jsonl")
        timeout = max(1, int(deadline - time.monotonic()))
        stopped: str | None = None
        timed_out = False
        try:
            r = chamber.exec(script, timeout=timeout, cwd=WORKSPACE, env=_public_env(rt),
                             secret_env=_secret_env(spec.agent, rt), stdout_path=output_path)
            timed_out = r.timed_out
            rec.event("installed_agent_exit", {"returncode": r.returncode, "timed_out": r.timed_out,
                                               "duration_s": r.duration_s, "stderr_tail": r.output[-500:]})
        except ChamberStopped as e:   # e.g. the agent ran `kill 1`: its failure, still graded
            stopped = str(e)
            rec.event("chamber_stopped", {"reason": stopped})

        rec.artifact("stderr.log", _get_text(chamber, STDERR).encode())
        self._collect_logs(chamber, rt.logs_dir, rec)

        try:
            output = output_path.read_text(errors="replace") if output_path.exists() else ""
            outcome = PARSERS[rt.output_parser](output, rec)
        except Exception as e:  # the output is the agent's: never a harness failure
            outcome = AgentOutcome("agent_error", message=f"unparseable agent output: {type(e).__name__}: {e}"[:2000])
        if stopped:
            outcome.exit_status, outcome.message = "agent_error", f"agent stopped its chamber: {stopped}"
        elif timed_out:
            outcome.exit_status, outcome.message = "timeout", "agent wall-clock limit reached"
        return outcome

    @staticmethod
    def _collect_logs(chamber: Sandbox, logs_dir: str, rec: Recorder) -> None:
        """The CLI's own session logs, streamed as one tar into the artifact folder (best effort)."""
        tar = rec.artifact_path("agent_logs.tar")
        try:
            chamber.snapshot(logs_dir, tar)
        except InfraError:
            tar.unlink(missing_ok=True)


def _runtime(agent: AgentSpec) -> InstalledRuntime:
    if agent.installed is None:
        raise InfraError("adapter", f"agent {agent.name} has no installed runtime")
    return agent.installed


def _public_env(rt: InstalledRuntime) -> dict[str, str]:
    return {k: v for k, v in rt.env.items() if "${" not in v}


def _secret_env(agent: AgentSpec, rt: InstalledRuntime) -> dict[str, str]:
    """Declared secrets, plus env entries that reference worker variables ("${VAR}").
    An entry whose variables aren't set is skipped rather than sent half-filled.
    (Missing *required* secrets are caught earlier, at submit and in PREPARE.)"""
    out = {k: os.environ[k] for k in [*agent.secrets, *agent.optional_secrets] if k in os.environ}
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

# A Claude Code `result` with is_error that is clearly the model provider failing
# (bad key, no permission, unknown model, rate limit, overload, 5xx), not the agent.
PROVIDER_ERROR = re.compile(
    r"API Error: (401|403|404|408|429|500|502|503|504|529)\b|authentication_error|permission_error"
    r"|rate_limit_error|overloaded_error|Invalid API key|Credit balance is too low", re.IGNORECASE)


def _dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _tokens(usage: dict[str, Any]) -> tuple[int, int]:
    tin = sum(safe_num(usage.get(k), int) for k in
              ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return tin, safe_num(usage.get("output_tokens"), int)


def parse_claude_stream_json(output: str, rec: Recorder) -> AgentOutcome:
    """Claude Code `--output-format stream-json`: one JSON event per line, ending with
    a `result` event (subtype, num_turns, total_cost_usd, usage). Tool calls become
    transcript entries. Everything here is agent output: unparseable lines are skipped
    and every number is sanitized (non-negative, finite), never trusted.

    No result event (e.g. the CLI was killed at the wall clock): tokens are summed from
    the assistant events instead (each message once; the CLI repeats usage per block).
    A provider failure (PROVIDER_ERROR) before the agent ran a single tool becomes
    exit status `model_error` (-> infra_error, retried). After a tool ran, the agent
    could have forged the line (it can write to the CLI's stdout), so it's graded.
    """
    n_tools, result = 0, None
    usage_by_msg: dict[str, dict[str, Any]] = {}
    for i, line in enumerate(output.splitlines()):
        try:
            ev = json.loads(line)
        except (json.JSONDecodeError, RecursionError):
            continue
        if not isinstance(ev, dict):
            continue
        if ev.get("type") == "assistant":
            msg = _dict(ev.get("message"))
            if isinstance(msg.get("usage"), dict):
                usage_by_msg[str(msg.get("id") or f"line{i}")] = msg["usage"]
            content = msg.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    n_tools += 1
                    rec.transcript({"type": "tool_use", "name": str(block.get("name"))[:200],
                                    "input": block.get("input")})
        elif ev.get("type") == "result":
            # The last one: the CLI emits exactly one, at its very end. A line the agent
            # forged mid-run (via the CLI's stdout) comes before it.
            result = ev
    if result is None:
        tin = tout = 0
        for u in usage_by_msg.values():
            a, b = _tokens(u)
            tin, tout = tin + a, tout + b
        return AgentOutcome("agent_error", input_tokens=tin, output_tokens=tout, n_commands=n_tools,
                            message="no result event in agent output (tokens summed from assistant events)")
    tin, tout = _tokens(_dict(result.get("usage")))
    subtype = str(result.get("subtype") or "")
    text = str(result.get("result") or subtype)
    is_error = result.get("is_error") is True
    if is_error and n_tools == 0 and PROVIDER_ERROR.search(text):
        status = "model_error"
    elif subtype == "success" and not is_error:
        status = "submitted"
    else:
        status = "step_limit" if subtype == "error_max_turns" else "agent_error"
    return AgentOutcome(
        exit_status=status,  # type: ignore[arg-type]
        steps=safe_num(result.get("num_turns"), int),
        input_tokens=tin,
        output_tokens=tout,
        cost_usd=safe_num(result.get("total_cost_usd"), float),
        n_commands=n_tools,
        message=text[:2000],
    )


PARSERS: dict[str, Callable[[str, Recorder], AgentOutcome]] = {
    "claude_stream_json": parse_claude_stream_json,
}
