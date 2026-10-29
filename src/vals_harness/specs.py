"""Specs: what a benchmark is, what an agent needs, and what experiment to run.

Three parties each own one spec (DESIGN.md §6):
  BenchmarkSpec  — benchmark adapter author  (benchmarks/<name>/harness.yaml)
  AgentSpec      — agent adapter author      (agents/<name>/agent.yaml)
  RunSpec        — the experimenter          (CLI flags)
They are resolved into one frozen TrialSpec per (task, attempt), which is also
the trial's manifest.

Raw YAML is parsed into these models at the edge ("parse, don't validate"), so
the rest of the harness only ever sees typed, immutable objects.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, model_validator


class Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ------------------------------------------------------------------ limits & policy


class Limits(Frozen):
    timeout_s: int = 600            # agent wall clock
    command_timeout_s: int = 30     # one Chamber API exec
    verify_timeout_s: int = 120     # grading
    step_limit: int = 50
    cost_limit_usd: float = 1.0
    cpus: float = 1.0
    memory: str = "1g"


class Policy(Frozen):
    expose_eval_files: bool = False
    network: Literal["none", "egress"] = "none"
    infra_retries: int = 2


LIMIT_KEYS = set(Limits.model_fields)


# ------------------------------------------------------------------ benchmark


class BenchmarkFields(Frozen):
    """Maps the benchmark's own dataset column names onto ours."""

    id: str
    instruction: str
    image: str


class BenchmarkSpec(Frozen):
    name: str
    version: str = "1"
    dataset: str = "dataset.json"
    fields: BenchmarkFields
    tooling_image: str | None = None        # an existing image, e.g. python:3.11-slim
    tooling_dockerfile: str | None = None   # or a Dockerfile in the benchmark folder
    prepare: list[str]                      # placeholders: {task_id}, {out}
    eval_paths: list[str] = []              # files moved from visible/ to eval/
    # Which agent-written files are carried into the verifier (globs, relative to the
    # workspace). "**" = the whole workspace. Anything narrower starts the verifier from
    # the pristine prepared workspace and copies in only these files, so an agent can't
    # plant files (pytest.py, conftest.py, ...) that hijack a grader which runs code.
    submission_paths: list[str] = ["**"]
    verify: list[str]                       # placeholders: {task_id}, {workspace}
    result_parser: Literal["json_stdout", "exit_code"]
    # Regexes that must all match the grader's raw output for a "pass" to count.
    # Guards graders that trust an exit code the submission can fake.
    pass_requires: list[str] = []
    defaults: dict[str, Any] = {}
    root: Path                              # set by the loader: the benchmark folder

    @model_validator(mode="after")
    def _one_tooling_source(self) -> BenchmarkSpec:
        if (self.tooling_image is None) == (self.tooling_dockerfile is None):
            raise ValueError("set exactly one of tooling_image / tooling_dockerfile")
        return self


class Task(Frozen):
    id: str
    instruction: str
    image: str


def load_benchmark(ref: str, root: Path) -> BenchmarkSpec:
    """`ref` is a folder path, or a name under <root>/benchmarks/."""
    folder = _resolve_folder(ref, root / "benchmarks")
    raw = yaml.safe_load((folder / "harness.yaml").read_text())
    return BenchmarkSpec(**raw, root=folder)


def load_tasks(bench: BenchmarkSpec) -> list[Task]:
    rows = json.loads((bench.root / bench.dataset).read_text())
    f = bench.fields
    return [Task(id=r[f.id], instruction=r[f.instruction], image=r[f.image]) for r in rows]


# ------------------------------------------------------------------ agent


class AgentSpec(Frozen):
    name: str
    version: str = "0"
    placement: Literal["host", "sidecar", "installed"]
    command: list[str]                      # how to start the agent runner (host/sidecar)
    secrets: list[str] = []                 # env var names forwarded to the agent only
    defaults: dict[str, Any] = {}           # model + limit defaults
    root: Path                              # set by the loader: the agent folder


def load_agent(ref: str, root: Path) -> AgentSpec:
    """`ref` is a folder path (agents/oracle) or a name under <root>/agents/."""
    folder = _resolve_folder(ref, root / "agents")
    raw = yaml.safe_load((folder / "agent.yaml").read_text())
    return AgentSpec(**raw, root=folder)


# ------------------------------------------------------------------ run & trial


class RunSpec(Frozen):
    agent: str
    benchmark: str
    task_ids: list[str] | None = None       # None = every task
    model: str | None = None                # overrides the agent default
    attempts: int = 1
    limits: dict[str, Any] = {}             # partial overrides of Limits
    policy: Policy = Policy()

    @model_validator(mode="after")
    def _check(self) -> RunSpec:
        unknown = set(self.limits) - LIMIT_KEYS
        if unknown:
            raise ValueError(f"unknown limits: {sorted(unknown)}")
        if self.attempts < 1:
            raise ValueError("attempts must be >= 1")
        return self


class TrialSpec(Frozen):
    """Everything needed to run (and re-run) one trial. Written as manifest.json."""

    run_id: str
    trial_id: str
    attempt: int
    task: Task
    benchmark: BenchmarkSpec
    agent: AgentSpec
    model: str | None
    limits: Limits
    policy: Policy
    harness_version: str
    # Content digests of the benchmark and agent folders (dataset, scripts, grader,
    # agent code), so editing grade.py changes the hash while moving the checkout doesn't.
    inputs_digest: dict[str, str]

    def manifest_hash(self) -> str:
        """Identifies the experiment, not the run: same hash = same inputs.

        Excludes ids that differ per run and host paths. Image ids are pinned at run
        time and recorded next to the hash in manifest.json.
        """
        data = self.model_dump(mode="json", exclude={"run_id": True, "trial_id": True, "attempt": True,
                                                     "benchmark": {"root"}, "agent": {"root"}})
        blob = json.dumps(data, sort_keys=True).encode()
        return "sha256:" + hashlib.sha256(blob).hexdigest()

    def agent_config(self) -> dict[str, Any]:
        """The config an agent receives in the protocol's `start` message."""
        extra = {k: v for k, v in self.agent.defaults.items() if k not in LIMIT_KEYS and k != "model"}
        return {
            **extra,
            "model": self.model,
            "step_limit": self.limits.step_limit,
            "cost_limit_usd": self.limits.cost_limit_usd,
            "command_timeout_s": self.limits.command_timeout_s,
        }


def resolve_trials(run: RunSpec, run_id: str, root: Path, harness_version: str) -> list[TrialSpec]:
    """RunSpec + BenchmarkSpec + AgentSpec -> one TrialSpec per (task, attempt).

    Precedence: run overrides > agent defaults > benchmark defaults > harness defaults.
    Pure apart from reading the spec files, so it is easy to test.
    """
    bench = load_benchmark(run.benchmark, root)
    agent = load_agent(run.agent, root)
    tasks = load_tasks(bench)
    if run.task_ids is not None:
        by_id = {t.id: t for t in tasks}
        missing = [t for t in run.task_ids if t not in by_id]
        if missing:
            raise ValueError(f"unknown task ids for {bench.name}: {missing}")
        tasks = [by_id[t] for t in run.task_ids]

    def limit_part(d: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in d.items() if k in LIMIT_KEYS}

    limits = Limits(**{**limit_part(bench.defaults), **limit_part(agent.defaults), **run.limits})
    model = run.model or agent.defaults.get("model")
    digests = {"benchmark": folder_digest(bench.root), "agent": folder_digest(agent.root)}
    return [
        TrialSpec(
            run_id=run_id,
            trial_id=f"{run_id}-{task.id}-a{attempt}",
            attempt=attempt,
            task=task,
            benchmark=bench,
            agent=agent,
            model=model,
            limits=limits,
            policy=run.policy,
            harness_version=harness_version,
            inputs_digest=digests,
        )
        for attempt in range(1, run.attempts + 1)
        for task in tasks
    ]


def folder_digest(folder: Path) -> str:
    """sha256 over every file's relative path and bytes (caches skipped)."""
    h = hashlib.sha256()
    for f in sorted(p for p in folder.rglob("*") if p.is_file()):
        rel = f.relative_to(folder)
        if "__pycache__" in rel.parts or f.suffix == ".pyc":
            continue
        h.update(str(rel).encode() + b"\0")
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def _resolve_folder(ref: str, default_parent: Path) -> Path:
    p = Path(ref)
    for candidate in (p, default_parent / ref):
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError(f"no such folder: {ref!r} (also tried {default_parent / ref})")
