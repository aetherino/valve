import pytest

from vals_harness.results import summarize
from vals_harness.specs import Policy, RunSpec, resolve_trials
from vals_harness.store import LostLease, Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "h.db")


def _enqueue(store, root, attempts=2, retries=2):
    run = RunSpec(agent="agents/oracle", benchmark="python-tasks", attempts=attempts,
                  policy=Policy(infra_retries=retries))
    trials = resolve_trials(run, "r1", root, "test")
    store.create_run("r1", run, trials)
    return trials


def _graded(passed, cost=0.1):
    return {"status": "graded", "grade": {"passed": passed, "score": float(passed), "details": {}},
            "metrics": {"cost_usd": cost}}


INFRA = {"status": "infra_error", "error": {"kind": "docker", "message": "boom"}, "metrics": {"cost_usd": 0.05}}


def test_claim_is_exclusive_and_fifo(store, root):
    trials = _enqueue(store, root)
    claimed = [store.claim("w", 60) for _ in range(len(trials))]
    assert [c.spec.trial_id for c in claimed] == [t.trial_id for t in sorted(trials, key=lambda t: t.trial_id)]
    assert len({c.token for c in claimed}) == len(trials)
    assert store.claim("w", 60) is None


def test_stale_worker_is_fenced_out(store, root):
    _enqueue(store, root, attempts=1)
    a = store.claim("worker-a", lease_s=-1)          # A's lease is already expired
    b = store.claim("worker-b", lease_s=60)          # B takes the trial over
    assert b.spec.trial_id == a.spec.trial_id and b.try_no == 2
    with pytest.raises(LostLease):
        store.set_status(a.spec.trial_id, a.token, "verifying")
    assert not store.heartbeat(a.spec.trial_id, a.token, 60)
    assert store.finish(a.spec.trial_id, a.token, _graded(True)) is None   # A's result is dropped
    assert store.finish(b.spec.trial_id, b.token, _graded(False)) == "graded"
    row = store.db.execute("SELECT r.passed, t.infra_attempts FROM trials t JOIN results r ON r.trial_id=t.id"
                           " WHERE t.id=?", (b.spec.trial_id,)).fetchone()
    assert (row[0], row[1]) == (0, 1)


def test_trial_that_keeps_losing_workers_is_closed(store, root):
    _enqueue(store, root, attempts=1, retries=1)
    tid = store.claim("w1", lease_s=-1).spec.trial_id   # dies
    store.claim("w2", lease_s=-1)                         # reclaimed (infra 1 <= 1), dies again
    # The third claim would exceed the retry budget: closed instead of handed out again.
    nxt = store.claim("w3", lease_s=60)
    assert nxt is None or nxt.spec.trial_id != tid
    status = store.db.execute("SELECT status FROM trials WHERE id=?", (tid,)).fetchone()[0]
    assert status == "infra_error"


def test_infra_errors_retry_then_stick_and_spend_is_kept(store, root):
    _enqueue(store, root, attempts=1, retries=1)
    c = store.claim("w", 60)
    assert store.finish(c.spec.trial_id, c.token, INFRA) == "queued"
    c = store.claim("w", 60)
    assert c.try_no == 2
    assert store.finish(c.spec.trial_id, c.token, INFRA) == "infra_error"
    spent = store.db.execute("SELECT spent_usd FROM trials WHERE id=?", (c.spec.trial_id,)).fetchone()[0]
    assert spent == pytest.approx(0.10)                   # both failed tries cost money


def test_cancel_only_touches_queued_trials(store, root):
    _enqueue(store, root, attempts=1)
    running = store.claim("w", 60)
    assert store.cancel_run("r1") == 2
    assert store.status_counts("r1") == {"preparing": 1, "cancelled": 2}
    assert store.finish(running.spec.trial_id, running.token, _graded(True)) == "graded"


def test_accuracy_and_sem_over_attempts_exclude_infra_errors(store, root):
    _enqueue(store, root, attempts=2, retries=0)
    outcomes = {(1, "py-001"): True, (1, "py-002"): True, (1, "py-003"): False,   # attempt 1 -> 2/3
                (2, "py-001"): True, (2, "py-002"): False, (2, "py-003"): None}   # attempt 2 -> 1/2 + infra
    while (c := store.claim("w", 60)) is not None:
        o = outcomes[(c.spec.attempt, c.spec.task.id)]
        store.finish(c.spec.trial_id, c.token, INFRA if o is None else _graded(o))
    s = summarize(store, "r1")
    assert s["status"] == "completed"
    assert s["counts"] == {"graded": 5, "infra_error": 1}
    assert s["accuracy"]["per_attempt"] == pytest.approx([2 / 3, 1 / 2])
    assert s["accuracy"]["mean"] == pytest.approx(7 / 12)
    assert s["accuracy"]["sem"] == pytest.approx(abs(2 / 3 - 1 / 2) / 2)  # stdev of 2 pts / sqrt(2)
    assert s["cost_usd"] == pytest.approx(5 * 0.1 + 0.05)                # infra try's spend included
