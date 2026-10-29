"""Phantom box: a stock agent CLI (e.g. Claude Code) in its own container next to the chamber.

The box mirrors the chamber so the agent sees one consistent world:
  - built FROM the pinned task image (+ the agent's install layer), so its files
    start out identical to the chamber's;
  - /workspace and /tmp are per-trial Docker volumes shared with the chamber
    (Claude Code tracks its cwd through files in /tmp);
  - /bin/sh and /bin/bash are the phantom shell (phantom/ crate): each command is
    relayed to the box's runner and from there to the worker's Chamber API, so it
    runs in the chamber.
What the agent's file tools see outside /workspace and /tmp is the box's own copy
of the task image: writes the chamber makes elsewhere (e.g. `pip install`) aren't
mirrored. v2: a FUSE filesystem over the Chamber API with prefetching.

Compared with installed mode the chamber stays the bare task image, offline, with
no secrets: the API key and network egress live only in the phantom box.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import tempfile
from pathlib import Path

from vals_harness.errors import InfraError
from vals_harness.installed import InstalledRunner, _public_env, _secret_env
from vals_harness.launchers import ProcessSession, _start
from vals_harness.protocol import StdioTransport
from vals_harness.sandbox.docker import HARDENING, _docker
from vals_harness.specs import AgentSpec

CRATE_DIR = Path(__file__).resolve().parents[2] / "phantom"
RUST_IMAGE = "rust:1.93-alpine"
OUTPUT = "/var/lib/phantom/output.jsonl"   # box-only: the chamber can't rewrite it
BOX_CPUS = "1"
BOX_MEMORY = "2g"

PHANTOM_LAYER = f"""\
ARG BASE
FROM {RUST_IMAGE} AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY phantom/ .
RUN cargo build --release --locked

FROM ${{BASE}}
COPY --from=build /src/target/release/phantom /usr/local/bin/phantom
# Keep the real bash for the shell snapshot, then put the phantom shell over every sh/bash.
# Must be the last RUN: after it, /bin/sh is the phantom shell.
RUN set -e; mkdir -p /usr/lib/phantom /var/lib/phantom /run/phantom; \\
    if [ -e /bin/bash ]; then cp "$(readlink -f /bin/bash)" /usr/lib/phantom/bash.real; fi; \\
    for s in /bin/bash /usr/bin/bash /bin/sh /usr/bin/sh; do \\
      if [ -e "$s" ] || [ -L "$s" ]; then ln -sf /usr/local/bin/phantom "$s"; fi; done
"""


def volume_names(claim_token: str) -> dict[str, str]:
    t = claim_token[:16]
    return {f"vals-ws-{t}": "/workspace", f"vals-tmp-{t}": "/tmp"}


def create_volumes(claim_token: str, labels: dict[str, str]) -> dict[str, str]:
    vols = volume_names(claim_token)
    for name in vols:
        args = ["volume", "create", "--label", "vals.managed=1"]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        _docker([*args, name])
    return vols


def remove_volumes(claim_token: str) -> None:
    for name in volume_names(claim_token):
        _docker(["volume", "rm", "-f", name], check=False, timeout=60)


class PhantomLauncher:
    def __init__(self) -> None:
        self.installed = InstalledRunner()
        self._ids: dict[str, str] = {}

    def prepare(self, agent: AgentSpec, task_image_id: str) -> str:
        """Task image + agent install layer + phantom layer; returns the image id."""
        key = agent.name + task_image_id
        if key in self._ids:
            return self._ids[key]
        base_id = self.installed.prepare(agent, task_image_id)
        h = hashlib.sha256(PHANTOM_LAYER.encode() + base_id.encode())
        for f in sorted(p for p in CRATE_DIR.rglob("*") if p.is_file() and "target" not in p.parts):
            h.update(str(f.relative_to(CRATE_DIR)).encode() + b"\0" + f.read_bytes())
        tag = f"vals/phantom-{agent.name}:{h.hexdigest()[:12]}"
        if _docker(["image", "inspect", tag], check=False).returncode != 0:
            base_tag = f"vals/base:{base_id.removeprefix('sha256:')[:16]}"
            _docker(["tag", base_id, base_tag])
            with tempfile.TemporaryDirectory(prefix="vals-phantom-build-") as ctx:
                shutil.copytree(CRATE_DIR, Path(ctx) / "phantom", ignore=shutil.ignore_patterns("target"))
                (Path(ctx) / "Dockerfile").write_text(PHANTOM_LAYER)
                _docker(["build", "-q", "--build-arg", f"BASE={base_tag}", "-t", tag, ctx],
                        timeout=1800, kind="image")
        image_id = _docker(["image", "inspect", "--format", "{{.Id}}", tag]).stdout.decode().strip()
        self._ids[key] = image_id
        return image_id

    def launch(self, agent: AgentSpec, image_id: str, stderr_path: Path, labels: dict[str, str],
               volumes: dict[str, str]) -> ProcessSession:
        rt = agent.installed
        if rt is None:
            raise InfraError("adapter", f"agent {agent.name} has no installed runtime")
        name = f"vals-phantom-{labels.get('vals.claim', os.urandom(6).hex())[:12]}"
        argv = ["docker", "run", "-i", "--rm", "--pull", "never", "--name", name,
                "--label", "vals.managed=1", "--label", "vals.role=phantom"]
        for k, v in labels.items():
            argv += ["--label", f"{k}={v}"]
        argv += [*HARDENING, "--cpus", BOX_CPUS, "--memory", BOX_MEMORY, "--network", "bridge"]
        for vol, path in volumes.items():
            argv += ["-v", f"{vol}:{path}"]
        for k, v in _public_env(rt).items():
            argv += ["-e", f"{k}={v}"]
        secrets = _secret_env(agent, rt)
        for k in secrets:
            argv += ["-e", k]            # value comes from the docker CLI's environment, never argv
        argv += ["-w", "/workspace", image_id, "phantom", "run", "--output", OUTPUT, "--", *shlex.split(rt.run)]
        proc = _start(argv, cwd=None, env={**os.environ, **secrets}, stderr_path=stderr_path, name=agent.name)
        assert proc.stdin and proc.stdout
        return ProcessSession(proc, StdioTransport(proc.stdout, proc.stdin), container=name)

