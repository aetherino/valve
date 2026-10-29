"""Entry point for one trial in its own process (spawned by the worker supervisor).

    python -m vals_harness.trial_child <job.json> <result.json>

The child runs the trial and writes the result file; the supervisor is the only
one that records final results in the DB, renews leases, and kills the child if
the trial wall clock runs out. A crash here can't take down the worker or any
other trial.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from vals_harness.sandbox.docker import DockerSandboxProvider
from vals_harness.specs import TrialSpec
from vals_harness.store import Claim, LostLease, Store
from vals_harness.trial import TrialRunner


def main(job_path: str, result_path: str) -> int:
    job = json.loads(Path(job_path).read_text())
    claim = Claim(TrialSpec.model_validate_json(job["spec"]), job["token"], job["try_no"])
    store = Store(Path(job["db"]))
    runner = TrialRunner(DockerSandboxProvider(), store, Path(job["artifacts"]))
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
