"""SQLite store: runs, trials (which double as the queue), results, events (DESIGN.md §9).

The DB is the queue. A worker claims the oldest queued trial (or one whose lease
expired because its worker died) inside a write transaction, so two workers can
never claim the same trial. Each claim gets a fresh token; every later write for
that trial must present it (a fencing token), so a worker that lost its lease
can't overwrite the work of the worker that took over. Postgres would replace
this behind the same methods (`FOR UPDATE SKIP LOCKED`).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vals_harness.specs import RunSpec, TrialSpec

ACTIVE = ("preparing", "running_agent", "verifying")
TERMINAL = ("graded", "infra_error", "cancelled")
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    spec_json   TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS trials (
    id             TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL REFERENCES runs(id),
    task_id        TEXT NOT NULL,
    attempt        INTEGER NOT NULL,
    status         TEXT NOT NULL,
    tries          INTEGER NOT NULL DEFAULT 0,   -- claims so far (1 + infra retries)
    infra_attempts INTEGER NOT NULL DEFAULT 0,   -- tries that ended in a harness failure
    max_infra      INTEGER NOT NULL,
    spent_usd      REAL NOT NULL DEFAULT 0,      -- model spend summed over every try
    spec_json      TEXT NOT NULL,
    manifest_hash  TEXT NOT NULL,
    claim_token    TEXT,                         -- fencing token of the current claim
    worker_id      TEXT,
    lease_until    REAL,
    created_at     REAL NOT NULL,
    started_at     REAL,
    finished_at    REAL
);
CREATE INDEX IF NOT EXISTS trials_queue ON trials(status, created_at);
CREATE INDEX IF NOT EXISTS trials_run ON trials(run_id);
CREATE TABLE IF NOT EXISTS results (
    trial_id      TEXT PRIMARY KEY REFERENCES trials(id),
    passed        INTEGER,
    score         REAL,
    grade_json    TEXT,
    agent_json    TEXT,
    metrics_json  TEXT,
    error_json    TEXT,
    artifacts     TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trial_id      TEXT NOT NULL,
    try_no        INTEGER NOT NULL,
    ts            REAL NOT NULL,
    type          TEXT NOT NULL,
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_trial ON events(trial_id, id);
"""


class LostLease(Exception):
    """This worker's claim was taken over (its lease expired). Stop and write nothing."""


@dataclass(frozen=True)
class Claim:
    spec: TrialSpec
    token: str      # fencing token: present it on every write for this trial
    try_no: int     # 1 for the first try, 2+ after infra retries


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path.resolve()
        # Every container of this home is labeled vals.home=<home_id>, so reapers of other
        # homes (another VALS_HOME, a test run) never touch it.
        self.home_id = hashlib.sha256(str(self.path).encode()).hexdigest()[:12]
        # isolation_level=None: autocommit; we open explicit transactions where it matters.
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        has_tables = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='trials'").fetchone()
        if has_tables and version != SCHEMA_VERSION:
            raise RuntimeError(f"{path} uses an older schema (v{version}); move it aside or delete it")
        self.db.executescript(SCHEMA)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    # ------------------------------------------------------------------ writes

    def create_run(self, run_id: str, spec: RunSpec, trials: list[TrialSpec]) -> None:
        now = time.time()
        with self._tx():
            self.db.execute("INSERT INTO runs VALUES (?, ?, ?)", (run_id, spec.model_dump_json(), now))
            self.db.executemany(
                "INSERT INTO trials (id, run_id, task_id, attempt, status, max_infra, spec_json, manifest_hash,"
                " created_at) VALUES (?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
                [(t.trial_id, run_id, t.task.id, t.attempt, t.policy.infra_retries, t.model_dump_json(),
                  t.manifest_hash(), now) for t in trials],
            )

    def claim(self, worker_id: str, lease_s: float, run_id: str | None = None) -> Claim | None:
        """Atomically take the next trial: oldest queued, or an active one whose lease expired.

        A reclaimed trial (its worker died) counts as one infra attempt; once those run
        out it is closed as infra_error instead of being handed out again, so a trial
        that keeps killing workers can't loop forever.
        """
        with self._tx():
            while True:
                now = time.time()
                row = self.db.execute(
                    f"SELECT id, status, infra_attempts, max_infra, tries FROM trials"
                    f" WHERE (status = 'queued' OR (status IN {ACTIVE} AND lease_until < ?))"
                    f" AND (? IS NULL OR run_id = ?)"
                    f" ORDER BY created_at, id LIMIT 1",
                    (now, run_id, run_id),
                ).fetchone()
                if row is None:
                    return None
                infra = row["infra_attempts"] + (1 if row["status"] in ACTIVE else 0)
                if infra > row["max_infra"]:
                    self._close_infra(row["id"], {"kind": "worker_lost",
                                                  "message": f"worker lost {infra} times; giving up"}, infra)
                    continue
                token, try_no = uuid.uuid4().hex, row["tries"] + 1
                self.db.execute(
                    "UPDATE trials SET status='preparing', claim_token=?, worker_id=?, lease_until=?,"
                    " started_at=?, infra_attempts=?, tries=? WHERE id=?",
                    (token, worker_id, now + lease_s, now, infra, try_no, row["id"]),
                )
                spec_json = self.db.execute("SELECT spec_json FROM trials WHERE id=?", (row["id"],)).fetchone()[0]
                return Claim(TrialSpec.model_validate_json(spec_json), token, try_no)

    def heartbeat(self, trial_id: str, token: str, lease_s: float) -> bool:
        cur = self.db.execute("UPDATE trials SET lease_until=? WHERE id=? AND claim_token=?",
                              (time.time() + lease_s, trial_id, token))
        return cur.rowcount == 1

    def set_status(self, trial_id: str, token: str, status: str) -> None:
        cur = self.db.execute("UPDATE trials SET status=? WHERE id=? AND claim_token=?", (status, trial_id, token))
        if cur.rowcount != 1:
            raise LostLease(trial_id)

    def finish(self, trial_id: str, token: str, result: dict[str, Any]) -> str | None:
        """Record a try's result. Infra errors are requeued until retries run out.

        Returns the trial's new status, or None if this claim was fenced out (another
        worker owns the trial now), in which case nothing is written.
        """
        spent = float((result.get("metrics") or {}).get("cost_usd") or 0.0)
        with self._tx():
            row = self.db.execute("SELECT infra_attempts, max_infra FROM trials WHERE id=? AND claim_token=?",
                                  (trial_id, token)).fetchone()
            if row is None:
                return None
            # Spend is real money whether or not this try counts, so it's always summed.
            self.db.execute("UPDATE trials SET spent_usd = spent_usd + ? WHERE id=?", (spent, trial_id))
            if result["status"] == "infra_error":
                infra = row["infra_attempts"] + 1
                self._event(trial_id, 0, "infra_error", {**result["error"], "infra_attempts": infra})
                if infra <= row["max_infra"]:
                    self.db.execute("UPDATE trials SET status='queued', infra_attempts=?, claim_token=NULL,"
                                    " worker_id=NULL, lease_until=NULL WHERE id=?", (infra, trial_id))
                    return "queued"
                self.db.execute("UPDATE trials SET infra_attempts=? WHERE id=?", (infra, trial_id))
                status = "infra_error"
            else:
                status = result["status"]
            self._write_result(trial_id, status, result)
        return status

    def release(self, trial_id: str, token: str) -> bool:
        """Give a claimed trial back to the queue without charging an infra attempt
        (the worker is shutting down cleanly). Fenced like every other write."""
        cur = self.db.execute("UPDATE trials SET status='queued', claim_token=NULL, worker_id=NULL,"
                              " lease_until=NULL WHERE id=? AND claim_token=?", (trial_id, token))
        return cur.rowcount == 1

    def cancel_run(self, run_id: str) -> int:
        """Cancel trials not yet started. Running trials finish normally."""
        with self._tx():
            ids = [r[0] for r in self.db.execute("SELECT id FROM trials WHERE run_id=? AND status='queued'",
                                                 (run_id,))]
            for tid in ids:
                self._write_result(tid, "cancelled", {"status": "cancelled"})
        return len(ids)

    def live_claims(self) -> set[str]:
        """Claim tokens of trials that are running under an unexpired lease."""
        rows = self.db.execute(f"SELECT claim_token FROM trials WHERE status IN {ACTIVE} AND lease_until >= ?",
                               (time.time(),))
        return {r[0] for r in rows if r[0]}

    def event(self, trial_id: str, try_no: int, type: str, payload: dict[str, Any]) -> None:
        self._event(trial_id, try_no, type, payload)

    def _event(self, trial_id: str, try_no: int, type: str, payload: dict[str, Any]) -> None:
        self.db.execute("INSERT INTO events (trial_id, try_no, ts, type, payload_json) VALUES (?, ?, ?, ?, ?)",
                        (trial_id, try_no, time.time(), type, json.dumps(payload, default=str)))

    def _close_infra(self, trial_id: str, error: dict[str, str], infra: int) -> None:
        self.db.execute("UPDATE trials SET infra_attempts=? WHERE id=?", (infra, trial_id))
        self._event(trial_id, 0, "infra_error", {**error, "infra_attempts": infra})
        self._write_result(trial_id, "infra_error", {"status": "infra_error", "error": error})

    def _write_result(self, trial_id: str, status: str, result: dict[str, Any]) -> None:
        grade = result.get("grade") or {}
        self.db.execute(
            "INSERT OR REPLACE INTO results VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (trial_id,
             None if not grade else int(grade["passed"]),
             grade.get("score"),
             json.dumps(grade or None),
             json.dumps(result.get("agent")),
             json.dumps(result.get("metrics")),
             json.dumps(result.get("error")),
             result.get("artifacts")),
        )
        self.db.execute("UPDATE trials SET status=?, finished_at=?, lease_until=NULL, claim_token=NULL WHERE id=?",
                        (status, time.time(), trial_id))

    # ------------------------------------------------------------------ reads

    def run(self, run_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()

    def runs(self, active_only: bool = False, limit: int = 20) -> list[sqlite3.Row]:
        where = f"WHERE id IN (SELECT run_id FROM trials WHERE status NOT IN {TERMINAL})" if active_only else ""
        return self.db.execute(f"SELECT * FROM runs {where} ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

    def status_counts(self, run_id: str) -> dict[str, int]:
        rows = self.db.execute("SELECT status, COUNT(*) n FROM trials WHERE run_id=? GROUP BY status", (run_id,))
        return {r["status"]: r["n"] for r in rows}

    def trial_rows(self, run_id: str) -> list[sqlite3.Row]:
        return self.db.execute(
            "SELECT t.id, t.task_id, t.attempt, t.status, t.tries, t.infra_attempts, t.spent_usd, t.started_at,"
            " t.finished_at, r.passed, r.score, r.grade_json, r.agent_json, r.metrics_json, r.error_json, r.artifacts"
            " FROM trials t LEFT JOIN results r ON r.trial_id = t.id"
            " WHERE t.run_id=? ORDER BY t.attempt, t.task_id", (run_id,)).fetchall()

    # ------------------------------------------------------------------ helpers

    def _tx(self) -> _Tx:
        return _Tx(self.db)


class _Tx:
    """BEGIN IMMEDIATE takes the write lock up front, so claim() is race-free across processes."""

    def __init__(self, db: sqlite3.Connection):
        self.db = db

    def __enter__(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")

    def __exit__(self, exc_type: object, *_: object) -> None:
        self.db.execute("ROLLBACK" if exc_type else "COMMIT")
