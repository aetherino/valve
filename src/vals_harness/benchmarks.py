"""Benchmark adapter: one YAML-driven class covers any benchmark with setup + grading scripts.

Lifecycle (template method): prepare -> [agent runs elsewhere] -> verify -> parse.
The benchmark's own scripts only ever run in its tooling image, never on the host
and never in the chamber.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from vals_harness.errors import ChamberStopped, InfraError
from vals_harness.sandbox import ExecResult, SandboxProvider
from vals_harness.specs import BenchmarkSpec, Task

BENCH_DIR = "/bench"        # benchmark folder inside tooling/verifier sandboxes
OUT_DIR = "/out"            # where prepare scripts write the initial workspace
WORKSPACE = "/workspace"
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


# ------------------------------------------------------------------ adapter


class BenchmarkAdapter:
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
               labels: dict[str, str], timeout: int) -> tuple[Grade, ExecResult | None]:
        """Grade the agent's work in a fresh sandbox the agent never touched.

        Everything that goes wrong because of what the agent left behind (no snapshot,
        bad links, a submission that hangs the grader) is graded, never an infra error:
        otherwise an agent could drop its own failures out of the accuracy number.
        """
        graded_ws, skipped = self.build_graded_workspace(snapshot_tar, prepared, workdir)
        with self.provider.create(self.tooling_image(), labels={**labels, "vals.role": "verifier"}) as sb:
            sb.put_dir(self.spec.root, BENCH_DIR)
            sb.put_dir(graded_ws, WORKSPACE)
            try:
                r = sb.exec(self._argv(self.spec.verify, task, workspace=WORKSPACE),
                            timeout=timeout, cwd=BENCH_DIR)
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

    def build_graded_workspace(self, snapshot_tar: Path, prepared: PreparedTask,
                               workdir: Path) -> tuple[Path, list[str]]:
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
        agent_ws = unpacked / Path(WORKSPACE).name
        agent_ws.mkdir(exist_ok=True)
        skipped += _drop_escaping_links(agent_ws)

        if "**" in self.spec.submission_paths:
            shutil.copytree(agent_ws, graded, symlinks=True)
        else:
            shutil.copytree(prepared.visible_dir, graded)       # pristine starting state
            for pattern in self.spec.submission_paths:
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
