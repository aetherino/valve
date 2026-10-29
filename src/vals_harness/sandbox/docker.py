"""Docker implementation of Sandbox / SandboxProvider, driving the `docker` CLI."""

from __future__ import annotations

import hashlib
import os
import subprocess
import threading
import time
from pathlib import Path

from vals_harness.errors import InfraError
from vals_harness.sandbox import ExecResult

MAX_OUTPUT_BYTES = 1_000_000        # per exec, returned to the agent
MAX_FILE_BYTES = 10_000_000         # per `get`
MAX_SNAPSHOT_BYTES = 1_000_000_000  # final workspace tar
CLIENT_GRACE_S = 5                  # extra time for the docker client on top of the in-container timeout
READ_CHUNK = 65536

# Applied to every sandbox (chamber, tooling, verifier). Root inside the container keeps
# only the file-ownership capabilities ordinary tasks need; no raw sockets, mknod,
# chroot, setfcap, ptrace, etc. Disk size isn't capped: overlay2 on Docker Desktop
# doesn't support --storage-opt size.
HARDENING = [
    "--init",                                   # PID 1 that reaps zombies
    "--pids-limit", "512",                      # fork bombs stay inside the box
    "--security-opt", "no-new-privileges",
    "--cap-drop", "ALL",
    *[f"--cap-add={c}" for c in ("CHOWN", "DAC_OVERRIDE", "FOWNER", "FSETID", "SETUID", "SETGID", "KILL")],
]


def _docker(args: list[str], *, input: bytes | None = None, timeout: float = 120,
            check: bool = True, kind: str = "docker") -> subprocess.CompletedProcess[bytes]:
    try:
        proc = subprocess.run(["docker", *args], input=input, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        raise InfraError(kind, f"docker {args[0]} timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise InfraError(kind, "docker CLI not found") from e
    if check and proc.returncode != 0:
        raise InfraError(kind, f"docker {' '.join(args[:2])}: {proc.stderr.decode(errors='replace').strip()}")
    return proc


def _run_capped(argv: list[str], *, timeout: float, cap: int, stdin: bytes | None = None,
                sink: Path | None = None, env: dict[str, str] | None = None) -> tuple[bytes, int, bool, bool]:
    """Run a command, keeping at most `cap` bytes of its merged output in memory.

    Output past the cap is read and discarded, so the child never blocks on a full
    pipe and the host's memory stays bounded no matter what the agent prints. With
    `sink`, output streams to that file instead (still capped).
    Returns (output, returncode, killed_by_timeout, truncated).
    """
    proc = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if sink is None else subprocess.PIPE)
    killed = threading.Event()

    def _kill() -> None:
        killed.set()
        proc.kill()

    timer = threading.Timer(timeout, _kill)
    timer.start()
    if stdin is not None:
        def _feed() -> None:
            try:
                assert proc.stdin
                proc.stdin.write(stdin)
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=_feed, daemon=True).start()

    buf = bytearray()
    total = 0
    out_file = open(sink, "wb") if sink else None
    try:
        assert proc.stdout
        while chunk := proc.stdout.read1(READ_CHUNK):  # type: ignore[attr-defined]
            total += len(chunk)
            room = cap - (len(buf) if out_file is None else total - len(chunk))
            if room > 0:
                part = chunk[:room]
                if out_file:
                    out_file.write(part)
                else:
                    buf += part
        rc = proc.wait()
    finally:
        timer.cancel()
        if out_file:
            out_file.close()
        if sink is not None and proc.stderr:
            buf += proc.stderr.read()[:4096]
    return bytes(buf), rc, killed.is_set(), total > cap


class DockerSandbox:
    def __init__(self, container_id: str):
        self.id = container_id
        probe = _docker(["exec", self.id, "sh", "-c",
                         "command -v bash >/dev/null && echo bash; command -v timeout >/dev/null && echo timeout; true"])
        found = probe.stdout.decode().split()
        # The shell is a property of the image, decided here once for every agent.
        self.shell = ["bash", "-lc"] if "bash" in found else ["sh", "-c"]
        self._has_timeout = "timeout" in found

    def alive(self) -> bool:
        """Ask Docker, never the command output (which the agent controls)."""
        r = _docker(["inspect", "-f", "{{.State.Running}}", self.id], check=False, timeout=30)
        return r.returncode == 0 and r.stdout.strip() == b"true"

    # -------------------------------------------------------------- commands

    def exec(self, cmd: str, *, timeout: int, cwd: str = "/workspace",
             env: dict[str, str] | None = None, stdin: bytes | None = None,
             secret_env: dict[str, str] | None = None) -> ExecResult:
        """`secret_env` values travel through the docker CLI's own environment
        (`-e NAME`), so they never appear on a command line or in `ps`."""
        args = ["exec"] + (["-i"] if stdin is not None else []) + ["-w", cwd]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        for k in secret_env or {}:
            args += ["-e", k]
        # Killing the docker client does not kill the process inside the container,
        # so enforce the timeout in-container when the image has `timeout`.
        wrapper = ["timeout", "-s", "KILL", str(timeout)] if self._has_timeout else []
        args += [self.id, *wrapper, *self.shell, cmd]

        start = time.monotonic()
        out, rc, killed, truncated = _run_capped(["docker", *args], timeout=timeout + CLIENT_GRACE_S,
                                                 cap=MAX_OUTPUT_BYTES, stdin=stdin,
                                                 env={**os.environ, **secret_env} if secret_env else None)
        duration = time.monotonic() - start
        # A non-zero exit is normally the agent's command failing. Only if Docker says
        # the container is gone is it the harness's problem.
        if rc != 0 and not self.alive():
            raise InfraError("docker", f"chamber stopped during exec (rc={rc})")
        return ExecResult(
            output=out.decode("utf-8", errors="replace"),
            returncode=-1 if killed else rc,
            timed_out=killed or (rc in (124, 137) and duration >= timeout - 0.5),
            truncated=truncated,
            duration_s=round(duration, 3),
        )

    # -------------------------------------------------------------- files

    def put(self, path: str, data: bytes) -> None:
        script = 'mkdir -p "$(dirname -- "$1")" && cat > "$1"'
        out, rc, _, _ = _run_capped(["docker", "exec", "-i", self.id, "sh", "-c", script, "sh", path],
                                    timeout=60, cap=4096, stdin=data)
        if rc != 0:
            if not self.alive():
                raise InfraError("docker", "chamber stopped during put")
            raise ValueError(f"cannot write {path}: {out.decode(errors='replace').strip()}")

    def get(self, path: str) -> bytes:
        out, rc, _, truncated = _run_capped(["docker", "exec", self.id, "cat", "--", path],
                                            timeout=60, cap=MAX_FILE_BYTES)
        if rc != 0:
            if not self.alive():
                raise InfraError("docker", "chamber stopped during get")
            raise FileNotFoundError(f"{path}: {out.decode(errors='replace').strip()}")
        if truncated:
            raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
        return out

    def put_dir(self, local_dir: Path, dest: str) -> None:
        _docker(["exec", self.id, "mkdir", "-p", dest])
        _docker(["cp", f"{local_dir}/.", f"{self.id}:{dest}"])

    def get_dir(self, src: str, local_dir: Path) -> None:
        local_dir.mkdir(parents=True, exist_ok=True)
        _docker(["cp", f"{self.id}:{src}/.", str(local_dir)])

    def snapshot(self, path: str, tar_path: Path) -> None:
        """Stream a tar of `path` (top-level entry = its basename) to `tar_path`.

        Raises InfraError if docker can't produce it; the caller decides whose fault
        that is (a deleted workspace is the agent's).
        """
        err, rc, killed, truncated = _run_capped(["docker", "cp", f"{self.id}:{path}", "-"], timeout=300,
                                                 cap=MAX_SNAPSHOT_BYTES, sink=tar_path)
        if rc != 0 or killed or truncated:
            tar_path.unlink(missing_ok=True)
            why = "too large" if truncated else "timed out" if killed else err.decode(errors="replace").strip()
            raise InfraError("docker", f"snapshot of {path} failed: {why}")

    # -------------------------------------------------------------- lifecycle

    def stop(self) -> None:
        try:
            _docker(["rm", "-f", "-v", self.id], check=False)
        except InfraError:
            pass  # the reaper will get it

    def __enter__(self) -> DockerSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


class DockerSandboxProvider:
    def ensure_image(self, image: str | None = None, *, dockerfile: Path | None = None) -> str:
        if dockerfile is not None:
            image = self._build(dockerfile)
        assert image is not None
        found = _docker(["image", "inspect", "--format", "{{.Id}}", image], check=False)
        if found.returncode != 0:
            _docker(["pull", image], timeout=600, kind="image")
            found = _docker(["image", "inspect", "--format", "{{.Id}}", image], kind="image")
        return found.stdout.decode().strip()

    @staticmethod
    def tooling_tag(dockerfile: Path) -> str:
        """Tag = content hash of the Dockerfile's folder (the build context)."""
        context = dockerfile.parent
        h = hashlib.sha256()
        for f in sorted(p for p in context.rglob("*") if p.is_file()):
            h.update(str(f.relative_to(context)).encode())
            h.update(f.read_bytes())
        return f"vals/{context.name}-tooling:{h.hexdigest()[:12]}"

    def _build(self, dockerfile: Path) -> str:
        """Build once per content hash of the build context."""
        context = dockerfile.parent
        tag = self.tooling_tag(dockerfile)
        if _docker(["image", "inspect", tag], check=False).returncode != 0:
            _docker(["build", "-q", "-t", tag, "-f", str(dockerfile), str(context)], timeout=900, kind="image")
        return tag

    def create(self, image: str, *, labels: dict[str, str], network: str = "none",
               cpus: float = 1.0, memory: str = "1g") -> DockerSandbox:
        args = ["run", "-d", "--label", "vals.managed=1"]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        args += [
            *HARDENING,
            "--network", "none" if network == "none" else "bridge",
            "--cpus", str(cpus), "--memory", memory,
            "-w", "/workspace",
            # Override any image entrypoint: we only need the container to stay alive.
            "--entrypoint", "tail", image, "-f", "/dev/null",
        ]
        cid = _docker(args, timeout=120).stdout.decode().strip()
        try:
            return DockerSandbox(cid)
        except Exception:
            _docker(["rm", "-f", cid], check=False)
            raise

    def reap(self, labels: dict[str, str]) -> int:
        """Remove sandboxes matching all labels. Never raises: cleanup must not crash a worker."""
        try:
            args = ["ps", "-aq", "--filter", "label=vals.managed=1"]
            for k, v in labels.items():
                args += ["--filter", f"label={k}={v}"]
            ids = _docker(args, check=False, timeout=60).stdout.decode().split()
            if ids:
                _docker(["rm", "-f", "-v", *ids], check=False, timeout=120)
            return len(ids)
        except InfraError:
            return 0

    def reap_stale(self, live_claims: set[str]) -> int:
        """Remove every harness sandbox whose claim is no longer live (dead workers' leftovers)."""
        try:
            out = _docker(["ps", "-a", "--filter", "label=vals.managed=1",
                           "--format", '{{.ID}} {{.Label "vals.claim"}}'], check=False, timeout=60).stdout.decode()
            stale = [cid for cid, _, claim in (line.partition(" ") for line in out.splitlines())
                     if claim not in live_claims]
            if stale:
                _docker(["rm", "-f", "-v", *stale], check=False, timeout=120)
            return len(stale)
        except InfraError:
            return 0


def vals_images() -> list[str]:
    """Every locally built harness image (repository vals/*), as repo:tag."""
    out = _docker(["images", "--format", "{{.Repository}}:{{.Tag}}", "--filter", "reference=vals/*"],
                  check=False, timeout=60).stdout.decode()
    return [line for line in out.split() if line and not line.endswith(":<none>")]


def remove_images(refs: list[str]) -> list[str]:
    """Remove images; returns the ones Docker refused (e.g. still used by a container)."""
    refused = []
    for ref in refs:
        if _docker(["rmi", ref], check=False, timeout=120).returncode != 0:
            refused.append(ref)
    return refused
