"""Entry point for one trial in its own process (spawned by the worker supervisor).

    python -m valve.trial_child <job.json> <result.json>

The child runs the trial and writes the result file; the supervisor is the only
one that records final results in the DB, renews leases, and kills the child if
the trial wall clock runs out. A crash here can't take down the worker or any
other trial.

The child runs in its own session (so the supervisor can kill its whole group),
which also means it would outlive a supervisor that is SIGKILLed. A watcher thread
notices the parent is gone, removes this claim's containers, hands the trial back
to the queue and exits, so nothing keeps running (and spending) unsupervised.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

from valve.sandbox.docker import DockerSandboxProvider
from valve.specs import TrialSpec
from valve.store import Claim, LostLease, Store
from valve.trial import TrialRunner

PARENT_POLL_S = 1.0


def _watch_parent(parent: int, claim: Claim, db: Path, provider: DockerSandboxProvider, job: Path) -> None:
    while os.getppid() == parent:
        time.sleep(PARENT_POLL_S)
    provider.reap({"valve.claim": claim.token})
    try:
        Store(db).release(claim.spec.trial_id, claim.token)   # fenced: a no-op if someone took over
    except Exception:
        pass   # the lease will expire instead
    job.unlink(missing_ok=True)
    # Again: the trial thread may have started a container meanwhile. Anything created
    # after this is left to the next worker's reap_stale (its claim is no longer live).
    provider.reap({"valve.claim": claim.token})
    os._exit(3)


def main(job_path: str, result_path: str) -> int:
    job = json.loads(Path(job_path).read_text())
    parent = int(job.get("supervisor_pid") or os.getppid())
    claim = Claim(TrialSpec.model_validate_json(job["spec"]), job["token"], job["try_no"])
    store = Store(Path(job["db"]))
    provider = DockerSandboxProvider()
    threading.Thread(target=_watch_parent, args=(parent, claim, Path(job["db"]), provider, Path(job_path)), daemon=True).start()
    runner = TrialRunner(provider, store, Path(job["artifacts"]))
    try:
        result = runner.run(claim)
    except LostLease:
        result = {"lost_lease": True}
    tmp = Path(result_path + ".tmp")
    tmp.write_text(json.dumps(result))
    tmp.replace(result_path)  # atomic: the supervisor never reads a half-written file
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
