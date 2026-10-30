"""Sandbox: any isolated container the harness can exec / put / get in.

The chamber, the verifier and the tooling container are all sandboxes; "chamber"
is a role, not a type. Docker is the v1 implementation (sandbox/docker.py);
Modal, Daytona etc. would be further SandboxProviders (DESIGN.md §5.3).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class ExecResult:
    output: str          # stdout + stderr combined (what agents like mini expect)
    returncode: int
    timed_out: bool
    truncated: bool
    duration_s: float

    def to_dict(self) -> dict:
        return asdict(self)


class Sandbox(Protocol):
    id: str
    shell: list[str]     # ["bash", "-lc"] or ["sh", "-c"], detected from the image

    def alive(self) -> bool:
        """Is the sandbox still running? Decides harness-vs-agent fault, never output text."""
        ...
    def exists(self) -> bool:
        """Does the sandbox still exist (running or stopped)? A stopped one can still be read."""
        ...
    def exec(self, cmd: str, *, timeout: int, cwd: str | None = None,
             env: dict[str, str] | None = None, stdin: bytes | None = None,
             secret_env: dict[str, str] | None = None, stdout_path: Path | None = None) -> ExecResult:
        """Raises ChamberStopped if the sandbox stopped from the inside, InfraError if
        the harness or Docker failed. With `stdout_path`, stdout streams to that host
        file and `output` holds only stderr."""
        ...
    def put(self, path: str, data: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...
    def put_dir(self, local_dir: Path, dest: str) -> None: ...
    def snapshot(self, path: str, tar_path: Path) -> None: ...
    def halt(self) -> None:
        """Kill every process but keep the filesystem (for a quiet snapshot)."""
        ...
    def stop(self) -> None: ...


class SandboxProvider(Protocol):
    def ensure_image(self, image: str | None = None, *, dockerfile: Path | None = None,
                     name: str | None = None) -> str:
        """Make the image available locally; return its immutable id (for the manifest)."""
        ...

    def create(self, image: str, *, labels: dict[str, str], network: str = "none",
               cpus: float = 1.0, memory: str = "1g", volumes: dict[str, str] | None = None,
               workdir: str = "/workspace") -> Sandbox: ...

    def reap(self, labels: dict[str, str]) -> int:
        """Remove sandboxes matching all labels; return how many. Never raises."""
        ...

    def reap_stale(self, live_claims: set[str], home: str) -> int:
        """Remove sandboxes of this harness home (`valve.home` label) whose claim isn't
        live any more (a dead worker's leftovers). Other homes' sandboxes are never touched."""
        ...
