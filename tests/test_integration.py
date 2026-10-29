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


def test_sidecar_agent_sees_nothing_of_the_host(root, tmp_path, monkeypatch):
    """The agent process itself (not just its commands) is confined: no host files,
    no worker environment, only its declared secrets."""
    import json
    import time

    from test_protocol import FakeSandbox, ListRecorder

    from vals_harness.chamber_api import ChamberApi
    from vals_harness.launchers import DockerLauncher
    from vals_harness.specs import AgentSpec

    folder = tmp_path / "snoop"
    folder.mkdir()
    (folder / "agent.py").write_text(
        "import json, os\n"
        "from vals_agent_sdk.client import AgentClient\n"
        "c = AgentClient('snoop', '1')\n"
        "report = {'env': sorted(os.environ), 'repo_visible': os.path.exists(%r),\n"
        "          'home_visible': os.path.exists(%r), 'declared': os.environ.get('VALS_TEST_SECRET')}\n"
        "c.finish('submitted', message=json.dumps(report))\n" % (str(root), str(root.home())))
    monkeypatch.setenv("VALS_TEST_SECRET", "s3cret")
    monkeypatch.setenv("VALS_TEST_UNDECLARED", "nope")
    spec = AgentSpec(name="snoop", placement="sidecar", command=["python", "agent.py"],
                     secrets=["VALS_TEST_SECRET"], root=folder)
    labels = {"vals.claim": "test-snoop"}
    session = DockerLauncher().launch(spec, tmp_path / "stderr.log", labels)
    api = ChamberApi(FakeSandbox(), ListRecorder(), trial_id="t", command_timeout_s=30)
    outcome = api.serve(session, {"instruction": "x", "config": {}}, deadline=time.monotonic() + 60)
    report = json.loads(outcome.message)
    assert outcome.exit_status == "submitted"
    assert report["declared"] == "s3cret"
    assert "VALS_TEST_UNDECLARED" not in report["env"]
    assert not report["repo_visible"] and not report["home_visible"]
