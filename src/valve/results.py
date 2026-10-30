"""Run-level aggregation (DESIGN.md §8.2).

accuracy = mean over attempts of (passed / graded) for that attempt.
SEM is taken over per-attempt scores when there are repeats (Vals' method for
multi-run benchmarks), otherwise over task-level scores. Infra errors never
count toward accuracy; they are reported separately.
"""

from __future__ import annotations

import json
import math
import statistics
from collections import defaultdict
from typing import Any

from valve.store import TERMINAL, Store


def _sem(xs: list[float]) -> float | None:
    return statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None


def summarize(store: Store, run_id: str) -> dict[str, Any]:
    run = store.run(run_id)
    if run is None:
        raise KeyError(f"no such run: {run_id}")
    rows = store.trial_rows(run_id)
    counts = store.status_counts(run_id)
    graded = [r for r in rows if r["status"] == "graded"]

    by_attempt: dict[int, list[int]] = defaultdict(list)
    for r in graded:
        by_attempt[r["attempt"]].append(r["passed"])
    per_attempt = [sum(v) / len(v) for _, v in sorted(by_attempt.items())]
    mean = statistics.fmean(per_attempt) if per_attempt else None
    sem = _sem(per_attempt) if len(per_attempt) > 1 else _sem([float(r["passed"]) for r in graded])

    tasks: dict[str, dict[str, int]] = defaultdict(lambda: {"passed": 0, "graded": 0, "infra_error": 0})
    cost = 0.0
    for r in rows:
        t = tasks[r["task_id"]]
        if r["status"] == "graded":
            t["graded"] += 1
            t["passed"] += r["passed"]
        elif r["status"] == "infra_error":
            t["infra_error"] += 1
        cost += r["spent_usd"]  # every try, including ones that ended in infra errors

    started = [r["started_at"] for r in rows if r["started_at"]]
    finished = [r["finished_at"] for r in rows if r["finished_at"]]
    done = all(r["status"] in TERMINAL for r in rows)
    return {
        "run_id": run_id,
        "spec": json.loads(run["spec_json"]),
        "status": "completed" if done else ("queued" if set(counts) == {"queued"} else "running"),
        "counts": counts,
        "accuracy": {"mean": mean, "sem": sem, "per_attempt": per_attempt, "n_graded": len(graded)},
        "cost_usd": round(cost, 4),
        "duration_s": round(max(finished) - min(started), 1) if done and started and finished else None,
        "tasks": [{"task_id": k, **v} for k, v in sorted(tasks.items())],
        "env": _env_summary(rows),
        "trials": [_trial(r) for r in rows],
    }


def _env_summary(rows: list[Any]) -> dict[str, Any]:
    """Environment mismatches across the run: declared expectations the chamber didn't
    meet, and commands that came back "not found" (rc 127)."""
    missing: dict[str, int] = defaultdict(int)
    checked = not_found = trials_not_found = 0
    for r in rows:
        if r["status"] != "graded":
            continue
        metrics = json.loads(r["metrics_json"] or "null") or {}
        if env := metrics.get("env"):
            checked += 1
            for prog in env.get("missing", []):
                missing[prog] += 1
        n = metrics.get("n_not_found") or 0
        not_found += n
        trials_not_found += 1 if n else 0
    return {"trials_checked": checked, "missing": dict(missing),
            "commands_not_found": not_found, "trials_with_not_found": trials_not_found}


def _trial(r: Any) -> dict[str, Any]:
    load = lambda s: json.loads(s) if s else None  # noqa: E731
    return {
        "trial_id": r["id"], "task_id": r["task_id"], "attempt": r["attempt"], "status": r["status"],
        "tries": r["tries"], "infra_attempts": r["infra_attempts"],
        "spent_usd": round(r["spent_usd"], 6), "grade": load(r["grade_json"]), "agent": load(r["agent_json"]),
        "metrics": load(r["metrics_json"]), "error": load(r["error_json"]), "artifacts": r["artifacts"],
    }
