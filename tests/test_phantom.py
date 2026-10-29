"""Phantom placement, at $0: a fake agent CLI runs in the phantom box and uses `sh -c`
(the phantom shell). Proves commands run in the chamber, the chamber stays offline
and secret-free while the box holds the secret, and the work is graded normally."""

import json
import textwrap

import pytest

from vals_harness import service
from vals_harness.results import summarize
from vals_harness.specs import RunSpec
from vals_harness.store import Store

pytestmark = pytest.mark.docker

FAKE_CLI = r"""#!/usr/lib/phantom/bash.real
# Pretends to be an agent CLI: every `sh -c` below goes through the phantom shell.
cat > /dev/null   # the instruction
sh -c "printf 'def add():\n    return 4\n' > solution.py"
in_chamber_secret=$(sh -c 'env' | grep -c '^VALS_TEST_SECRET=')
chamber_net=$(sh -c "python3 -c 'import urllib.request; urllib.request.urlopen(\"http://example.com\", timeout=3)' >/dev/null 2>&1 && echo online || echo offline")
rc=$(sh -c 'exit 7'; echo $?)
printf '{"type":"result","subtype":"success","num_turns":1,"total_cost_usd":0,"result":"box_secret=%s chamber_secret=%s chamber_net=%s rc=%s"}\n' \
  "${VALS_TEST_SECRET:-none}" "$in_chamber_secret" "$chamber_net" "$rc"
"""


def test_phantom_box_relays_its_shell_into_the_chamber(root, tmp_path, monkeypatch):
    folder = tmp_path / "fake-phantom"
    folder.mkdir()
    (folder / "fakecli").write_text(FAKE_CLI)
    (folder / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nCOPY fakecli /usr/local/bin/fakecli\n"
                                               "RUN chmod +x /usr/local/bin/fakecli\n")
    (folder / "agent.yaml").write_text(textwrap.dedent("""\
        name: fake-phantom
        placement: phantom
        installed: {run: fakecli, output_parser: claude_stream_json}
        secrets: [VALS_TEST_SECRET]
        """))
    monkeypatch.setenv("VALS_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("VALS_TEST_SECRET", "s3cret")
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(folder), benchmark="python-tasks", task_ids=["py-001"]))
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "graded", t
    msg = t["agent"]["message"]
    assert "box_secret=s3cret" in msg            # the box has its secret
    assert "chamber_secret=0" in msg             # the chamber never sees it
    assert "chamber_net=offline" in msg          # commands ran in the offline chamber
    assert "rc=7" in msg                         # exit codes come back through the relay
    assert t["grade"]["passed"]                  # solution.py written via the phantom shell, graded
    assert t["metrics"]["n_commands"] == 4       # every relayed command is in our transcript
    manifest = json.loads(next((tmp_path / "home" / "artifacts").rglob("manifest.json")).read_text())
    assert manifest["pins"]["agent_image"] != manifest["pins"]["task_image"]


ENV_CLI = r"""#!/usr/lib/phantom/bash.real
cat > /dev/null
sh -c 'definitely-not-a-command --version'   # rc 127 in the chamber
printf '{"type":"result","subtype":"success","num_turns":1,"total_cost_usd":0,"result":"ok"}\n'
"""


@pytest.mark.parametrize("benchmark,task,missing", [("bash-operations", "bash-001", ["bash"]),
                                                     ("python-tasks", "py-001", [])])
def test_env_check_flags_declared_expectations_the_chamber_lacks(root, tmp_path, monkeypatch, benchmark, task, missing):
    """Alpine has no bash: flagged, not fixed (the task image is the experiment)."""
    folder = tmp_path / "fake-env"
    folder.mkdir()
    (folder / "fakecli").write_text(ENV_CLI)
    (folder / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nCOPY fakecli /usr/local/bin/fakecli\n"
                                               "RUN chmod +x /usr/local/bin/fakecli\n")
    (folder / "agent.yaml").write_text("name: fake-env\nplacement: phantom\nexpects: [bash]\n"
                                       "installed: {run: fakecli, output_parser: claude_stream_json}\n")
    if benchmark == "bash-operations":
        # alpine has no bash for the fake CLI's shebang inside the box either: give the box one
        (folder / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nRUN apk add --no-cache bash\n"
                                                   "COPY fakecli /usr/local/bin/fakecli\nRUN chmod +x /usr/local/bin/fakecli\n")
    monkeypatch.setenv("VALS_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(folder), benchmark=benchmark, task_ids=[task]))
    service.work(paths, run_id=run_id)
    s = summarize(Store(paths.db), run_id)
    (t,) = s["trials"]
    assert t["status"] == "graded", t
    assert t["metrics"]["env"] == {"expects": ["bash"], "missing": missing}
    assert t["metrics"]["n_not_found"] == 1
    assert s["env"]["missing"] == ({"bash": 1} if missing else {})
    assert s["env"]["commands_not_found"] == 1


SYNC_CLI = r"""#!/usr/lib/phantom/bash.real
# Mixes local file edits (like Claude's Write tool) with relayed shell commands.
cat > /dev/null
echo "from-box" > local.txt                              # local write, never through the shell
seen_in_chamber=$(sh -c 'cat local.txt')                 # pushed before the command runs?
sh -c 'echo from-chamber > chamber.txt && mkdir -p d && echo x > d/f'
seen_in_box=$(cat chamber.txt; cat d/f)                  # pulled after the command?
rm local.txt                                             # local delete
gone_in_chamber=$(sh -c 'test -e local.txt && echo still-there || echo gone')
printf 'def add():\n    return 4\n' > solution.py         # last edit: pushed when the agent exits
printf '{"type":"result","subtype":"success","num_turns":1,"total_cost_usd":0,"result":"chamber_saw=%s box_saw=%s delete=%s"}\n' \
  "$seen_in_chamber" "$(echo $seen_in_box)" "$gone_in_chamber"
"""


@pytest.mark.parametrize("link", ["network", "volumes"])
def test_phantom_workspace_stays_in_step_with_the_chamber(root, tmp_path, monkeypatch, link):
    folder = tmp_path / f"fake-sync-{link}"
    folder.mkdir()
    (folder / "fakecli").write_text(SYNC_CLI)
    (folder / "install.Dockerfile").write_text("ARG BASE\nFROM ${BASE}\nCOPY fakecli /usr/local/bin/fakecli\n"
                                               "RUN chmod +x /usr/local/bin/fakecli\n")
    (folder / "agent.yaml").write_text(f"name: fake-sync-{link}\nplacement: phantom\nlink: {link}\n"
                                       "installed: {run: fakecli, output_parser: claude_stream_json}\n")
    monkeypatch.setenv("VALS_HOME", str(tmp_path / "home"))
    paths = service.Paths.from_root(root)
    run_id = service.submit(paths, RunSpec(agent=str(folder), benchmark="python-tasks", task_ids=["py-001"]))
    service.work(paths, run_id=run_id)
    (t,) = summarize(Store(paths.db), run_id)["trials"]
    assert t["status"] == "graded", t
    msg = t["agent"]["message"]
    assert "chamber_saw=from-box" in msg, msg          # box edit reached the chamber before the command
    assert "box_saw=from-chamber x" in msg, msg        # chamber's files (and new dir) reached the box
    assert "delete=gone" in msg, msg                   # a local delete propagated
    assert t["grade"]["passed"], t                     # the final edit was pushed and graded
