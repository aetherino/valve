"""Docker implementation of Sandbox / SandboxProvider, driving the `docker` CLI."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import tempfile
import json
import os
import tarfile
import subprocess
import threading
import time
from pathlib import Path

from vals_harness.errors import ChamberStopped, InfraError
from vals_harness.sandbox import ExecResult

MAX_OUTPUT_BYTES = 1_000_000        # per exec, returned to the agent
MAX_FILE_BYTES = 10_000_000         # per `get`
MAX_SNAPSHOT_BYTES = 1_000_000_000  # final workspace tar
MAX_STREAM_BYTES = 50_000_000       # exec with stdout_path (an installed CLI's stream-json)
PULL_TIMEOUT_S = 600                # image builds/pulls; their sum must fit specs.PREPARE_SLACK_S
TOOLING_BUILD_TIMEOUT_S = 900
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
    err_buf = bytearray()
    drain = None
    if sink is not None:
        # Drain stderr concurrently: a full stderr pipe must never stall the stdout stream.
        def _drain() -> None:
            assert proc.stderr
            while chunk := proc.stderr.read1(READ_CHUNK):  # type: ignore[attr-defined]
                if len(err_buf) < 4096:
                    err_buf.extend(chunk[:4096 - len(err_buf)])
        drain = threading.Thread(target=_drain, daemon=True)
        drain.start()
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
        if drain is not None:
            drain.join(timeout=5)
            buf += err_buf
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
        self._halted = False                    # the harness stopped it on purpose
        self.stopped_inside: str | None = None  # why it stopped from the inside, once known

    def alive(self) -> bool:
        """Ask Docker, never the command output (which the agent controls)."""
        r = _docker(["inspect", "-f", "{{.State.Running}}", self.id], check=False, timeout=30)
        return r.returncode == 0 and r.stdout.strip() == b"true"

    def exists(self) -> bool:
        return _docker(["inspect", "-f", "{{.Id}}", self.id], check=False, timeout=30).returncode == 0

    def _stopped(self, what: str) -> ChamberStopped:
        """The container is not running: decide whose fault that is, from Docker's own records.

        Stopped from the inside (an agent's `kill 1`, `pkill tail`, `kill -9 -1`, or its
        processes blowing the memory limit) -> ChamberStopped, returned for the caller to
        raise (the agent's failure). Removed, stopped through the Docker API (`docker stop`,
        a daemon shutting down: a `kill`/`stop` event), stopped by the harness, or a daemon
        we can't ask -> InfraError, raised here.
        """
        if self.stopped_inside:
            return ChamberStopped(self.stopped_inside)
        r = _docker(["inspect", "-f", "{{json .State}}", self.id], check=False, timeout=30)
        if r.returncode != 0:
            raise InfraError("docker", f"sandbox vanished during {what}: {r.stderr.decode(errors='replace').strip()}")
        try:
            state = json.loads(r.stdout)
        except json.JSONDecodeError as e:
            raise InfraError("docker", f"unreadable sandbox state after {what}") from e
        if self._halted or state.get("Status") != "exited" or state.get("ExitCode") == 255:
            raise InfraError("docker", f"sandbox stopped during {what} (state {state.get('Status')}, "
                                       f"exit {state.get('ExitCode')})")
        if not state.get("OOMKilled"):
            # Docker records API-initiated stops as kill/stop events; a process inside the
            # container killing PID 1 leaves only `die`.
            ev = _docker(["events", "--since", "0", "--until", f"{time.time():.3f}",
                          "--filter", f"container={self.id}", "--filter", "event=kill",
                          "--filter", "event=stop", "--format", "{{.Action}}"], check=False, timeout=30)
            if ev.returncode != 0 or ev.stdout.strip():
                raise InfraError("docker", f"sandbox stopped through the Docker API during {what}")
        why = "out of memory" if state.get("OOMKilled") else f"init exited with {state.get('ExitCode')}"
        self.stopped_inside = f"sandbox stopped from the inside during {what} ({why})"
        return ChamberStopped(self.stopped_inside)

    # -------------------------------------------------------------- commands

    def exec(self, cmd: str, *, timeout: int, cwd: str | None = None,
             env: dict[str, str] | None = None, stdin: bytes | None = None,
             secret_env: dict[str, str] | None = None, stdout_path: Path | None = None) -> ExecResult:
        """`secret_env` values travel through the docker CLI's own environment
        (`-e NAME`), so they never appear on a command line or in `ps`. With
        `stdout_path`, stdout streams to that host file (capped at MAX_STREAM_BYTES)."""
        if self.stopped_inside:
            raise ChamberStopped(self.stopped_inside)
        # cwd None = the container's working directory (the task workspace, set at create)
        args = ["exec"] + (["-i"] if stdin is not None else []) + (["-w", cwd] if cwd else [])
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
                                                 cap=MAX_STREAM_BYTES if stdout_path else MAX_OUTPUT_BYTES,
                                                 stdin=stdin, sink=stdout_path,
                                                 env={**os.environ, **secret_env} if secret_env else None)
        duration = time.monotonic() - start
        # A non-zero exit is normally the agent's command failing. If the container is
        # no longer running, Docker's records decide whose fault it is (_stopped).
        if rc != 0 and not self.alive():
            raise self._stopped("exec")
        return ExecResult(
            output=out.decode("utf-8", errors="replace"),
            returncode=-1 if killed else rc,
            timed_out=killed or (rc in (124, 137) and duration >= timeout - 0.5),
            truncated=truncated,
            duration_s=round(duration, 3),
        )

    # -------------------------------------------------------------- files

    def put(self, path: str, data: bytes) -> None:
        if self.stopped_inside:
            raise ChamberStopped(self.stopped_inside)
        script = 'mkdir -p "$(dirname -- "$1")" && cat > "$1"'
        out, rc, _, _ = _run_capped(["docker", "exec", "-i", self.id, "sh", "-c", script, "sh", path],
                                    timeout=60, cap=4096, stdin=data)
        if rc != 0:
            if not self.alive():
                raise self._stopped("put")
            raise ValueError(f"cannot write {path}: {out.decode(errors='replace').strip()}")

    def get(self, path: str) -> bytes:
        """Read one file. Works on a stopped sandbox too (via `docker cp`)."""
        if self.stopped_inside or self._halted:
            return self._get_stopped(path)
        out, rc, _, truncated = _run_capped(["docker", "exec", self.id, "cat", "--", path],
                                            timeout=60, cap=MAX_FILE_BYTES)
        if rc != 0:
            if not self.alive():
                self._stopped("get")    # raises InfraError unless it stopped from the inside
                return self._get_stopped(path)
            raise FileNotFoundError(f"{path}: {out.decode(errors='replace').strip()}")
        if truncated:
            raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
        return out

    def _get_stopped(self, path: str) -> bytes:
        tar, rc, _, truncated = _run_capped(["docker", "cp", f"{self.id}:{path}", "-"], timeout=60,
                                            cap=MAX_FILE_BYTES + 1_000_000)
        if rc != 0:
            if not self.exists():
                raise InfraError("docker", "sandbox vanished during get")
            raise FileNotFoundError(f"{path}: {tar.decode(errors='replace').strip()}")
        if truncated:
            raise ValueError(f"{path} is larger than {MAX_FILE_BYTES} bytes")
        try:
            with tarfile.open(fileobj=io.BytesIO(tar)) as t:
                member = t.next()
                f = t.extractfile(member) if member is not None and member.isfile() else None
                if f is None:
                    raise FileNotFoundError(f"{path}: not a regular file")
                return f.read()
        except tarfile.TarError as e:
            raise FileNotFoundError(f"{path}: {e}") from e

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

    def halt(self) -> None:
        """Kill every process in the sandbox, keep its filesystem (docker cp still works).

        Used before the final snapshot, so nothing the agent left running (busybox
        `timeout` leaves grandchildren) keeps writing while the workspace is copied out.
        """
        self._halted = True
        _docker(["kill", self.id], check=False, timeout=60)

    def stop(self) -> None:
        try:
            _docker(["rm", "-f", "-v", self.id], check=False)
        except InfraError:
            pass  # the reaper will get it

    def __enter__(self) -> DockerSandbox:
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()


@contextlib.contextmanager
def build_lock(tag: str):
    """Exclusive lock per image tag across processes on this machine (flock)."""
    lock_dir = Path(tempfile.gettempdir()) / "vals-build-locks"
    lock_dir.mkdir(exist_ok=True)
    with open(lock_dir / (hashlib.sha256(tag.encode()).hexdigest()[:16] + ".lock"), "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class DockerSandboxProvider:
    def ensure_image(self, image: str | None = None, *, dockerfile: Path | None = None,
                     name: str | None = None) -> str:
        if dockerfile is not None:
            image = self._build(dockerfile, name)
        assert image is not None
        found = _docker(["image", "inspect", "--format", "{{.Id}}", image], check=False)
        if found.returncode != 0:
            _docker(["pull", image], timeout=PULL_TIMEOUT_S, kind="image")
            found = _docker(["image", "inspect", "--format", "{{.Id}}", image], kind="image")
        return found.stdout.decode().strip()

    @staticmethod
    def tooling_tag(dockerfile: Path, name: str | None = None) -> str:
        """Tag = content hash of the Dockerfile + its folder (the build context)."""
        context = dockerfile.parent
        h = hashlib.sha256()
        h.update(dockerfile.name.encode())
        for f in sorted(p for p in context.rglob("*") if p.is_file()):
            h.update(str(f.relative_to(context)).encode())
            h.update(f.read_bytes())
        return f"vals/{name or context.name + '-tooling'}:{h.hexdigest()[:12]}"

    def _build(self, dockerfile: Path, name: str | None = None) -> str:
        """Build once per content hash of the build context. Concurrent trials needing the
        same image wait on a per-tag lock instead of racing two builds (Docker's legacy
        builder fails intermittently when they collide)."""
        context = dockerfile.parent
        tag = self.tooling_tag(dockerfile, name)
        if _docker(["image", "inspect", tag], check=False).returncode == 0:
            return tag
        with build_lock(tag):
            if _docker(["image", "inspect", tag], check=False).returncode != 0:
                _docker(["build", "-q", "-t", tag, "-f", str(dockerfile), str(context)],
                        timeout=TOOLING_BUILD_TIMEOUT_S, kind="image")
        return tag

    def create(self, image: str, *, labels: dict[str, str], network: str = "none",
               cpus: float = 1.0, memory: str = "1g", volumes: dict[str, str] | None = None,
               workdir: str = "/workspace") -> DockerSandbox:
        args = ["run", "-d", "--label", "vals.managed=1"]
        for k, v in labels.items():
            args += ["--label", f"{k}={v}"]
        for vol, path in (volumes or {}).items():   # phantom placement: /workspace + /tmp shared with the box
            args += ["-v", f"{vol}:{path}"]
        args += [
            *HARDENING,
            "--network", "none" if network == "none" else "bridge",
            "--cpus", str(cpus), "--memory", memory,
            "-w", workdir,
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

    def reap_stale(self, live_claims: set[str], home: str) -> int:
        """Remove this home's harness sandboxes whose claim is no longer live (dead workers'
        leftovers). Filtering by `vals.home` keeps a worker (or a test) with another
        VALS_HOME from deleting live containers it knows nothing about."""
        try:
            out = _docker(["ps", "-a", "--filter", "label=vals.managed=1", "--filter", f"label=vals.home={home}",
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


def image_ids(refs: list[str]) -> dict[str, str]:
    """Local image id of each ref that exists locally (missing refs are left out)."""
    out: dict[str, str] = {}
    for ref in refs:
        r = _docker(["image", "inspect", "--format", "{{.Id}}", ref], check=False, timeout=60)
        if r.returncode == 0:
            out[ref] = r.stdout.decode().strip()
    return out
