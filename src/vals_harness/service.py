"""Service layer: everything the CLI does, without the CLI (so it's testable)."""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from vals_harness.launchers import DockerLauncher
from vals_harness.sandbox.docker import DockerSandboxProvider, remove_images, vals_images
from vals_harness.specs import RunSpec, load_agent, load_benchmark, resolve_trials, trial_budget_s
from vals_harness.store import Claim, Store

LEASE_S = 60
HEARTBEAT_S = 10


@dataclass(frozen=True)
class Paths:
    root: Path        # where benchmarks/ and agents/ live
    home: Path        # local state: DB + artifacts

    @property
    def db(self) -> Path:
        return self.home / "harness.db"

    @property
    def artifacts(self) -> Path:
        return self.home / "artifacts"

    @classmethod
    def from_root(cls, root: Path) -> Paths:
        root = root.resolve()
        load_dotenv(root / ".env")
        return cls(root=root, home=Path(os.environ.get("VALS_HOME", root / ".vals")).resolve())


def load_dotenv(path: Path) -> None:
    """Minimal KEY=VALUE loader. Real environment variables win over the file.

    Values only reach an agent if its AgentSpec declares them in `secrets`.
    """
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def harness_version(root: Path) -> str:
    try:
        sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, capture_output=True,
                             text=True, timeout=5).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                               text=True, timeout=5).stdout.strip()
        return (sha or "uncommitted") + ("+dirty" if dirty else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def new_run_id() -> str:
    return f"r{time.strftime('%m%d-%H%M')}-{secrets.token_hex(2)}"


def submit(paths: Paths, run: RunSpec) -> str:
    """Resolve specs into trials and enqueue them. Returns immediately."""
    run_id = new_run_id()
    trials = resolve_trials(run, run_id, paths.root, harness_version(paths.root))
    Store(paths.db).create_run(run_id, run, trials)
    return run_id


@dataclass
class _Running:
    claim: Claim
    proc: subprocess.Popen[bytes]
    result_path: Path
    deadline: float              # monotonic; trial wall clock


def work(paths: Paths, *, slots: int = 1, run_id: str | None = None, stop_when_empty: bool = True,
         on_result: Callable[[dict[str, Any], str | None], None] | None = None) -> None:
    """Worker supervisor: keeps up to `slots` trials running, each in its own child process.

    The supervisor is the only writer of final results. It renews every running
    trial's lease, enforces the trial wall clock (kill the child's process group,
    reap its containers by claim label, record `timeout_harness`), and turns a child
    that died without a result into an infra_error. Nothing a trial does can stop
    this loop. Ctrl-C hands claimed trials back to the queue without charging them.
    """
    if slots < 1:
        raise ValueError("slots must be >= 1")
    store = Store(paths.db)
    provider = DockerSandboxProvider()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    jobs_dir = paths.home / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    provider.reap_stale(store.live_claims())  # leftovers from workers that died
    running: dict[str, _Running] = {}
    last_heartbeat = time.monotonic()

    def record(r: _Running, result: dict[str, Any] | None) -> None:
        status = store.finish(r.claim.spec.trial_id, r.claim.token, result) if result else None
        if on_result and result:
            on_result(result, status)

    try:
        while True:
            # 1. collect finished children, enforce the trial wall clock
            for token, r in list(running.items()):
                if r.proc.poll() is None:
                    if time.monotonic() > r.deadline:
                        _kill_group(r.proc)
                        provider.reap({"vals.claim": token})
                        del running[token]
                        budget = trial_budget_s(r.claim.spec.limits)
                        record(r, _infra(r.claim, "timeout_harness", f"trial exceeded its {budget}s wall clock"))
                    continue
                del running[token]
                provider.reap({"vals.claim": token})  # in case the child died before its own cleanup
                result = _read_result(r.result_path)
                if result is None:
                    record(r, _infra(r.claim, "adapter", f"trial process exited ({r.proc.returncode}) without a result"))
                elif not result.get("lost_lease"):
                    record(r, result)

            # 2. fill free slots
            claimed_any = False
            while len(running) < slots:
                claim = store.claim(worker_id, LEASE_S, run_id=run_id)
                if claim is None:
                    break
                claimed_any = True
                running[claim.token] = _spawn(paths, jobs_dir, claim)

            if not running and not claimed_any:
                if stop_when_empty:
                    return
                time.sleep(2)
                continue

            # 3. renew leases; a lease we lost means another worker owns the trial now
            if time.monotonic() - last_heartbeat >= HEARTBEAT_S:
                last_heartbeat = time.monotonic()
                for token, r in list(running.items()):
                    if not store.heartbeat(r.claim.spec.trial_id, token, LEASE_S):
                        _kill_group(r.proc)
                        provider.reap({"vals.claim": token})
                        del running[token]
            time.sleep(0.2)
    finally:
        for token, r in running.items():  # only on Ctrl-C / unexpected exit
            _kill_group(r.proc)
            provider.reap({"vals.claim": token})
            store.release(r.claim.spec.trial_id, token)


def _spawn(paths: Paths, jobs_dir: Path, claim: Claim) -> _Running:
    job = jobs_dir / f"{claim.token}.job.json"
    result = jobs_dir / f"{claim.token}.result.json"
    job.write_text(json.dumps({"spec": claim.spec.model_dump_json(), "token": claim.token,
                               "try_no": claim.try_no, "db": str(paths.db), "artifacts": str(paths.artifacts)}))
    # Own session = own process group, so a timeout kills the child and everything it started.
    proc = subprocess.Popen([sys.executable, "-m", "vals_harness.trial_child", str(job), str(result)],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, start_new_session=True)
    return _Running(claim, proc, result, time.monotonic() + trial_budget_s(claim.spec.limits))


def _read_result(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    finally:
        path.unlink(missing_ok=True)
        path.with_name(path.name.replace(".result.json", ".job.json")).unlink(missing_ok=True)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    proc.wait()


def _infra(claim: Claim, kind: str, message: str) -> dict[str, Any]:
    s = claim.spec
    return {"status": "infra_error", "error": {"kind": kind, "message": message}, "trial_id": s.trial_id,
            "run_id": s.run_id, "task_id": s.task.id, "attempt": s.attempt, "try": claim.try_no, "metrics": {}}


def cancel(paths: Paths, run_id: str) -> int:
    return Store(paths.db).cancel_run(run_id)


def current_image_tags(paths: Paths) -> set[str]:
    """Tags the current agent and benchmark folders would build."""
    keep: set[str] = set()
    for folder in sorted((paths.root / "agents").glob("*/agent.yaml")):
        agent = load_agent(str(folder.parent), paths.root)
        if agent.placement == "sidecar":
            keep.add(DockerLauncher.tag_for(agent))
        elif agent.placement == "installed":
            keep.add(f"vals/installed-{agent.name}:*")   # one per task image; see prune()
    for folder in sorted((paths.root / "benchmarks").glob("*/harness.yaml")):
        bench = load_benchmark(str(folder.parent), paths.root)
        if bench.tooling_dockerfile:
            keep.add(DockerSandboxProvider.tooling_tag(bench.root / bench.tooling_dockerfile))
    return keep


def prune(paths: Paths, *, dry_run: bool = False) -> dict[str, list[str] | int]:
    """Remove harness images that no current agent/benchmark folder would build, and
    containers left by dead workers. Images still in use are kept (Docker refuses)."""
    keep = current_image_tags(paths)
    wild = {k[:-1] for k in keep if k.endswith(":*")}   # installed layers: kept per agent
    stale = sorted(i for i in set(vals_images()) - keep
                   if not any(i.startswith(w) for w in wild) and not i.startswith("vals/base:"))
    containers = 0
    refused: list[str] = []
    if not dry_run:
        containers = DockerSandboxProvider().reap_stale(Store(paths.db).live_claims())
        refused = remove_images(stale)
    return {"kept": sorted(keep), "removed": [i for i in stale if i not in refused], "refused": refused,
            "containers_removed": containers}
