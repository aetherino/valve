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

from vals_harness.installed import InstalledRunner
from vals_harness.launchers import DockerLauncher
from vals_harness.sandbox.docker import DockerSandboxProvider, image_ids, remove_images, vals_images
from vals_harness.specs import (AgentSpec, BenchmarkSpec, RunSpec, TrialSpec, load_agent, load_benchmark,
                                missing_secrets, resolve_trials, trial_budget_s)
from vals_harness.store import ACTIVE, Claim, Store

LEASE_S = 60
FOLLOW_EVENTS = ("phase", "exec", "agent_finished", "infra_error")
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
    """Resolve specs into trials and enqueue them. Returns immediately.

    Refuses a run whose agent's required secrets aren't set: every trial would fail at
    the model provider and be graded 0 (PREPARE checks again, for the worker's environment).
    """
    run_id = new_run_id()
    trials = resolve_trials(run, run_id, paths.root, harness_version(paths.root))
    if trials and (missing := missing_secrets(trials[0].agent)):
        raise ValueError(f"agent {trials[0].agent.name} needs {', '.join(missing)}: set it in the environment "
                         "or in .env (nothing was queued)")
    Store(paths.db).create_run(run_id, run, trials)
    return run_id


@dataclass
class _Running:
    claim: Claim
    proc: subprocess.Popen[bytes]
    result_path: Path
    deadline: float              # monotonic; trial wall clock


def max_active_trials() -> int | None:
    """VALS_MAX_ACTIVE_TRIALS: global cap on running trials for this home (all workers).
    Each trial runs at most 3 containers at once, so this also bounds live containers."""
    v = os.environ.get("VALS_MAX_ACTIVE_TRIALS")
    return int(v) if v else None


def work(paths: Paths, *, slots: int = 1, run_id: str | None = None, stop_when_empty: bool = True,
         on_result: Callable[[dict[str, Any], str | None], None] | None = None,
         on_event: Callable[[Any], None] | None = None) -> None:
    """Worker supervisor: keeps up to `slots` trials running, each in its own child process.

    The supervisor is the only writer of final results. It renews every running
    trial's lease, enforces the trial wall clock (kill the child's process group,
    reap its containers by claim label, record `timeout_harness`), and turns a child
    that died without a result into an infra_error. Nothing a trial does can stop
    this loop. Ctrl-C, SIGTERM and SIGHUP hand claimed trials back to the queue without
    charging them (a trial whose child already wrote its result is recorded instead).
    If the supervisor is SIGKILLed, each child notices and cleans up itself (trial_child).
    """
    if slots < 1:
        raise ValueError("slots must be >= 1")
    store = Store(paths.db)
    provider = DockerSandboxProvider()
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    jobs_dir = paths.home / "jobs"
    jobs_dir.mkdir(parents=True, exist_ok=True)
    provider.reap_stale(store.live_claims(), store.home_id)  # leftovers from this home's dead workers
    running: dict[str, _Running] = {}
    last_heartbeat = time.monotonic()
    cap = max_active_trials()
    last_event = store.max_event_id()
    mine: set[str] = set()   # trials this worker claimed (their last events can arrive after they finish)
    restore = _shutdown_on_signals()

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
                        _remove_job(r.result_path)
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

            # 2. fill free slots (bounded by the global cap, if set)
            claimed_any = False
            while len(running) < slots:
                claim = store.claim(worker_id, LEASE_S, run_id=run_id, max_active=cap)
                if claim is None:
                    break
                claimed_any = True
                mine.add(claim.spec.trial_id)
                running[claim.token] = _spawn(paths, jobs_dir, claim)

            # live progress (e.g. `run --follow`): new events from trials this worker ran
            if on_event:
                for ev in store.events_after(last_event, FOLLOW_EVENTS):
                    last_event = ev["id"]
                    if ev["trial_id"] in mine:
                        on_event(ev)

            if not running and not claimed_any:
                # Nothing here; others may still hold the cap or be finishing trials.
                if stop_when_empty and store.queued_count(run_id) == 0:
                    return
                time.sleep(1)
                continue

            # 3. renew leases; a lease we lost means another worker owns the trial now
            if time.monotonic() - last_heartbeat >= HEARTBEAT_S:
                last_heartbeat = time.monotonic()
                for token, r in list(running.items()):
                    if not store.heartbeat(r.claim.spec.trial_id, token, LEASE_S):
                        _kill_group(r.proc)
                        provider.reap({"vals.claim": token})
                        _remove_job(r.result_path)
                        del running[token]
            time.sleep(0.2)
    finally:
        restore()
        for token, r in running.items():  # only on Ctrl-C / SIGTERM / unexpected exit
            _kill_group(r.proc)
            provider.reap({"vals.claim": token})
            result = _read_result(r.result_path)   # also removes the job files
            if result is not None and not result.get("lost_lease"):
                record(r, result)                  # it finished just in time: keep it
            else:
                store.release(r.claim.spec.trial_id, token)


class Shutdown(KeyboardInterrupt):
    """SIGTERM / SIGHUP: take the same clean path as Ctrl-C."""


def _shutdown_on_signals() -> Callable[[], None]:
    """Map SIGTERM/SIGHUP onto the Ctrl-C path (Python's default for them skips `finally`,
    leaving children and their containers running). Returns a function that restores the
    previous handlers. A no-op outside the main thread, where handlers can't be set."""
    def _raise(signum: int, _frame: Any) -> None:
        raise Shutdown(f"signal {signum}")

    old: dict[int, Any] = {}
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, _raise)
    except ValueError:   # not the main thread
        pass

    def restore() -> None:
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return restore


def _spawn(paths: Paths, jobs_dir: Path, claim: Claim) -> _Running:
    job = jobs_dir / f"{claim.token}.job.json"
    result = jobs_dir / f"{claim.token}.result.json"
    job.write_text(json.dumps({"spec": claim.spec.model_dump_json(), "token": claim.token,
                               "try_no": claim.try_no, "db": str(paths.db), "artifacts": str(paths.artifacts),
                               "supervisor_pid": os.getpid()}))
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
        _remove_job(path)


def _remove_job(result_path: Path) -> None:
    """A trial's job files are removed however it ends (result, timeout, lost lease, shutdown)."""
    for p in (result_path, result_path.with_name(result_path.name.replace(".result.json", ".job.json")),
              result_path.with_name(result_path.name + ".tmp")):
        p.unlink(missing_ok=True)


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


def _tags_for(agents: list[AgentSpec], benches: list[BenchmarkSpec], task_image_ids: set[str]) -> set[str]:
    keep: set[str] = set()
    for agent in agents:
        if agent.placement == "sidecar" and agent.root.is_dir():
            keep.add(DockerLauncher.tag_for(agent))
        elif agent.placement == "installed" and agent.root.is_dir():
            # One layer (and one `vals/base:` tag) per task image.
            for image_id in task_image_ids:
                keep |= {InstalledRunner.tag_for(agent, image_id), InstalledRunner.base_tag(image_id)}
    for bench in benches:
        if bench.tooling_dockerfile and (bench.root / bench.tooling_dockerfile).is_file():
            keep.add(DockerSandboxProvider.tooling_tag(bench.root / bench.tooling_dockerfile))
    return keep


def current_image_tags(paths: Paths, task_image_ids: set[str] | None = None) -> set[str]:
    """Tags the current agent and benchmark folders would build (installed-agent layers
    for the given task image ids)."""
    agents = [load_agent(str(f.parent), paths.root) for f in sorted((paths.root / "agents").glob("*/agent.yaml"))]
    benches = [load_benchmark(str(f.parent), paths.root)
               for f in sorted((paths.root / "benchmarks").glob("*/harness.yaml"))]
    return _tags_for(agents, benches, task_image_ids or set())


def prune(paths: Paths, *, dry_run: bool = False) -> dict[str, list[str] | int]:
    """Remove harness images that nothing needs any more, and containers left by this
    home's dead workers. Kept: what the current agent/benchmark folders build; installed
    layers (+ `vals/base:` tags) for the task images of every run in the DB; and every
    image a queued or running trial needs, even if its agent lives outside agents/.
    Images in use by a container are kept too (Docker refuses)."""
    store = Store(paths.db)
    specs = [(row["status"], TrialSpec.model_validate_json(row["spec_json"]))
             for row in store.db.execute("SELECT status, spec_json FROM trials")]
    task_ids = set(image_ids(sorted({s.task.image for _, s in specs})).values())
    pending = [s for status, s in specs if status == "queued" or status in ACTIVE]
    keep = current_image_tags(paths, task_ids) | _tags_for([s.agent for s in pending],
                                                           [s.benchmark for s in pending], task_ids)
    stale = sorted(set(vals_images()) - keep)
    containers = 0
    refused: list[str] = []
    if not dry_run:
        containers = DockerSandboxProvider().reap_stale(store.live_claims(), store.home_id)
        refused = remove_images(stale)
    return {"kept": sorted(keep), "removed": [i for i in stale if i not in refused], "refused": refused,
            "containers_removed": containers}
