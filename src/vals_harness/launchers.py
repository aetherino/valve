"""AgentLauncher: start an agent for a trial and hand back a session (DESIGN.md §5.3).

The worker never assumes the agent is a local process; it only talks to an
AgentSession (a transport + wait/kill). v1: HostLauncher (dev). Phase 2 adds
DockerLauncher (`docker run -i`, same stdio transport); later RemoteLauncher.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from vals_harness.errors import InfraError
from vals_harness.protocol import StdioTransport, Transport
from vals_harness.specs import AgentSpec

SRC_DIR = Path(__file__).resolve().parents[1]  # .../src, so agents can import vals_agent_sdk


class AgentSession(Protocol):
    transport: Transport

    def wait(self, timeout: float) -> int | None:
        """Exit code, or None if still running after `timeout`."""
        ...

    def kill(self) -> None: ...


class AgentLauncher(Protocol):
    def launch(self, agent: AgentSpec, stderr_path: Path) -> AgentSession: ...


@dataclass
class ProcessSession:
    proc: subprocess.Popen[bytes]
    transport: Transport

    def wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.transport.close()


class HostLauncher:
    """Runs the agent runner as a host subprocess. Dev only: agent code shares the host.

    The environment is built from scratch so the agent sees only its declared
    secrets, not the worker's whole environment.
    """

    def launch(self, agent: AgentSpec, stderr_path: Path) -> AgentSession:
        argv = [sys.executable if a == "python" else a for a in agent.command]
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        env["PYTHONPATH"] = str(SRC_DIR)
        env["PYTHONUNBUFFERED"] = "1"
        for name in agent.secrets:
            if name in os.environ:
                env[name] = os.environ[name]
        with open(stderr_path, "wb") as stderr:  # the child keeps its own copy of the fd
            try:
                proc = subprocess.Popen(argv, cwd=agent.root, env=env, stdin=subprocess.PIPE,
                                        stdout=subprocess.PIPE, stderr=stderr)
            except OSError as e:
                raise InfraError("adapter", f"could not start agent {agent.name}: {e}") from e
        assert proc.stdin and proc.stdout
        return ProcessSession(proc, StdioTransport(proc.stdout, proc.stdin))
