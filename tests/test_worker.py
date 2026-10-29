"""Worker lifecycle: signals, a dead supervisor, homes that must not reap each other,
inputs that drift after submit, image pruning, and the launch/prepare budgets."""

import io
import os
import shutil
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from vals_harness import launchers, service
from vals_harness.results import summarize
from vals_harness.sandbox.docker import DockerSandboxProvider
from vals_harness.specs import Policy, RunSpec
from vals_harness.store import Store

SLEEPER = """\
from vals_agent_sdk.client import AgentClient
c = AgentClient("sleeper", "1")
for _ in range(20):
    c.exec("sleep 25")
c.finish("gave_up")
"""


def _containers(label: str) -> list[dict[str, str]]:
    out = subprocess.run(["docker", "ps", "-a", "--filter", f"label={label}", "--format",
                          '{{.ID}} {{.Label "vals.role"}} {{.Label "vals.home"}}'],
                         capture_output=True, text=True).stdout
    return [dict(zip(("id", "role", "home"), line.split())) for line in out.splitlines()]


def _wait(cond, timeout: float, what: str):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if (v := cond()):
            return v
        time.sleep(0.5)
    raise AssertionError(f"timed out waiting for {what}")


def _supervisor(root, home, run_id):
    code = ("import sys; from pathlib import Path; from vals_harness import service; "
            "service.work(service.Paths.from_root(Path(sys.argv[1])), run_id=sys.argv[2])")
    return subprocess.Popen([sys.executable, "-c", code, str(root), run_id],
                            env={**os.environ, "VALS_HOME": str(home)})


@pytest.mark.docker
def test_supervisor_signals_and_death_clean_up_and_homes_dont_reap_each_other(root, tmp_path, monkeypatch):
    agent = tmp_path / "sleeper"
    agent.mkdir()
    (agent / "agent.py").write_text(SLEEPER)
    (agent / "agent.yaml").write_text('name: sleeper\nplacement: sidecar\ncommand: ["python", "agent.py"]\n')
    home, other = tmp_path / "home", tmp_path / "other-home"
    monkeypatch.setenv("VALS_HOME", str(home))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(agent), benchmark="bash-operations", task_ids=["bash-001"]))
    store = Store(paths.db)
    label = f"vals.run_id={run_id}"

    def running():
        roles = {c["role"] for c in _containers(label)}
        return {"chamber", "agent"} <= roles

    try:
        # 1. SIGTERM takes the Ctrl-C path: containers reaped, trial released (not charged).
        sup = _supervisor(root, home, run_id)
        _wait(running, 120, "the trial's chamber and agent")
        # Every container carries this home's label; another home's reaper leaves them alone.
        assert {c["home"] for c in _containers(label)} == {store.home_id}
        assert DockerSandboxProvider().reap_stale(set(), Store(other / "harness.db").home_id) == 0
        assert running()
        sup.send_signal(signal.SIGTERM)
        assert sup.wait(timeout=60) is not None
        assert _containers(label) == []
        (t,) = summarize(store, run_id)["trials"]
        assert t["status"] == "queued" and t["infra_attempts"] == 0, t
        assert list((home / "jobs").iterdir()) == []

        # 2. SIGKILL: the supervisor's finally never runs; the trial child notices and cleans up.
        sup = _supervisor(root, home, run_id)
        _wait(running, 120, "the trial's chamber and agent (second claim)")
        sup.kill()
        sup.wait()
        _wait(lambda: _containers(label) == [], 30, "the orphaned child to reap its containers")
        _wait(lambda: summarize(store, run_id)["trials"][0]["status"] == "queued", 30, "the trial to be released")
    finally:
        DockerSandboxProvider().reap({"vals.run_id": run_id})


def test_trial_fails_if_inputs_changed_since_submit(root, tmp_path, monkeypatch):
    agent = tmp_path / "noop"
    shutil.copytree(root / "agents" / "noop", agent)
    monkeypatch.setenv("VALS_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(agent), benchmark="bash-operations", task_ids=["bash-001"],
                                           policy=Policy(infra_retries=0)))
    (agent / "agent.py").write_text((agent / "agent.py").read_text() + "\n# edited after submit\n")
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "infra_error" and t["error"]["kind"] == "adapter", t
    assert "inputs changed since submit: agent" in t["error"]["message"]


def test_prune_keeps_current_and_pending_images_only(root, tmp_path, monkeypatch):
    from vals_harness.installed import InstalledRunner
    from vals_harness.launchers import DockerLauncher
    from vals_harness.specs import load_agent

    fake = tmp_path / "fake-cli"
    fake.mkdir()
    (fake / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\n")
    (fake / "agent.yaml").write_text("name: fake-cli\nplacement: installed\n"
                                     "installed: {run: x, output_parser: claude_stream_json}\n")
    monkeypatch.setenv("VALS_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    service.submit(paths, RunSpec(agent=str(fake), benchmark="bash-operations", task_ids=["bash-001"]))  # queued
    task_id = "sha256:" + "a" * 64
    monkeypatch.setattr(service, "image_ids", lambda refs: {r: task_id for r in refs if r == "alpine:latest"})

    cc = load_agent("claude-code", root)
    fake_agent = load_agent(str(fake), root)
    keep = [DockerLauncher.tag_for(load_agent("noop", root)),
            InstalledRunner.tag_for(cc, task_id), InstalledRunner.base_tag(task_id),
            InstalledRunner.tag_for(fake_agent, task_id)]          # a queued trial's agent, outside agents/
    stale = ["vals/agent-noop:000000000000", "vals/installed-claude-code:000000000000",
             "vals/base:0000000000000000", "vals/installed-fake-cli:000000000000"]
    monkeypatch.setattr(service, "vals_images", lambda: keep + stale)
    r = service.prune(paths, dry_run=True)
    assert sorted(r["removed"]) == sorted(stale)


def test_image_timeouts_fit_the_prepare_slack():
    from vals_harness.benchmarks import PREPARE_TIMEOUT_S
    from vals_harness.installed import INSTALLED_BUILD_TIMEOUT_S
    from vals_harness.sandbox.docker import PULL_TIMEOUT_S, TOOLING_BUILD_TIMEOUT_S
    from vals_harness.specs import PREPARE_SLACK_S
    worst = PULL_TIMEOUT_S + TOOLING_BUILD_TIMEOUT_S + max(launchers.AGENT_BUILD_TIMEOUT_S,
                                                           INSTALLED_BUILD_TIMEOUT_S) + PREPARE_TIMEOUT_S
    assert worst <= PREPARE_SLACK_S


def test_sidecar_runs_the_pinned_image_id_and_never_pulls(monkeypatch, tmp_path):
    from vals_harness.specs import AgentSpec
    seen = {}

    def fake_start(argv, **kw):
        seen["argv"] = argv
        return SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), poll=lambda: 0)

    monkeypatch.setattr(launchers, "_start", fake_start)
    launcher = launchers.DockerLauncher()
    launcher._ids["x"] = "sha256:" + "b" * 64
    spec = AgentSpec(name="x", placement="sidecar", command=["python", "agent.py"], root=tmp_path)
    launcher.launch(spec, tmp_path / "stderr.log", {"vals.claim": "c" * 32})
    argv = seen["argv"]
    assert "sha256:" + "b" * 64 in argv and ["--pull", "never"] == argv[argv.index("--pull"):argv.index("--pull") + 2]
    assert not any(a.startswith("vals/agent-") for a in argv)


def test_jobs_are_removed_however_a_trial_ends(tmp_path):
    from vals_harness.service import _remove_job
    jobs = tmp_path
    for name in ("t.job.json", "t.result.json", "t.result.json.tmp"):
        (jobs / name).write_text("{}")
    _remove_job(jobs / "t.result.json")
    assert list(jobs.iterdir()) == []
