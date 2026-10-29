"""End to end in real Docker with the $0 test agents. Expectations:
oracle 100% (pipeline works), noop 0% (graders don't pass untouched work),
cheater 0% (grading can't be read, edited or hijacked)."""

import pytest

from vals_harness import service
from vals_harness.results import summarize
from vals_harness.specs import RunSpec
from vals_harness.store import Store

pytestmark = pytest.mark.docker


@pytest.mark.parametrize("benchmark", ["bash-operations", "python-tasks"])
@pytest.mark.parametrize("agent,expected", [("oracle", 1.0), ("noop", 0.0), ("cheater", 0.0)])
def test_test_agents(root, tmp_path, monkeypatch, agent, expected, benchmark):
    monkeypatch.setenv("VALS_HOME", str(tmp_path))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=f"agents/{agent}", benchmark=benchmark))
    service.work(paths, run_id=run_id)
    s = summarize(Store(paths.db), run_id)
    assert s["status"] == "completed"
    assert s["counts"].get("infra_error", 0) == 0, s["trials"]
    assert s["accuracy"]["mean"] == expected, [(t["task_id"], t["grade"]) for t in s["trials"]]


@pytest.mark.parametrize("benchmark", ["bash-operations", "python-tasks"])
def test_vandal_cannot_turn_its_failure_into_an_infra_error(root, tmp_path, monkeypatch, benchmark):
    """Malformed protocol messages + `rm -rf /workspace` must be graded 0, not excluded."""
    monkeypatch.setenv("VALS_HOME", str(tmp_path))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent="agents/vandal", benchmark=benchmark))
    service.work(paths, run_id=run_id)
    s = summarize(Store(paths.db), run_id)
    assert s["counts"] == {"graded": len(s["trials"])}, s["trials"]
    assert s["accuracy"]["mean"] == 0.0
