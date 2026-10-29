"""TrialRunner: one try at one trial, start to finish (DESIGN.md §4).

    PREPARE -> CHAMBER -> AGENT -> COLLECT -> VERIFY -> RECORD

Agent failures become graded results (score 0). Harness failures raise
InfraError and become `infra_error` (retried by the store, excluded from accuracy).
Every container is labeled with this claim's token and reaped in `finally`, so a
stale worker can never remove the containers of the worker that took over.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from vals_harness.benchmarks import BenchmarkAdapter
from vals_harness.chamber_api import AgentOutcome, ChamberApi
from vals_harness.errors import InfraError
from vals_harness.launchers import AgentLauncher, DockerLauncher, HostLauncher
from vals_harness.sandbox import SandboxProvider
from vals_harness.specs import TrialSpec
from vals_harness.store import Claim, LostLease, Store

WORKSPACE = "/workspace"


class TrialRecorder:
    """Hot data to the DB (events), cold data to this try's artifact folder."""

    def __init__(self, store: Store, trial_id: str, try_no: int, folder: Path):
        self.store = store
        self.trial_id = trial_id
        self.try_no = try_no
        self.folder = folder
        (folder / "agent_logs").mkdir(parents=True, exist_ok=True)
        self._transcript = open(folder / "transcript.jsonl", "w")

    def event(self, type: str, payload: dict[str, Any]) -> None:
        self.store.event(self.trial_id, self.try_no, type, payload)

    def transcript(self, entry: dict[str, Any]) -> None:
        self._transcript.write(json.dumps({"ts": time.time(), **entry}) + "\n")
        self._transcript.flush()

    def artifact(self, name: str, data: bytes) -> None:
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:100].lstrip(".") or "artifact"
        (self.folder / "agent_logs" / safe).write_bytes(data)

    def close(self) -> None:
        self._transcript.close()


class TrialRunner:
    def __init__(self, provider: SandboxProvider, store: Store, artifacts_root: Path,
                 launchers: dict[str, AgentLauncher] | None = None):
        self.provider = provider
        self.store = store
        self.artifacts_root = artifacts_root
        self.launchers = launchers or {"host": HostLauncher(), "sidecar": DockerLauncher()}

    def run(self, claim: Claim) -> dict[str, Any]:
        """Run one try. Raises LostLease if another worker took the trial over."""
        spec = claim.spec
        folder = self.artifacts_root / spec.run_id / spec.trial_id / f"try{claim.try_no}"
        folder.mkdir(parents=True, exist_ok=True)
        rec = TrialRecorder(self.store, spec.trial_id, claim.try_no, folder)
        labels = {"vals.trial_id": spec.trial_id, "vals.run_id": spec.run_id, "vals.claim": claim.token}
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        outcome: AgentOutcome | None = None
        result: dict[str, Any]

        try:
            # 1 PREPARE
            self._phase(claim, rec, "preparing")
            bench = BenchmarkAdapter(spec.benchmark, self.provider)
            launcher = self.launchers.get(spec.agent.placement)
            if launcher is None:
                raise InfraError("adapter", f"placement {spec.agent.placement!r} is not available yet")
            # Images (task, tooling, agent) are resolved before any container starts,
            # so a slow build never holds a chamber open, and all three get pinned.
            pins = {"task_image": self.provider.ensure_image(spec.task.image),
                    "tooling_image": bench.tooling_image()}
            if agent_image := launcher.prepare(spec.agent):
                pins["agent_image"] = agent_image
            self._write_manifest(spec, pins, folder)
            prepared = bench.prepare(spec.task, folder / "work", labels)

            # 2 CHAMBER + 3 AGENT + 4 COLLECT
            self._phase(claim, rec, "running_agent")
            t_agent = time.monotonic()
            # Created from the pinned image id, so the tag can't move under a running trial.
            with self.provider.create(pins["task_image"], labels={**labels, "vals.role": "chamber"},
                                      network=spec.policy.network, cpus=spec.limits.cpus,
                                      memory=spec.limits.memory) as chamber:
                chamber.put_dir(prepared.visible_dir, WORKSPACE)
                if spec.policy.expose_eval_files and any(prepared.eval_dir.iterdir()):
                    chamber.put_dir(prepared.eval_dir, WORKSPACE)
                session = launcher.launch(spec.agent, folder / "agent_stderr.log", labels)
                api = ChamberApi(chamber, rec, trial_id=spec.trial_id,
                                 command_timeout_s=spec.limits.command_timeout_s)
                outcome = api.serve(session, self._start_payload(spec),
                                    deadline=time.monotonic() + spec.limits.timeout_s)
                self._collect(chamber, folder / "workspace.tar", rec)
            timings["agent_s"] = time.monotonic() - t_agent

            # 5 VERIFY
            self._phase(claim, rec, "verifying")
            t_verify = time.monotonic()
            grade, vr = bench.verify(spec.task, folder / "workspace.tar", prepared, folder / "work",
                                     labels, timeout=spec.limits.verify_timeout_s)
            timings["verify_s"] = time.monotonic() - t_verify
            (folder / "verifier.log").write_text(vr.output if vr else "")
            result = {"status": "graded", "grade": {"passed": grade.passed, "score": grade.score,
                                                    "details": grade.details}}
        except LostLease:
            raise
        except InfraError as e:
            result = {"status": "infra_error", "error": e.to_dict()}
        except Exception as e:  # a harness bug is still a harness failure, never the agent's
            result = {"status": "infra_error", "error": {"kind": "adapter", "message": f"{type(e).__name__}: {e}"}}
        finally:
            self.provider.reap({"vals.claim": claim.token})
            rec.close()

        # 6 RECORD
        timings["duration_s"] = time.monotonic() - t0
        result.update({
            "trial_id": spec.trial_id, "run_id": spec.run_id, "task_id": spec.task.id,
            "attempt": spec.attempt, "try": claim.try_no, "artifacts": str(folder),
            "agent": outcome.to_dict() if outcome else None,
            "metrics": {**{k: round(v, 2) for k, v in timings.items()},
                        **({"input_tokens": outcome.input_tokens, "output_tokens": outcome.output_tokens,
                            "cost_usd": outcome.cost_usd, "n_commands": outcome.n_commands,
                            "steps": outcome.steps} if outcome else {})},
            "manifest_hash": spec.manifest_hash(),
        })
        (folder / "result.json").write_text(json.dumps(result, indent=2))
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _collect(chamber: Any, tar_path: Path, rec: TrialRecorder) -> None:
        """Snapshot /workspace. If that fails while the chamber is still running, the agent
        broke its own workspace (e.g. `rm -rf /workspace`): an empty submission, graded."""
        try:
            chamber.snapshot(WORKSPACE, tar_path)
        except InfraError as e:
            if not chamber.alive():
                raise
            tar_path.unlink(missing_ok=True)
            rec.event("snapshot_missing", {"reason": e.message})

    def _phase(self, claim: Claim, rec: TrialRecorder, status: str) -> None:
        self.store.set_status(claim.spec.trial_id, claim.token, status)  # raises LostLease if fenced out
        rec.event("phase", {"status": status})

    @staticmethod
    def _start_payload(spec: TrialSpec) -> dict[str, Any]:
        return {"instruction": spec.task.instruction, "config": spec.agent_config(),
                "task": {"id": spec.task.id}, "workspace": WORKSPACE,
                "deadline_unix": time.time() + spec.limits.timeout_s}

    @staticmethod
    def _write_manifest(spec: TrialSpec, pins: dict[str, str], folder: Path) -> None:
        manifest = {"trial": spec.model_dump(mode="json"), "pins": pins,
                    "manifest_hash": spec.manifest_hash()}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
