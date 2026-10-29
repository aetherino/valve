import pytest
from pydantic import ValidationError

from vals_harness.specs import RunSpec, load_agent, load_benchmark, load_tasks, resolve_trials


def test_benchmarks_normalize_their_own_field_names(root):
    bash = load_tasks(load_benchmark("bash-operations", root))
    py = load_tasks(load_benchmark("python-tasks", root))
    assert [t.id for t in bash] == ["bash-001", "bash-002", "bash-003", "bash-004", "bash-005"]
    assert [t.id for t in py] == ["py-001", "py-002", "py-003"]
    assert bash[0].image == "alpine:latest" and py[0].image == "python:3.11-slim"
    assert "hello.txt" in bash[0].instruction and "add()" in py[0].instruction


def test_agent_spec_loads_from_path_or_name(root):
    assert load_agent("agents/oracle", root).name == load_agent("oracle", root).name == "oracle"


def test_resolve_expands_tasks_times_attempts(root):
    run = RunSpec(agent="agents/oracle", benchmark="python-tasks", attempts=3)
    trials = resolve_trials(run, "r1", root, "test")
    assert len(trials) == 9
    assert {t.attempt for t in trials} == {1, 2, 3}
    assert len({t.trial_id for t in trials}) == 9


def test_precedence_run_over_agent_over_harness(root):
    run = RunSpec(agent="agents/mini-swe-agent", benchmark="bash-operations", task_ids=["bash-001"],
                  limits={"step_limit": 7}, model="anthropic/claude-haiku-5-5")
    (t,) = resolve_trials(run, "r1", root, "test")
    assert t.limits.step_limit == 7                 # run override
    assert t.limits.cost_limit_usd == 1.0           # agent default
    assert t.limits.verify_timeout_s == 120         # harness default
    assert t.model == "anthropic/claude-haiku-5-5"
    assert t.agent_config()["step_limit"] == 7


def test_rejects_unknown_tasks_and_limits(root):
    with pytest.raises(ValueError, match="unknown task ids"):
        resolve_trials(RunSpec(agent="agents/oracle", benchmark="python-tasks", task_ids=["nope"]),
                       "r1", root, "test")
    with pytest.raises(ValidationError, match="unknown limits"):
        RunSpec(agent="a", benchmark="b", limits={"tiemout_s": 5})


def test_manifest_hash_is_stable_and_sensitive(root):
    a = resolve_trials(RunSpec(agent="agents/oracle", benchmark="python-tasks"), "r1", root, "test")
    b = resolve_trials(RunSpec(agent="agents/oracle", benchmark="python-tasks"), "r1", root, "test")
    c = resolve_trials(RunSpec(agent="agents/oracle", benchmark="python-tasks", limits={"cpus": 2}), "r1", root, "test")
    assert a[0].manifest_hash() == b[0].manifest_hash() != c[0].manifest_hash()


def test_expects_must_be_program_names(tmp_path):
    from vals_harness.specs import AgentSpec
    AgentSpec(name="a", placement="sidecar", command=["x"], expects=["bash", "python3"], root=tmp_path)
    with pytest.raises(ValidationError):
        AgentSpec(name="a", placement="sidecar", command=["x"], expects=["bash; rm -rf /"], root=tmp_path)
