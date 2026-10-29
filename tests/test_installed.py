"""Installed-agent path, at $0: a fake CLI is installed into the chamber and speaks
Claude Code's stream-json format. Plus unit tests for the parser and secret env."""

import json
import textwrap

import pytest

from vals_harness import service
from vals_harness.installed import _secret_env, parse_claude_stream_json
from vals_harness.results import summarize
from vals_harness.specs import AgentSpec, InstalledRuntime, RunSpec
from vals_harness.store import Store


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
  "$VALS_TEST_SECRET" "$(ls /workspace | tr '\n' ',')"
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
        secrets: [VALS_TEST_SECRET]
        """))
    monkeypatch.setenv("VALS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VALS_TEST_SECRET", "s3cret")
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
