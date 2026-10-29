python3 - <<'PY'
import csv, json
tot = {}
for r in csv.DictReader(open("data/sales.csv")):
    tot[r["region"]] = tot.get(r["region"], 0) + int(r["units"]) * float(r["unit_price"])
json.dump({k: round(v, 2) for k, v in tot.items()}, open("report.json", "w"))
PY
