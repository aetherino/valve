"""End to end in real Docker with the $0 test agents. Expectations:
oracle 100% (pipeline works), noop 0% (graders don't pass untouched work),
cheater 0% (grading can't be read, edited or hijacked)."""

import pytest

from valve import service
from valve.results import summarize
from valve.specs import RunSpec
from valve.store import Store

pytestmark = pytest.mark.docker


@pytest.mark.parametrize("benchmark", ["bash-operations", "python-tasks"])
@pytest.mark.parametrize("agent,expected", [("oracle", 1.0), ("noop", 0.0), ("cheater", 0.0)])
def test_test_agents(root, tmp_path, monkeypatch, agent, expected, benchmark):
    monkeypatch.setenv("VALVE_HOME", str(tmp_path))
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
    monkeypatch.setenv("VALVE_HOME", str(tmp_path))
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

    from valve.chamber_api import ChamberApi
    from valve.launchers import DockerLauncher
    from valve.specs import AgentSpec

    folder = tmp_path / "snoop"
    folder.mkdir()
    (folder / "agent.py").write_text(
        "import json, os\n"
        "from valve_agent_sdk.client import AgentClient\n"
        "c = AgentClient('snoop', '1')\n"
        "report = {'env': sorted(os.environ), 'repo_visible': os.path.exists(%r),\n"
        "          'home_visible': os.path.exists(%r), 'declared': os.environ.get('VALVE_TEST_SECRET')}\n"
        "c.finish('submitted', message=json.dumps(report))\n" % (str(root), str(root.home())))
    monkeypatch.setenv("VALVE_TEST_SECRET", "s3cret")
    monkeypatch.setenv("VALVE_TEST_UNDECLARED", "nope")
    spec = AgentSpec(name="snoop", placement="sidecar", command=["python", "agent.py"],
                     secrets=["VALVE_TEST_SECRET"], root=folder)
    labels = {"valve.claim": "test-snoop"}
    session = DockerLauncher().launch(spec, tmp_path / "stderr.log", labels)
    api = ChamberApi(FakeSandbox(), ListRecorder(), trial_id="t", command_timeout_s=30)
    outcome = api.serve(session, {"instruction": "x", "config": {}}, deadline=time.monotonic() + 60)
    report = json.loads(outcome.message)
    assert outcome.exit_status == "submitted"
    assert report["declared"] == "s3cret"
    assert "VALVE_TEST_UNDECLARED" not in report["env"]
    assert not report["repo_visible"] and not report["home_visible"]


def _peak_concurrency(store, run_id):
    rows = store.db.execute("SELECT started_at, finished_at FROM trials WHERE run_id=?", (run_id,)).fetchall()
    return max(sum(1 for s, f in rows if s <= t < f) for t, _ in rows)


def test_slots_run_trials_concurrently_in_child_processes(root, tmp_path, monkeypatch):
    monkeypatch.setenv("VALVE_HOME", str(tmp_path))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent="agents/oracle", benchmark="python-tasks", attempts=2))
    service.work(paths, slots=3, run_id=run_id)
    store = Store(paths.db)
    s = summarize(store, run_id)
    assert s["counts"] == {"graded": 6} and s["accuracy"]["mean"] == 1.0
    assert _peak_concurrency(store, run_id) >= 2
    assert not any((tmp_path / "jobs").iterdir())


def test_trial_wall_clock_kills_the_trial_and_its_containers(root, tmp_path, monkeypatch):
    import subprocess

    from valve.specs import Policy

    # An agent that never finishes, so the trial clock (not the agent's own) always fires first.
    sleeper = tmp_path / "sleeper"
    sleeper.mkdir()
    (sleeper / "agent.py").write_text("import time\nfrom valve_agent_sdk.client import AgentClient\n"
                                      "AgentClient('sleeper', '1')\ntime.sleep(600)\n")
    (sleeper / "agent.yaml").write_text("name: sleeper\nplacement: sidecar\ncommand: [python, agent.py]\n")
    monkeypatch.setenv("VALVE_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(sleeper), benchmark="python-tasks", task_ids=["py-001"],
                                           limits={"trial_timeout_s": 8}, policy=Policy(infra_retries=0)))
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "infra_error" and t["error"]["kind"] == "timeout_harness"
    left = subprocess.run(["docker", "ps", "-aq", "--filter", f"label=valve.run_id={run_id}"],
                          capture_output=True, text=True).stdout.split()
    assert left == []


@pytest.mark.parametrize("benchmark", ["bash-operations", "python-tasks"])
def test_saboteur_stopping_its_chamber_is_graded_not_infra(root, tmp_path, monkeypatch, benchmark):
    """`kill 1`, killing the keep-alive process and `kill -9 -1` stop the chamber from the
    inside; a submission that kills the verifier's PID 1 stops the grader. All graded 0,
    with the agent's reported spend kept."""
    monkeypatch.setenv("VALVE_HOME", str(tmp_path))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent="agents/saboteur", benchmark=benchmark))
    service.work(paths, slots=3, run_id=run_id)
    s = summarize(Store(paths.db), run_id)
    assert s["counts"] == {"graded": len(s["trials"])}, s["trials"]
    assert s["accuracy"]["mean"] == 0.0
    for t in s["trials"]:
        assert t["agent"]["exit_status"] == "agent_error", t["agent"]
        assert "stopped its chamber" in t["agent"]["message"], t["agent"]
        assert t["metrics"]["cost_usd"] == 0.25 and t["spent_usd"] == 0.25
        if benchmark == "python-tasks":
            assert "submission stopped the grader" in t["grade"]["details"]["harness"]["error"], t["grade"]
