"""Benchmark formats beyond the starter's: task folders (Harbor style), the Python adapter
hook, reward files, verify modes, per-task workspace, and the agent's answer reaching the
grader."""

import textwrap

import pytest
from pydantic import ValidationError

from valve import service
from valve.benchmarks import make_benchmark, parse_reward
from valve.results import summarize
from valve.specs import RunSpec, Task, load_benchmark, load_tasks
from valve.store import Store


# ---------------------------------------------------------------- unit


def test_parse_reward_forms():
    assert parse_reward(" 0.5\n", "txt")[0] == 0.5
    assert parse_reward('{"reward": 1, "style": 0.2}', "json")[0] == 1.0
    assert parse_reward('{"a": 1, "b": 0}', "json")[0] == 0.5
    for bad, kind in (("nan", "txt"), ("x", "txt"), ('{"a": "x"}', "json"), ("[1]", "json")):
        with pytest.raises(ValueError):
            parse_reward(bad, kind)


def test_task_folders_parse_into_tasks(root):
    tasks = {t.id: t for t in load_tasks(load_benchmark("system-tasks", root))}
    assert set(tasks) == {"sys-001-service", "sys-002-assets", "sys-003-path"}
    t = tasks["sys-002-assets"]
    assert t.build == "tasks/sys-002-assets/environment/Dockerfile" and t.image is None
    assert (t.workspace, t.verify_mode, t.limits["verify_timeout_s"]) == ("/app", "task_image", 60)
    assert tasks["sys-001-service"].verify_mode == "chamber"


def test_task_validation():
    with pytest.raises(ValidationError):
        Task(id="x", instruction="i")                                  # no image and no build
    with pytest.raises(ValidationError):
        Task(id="x", instruction="i", image="alpine", workspace="/tmp")


def test_task_limits_override_agent_defaults_and_run_overrides_win(root):
    from valve.specs import resolve_trials
    run = RunSpec(agent="agents/oracle", benchmark="system-tasks", task_ids=["sys-003-path"])
    (t,) = resolve_trials(run, "r1", root, "test")
    assert t.limits.verify_timeout_s == 30                              # from task.toml
    run = RunSpec(agent="agents/oracle", benchmark="system-tasks", task_ids=["sys-003-path"],
                  limits={"verify_timeout_s": 99})
    (t,) = resolve_trials(run, "r1", root, "test")
    assert t.limits.verify_timeout_s == 99                              # run override wins


ADAPTER = '''
from valve.benchmarks import Grade, PreparedTask
from valve.specs import Task

class QA:
    """Answers graded on the agent's final message: no files, no tests in a container."""
    def __init__(self, spec, provider):
        self.spec, self.provider = spec, provider
    def tasks(self):
        return [Task(id="q1", instruction="What is 6 x 7? Answer with the number.", image="alpine:latest",
                     meta={"expected": "42"})]
    def images(self, task):
        return {"task_image": self.provider.ensure_image(task.image)}
    def prepare(self, task, workdir, labels):
        (workdir / "v").mkdir(parents=True, exist_ok=True); (workdir / "e").mkdir(exist_ok=True)
        return PreparedTask(workdir / "v", workdir / "e")
    def verify_mode(self, task):
        return "task_image"
    def verify(self, task, snapshot_tar, prepared, workdir, labels, timeout, *, images, answer=""):
        ok = answer.strip() == task.meta["expected"]
        return Grade(ok, float(ok), {"answer": answer}), None
    def verify_in_chamber(self, *a, **k):
        raise NotImplementedError
'''


def test_python_adapter_hook(tmp_path):
    folder = tmp_path / "qa"
    folder.mkdir()
    (folder / "adapter.py").write_text(ADAPTER)
    (folder / "harness.yaml").write_text("name: qa\nformat: python\nadapter: adapter.py:QA\n")
    spec = load_benchmark(str(folder), tmp_path)
    (task,) = load_tasks(spec)
    assert task.meta["expected"] == "42"
    assert make_benchmark(spec, None).verify_mode(task) == "task_image"


# ---------------------------------------------------------------- Docker integration



def _agent(tmp_path, name, body):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "agent.py").write_text("from valve_agent_sdk.client import AgentClient\nc = AgentClient('x', '1')\n"
                                     + textwrap.dedent(body))
    (folder / "agent.yaml").write_text(f"name: {name}\nplacement: sidecar\ncommand: [python, agent.py]\n")
    return folder


def _run(root, tmp_path, monkeypatch, agent, benchmark, task_ids=None):
    monkeypatch.setenv("VALVE_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(agent), benchmark=str(benchmark), task_ids=task_ids))
    service.work(paths, run_id=run_id, slots=3)
    return summarize(Store(paths.db), run_id)


@pytest.mark.docker
@pytest.mark.parametrize("agent,expected", [("oracle", 1.0), ("noop", 0.0), ("cheater", 0.0)])
def test_test_agents_on_task_folders(root, tmp_path, monkeypatch, agent, expected):
    s = _run(root, tmp_path, monkeypatch, root / "agents" / agent, root / "benchmarks" / "system-tasks")
    assert s["counts"] == {"graded": 3}, s["trials"]
    assert s["accuracy"]["mean"] == expected, [(t["task_id"], t["grade"]) for t in s["trials"]]


@pytest.mark.docker
def test_agent_cannot_preplant_the_reward_in_chamber_mode(root, tmp_path, monkeypatch):
    """The reward dir is wiped before the tests run, so a pre-written 1 doesn't count."""
    forger = _agent(tmp_path, "forger", """
        c.exec("mkdir -p /logs/verifier && echo 1 > /logs/verifier/reward.txt && echo '{\\"reward\\": 1}' > /logs/verifier/reward.json")
        c.finish("submitted")
    """)
    s = _run(root, tmp_path, monkeypatch, forger, root / "benchmarks" / "system-tasks", ["sys-003-path"])
    (t,) = s["trials"]
    assert t["status"] == "graded" and not t["grade"]["passed"]


@pytest.mark.docker
def test_answer_reaches_the_grader_and_python_adapter_runs_end_to_end(root, tmp_path, monkeypatch):
    bench = tmp_path / "qa"
    bench.mkdir()
    (bench / "adapter.py").write_text(ADAPTER)
    (bench / "harness.yaml").write_text("name: qa\nformat: python\nadapter: adapter.py:QA\n")
    answerer = _agent(tmp_path, "answerer", 'c.finish("submitted", answer="42")\n')
    s = _run(root, tmp_path, monkeypatch, answerer, bench)
    (t,) = s["trials"]
    assert t["status"] == "graded" and t["grade"]["passed"], t
    assert t["agent"]["answer"] == "42"


@pytest.mark.docker
def test_answer_file_in_task_folder_tests(root, tmp_path, monkeypatch):
    """task_dirs tests can read the agent's final message at /logs/agent/answer.txt."""
    bench = tmp_path / "qa-dirs"
    task = bench / "tasks" / "q1"
    (task / "environment").mkdir(parents=True)
    (task / "tests").mkdir()
    (bench / "harness.yaml").write_text("name: qa-dirs\nformat: task_dirs\n")
    (task / "instruction.md").write_text("What is 6 x 7?")
    (task / "task.toml").write_text('[verifier]\nmode = "task_image"\n')
    (task / "environment" / "Dockerfile").write_text("FROM alpine:3.20\nWORKDIR /app\n")
    (task / "tests" / "test.sh").write_text('#!/bin/sh\ngrep -qx 42 /logs/agent/answer.txt && echo 1 > /logs/verifier/reward.txt || echo 0 > /logs/verifier/reward.txt\n')
    answerer = _agent(tmp_path, "answerer", 'c.finish("submitted", answer="42")\n')
    s = _run(root, tmp_path, monkeypatch, answerer, bench)
    (t,) = s["trials"]
    assert t["status"] == "graded" and t["grade"]["passed"], t


def test_build_lock_serializes_across_processes(tmp_path):
    """Two processes building the same tag take turns instead of racing."""
    import subprocess
    import sys
    code = ("import sys, time\nfrom valve.sandbox.docker import build_lock\n"
            "with build_lock('valve/test-lock:x'):\n"
            "    open(sys.argv[1], 'a').write('in\\n'); time.sleep(0.5); open(sys.argv[1], 'a').write('out\\n')\n")
    log = tmp_path / "log"
    procs = [subprocess.Popen([sys.executable, "-c", code, str(log)]) for _ in range(2)]
    for p in procs:
        assert p.wait() == 0
    assert log.read_text().split() == ["in", "out", "in", "out"]
