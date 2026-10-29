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

from vals_harness.benchmarks import make_benchmark
from vals_harness.chamber_api import AgentOutcome, ChamberApi
from vals_harness.errors import InfraError
from vals_harness.installed import InstalledRunner
from vals_harness.phantom import PhantomLauncher
from vals_harness.phantom import create_volumes as phantom_volumes
from vals_harness.phantom import remove_volumes as remove_phantom_volumes
from vals_harness.launchers import AgentLauncher, DockerLauncher, HostLauncher
from vals_harness.sandbox import SandboxProvider
from vals_harness.specs import TrialSpec, folder_digest, missing_secrets
from vals_harness.store import Claim, LostLease, Store



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
        self.artifact_path(name).write_bytes(data)

    def artifact_path(self, name: str) -> Path:
        """Where an artifact goes, for writers that stream (big tars, CLI output)."""
        safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:100].lstrip(".") or "artifact"
        return self.folder / "agent_logs" / safe

    def close(self) -> None:
        self._transcript.close()


class TrialRunner:
    def __init__(self, provider: SandboxProvider, store: Store, artifacts_root: Path,
                 launchers: dict[str, AgentLauncher] | None = None):
        self.provider = provider
        self.store = store
        self.artifacts_root = artifacts_root
        self.launchers = launchers or {"host": HostLauncher(), "sidecar": DockerLauncher()}
        self.installed = InstalledRunner()
        self.phantom = PhantomLauncher()

    def run(self, claim: Claim) -> dict[str, Any]:
        """Run one try. Raises LostLease if another worker took the trial over."""
        spec = claim.spec
        folder = self.artifacts_root / spec.run_id / spec.trial_id / f"try{claim.try_no}"
        folder.mkdir(parents=True, exist_ok=True)
        rec = TrialRecorder(self.store, spec.trial_id, claim.try_no, folder)
        labels = {"vals.trial_id": spec.trial_id, "vals.run_id": spec.run_id, "vals.claim": claim.token,
                  "vals.home": self.store.home_id}
        t0 = time.monotonic()
        timings: dict[str, float] = {}
        outcome: AgentOutcome | None = None
        env_check: dict[str, list[str]] | None = None
        result: dict[str, Any]

        try:
            # 1 PREPARE
            self._phase(claim, rec, "preparing")
            self._check_inputs(spec)
            bench = make_benchmark(spec.benchmark, self.provider)
            task, workspace = spec.task, spec.task.workspace
            mode = bench.verify_mode(task)
            placement = spec.agent.placement
            launcher = self.launchers.get(placement) if placement in ("host", "sidecar") else None
            if placement in ("host", "sidecar") and launcher is None:
                raise InfraError("adapter", f"placement {placement!r} is not available")
            # Images (task, tooling, agent) are resolved before any container starts,
            # so a slow build never holds a chamber open, and all of them get pinned.
            pins = bench.images(task)
            chamber_image = pins["task_image"]
            if placement == "installed":
                # The chamber itself is task image + agent runtime (a prebuilt layer, never installed live).
                pins["agent_image"] = chamber_image = self.installed.prepare(spec.agent, pins["task_image"])
            elif placement == "phantom":
                pins["agent_image"] = self.phantom.prepare(spec.agent, pins["task_image"])
            elif agent_image := launcher.prepare(spec.agent):  # type: ignore[union-attr]
                pins["agent_image"] = agent_image
            self._write_manifest(spec, pins, folder)
            prepared = bench.prepare(task, folder / "work", labels)

            # 2 CHAMBER + 3 AGENT (+ VERIFY in chamber mode) + 4 COLLECT
            self._phase(claim, rec, "running_agent")
            t_agent = time.monotonic()
            deadline = time.monotonic() + spec.limits.timeout_s
            # An installed agent calls its model from inside the chamber, so that chamber
            # needs egress (v2: model proxy only). Everything else stays offline.
            network = "egress" if placement == "installed" else spec.policy.network
            # Phantom box and chamber share the workspace and /tmp (per-trial volumes).
            volumes = phantom_volumes(claim.token, labels, workspace) if placement == "phantom" else None
            grade = vr = None
            # Created from a pinned image id, so a tag can't move under a running trial.
            with self.provider.create(chamber_image, labels={**labels, "vals.role": "chamber"},
                                      network=network, cpus=spec.limits.cpus, memory=spec.limits.memory,
                                      volumes=volumes, workdir=workspace) as chamber:
                if any(prepared.visible_dir.iterdir()):
                    chamber.put_dir(prepared.visible_dir, workspace)
                env_check = self._check_env(chamber, spec.agent.expects, rec)
                if spec.policy.expose_eval_files and any(prepared.eval_dir.iterdir()):
                    chamber.put_dir(prepared.eval_dir, workspace)
                if placement == "installed":
                    outcome = self.installed.run(spec, chamber, rec, deadline)
                else:
                    if placement == "phantom":
                        session = self.phantom.launch(spec.agent, pins["agent_image"], folder / "agent_stderr.log",
                                                      labels, volumes or {})
                    else:
                        session = launcher.launch(spec.agent, folder / "agent_stderr.log", labels)  # type: ignore[union-attr]
                    api = ChamberApi(chamber, rec, trial_id=spec.trial_id,
                                     command_timeout_s=spec.limits.command_timeout_s, workspace=workspace)
                    outcome = api.serve(session, self._start_payload(spec, chamber), deadline=deadline)
                timings["agent_s"] = time.monotonic() - t_agent
                if outcome.exit_status == "model_error":
                    # The model provider failed before the agent did anything (ChamberApi and the
                    # installed parser only allow this status then): not the agent's failure.
                    raise InfraError("model_api", outcome.message or "model provider error")
                if mode == "chamber":
                    # The outcome is the environment's state (a running service, files outside
                    # the workspace), so the tests run here, before anything is stopped.
                    self._phase(claim, rec, "verifying")
                    t_verify = time.monotonic()
                    grade, vr = bench.verify_in_chamber(task, chamber, prepared, spec.limits.verify_timeout_s,
                                                        answer=outcome.answer)
                self._collect(chamber, folder / "workspace.tar", rec, workspace)

            # 5 VERIFY (fresh sandbox: tooling / task_image modes)
            if grade is None:
                self._phase(claim, rec, "verifying")
                t_verify = time.monotonic()
                grade, vr = bench.verify(task, folder / "workspace.tar", prepared, folder / "work", labels,
                                         timeout=spec.limits.verify_timeout_s, images=pins, answer=outcome.answer)
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
            if spec.agent.placement == "phantom":
                remove_phantom_volumes(claim.token)
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
            "env": env_check,
        })
        if outcome:
            result["metrics"]["n_not_found"] = outcome.n_not_found
        if env_check:
            result["metrics"]["env"] = env_check   # stored with the metrics (no schema change)
        (folder / "result.json").write_text(json.dumps(result, indent=2))
        return result

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _check_env(chamber: Any, expects: list[str], rec: TrialRecorder) -> dict[str, list[str]] | None:
        """Check the agent's declared assumptions against the chamber before the agent
        starts. Nothing is changed or blocked: a mismatch is a recorded fact (the task
        image is the experiment), surfaced in results so it can't hide in transcripts."""
        if not expects:
            return None
        script = 'for p in "$@"; do command -v "$p" >/dev/null 2>&1 || echo "$p"; done'
        r = chamber.exec(f"sh -c '{script}' sh " + " ".join(expects), timeout=30)
        missing = sorted(set(r.output.split()) & set(expects))
        check = {"expects": list(expects), "missing": missing}
        rec.event("env_check", check)
        return check

    @staticmethod
    def _check_inputs(spec: TrialSpec) -> None:
        """The trial runs whatever is in the folders now; make sure that's what was submitted,
        and that the agent's required secrets are here (this process may not be the submitter)."""
        now = {"benchmark": folder_digest(spec.benchmark.root), "agent": folder_digest(spec.agent.root)}
        changed = sorted(k for k, v in now.items() if spec.inputs_digest.get(k) != v)
        if changed:
            raise InfraError("adapter", f"inputs changed since submit: {', '.join(changed)} folder "
                                        "(resubmit the run to use the new version)")
        if missing := missing_secrets(spec.agent):
            raise InfraError("config", f"agent {spec.agent.name} needs {', '.join(missing)}, not set for the worker")

    @staticmethod
    def _collect(chamber: Any, tar_path: Path, rec: TrialRecorder, workspace: str) -> None:
        """Stop everything in the chamber, then snapshot /workspace from the stopped container.

        Halting first means nothing the agent left running keeps writing during the copy.
        If the snapshot fails while the container still exists, the agent broke its own
        workspace (e.g. `rm -rf /workspace`): an empty submission, graded. If the container
        is gone, Docker failed: infra_error.
        """
        chamber.halt()
        try:
            chamber.snapshot(workspace, tar_path)
        except InfraError as e:
            if not chamber.exists():
                raise
            tar_path.unlink(missing_ok=True)
            rec.event("snapshot_missing", {"reason": e.message})

    def _phase(self, claim: Claim, rec: TrialRecorder, status: str) -> None:
        self.store.set_status(claim.spec.trial_id, claim.token, status)  # raises LostLease if fenced out
        rec.event("phase", {"status": status})

    @staticmethod
    def _start_payload(spec: TrialSpec, chamber: Any) -> dict[str, Any]:
        # The chamber's uname, asked by the harness so an agent (mini puts it in its prompt)
        # needn't run a command before its first model call (see `model_error`).
        try:
            uname = chamber.exec("uname -s; uname -r; uname -v; uname -m", timeout=30).output
        except Exception:
            uname = ""
        return {"instruction": spec.task.instruction, "config": spec.agent_config(),
                "task": {"id": spec.task.id}, "workspace": spec.task.workspace,
                "deadline_unix": time.time() + spec.limits.timeout_s, "chamber": {"uname": uname}}

    @staticmethod
    def _write_manifest(spec: TrialSpec, pins: dict[str, str], folder: Path) -> None:
        manifest = {"trial": spec.model_dump(mode="json"), "pins": pins,
                    "manifest_hash": spec.manifest_hash()}
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2))
