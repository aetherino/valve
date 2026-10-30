"""Installed-agent path, at $0: a fake CLI is installed into the chamber and speaks
Claude Code's stream-json format. Plus unit tests for the parser and secret env."""

import json
import textwrap

import pytest

from valve import service
from valve.installed import _secret_env, parse_claude_stream_json
from valve.results import summarize
from valve.specs import AgentSpec, InstalledRuntime, RunSpec
from valve.store import Store


class _Rec:
    def __init__(self):
        self.entries = []

    def transcript(self, e):
        self.entries.append(e)


def _events(*evs):
    return "\n".join(json.dumps(e) for e in evs)


def test_parser_maps_result_and_counts_tools():
    rec = _Rec()
    out = _events(
        {"type": "system", "subtype": "init"},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        "not json at all",
        {"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 0.01, "result": "done",
         "usage": {"input_tokens": 3, "cache_read_input_tokens": 7, "output_tokens": 5}},
    ) + "\n{broken"
    o = parse_claude_stream_json(out, rec)
    assert (o.exit_status, o.steps, o.cost_usd, o.input_tokens, o.output_tokens, o.n_commands) == \
           ("submitted", 2, 0.01, 10, 5, 1)
    assert rec.entries[0]["name"] == "Bash"


@pytest.mark.parametrize("subtype,expected", [("error_max_turns", "step_limit"), ("error_during_execution", "agent_error")])
def test_parser_maps_failures(subtype, expected):
    assert parse_claude_stream_json(_events({"type": "result", "subtype": subtype}), _Rec()).exit_status == expected


def test_parser_without_result_is_agent_error():
    assert parse_claude_stream_json("", _Rec()).exit_status == "agent_error"


@pytest.mark.parametrize("result", [
    {"usage": "not a dict"},
    {"usage": [1, 2]},
    {"total_cost_usd": "x"},
    {"total_cost_usd": -3.0},
    {"total_cost_usd": float("nan")},
    {"num_turns": 1e400},
    {"num_turns": "many", "usage": {"input_tokens": -7, "output_tokens": "lots"}},
    {"usage": {"input_tokens": 1e400, "output_tokens": None}},
    {"subtype": ["x"], "result": {"a": 1}, "is_error": "yes"},
])
def test_parser_sanitizes_agent_controlled_numbers(result):
    """The stream is the agent's output: nothing in it may crash parsing or go negative."""
    ev = {"type": "result", "subtype": "success", **result}
    o = parse_claude_stream_json(json.dumps(ev).replace("Infinity", "1e400").replace("NaN", "1e400"), _Rec())
    assert o.cost_usd >= 0 and o.steps >= 0 and o.input_tokens >= 0 and o.output_tokens >= 0
    assert o.cost_usd == 0 or "total_cost_usd" not in result


@pytest.mark.parametrize("line", [
    '{"type": "assistant", "message": "not a dict"}',
    '{"type": "assistant", "message": {"content": "not a list"}}',
    '{"type": "assistant", "message": {"content": [1, null, {"type": "tool_use"}]}}',
    '[' * 100_000,
    '{"type": "result"}',
])
def test_parser_survives_malformed_events(line):
    o = parse_claude_stream_json(line, _Rec())
    assert o.exit_status in ("agent_error", "submitted")


def test_parser_sums_assistant_usage_when_there_is_no_result():
    """Killed at the wall clock: no result event, but the tokens were still spent. The CLI
    repeats a message's usage on every content block, so each message counts once."""
    usage = {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 3}
    out = _events(
        {"type": "assistant", "message": {"id": "m1", "usage": usage, "content": [{"type": "text"}]}},
        {"type": "assistant", "message": {"id": "m1", "usage": usage,
                                          "content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
        {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 1, "output_tokens": 1}}},
    )
    o = parse_claude_stream_json(out, _Rec())
    assert (o.exit_status, o.input_tokens, o.output_tokens, o.n_commands) == ("agent_error", 16, 4, 1)


API_ERROR = {"type": "result", "subtype": "success", "is_error": True, "num_turns": 1,
             "result": 'API Error: 401 {"type":"error","error":{"type":"authentication_error"}}'}


@pytest.mark.parametrize("text", [
    'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}',
    "Invalid API key · Fix external API key",
    'API Error: 429 {"type":"error","error":{"type":"rate_limit_error"}}',
    'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}',
    'API Error: 404 {"type":"error","error":{"type":"not_found_error","message":"model: claude-nope"}}',
    "API Error: 500 Internal server error",
])
def test_provider_failure_before_any_tool_is_model_error(text):
    o = parse_claude_stream_json(_events({**API_ERROR, "result": text}), _Rec())
    assert o.exit_status == "model_error"


def test_provider_failure_needs_is_error_and_no_tool_use():
    tool = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}}
    # After a tool ran, the agent could have forged the line by writing to the CLI's stdout.
    assert parse_claude_stream_json(_events(tool, API_ERROR), _Rec()).exit_status == "agent_error"
    # The model merely *saying* "API Error: 401" is a normal answer.
    said = {**API_ERROR, "is_error": False}
    assert parse_claude_stream_json(_events(said), _Rec()).exit_status == "submitted"
    # An error that isn't recognizably the provider's stays the agent's.
    other = {**API_ERROR, "result": "something broke"}
    assert parse_claude_stream_json(_events(other), _Rec()).exit_status == "agent_error"


def test_parser_takes_the_last_result_event():
    forged = {"type": "result", "subtype": "success", "total_cost_usd": 0, "num_turns": 1}
    real = {"type": "result", "subtype": "error_max_turns", "total_cost_usd": 0.3, "num_turns": 50}
    o = parse_claude_stream_json(_events(forged, real), _Rec())
    assert (o.exit_status, o.cost_usd) == ("step_limit", 0.3)


def test_secret_env_expands_templates_and_skips_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("KEY", "k")
    monkeypatch.setenv("WS", "w1")
    monkeypatch.delenv("MISSING", raising=False)
    rt = InstalledRuntime(run="x", output_parser="claude_stream_json",
                          env={"HDR": "id: ${WS}", "GONE": "x ${MISSING}", "PLAIN": "1"})
    agent = AgentSpec(name="a", placement="installed", installed=rt, secrets=["KEY", "NOPE"], root=tmp_path)
    assert _secret_env(agent, rt) == {"KEY": "k", "HDR": "id: w1"}


FAKE_CLI = r"""#!/bin/sh
# Pretends to be Claude Code: solves bash-001 and reports in stream-json.
instruction=$(cat)
printf 'Hello World' > hello.txt
echo '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"printf Hello World > hello.txt"}}]}}'
printf '{"type":"result","subtype":"success","num_turns":1,"total_cost_usd":0,"result":"secret=%s net=%s"}\n' \
  "$VALVE_TEST_SECRET" "$(ls /workspace | tr '\n' ',')"
"""


@pytest.mark.docker
def test_installed_agent_end_to_end(root, tmp_path, monkeypatch):
    folder = tmp_path / "fake-cli"
    folder.mkdir()
    (folder / "fakecli").write_text(FAKE_CLI)
    (folder / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nCOPY fakecli /usr/local/bin/fakecli\n"
                                               "RUN chmod +x /usr/local/bin/fakecli\n")
    (folder / "agent.yaml").write_text(textwrap.dedent("""\
        name: fake-cli
        placement: installed
        installed: {run: fakecli, output_parser: claude_stream_json}
        secrets: [VALVE_TEST_SECRET]
        """))
    monkeypatch.setenv("VALVE_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VALVE_TEST_SECRET", "s3cret")
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(folder), benchmark="bash-operations", task_ids=["bash-001"]))
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "graded" and t["grade"]["passed"], t
    assert t["agent"]["exit_status"] == "submitted" and t["agent"]["n_commands"] == 1
    assert "secret=s3cret" in t["agent"]["message"]
    manifest = json.loads(next((tmp_path / "home" / "artifacts").rglob("manifest.json")).read_text())
    assert manifest["pins"]["agent_image"].startswith("sha256:")
    assert manifest["pins"]["agent_image"] != manifest["pins"]["task_image"]


KILLER_CLI = r"""#!/bin/sh
# Pretends to be Claude Code whose Bash tool runs `kill 1` (no result event ever comes).
cat > /dev/null
echo '{"type":"assistant","message":{"id":"m1","usage":{"input_tokens":40,"output_tokens":2},"content":[{"type":"tool_use","name":"Bash","input":{"command":"kill 1"}}]}}'
printf 'not Hello' > hello.txt
kill 1
sleep 30
"""


def _fake_installed_agent(tmp_path, name, script, secrets="[]"):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "fakecli").write_text(script)
    (folder / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nCOPY fakecli /usr/local/bin/fakecli\n"
                                               "RUN chmod +x /usr/local/bin/fakecli\n")
    (folder / "agent.yaml").write_text(f"name: {name}\nplacement: installed\n"
                                       "installed: {run: fakecli, output_parser: claude_stream_json}\n"
                                       f"secrets: {secrets}\n")
    return folder


@pytest.mark.docker
def test_installed_agent_that_stops_its_chamber_is_graded(root, tmp_path, monkeypatch):
    folder = _fake_installed_agent(tmp_path, "fake-killer-cli", KILLER_CLI)
    monkeypatch.setenv("VALVE_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(folder), benchmark="bash-operations", task_ids=["bash-001"]))
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "graded" and not t["grade"]["passed"], t
    assert t["agent"]["exit_status"] == "agent_error" and "stopped its chamber" in t["agent"]["message"]
    assert t["agent"]["input_tokens"] == 40          # summed from the streamed assistant event
    # The stream reached the host as it was produced, even though the chamber died.
    out = next((tmp_path / "home" / "artifacts").rglob("output.jsonl")).read_text()
    assert '"kill 1"' in out


def test_submit_refuses_a_run_whose_agent_secret_is_missing(root, tmp_path, monkeypatch):
    folder = _fake_installed_agent(tmp_path, "needs-key", "#!/bin/sh\n", secrets="[VALVE_TEST_MISSING_KEY]")
    monkeypatch.delenv("VALVE_TEST_MISSING_KEY", raising=False)
    monkeypatch.setenv("VALVE_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    with pytest.raises(ValueError, match="VALVE_TEST_MISSING_KEY"):
        service.submit(paths, RunSpec(agent=str(folder), benchmark="bash-operations", task_ids=["bash-001"]))
    assert Store(paths.db).runs() == []


def test_prepare_fails_fast_when_the_worker_lacks_the_secret(root, tmp_path, monkeypatch):
    """Submitted with the key, run by a worker without it: infra_error (config), retried, never graded."""
    from valve.specs import Policy
    folder = _fake_installed_agent(tmp_path, "needs-key", "#!/bin/sh\n", secrets="[VALVE_TEST_MISSING_KEY]")
    monkeypatch.setenv("VALVE_TEST_MISSING_KEY", "k")
    monkeypatch.setenv("VALVE_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(folder), benchmark="bash-operations", task_ids=["bash-001"],
                                           policy=Policy(infra_retries=0)))
    monkeypatch.delenv("VALVE_TEST_MISSING_KEY")
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "infra_error" and t["error"]["kind"] == "config", t
