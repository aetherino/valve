"""AgentLauncher: start an agent for a trial and hand back a session (DESIGN.md §5.3).

The worker never assumes the agent is a local process; it only talks to an
AgentSession (a transport + wait/kill). Both launchers speak the same stdio
transport; only how the process is started differs:
  HostLauncher    agent runner as a host subprocess (dev only)
  DockerLauncher  agent runner in its own container via `docker run -i` (sidecar, default)
A RemoteLauncher would hand back a WebSocket transport instead.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from vals_harness.errors import InfraError
from vals_harness.protocol import StdioTransport, Transport
from vals_harness.sandbox.docker import build_lock, HARDENING, _docker
from vals_harness.specs import AgentSpec

SRC_DIR = Path(__file__).resolve().parents[1]  # .../src, so agents can import vals_agent_sdk
SDK_DIR = SRC_DIR / "vals_agent_sdk"

# Used for agents whose folder has no Dockerfile: Python + the SDK + the agent folder.
DEFAULT_AGENT_DOCKERFILE = """\
FROM python:3.11-slim
COPY vals_agent_sdk /opt/vals/vals_agent_sdk
ENV PYTHONPATH=/opt/vals PYTHONUNBUFFERED=1
WORKDIR /agent
COPY agent/ /agent/
"""

AGENT_CPUS = "1"
AGENT_MEMORY = "2g"
AGENT_BUILD_TIMEOUT_S = 1200   # see specs.PREPARE_SLACK_S


class AgentSession(Protocol):
    transport: Transport

    def wait(self, timeout: float) -> int | None:
        """Exit code, or None if still running after `timeout`."""
        ...

    def kill(self) -> None: ...


class AgentLauncher(Protocol):
    def prepare(self, agent: AgentSpec) -> str | None:
        """Get everything ready before any chamber exists (e.g. build the image).
        Returns an id to pin in the manifest, if there is one."""
        ...

    def launch(self, agent: AgentSpec, stderr_path: Path, labels: dict[str, str]) -> AgentSession: ...


@dataclass
class ProcessSession:
    proc: subprocess.Popen[bytes]
    transport: Transport
    container: str | None = None   # for sidecars: killing the docker client doesn't stop the container
    _killed: bool = field(default=False, repr=False)

    def wait(self, timeout: float) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def kill(self) -> None:
        if self.container and not self._killed:
            _docker(["rm", "-f", self.container], check=False, timeout=60)
        self._killed = True
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.transport.close()


def _start(argv: list[str], *, cwd: Path | None, env: dict[str, str], stderr_path: Path,
           name: str) -> subprocess.Popen[bytes]:
    with open(stderr_path, "wb") as stderr:  # the child keeps its own copy of the fd
        try:
            return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.PIPE,
                                    stdout=subprocess.PIPE, stderr=stderr)
        except OSError as e:
            raise InfraError("adapter", f"could not start agent {name}: {e}") from e


class HostLauncher:
    """Runs the agent runner as a host subprocess. Dev only: agent code shares the host.

    The environment is built from scratch so the agent sees only its declared
    secrets, not the worker's whole environment.
    """

    def prepare(self, agent: AgentSpec) -> str | None:
        return None

    def launch(self, agent: AgentSpec, stderr_path: Path, labels: dict[str, str]) -> AgentSession:
        argv = [sys.executable if a == "python" else a for a in agent.command]
        env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "TMPDIR") if k in os.environ}
        env["PYTHONPATH"] = str(SRC_DIR)
        env["PYTHONUNBUFFERED"] = "1"
        for name in [*agent.secrets, *agent.optional_secrets]:
            if name in os.environ:
                env[name] = os.environ[name]
        proc = _start(argv, cwd=agent.root, env=env, stderr_path=stderr_path, name=agent.name)
        assert proc.stdin and proc.stdout
        return ProcessSession(proc, StdioTransport(proc.stdout, proc.stdin))


class DockerLauncher:
    """Runs the agent runner in its own hardened container (the sidecar).

    The container gets the agent's code, its declared secrets and network egress
    for model calls; it has no mounts and no route to the chamber or the worker
    other than its own stdin/stdout.
    """

    def __init__(self) -> None:
        self._images: dict[str, str] = {}
        self._ids: dict[str, str] = {}      # agent name -> pinned image id from prepare()

    @staticmethod
    def _recipe(agent: AgentSpec) -> bytes:
        dockerfile = agent.root / "Dockerfile"
        return dockerfile.read_bytes() if dockerfile.exists() else DEFAULT_AGENT_DOCKERFILE.encode()

    @classmethod
    def tag_for(cls, agent: AgentSpec) -> str:
        """Tag = content hash of (Dockerfile + agent folder + SDK)."""
        h = hashlib.sha256(cls._recipe(agent))
        for base in (agent.root, SDK_DIR):
            for f in sorted(p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
                h.update(str(f.relative_to(base)).encode() + b"\0" + f.read_bytes())
        return f"vals/agent-{agent.name}:{h.hexdigest()[:12]}"

    def image_for(self, agent: AgentSpec) -> str:
        """Build the agent image once per content hash."""
        if agent.name in self._images:
            return self._images[agent.name]
        recipe = self._recipe(agent)
        tag = self.tag_for(agent)
        with build_lock(tag):   # concurrent trials wait instead of racing the same build
            if _docker(["image", "inspect", tag], check=False).returncode != 0:
                with tempfile.TemporaryDirectory(prefix="vals-agent-build-") as ctx:
                    # Build context = the agent folder + the SDK, nothing else from the repo.
                    shutil.copytree(agent.root, Path(ctx) / "agent",
                                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".env*"))
                    shutil.copytree(SDK_DIR, Path(ctx) / "vals_agent_sdk", ignore=shutil.ignore_patterns("__pycache__"))
                    (Path(ctx) / "Dockerfile").write_bytes(recipe)
                    _docker(["build", "-q", "-t", tag, ctx], timeout=AGENT_BUILD_TIMEOUT_S, kind="image")
        self._images[agent.name] = tag
        return tag

    def prepare(self, agent: AgentSpec) -> str | None:
        tag = self.image_for(agent)
        image_id = _docker(["image", "inspect", "--format", "{{.Id}}", tag], kind="image").stdout.decode().strip()
        self._ids[agent.name] = image_id
        return image_id

    def launch(self, agent: AgentSpec, stderr_path: Path, labels: dict[str, str]) -> AgentSession:
        # The pinned id from prepare(), never the tag: a tag can move, and a missing one
        # would make `docker run` pull `vals/agent-...` from a registry (`--pull never`).
        image = self._ids.get(agent.name) or self.prepare(agent)
        name = f"vals-agent-{labels.get('vals.claim', os.urandom(6).hex())[:12]}"
        argv = ["docker", "run", "-i", "--rm", "--pull", "never", "--name", name, "--label", "vals.managed=1",
                "--label", "vals.role=agent"]
        for k, v in labels.items():
            argv += ["--label", f"{k}={v}"]
        argv += [*HARDENING, "--cpus", AGENT_CPUS, "--memory", AGENT_MEMORY, "--network", "bridge"]
        # `-e NAME` (no value) makes docker read the value from its own environment, so
        # secrets never appear in this command line (or in `ps`).
        env = dict(os.environ)
        for secret in [*agent.secrets, *agent.optional_secrets]:
            if secret in os.environ:
                argv += ["-e", secret]
        argv += ["-w", "/agent", image, *agent.command]
        proc = _start(argv, cwd=None, env=env, stderr_path=stderr_path, name=agent.name)
        assert proc.stdin and proc.stdout
        return ProcessSession(proc, StdioTransport(proc.stdout, proc.stdin), container=name)
