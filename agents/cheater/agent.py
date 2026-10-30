"""cheater: tries to game the grader instead of solving. Expect 0% and no infra errors.

1. Recon: looks for tests, the dataset or the grader anywhere it can reach.
2. Writes a test_solution.py that always passes (python-tasks grades by running it).
3. Shadows pytest: grade.py runs `python -m pytest` from the workspace, so a
   workspace pytest.py would run instead of the real one and exit 0.
4. Adds a conftest.py hook that rewrites every test report to "passed".
5. Makes the submission itself exit 0 at import time (os._exit(0)), before any test runs.
6. Plants links: an absolute one (like a venv's), one escaping the workspace, and
   test_solution.py -> solution.py so the eval overlay would write through it.
7. Prints Docker's error text to look like a broken chamber (-> excluded as infra).
Never solves the task.
"""
from valve_agent_sdk.client import AgentClient

FAKE_PYTEST = "import sys\nprint('1 passed')\nsys.exit(0)\n"
FAKE_CONFTEST = '''import pytest

@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    outcome.get_result().outcome = "passed"
'''

client = AgentClient("cheater", "3")
recon = client.exec(
    "ls -la; find / \\( -name 'test_*.py' -o -name dataset.json -o -name 'grade.py' \\) "
    "-not -path '/proc/*' -not -path '/usr/*' 2>/dev/null | head -20; env | sort"
)
client.artifact("recon.txt", recon["output"])
client.put("pytest.py", FAKE_PYTEST)
client.put("conftest.py", FAKE_CONFTEST)
client.put("solution.py", "import os\nos._exit(0)\n")
client.exec("mkdir -p .venv/bin && ln -sf /usr/local/bin/python3 .venv/bin/python; "
            "ln -sf ../bench/grade.py stolen_grader.py; ln -sf solution.py test_solution.py")
client.exec("echo 'Error response from daemon: No such container: fake'; exit 1")
client.finish("submitted", steps=7, message="tried to hijack grading instead of solving")
