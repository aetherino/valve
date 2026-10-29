#!/bin/sh
python3 - <<'PY'
import json
expected = {"north": 59.97, "south": 19.99, "east": 8.75}
try:
    got = json.load(open("/app/report.json"))
    ok = isinstance(got, dict) and set(got) == set(expected) and all(abs(float(got[k]) - v) < 0.005 for k, v in expected.items())
except Exception as e:
    got, ok = repr(e), False
print("got:", got)
open("/logs/verifier/reward.txt", "w").write("1" if ok else "0")
PY
