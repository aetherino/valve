import io
import tarfile
from pathlib import Path

import pytest

from vals_harness.benchmarks import (BenchmarkAdapter, PreparedTask, parse_exit_code,
                                     parse_json_stdout)
from vals_harness.errors import InfraError
from vals_harness.sandbox import ExecResult
from vals_harness.specs import load_benchmark, load_tasks


def _r(output: str, rc: int = 0, timed_out: bool = False) -> ExecResult:
    return ExecResult(output=output, returncode=rc, timed_out=timed_out, truncated=False, duration_s=0.1)


def test_json_parser_ignores_surrounding_noise_and_exit_code():
    g = parse_json_stdout(_r('warning: x\n{"passed": false, "score": 0.0, "details": {"a": 1}}\n', rc=1))
    assert (g.passed, g.score, g.details) == (False, 0.0, {"a": 1})


def test_json_parser_failure_is_an_infra_error_not_a_zero():
    with pytest.raises(InfraError) as e:
        parse_json_stdout(_r("Traceback: boom"))
    assert e.value.kind == "verifier"


def test_exit_code_parser():
    assert parse_exit_code(_r("", 0)).passed and not parse_exit_code(_r("", 1)).passed


# ---------------------------------------------------------------- graded workspace


def _tar(files: dict[str, bytes], links: dict[str, str] | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        dirinfo = tarfile.TarInfo("workspace")
        dirinfo.type = tarfile.DIRTYPE
        tar.addfile(dirinfo)
        for name, data in files.items():
            info = tarfile.TarInfo(f"workspace/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        for name, target in (links or {}).items():
            info = tarfile.TarInfo(f"workspace/{name}")
            info.type, info.linkname = tarfile.SYMTYPE, target
            tar.addfile(info)
    return buf.getvalue()


def _prepared(tmp: Path, visible: dict[str, str], evals: dict[str, str]) -> PreparedTask:
    v, e = tmp / "visible", tmp / "eval"
    for d, files in ((v, visible), (e, evals)):
        d.mkdir()
        for name, text in files.items():
            (d / name).write_text(text)
    return PreparedTask(v, e)


def _adapter(root, name):
    return BenchmarkAdapter(load_benchmark(name, root), provider=None)  # type: ignore[arg-type]


def test_restricted_submission_drops_planted_files_and_restores_tests(root, tmp_path):
    prepared = _prepared(tmp_path, {"solution.py": "# stub\n"}, {"test_solution.py": "REAL TESTS"})
    snap = tmp_path / "ws.tar"
    snap.write_bytes(_tar({"solution.py": b"def add(): return 4\n", "pytest.py": b"import sys; sys.exit(0)",
                           "conftest.py": b"hijack", "test_solution.py": b"FAKE"}))
    graded, _ = _adapter(root, "python-tasks").build_graded_workspace(snap, prepared, tmp_path / "work")
    assert sorted(p.name for p in graded.iterdir()) == ["solution.py", "test_solution.py"]
    assert (graded / "solution.py").read_text() == "def add(): return 4\n"
    assert (graded / "test_solution.py").read_text() == "REAL TESTS"


def test_full_submission_keeps_agent_files_and_deletions(root, tmp_path):
    prepared = _prepared(tmp_path, {"input.txt": "a\nb"}, {})
    snap = tmp_path / "ws.tar"
    snap.write_bytes(_tar({"count.txt": b"2"}))  # agent deleted input.txt
    graded, _ = _adapter(root, "bash-operations").build_graded_workspace(snap, prepared, tmp_path / "work")
    assert sorted(p.name for p in graded.iterdir()) == ["count.txt"]


def test_bad_links_are_dropped_and_reported_not_fatal(root, tmp_path):
    """A venv's absolute links are normal; they must not turn into an infra error."""
    prepared = _prepared(tmp_path, {}, {})
    snap = tmp_path / "ws.tar"
    snap.write_bytes(_tar({"ok.txt": b"x"}, links={"python": "/usr/bin/python3", "stolen": "../bench/grade.py",
                                                   "inside": "ok.txt"}))
    graded, skipped = _adapter(root, "bash-operations").build_graded_workspace(snap, prepared, tmp_path / "work")
    assert sorted(p.name for p in graded.iterdir()) == ["inside", "ok.txt"]
    assert any("python" in s for s in skipped) and any("stolen" in s for s in skipped)


def test_missing_snapshot_is_an_empty_submission(root, tmp_path):
    prepared = _prepared(tmp_path, {"solution.py": "# stub\n"}, {"test_solution.py": "T"})
    graded, skipped = _adapter(root, "python-tasks").build_graded_workspace(tmp_path / "absent.tar", prepared,
                                                                            tmp_path / "work")
    assert sorted(p.name for p in graded.iterdir()) == ["solution.py", "test_solution.py"]
    assert skipped == ["<no snapshot: workspace missing>"]


def test_eval_overlay_never_writes_through_agent_links(tmp_path, root):
    """With "**" + eval files, an agent link test_solution.py -> elsewhere must be replaced, not followed."""
    spec = load_benchmark("python-tasks", root).model_copy(update={"submission_paths": ["**"]})
    ad = BenchmarkAdapter(spec, provider=None)  # type: ignore[arg-type]
    prepared = _prepared(tmp_path, {"solution.py": "PRISTINE"}, {"test_solution.py": "REAL TESTS"})
    snap = tmp_path / "ws.tar"
    snap.write_bytes(_tar({"solution.py": b"agent"}, links={"test_solution.py": "solution.py"}))
    graded, _ = ad.build_graded_workspace(snap, prepared, tmp_path / "work")
    assert not (graded / "test_solution.py").is_symlink()
    assert (graded / "test_solution.py").read_text() == "REAL TESTS"
    assert (graded / "solution.py").read_text() == "agent"
    assert (prepared.visible_dir / "solution.py").read_text() == "PRISTINE"


# ---------------------------------------------------------------- verify outcome rules


class _FakeSandbox:
    def __init__(self, result: ExecResult):
        self.result = result

    def put_dir(self, *_):
        pass

    def exec(self, *_, **__):
        return self.result

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class _FakeProvider:
    def __init__(self, result: ExecResult):
        self.result = result

    def ensure_image(self, image=None, *, dockerfile=None):
        return "img"

    def create(self, *_, **__):
        return _FakeSandbox(self.result)


def _verify(root, tmp_path, grader_output: ExecResult):
    ad = BenchmarkAdapter(load_benchmark("python-tasks", root), _FakeProvider(grader_output))  # type: ignore[arg-type]
    prepared = _prepared(tmp_path, {"solution.py": "x"}, {"test_solution.py": "T"})
    task = load_tasks(ad.spec)[0]
    return ad.verify(task, tmp_path / "none.tar", prepared, tmp_path / "work", {}, timeout=5)[0]


def test_exit_code_pass_without_pytest_evidence_is_rejected(root, tmp_path):
    """os._exit(0) at import: grade.py says passed, but pytest never printed a summary."""
    out = '{"passed": true, "score": 1.0, "details": {"stdout": "collecting ... "}}'
    g = _verify(root, tmp_path, _r(out))
    assert not g.passed and "pass_rejected" in g.details["harness"]


def test_real_pass_is_accepted(root, tmp_path):
    out = '{"passed": true, "score": 1.0, "details": {"stdout": "==== 1 passed in 0.01s ===="}}'
    assert _verify(root, tmp_path, _r(out)).passed


def test_grader_timeout_is_graded_not_infra(root, tmp_path):
    g = _verify(root, tmp_path, _r("", rc=-1, timed_out=True))
    assert not g.passed and "timed out" in g.details["harness"]["error"]
