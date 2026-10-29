"""Benchmarks: every format implements one protocol, so the trial runner never knows which.

    tasks()  ->  images(task)  ->  prepare(task)  ->  [agent]  ->  verify / verify_in_chamber

Formats (BenchmarkSpec.format):
  scripts    ScriptBenchmark: a dataset file + the benchmark's own setup/grading scripts,
             run in its tooling image (never on the host, never in the chamber).
  task_dirs  TaskDirBenchmark: Harbor / Terminal-Bench style task folders (instruction.md,
             task.toml, environment/Dockerfile + assets, tests/test.sh -> reward file).
  python     any class in the benchmark folder implementing Benchmark (`adapter: f.py:Cls`).

Where grading runs (VerifyMode):
  tooling     fresh tooling-image container + the agent's submission (anti-tamper default)
  task_image  fresh container from the task's own image + submission (SWE-bench style:
              the grader needs the task's dependencies)
  chamber     in the agent's chamber after it stops (Terminal-Bench style: for outcomes
              that are system state, e.g. a running service or files outside the workspace).
              Weaker integrity: the agent shaped the environment the tests run in.
"""

from __future__ import annotations

import importlib.util
import json
import math
import os
import re
import shlex
import shutil
import tarfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from vals_harness.errors import ChamberStopped, InfraError
from vals_harness.sandbox import ExecResult, Sandbox, SandboxProvider
from vals_harness.specs import BenchmarkSpec, Task, VerifyMode

BENCH_DIR = "/bench"        # benchmark folder inside tooling/verifier sandboxes
OUT_DIR = "/out"            # where prepare scripts write the initial workspace
TESTS_DIR = "/tests"        # task_dirs: tests copied in only at verify time
REWARD_DIR = "/logs/verifier"
ANSWER_FILE = "/logs/agent/answer.txt"   # the agent's final message, for graders that need it
PREPARE_TIMEOUT_S = 300


@dataclass(frozen=True)
class Grade:
    passed: bool
    score: float
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PreparedTask:
    visible_dir: Path   # copied into the chamber
    eval_dir: Path      # never enters the chamber; overlaid at verify time


# ------------------------------------------------------------------ result parsers (strategy)


def parse_json_stdout(r: ExecResult) -> Grade:
    """Grader printed a JSON object with at least `passed`."""
    text = r.output
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise InfraError("verifier", f"no JSON in grader output: {text[-500:]!r}")
    try:
        data = json.loads(text[start:end + 1])
        passed = bool(data["passed"])
    except (json.JSONDecodeError, KeyError) as e:
        raise InfraError("verifier", f"bad grader JSON: {e}") from e
    return Grade(passed=passed, score=float(data.get("score", 1.0 if passed else 0.0)),
                 details=data.get("details", {}))


def parse_exit_code(r: ExecResult) -> Grade:
    """Grader signals pass/fail only through its exit code."""
    passed = r.returncode == 0
    return Grade(passed=passed, score=1.0 if passed else 0.0, details={"output_tail": r.output[-2000:]})


PARSERS: dict[str, Callable[[ExecResult], Grade]] = {
    "json_stdout": parse_json_stdout,
    "exit_code": parse_exit_code,
}


def parse_reward(raw: str, kind: str) -> tuple[float, dict[str, Any]]:
    """reward.txt: one number. reward.json: {"reward": x, ...} or a dict of named scores
    (their mean). Raises ValueError on anything else."""
    if kind == "txt":
        v = float(raw.strip())
        details: dict[str, Any] = {}
    else:
        data = json.loads(raw)
        if isinstance(data, (int, float)):
            v, details = float(data), {}
        elif isinstance(data, dict):
            nums = {k: float(x) for k, x in data.items() if isinstance(x, (int, float)) and not isinstance(x, bool)}
            if "reward" in nums:
                v = nums["reward"]
            elif nums:
                v = sum(nums.values()) / len(nums)
            else:
                raise ValueError("reward.json has no numbers")
            details = {"reward_json": data}
        else:
            raise ValueError("reward.json must be a number or an object")
    if not math.isfinite(v):
        raise ValueError(f"reward is not finite: {v}")
    return v, details


# ------------------------------------------------------------------ protocol


class Benchmark(Protocol):
    """What the trial runner needs from any benchmark format."""

    spec: BenchmarkSpec

    def tasks(self) -> list[Task]: ...
    def images(self, task: Task) -> dict[str, str]:
        """Pull/build what the task needs; return pinned ids ({"task_image": ..., ...})."""
        ...
    def prepare(self, task: Task, workdir: Path, labels: dict[str, str]) -> PreparedTask: ...
    def verify_mode(self, task: Task) -> VerifyMode: ...
    def verify(self, task: Task, snapshot_tar: Path, prepared: PreparedTask, workdir: Path,
               labels: dict[str, str], timeout: int, *, images: dict[str, str],
               answer: str = "") -> tuple[Grade, ExecResult | None]:
        """Grade in a fresh sandbox (tooling / task_image modes)."""
        ...
    def verify_in_chamber(self, task: Task, chamber: Sandbox, prepared: PreparedTask, timeout: int, *,
                          answer: str = "") -> tuple[Grade, ExecResult | None]:
        """Grade in the agent's chamber after it stopped (chamber mode)."""
        ...


def make_benchmark(spec: BenchmarkSpec, provider: SandboxProvider | None) -> Benchmark:
    """`provider` may be None when only tasks() is needed (submit time)."""
    if spec.format == "scripts":
        return ScriptBenchmark(spec, provider)   # type: ignore[arg-type]
    if spec.format == "task_dirs":
        return TaskDirBenchmark(spec, provider)  # type: ignore[arg-type]
    file, _, cls_name = (spec.adapter or "").partition(":")
    path = spec.root / file
    if not path.is_file() or not cls_name:
        raise InfraError("adapter", f"benchmark {spec.name}: adapter must be 'file.py:Class' in its folder")
    mod_spec = importlib.util.spec_from_file_location(f"vals_bench_{spec.name.replace('-', '_')}", path)
    assert mod_spec and mod_spec.loader
    module = importlib.util.module_from_spec(mod_spec)
    mod_spec.loader.exec_module(module)
    return getattr(module, cls_name)(spec, provider)


# ------------------------------------------------------------------ scripts format


class ScriptBenchmark:
    def __init__(self, spec: BenchmarkSpec, provider: SandboxProvider):
        self.spec = spec
        self.provider = provider
        self._tooling_image: str | None = None

    def tooling_image(self) -> str:
        if self._tooling_image is None:
            s = self.spec
            if s.tooling_dockerfile:
                self._tooling_image = self.provider.ensure_image(dockerfile=s.root / s.tooling_dockerfile)
            else:
                self._tooling_image = self.provider.ensure_image(s.tooling_image)
        return self._tooling_image

    def tasks(self) -> list[Task]:
        from vals_harness.specs import load_tasks
        return load_tasks(self.spec)

    def images(self, task: Task) -> dict[str, str]:
        image = (self.provider.ensure_image(task.image) if task.image else
                 self.provider.ensure_image(dockerfile=self.spec.root / str(task.build), name=f"task-{self.spec.name}"))
        return {"task_image": image, "tooling_image": self.tooling_image()}

    def verify_mode(self, task: Task) -> VerifyMode:
        return "tooling"   # its graders are scripts in the benchmark folder

    def verify_in_chamber(self, task: Task, chamber: Sandbox, prepared: PreparedTask, timeout: int, *,
                          answer: str = "") -> tuple[Grade, ExecResult | None]:
        raise InfraError("adapter", "scripts benchmarks grade in their tooling image")

    def _argv(self, template: list[str], task: Task, **paths: str) -> str:
        return shlex.join(a.format(task_id=task.id, **paths) for a in template)

    def prepare(self, task: Task, workdir: Path, labels: dict[str, str]) -> PreparedTask:
        visible, evaldir = workdir / "visible", workdir / "eval"
        for d in (visible, evaldir):
            if d.exists():
                shutil.rmtree(d)
        evaldir.mkdir(parents=True)

        with self.provider.create(self.tooling_image(), labels={**labels, "vals.role": "tooling"}) as sb:
            sb.put_dir(self.spec.root, BENCH_DIR)
            try:
                r = sb.exec(self._argv(self.spec.prepare, task, out=OUT_DIR),
                            timeout=PREPARE_TIMEOUT_S, cwd=BENCH_DIR)
            except ChamberStopped as e:   # the benchmark's own script, not the agent
                raise InfraError("prepare", str(e)) from e
            if r.returncode != 0:
                raise InfraError("prepare", f"exit {r.returncode}: {r.output[-2000:]}")
            sb.get_dir(OUT_DIR, visible)

        # Split eval-only files out of what the agent will see.
        for rel in self.spec.eval_paths:
            src = visible / rel
            if not src.exists():
                raise InfraError("adapter", f"eval path {rel!r} not produced by prepare")
            dst = evaldir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
        return PreparedTask(visible_dir=visible, eval_dir=evaldir)

    def verify(self, task: Task, snapshot_tar: Path, prepared: PreparedTask, workdir: Path,
               labels: dict[str, str], timeout: int, *, images: dict[str, str] | None = None,
               answer: str = "") -> tuple[Grade, ExecResult | None]:
        """Grade the agent's work in a fresh sandbox the agent never touched.

        Everything that goes wrong because of what the agent left behind (no snapshot,
        bad links, a submission that hangs the grader) is graded, never an infra error:
        otherwise an agent could drop its own failures out of the accuracy number.
        """
        graded_ws, skipped = self.build_graded_workspace(snapshot_tar, prepared, workdir, task.workspace)
        with self.provider.create(self.tooling_image(), labels={**labels, "vals.role": "verifier"},
                                  network=self.spec.verify_network) as sb:
            sb.put_dir(self.spec.root, BENCH_DIR)
            sb.put_dir(graded_ws, task.workspace)
            try:
                r = sb.exec(self._argv(self.spec.verify, task, workspace=task.workspace),
                            timeout=timeout, cwd=BENCH_DIR, secret_env=_verify_secrets(self.spec))
            except ChamberStopped as e:
                # The submission runs inside the grader (pytest imports it) and stopped the
                # verifier (e.g. os.kill(1, SIGTERM) at import): the agent's failure.
                harness = {"skipped_files": skipped} if skipped else {}
                return Grade(False, 0.0, {"harness": {**harness, "error": f"submission stopped the grader: {e}"}}), None
        harness = {"skipped_files": skipped} if skipped else {}
        if r.timed_out:
            # The submission runs inside the grader (e.g. pytest imports it), so a hang
            # is the agent's failure. Graders with their own timeouts never get here.
            return Grade(False, 0.0, {"harness": {**harness, "error": f"grader timed out after {timeout}s"}}), r
        assert self.spec.result_parser is not None
        grade = PARSERS[self.spec.result_parser](r)
        if grade.passed:
            missing = [p for p in self.spec.pass_requires if not re.search(p, r.output)]
            if missing:
                # The grader said "pass" but its own evidence isn't there, e.g. the
                # submission called os._exit(0) before pytest ran a single test.
                harness["pass_rejected"] = f"grader output lacks {missing}"
                grade = Grade(False, 0.0, grade.details)
        if harness:
            grade = Grade(grade.passed, grade.score, {**grade.details, "harness": harness})
        return grade, r

    def build_graded_workspace(self, snapshot_tar: Path, prepared: PreparedTask, workdir: Path,
                               workspace: str = "/workspace") -> tuple[Path, list[str]]:
        return build_graded_workspace(snapshot_tar, prepared, workdir, workspace, self.spec.submission_paths)


def build_graded_workspace(snapshot_tar: Path, prepared: PreparedTask, workdir: Path, workspace: str,
                           submission_paths: list[str]) -> tuple[Path, list[str]]:
    """What the grader sees: the agent's submission + the original eval files on top.

    The snapshot is agent-controlled. Members the "data" filter rejects (absolute
    or escaping links, device files) and links that point outside the workspace
    are dropped and reported, not fatal: a venv full of absolute symlinks is normal.
    A missing snapshot (the agent deleted /workspace) is an empty submission.
    Returns (graded workspace, skipped entries).
    """
    unpacked, graded = workdir / "snapshot", workdir / "graded"
    for d in (unpacked, graded):
        if d.exists():
            shutil.rmtree(d)
    unpacked.mkdir(parents=True)
    skipped: list[str] = []

    def keep(member: tarfile.TarInfo, dest: str) -> tarfile.TarInfo | None:
        try:
            return tarfile.data_filter(member, dest)
        except tarfile.FilterError as e:
            skipped.append(f"{member.name} ({type(e).__name__})")
            return None

    if snapshot_tar.exists():
        try:
            with tarfile.open(snapshot_tar) as tar:
                tar.extractall(unpacked, filter=keep)
        except tarfile.TarError as e:
            skipped.append(f"<snapshot unreadable: {e}>")
    else:
        skipped.append("<no snapshot: workspace missing>")
    agent_ws = unpacked / Path(workspace).name   # tar top entry = the path's basename
    agent_ws.mkdir(exist_ok=True)
    skipped += _drop_escaping_links(agent_ws)

    if "**" in submission_paths:
        shutil.copytree(agent_ws, graded, symlinks=True)
    else:
        shutil.copytree(prepared.visible_dir, graded)       # pristine starting state
        for pattern in submission_paths:
            for src in agent_ws.glob(pattern):
                if src.is_file() and not src.is_symlink():
                    _copy_over(src, graded / src.relative_to(agent_ws), graded)
    for src in prepared.eval_dir.rglob("*"):                # eval files always win
        if src.is_file():
            _copy_over(src, graded / src.relative_to(prepared.eval_dir), graded)
    return graded, skipped


def _drop_escaping_links(root: Path) -> list[str]:
    """Delete symlinks whose target resolves outside `root` (they'd be dangling or worse
    in the verifier, and docker cp refuses some of them)."""
    dropped = []
    base = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                link = os.readlink(p)
                target = Path(os.path.normpath(Path(dirpath) / link))
                if not target.is_relative_to(base) and not target.is_relative_to(root):
                    p.unlink()
                    dropped.append(f"{p.relative_to(root)} -> {link} (link escapes workspace)")
    return dropped


def _copy_over(src: Path, dst: Path, root: Path) -> None:
    """Copy a file to dst, replacing whatever is there and never writing through a
    symlink the agent planted (at dst or at any parent directory under root)."""
    rel = dst.relative_to(root)
    cur = root
    for part in rel.parts[:-1]:
        cur = cur / part
        if cur.is_symlink() or (cur.exists() and not cur.is_dir()):
            cur.unlink()
        cur.mkdir(exist_ok=True)
    if dst.is_symlink() or dst.is_file():
        dst.unlink()
    elif dst.is_dir():
        shutil.rmtree(dst)
    shutil.copyfile(src, dst)


def _verify_secrets(spec: BenchmarkSpec) -> dict[str, str] | None:
    """Secrets a grader declared (e.g. an LLM judge's key), via `docker exec -e NAME`."""
    return {k: os.environ[k] for k in spec.verify_secrets if k in os.environ} or None


# ------------------------------------------------------------------ task_dirs format


class TaskDirBenchmark:
    """Harbor / Terminal-Bench style: one folder per task.

        tasks/<id>/instruction.md        the instruction
        tasks/<id>/task.toml             [agent] timeout_sec · [verifier] timeout_sec, mode
                                         [environment] workdir, cpus, memory, image
        tasks/<id>/environment/          Dockerfile + any assets (build context)
        tasks/<id>/tests/test.sh         writes /logs/verifier/reward.txt (number) or reward.json
        tasks/<id>/solution/solve.sh     reference solution (not used by the harness)

    The image *is* the starting state (assets baked in), so prepare() copies nothing into
    the chamber. Tests are copied in only at verify time and the reward dir is wiped
    first, so nothing the agent planted there counts.
    """

    def __init__(self, spec: BenchmarkSpec, provider: SandboxProvider):
        self.spec = spec
        self.provider = provider

    def tasks(self) -> list[Task]:
        base = self.spec.root / self.spec.tasks_dir
        out = []
        for d in sorted(p for p in base.iterdir() if (p / "instruction.md").is_file()):
            cfg = tomllib.loads((d / "task.toml").read_text()) if (d / "task.toml").exists() else {}
            env, agent, verifier = cfg.get("environment", {}), cfg.get("agent", {}), cfg.get("verifier", {})
            limits = {k: v for k, v in {
                "timeout_s": agent.get("timeout_sec"), "verify_timeout_s": verifier.get("timeout_sec"),
                "cpus": env.get("cpus"), "memory": env.get("memory"),
            }.items() if v is not None}
            dockerfile = d / "environment" / "Dockerfile"
            out.append(Task(
                id=d.name,
                instruction=(d / "instruction.md").read_text(),
                build=str(dockerfile.relative_to(self.spec.root)) if dockerfile.exists() else None,
                image=None if dockerfile.exists() else env.get("image"),
                workspace=env.get("workdir", "/app"),
                verify_mode=verifier.get("mode", "chamber"),
                limits={k: (int(v) if k.endswith("_s") else v) for k, v in limits.items()},
                meta={"dir": str(d.relative_to(self.spec.root))},
            ))
        return out

    def images(self, task: Task) -> dict[str, str]:
        if task.build:
            image = self.provider.ensure_image(dockerfile=self.spec.root / task.build,
                                               name=f"task-{self.spec.name}-{task.id}")
        else:
            image = self.provider.ensure_image(task.image)
        return {"task_image": image}

    def verify_mode(self, task: Task) -> VerifyMode:
        mode = task.verify_mode or "chamber"
        if mode == "tooling":
            raise InfraError("adapter", f"{task.id}: task_dirs grade in 'chamber' or 'task_image' mode")
        return mode

    def prepare(self, task: Task, workdir: Path, labels: dict[str, str]) -> PreparedTask:
        visible, evaldir = workdir / "visible", workdir / "eval"
        for d in (visible, evaldir):
            if d.exists():
                shutil.rmtree(d)
            d.mkdir(parents=True)
        if not (self.spec.root / task.meta["dir"] / "tests" / "test.sh").is_file():
            raise InfraError("adapter", f"{task.id}: tests/test.sh missing")
        return PreparedTask(visible_dir=visible, eval_dir=evaldir)

    def verify(self, task: Task, snapshot_tar: Path, prepared: PreparedTask, workdir: Path,
               labels: dict[str, str], timeout: int, *, images: dict[str, str],
               answer: str = "") -> tuple[Grade, ExecResult | None]:
        """task_image mode: a fresh container from the task's own image, its workspace
        replaced by the agent's submission, then the tests."""
        graded_ws, skipped = build_graded_workspace(snapshot_tar, prepared, workdir, task.workspace,
                                                    self.spec.submission_paths)
        with self.provider.create(images["task_image"], labels={**labels, "vals.role": "verifier"},
                                  network=self.spec.verify_network, workdir=task.workspace) as sb:
            if "**" in self.spec.submission_paths:   # mirror deletions too, not just new files
                sb.exec(f"find {shlex.quote(task.workspace)} -mindepth 1 -delete", timeout=60, cwd="/")
            sb.put_dir(graded_ws, task.workspace)
            return self._run_tests(sb, task, timeout, answer, skipped, _verify_secrets(self.spec))

    def verify_in_chamber(self, task: Task, chamber: Sandbox, prepared: PreparedTask, timeout: int, *,
                          answer: str = "") -> tuple[Grade, ExecResult | None]:
        """Chamber mode's residual risk: anything the agent left running (needed for e.g. a
        service task) could also interfere with the tests, e.g. keep rewriting the reward
        file. That can't be prevented without breaking such tasks, so it's made visible:
        the processes alive when the tests start are recorded with the grade."""
        survivors = _agent_processes(chamber)
        grade, r = self._run_tests(chamber, task, timeout, answer, [], None)
        if survivors:
            grade = Grade(grade.passed, grade.score,
                          {**grade.details, "harness": {**grade.details.get("harness", {}),
                                                        "processes_during_tests": survivors}})
        return grade, r

    def _run_tests(self, sb: Sandbox, task: Task, timeout: int, answer: str, skipped: list[str],
                   secrets: dict[str, str] | None) -> tuple[Grade, ExecResult | None]:
        harness: dict[str, Any] = {"skipped_files": skipped} if skipped else {}
        tests = self.spec.root / task.meta["dir"] / "tests"
        try:
            # Wipe anything the agent left where the tests and reward go, then copy tests in.
            sb.exec(f"rm -rf {TESTS_DIR} {REWARD_DIR} && mkdir -p {TESTS_DIR} {REWARD_DIR} $(dirname {ANSWER_FILE})",
                    timeout=60, cwd="/")
            sb.put_dir(tests, TESTS_DIR)
            sb.put(ANSWER_FILE, answer.encode())
            r = sb.exec(f"chmod +x {TESTS_DIR}/test.sh && {TESTS_DIR}/test.sh", timeout=timeout,
                        cwd=task.workspace, secret_env=secrets)
        except ChamberStopped as e:
            return Grade(False, 0.0, {"harness": {**harness, "error": f"environment stopped during tests: {e}"}}), None
        if r.timed_out:
            return Grade(False, 0.0, {"harness": {**harness, "error": f"tests timed out after {timeout}s"}}), r
        score, details = None, {}
        for kind in ("json", "txt"):
            try:
                raw = sb.get(f"{REWARD_DIR}/reward.{kind}").decode("utf-8", errors="replace")
            except (FileNotFoundError, ValueError):
                continue
            try:
                score, details = parse_reward(raw, kind)
            except ValueError as e:
                harness["error"] = f"bad reward.{kind}: {e}"
            break
        details["output_tail"] = r.output[-2000:]
        if score is None:
            # No reward: the tests couldn't run to the end in the environment the agent left
            # behind. Graded 0 (in chamber mode the agent shaped that environment).
            harness.setdefault("error", f"tests wrote no reward (exit {r.returncode})")
            return Grade(False, 0.0, {**details, "harness": harness}), r
        passed = score >= self.spec.pass_threshold
        if passed and (missing := [p for p in self.spec.pass_requires if not re.search(p, r.output)]):
            harness["pass_rejected"] = f"test output lacks {missing}"
            passed = False
        if harness:
            details["harness"] = harness
        return Grade(passed, score, details), r


def _agent_processes(sb: Sandbox) -> list[str]:
    """Command lines of processes in the sandbox other than the keep-alive (via /proc,
    so it works without ps). Best effort."""
    script = ('for d in /proc/[0-9]*; do c=$(tr "\\0" " " < $d/cmdline 2>/dev/null); '
              '[ -n "$c" ] && echo "$c"; done')
    try:
        r = sb.exec(script, timeout=20, cwd="/")
    except Exception:
        return []
    noise = ("tail -f /dev/null", "/sbin/docker-init", "docker-init", "for d in /proc", "tr \\0")
    return sorted({ln.strip()[:200] for ln in r.output.splitlines()
                   if ln.strip() and not any(n in ln for n in noise)})[:20]


# Back-compat name used by older tests/callers.
BenchmarkAdapter = ScriptBenchmark
