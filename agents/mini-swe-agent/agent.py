"""mini-swe-agent runner.

mini's own loop, prompts (config/mini.yaml) and bash tool are used unchanged.
Only the environment is swapped: ChamberEnvironment sends each command through
the Chamber API instead of running it on this machine.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

# Must happen before importing minisweagent: on import it prints a banner to stdout
# (which would corrupt the protocol channel) and loads a per-user global .env
# (which would make runs depend on whoever's machine this is).
os.environ["MSWEA_SILENT_STARTUP"] = "1"
os.environ["MSWEA_GLOBAL_CONFIG_DIR"] = tempfile.mkdtemp(prefix="mswea-")

import litellm  # noqa: E402
import yaml  # noqa: E402
from minisweagent import package_dir  # noqa: E402
from minisweagent.agents.default import DefaultAgent  # noqa: E402
from minisweagent.exceptions import Submitted  # noqa: E402
from minisweagent.models import get_model  # noqa: E402

from vals_agent_sdk.client import AgentClient  # noqa: E402

SUBMIT_MARKER = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"

# mini exit statuses -> protocol exit statuses
EXIT_MAP = {"Submitted": "submitted", "LimitsExceeded": "step_limit", "TimeExceeded": "timeout"}

# Clearly the model provider's failure, not the agent's (ContextWindowExceeded etc. are the agent's).
_X = litellm.exceptions
PROVIDER_ERRORS = (_X.AuthenticationError, _X.PermissionDeniedError, _X.NotFoundError, _X.RateLimitError,
                   _X.ServiceUnavailableError, _X.BadGatewayError, _X.InternalServerError,
                   _X.APIConnectionError, _X.Timeout, _X.PaymentRequiredError)


class ChamberEnvironment:
    """Implements mini's environment interface (execute / get_template_vars / serialize)."""

    def __init__(self, client: AgentClient, *, env: dict[str, str], timeout: int, cwd: str):
        self.client = client
        self.env = env
        self.timeout = timeout
        self.cwd = cwd
        # mini's prompt mentions the OS it runs on. Report the chamber's, not this
        # machine's, so the agent sees one consistent world. The harness sends it in
        # `start`; asking the chamber is the fallback (it counts as a chamber command).
        uname = client.chamber.get("uname") or client.exec("uname -s; uname -r; uname -v; uname -m")["output"]
        lines = uname.splitlines()
        self.uname = dict(zip(("system", "release", "version", "machine"), (lines + [""] * 4)[:4]))

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        r = self.client.exec(action.get("command", ""), cwd=cwd or self.cwd,
                             timeout_s=timeout or self.timeout, env=self.env)
        output: dict[str, Any] = {"output": r["output"], "returncode": r["returncode"], "exception_info": ""}
        if r["timed_out"]:
            output["exception_info"] = f"The command timed out after {timeout or self.timeout}s and was killed."
        if r["truncated"]:
            output["output"] += "\n[output truncated by the harness]"
        self._check_finished(output)
        return output

    @staticmethod
    def _check_finished(output: dict[str, Any]) -> None:
        """Same rule as mini's own environments: this is how mini knows the task is done."""
        lines = output.get("output", "").lstrip().splitlines(keepends=True)
        if lines and lines[0].strip() == SUBMIT_MARKER and output["returncode"] == 0:
            submission = "".join(lines[1:])
            raise Submitted({"role": "exit", "content": submission,
                             "extra": {"exit_status": "Submitted", "submission": submission}})

    def get_template_vars(self, **kwargs: Any) -> dict[str, Any]:
        return {"cwd": self.cwd, "env": self.env, "timeout": self.timeout, **self.uname, **kwargs}

    def serialize(self) -> dict[str, Any]:
        return {"info": {"config": {
            "environment": {"cwd": self.cwd, "env": self.env, "timeout": self.timeout},
            "environment_type": "vals.ChamberEnvironment",
        }}}


def main() -> None:
    client = AgentClient("mini-swe-agent", "2.4.6", capabilities=["exec"])
    cfg = client.config
    mini_cfg = yaml.safe_load((Path(package_dir) / "config" / "mini.yaml").read_text())

    env = ChamberEnvironment(client, env=mini_cfg["environment"].get("env", {}),
                             timeout=int(cfg.get("command_timeout_s", 30)), cwd=client.workspace)
    model_cfg = dict(mini_cfg["model"])
    if ws := os.environ.get("ANTHROPIC_WORKSPACE_ID"):
        # Needed when the API key isn't scoped to a single workspace.
        model_cfg["model_kwargs"] = {**model_cfg.get("model_kwargs", {}),
                                     "extra_headers": {"anthropic-workspace-id": ws}}
    model = get_model(cfg["model"], model_cfg)
    # mini retries everything except a few auth/limit errors for ~4 minutes. A 400 is
    # never transient (bad key scope, bad params), so fail the attempt immediately.
    model.abort_exceptions = [*getattr(model, "abort_exceptions", []), litellm.exceptions.BadRequestError]
    agent = DefaultAgent(model, env, **{
        **mini_cfg["agent"],
        "step_limit": int(cfg.get("step_limit", 50)),
        "cost_limit": float(cfg.get("cost_limit_usd", 1.0)),
    })

    exit_status, message = "agent_error", ""
    try:
        info = agent.run(client.instruction)
        exit_status = EXIT_MAP.get(info.get("exit_status", ""), "agent_error")
        message = info.get("exit_status", "")
    except PROVIDER_ERRORS as e:
        # The provider failed (bad key, rate limit, overload, unknown model). The harness
        # treats this as its own failure (retried) only if no command reached the chamber.
        exit_status, message = "model_error", f"{type(e).__name__}: {e}"
    except Exception as e:  # anything else ends the attempt as the agent's failure
        message = f"{type(e).__name__}: {e}"
    finally:
        client.artifact("trajectory.json", json.dumps(agent.serialize(), indent=1, default=str))

    usage = _usage(agent.messages)
    client.finish(exit_status, steps=agent.n_calls, cost_usd=round(agent.cost, 6),
                  message=message[:2000], **usage)


def _usage(messages: list[dict]) -> dict[str, int]:
    """Sum token usage from litellm responses stored in mini's message extras."""
    tin = tout = 0
    for m in messages:
        resp = (m.get("extra") or {}).get("response") or {}
        u = resp.get("usage") or {}
        tin += int(u.get("prompt_tokens") or 0)
        tout += int(u.get("completion_tokens") or 0)
    return {"input_tokens": tin, "output_tokens": tout}


if __name__ == "__main__":
    main()
