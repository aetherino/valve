"""saboteur: stops its own chamber, and the grader's sandbox. Expect 0%, no infra errors.

A stopped container used to look like a broken chamber (-> infra_error, excluded
from accuracy). Each task gets one way of stopping the chamber from the inside:
  kill 1          PID 1 (the init) forwards SIGTERM to the keep-alive process
  "pkill tail"    kill the keep-alive process itself (via /proc: no procps on slim)
  kill -9 -1      kill every process it can
Before that it plants a solution.py that kills the *verifier's* PID 1 when imported
(python-tasks' grader imports it). After the chamber is gone it tries one more
command (expects error -32003) and finishes with numbers the harness must keep.
"""
from valve_agent_sdk.client import AgentClient, ChamberError

ATTACKS = [
    "kill 1; sleep 5",
    "for p in /proc/[0-9]*; do [ \"$(cat $p/comm 2>/dev/null)\" = tail ] && kill ${p#/proc/}; done; sleep 5",
    "kill -9 -1; sleep 5",
]

client = AgentClient("saboteur", "1")
attack = ATTACKS[sum(map(ord, client.task.get("id", ""))) % len(ATTACKS)]
client.put("solution.py", "import os, signal\nos.kill(1, signal.SIGTERM)\nimport time\ntime.sleep(5)\n")
codes = []
try:
    client.exec(attack)
except ChamberError as e:
    codes.append(e.code)
try:
    client.exec("echo still here?")
except ChamberError as e:
    codes.append(e.code)
client.finish("submitted", steps=2, input_tokens=100, output_tokens=10, cost_usd=0.25,
              message=f"attack={attack!r} codes={codes}")
