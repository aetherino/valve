"""Service layer: everything the CLI does, without the CLI (so it's testable)."""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from vals_harness.sandbox.docker import DockerSandboxProvider
from vals_harness.specs import RunSpec, resolve_trials
from vals_harness.store import LostLease, Store
from vals_harness.trial import TrialRunner

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


def work(paths: Paths, *, run_id: str | None = None, stop_when_empty: bool = True,
         on_result: Callable[[dict[str, Any], str | None], None] | None = None) -> None:
    """Worker loop with one slot: claim -> run -> record, renewing the lease while running.

    Nothing a single trial does can kill this loop: harness bugs become that trial's
    infra_error, and a lost lease just drops the stale result. Phase 3 adds N slots
    with one child process per trial.
    """
    store = Store(paths.db)
    provider = DockerSandboxProvider()
    runner = TrialRunner(provider, store, paths.artifacts)
    worker_id = f"{socket.gethostname()}:{os.getpid()}"
    provider.reap_stale(store.live_claims())  # leftovers from workers that died
    while True:
        claim = store.claim(worker_id, LEASE_S, run_id=run_id)
        if claim is None:
            if stop_when_empty:
                return
            time.sleep(2)
            continue
        spec = claim.spec
        stop = threading.Event()
        hb = threading.Thread(target=_heartbeat, args=(paths.db, spec.trial_id, claim.token, stop), daemon=True)
        hb.start()
        try:
            result = runner.run(claim)
        except LostLease:
            result = None
        except Exception as e:  # defensive: runner.run already converts its own errors
            result = {"status": "infra_error", "error": {"kind": "adapter", "message": f"{type(e).__name__}: {e}"},
                      "trial_id": spec.trial_id, "run_id": spec.run_id, "task_id": spec.task.id,
                      "attempt": spec.attempt, "metrics": {}}
        finally:
            stop.set()
            hb.join()
        status = store.finish(spec.trial_id, claim.token, result) if result else None
        if on_result and result:
            on_result(result, status)


def cancel(paths: Paths, run_id: str) -> int:
    return Store(paths.db).cancel_run(run_id)


def _heartbeat(db: Path, trial_id: str, token: str, stop: threading.Event) -> None:
    store = Store(db)  # own connection: sqlite connections shouldn't be shared across threads
    while not stop.wait(HEARTBEAT_S):
        if not store.heartbeat(trial_id, token, LEASE_S):
            return  # fenced out; the runner will hit LostLease at its next write
