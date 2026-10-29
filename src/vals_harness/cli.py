"""CLI: thin layer over service.py — parse flags, call the service, print."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click

from vals_harness import results as results_mod
from vals_harness import service
from vals_harness.specs import LIMIT_KEYS, Policy, RunSpec
from vals_harness.store import Store

root_option = click.option("--root", type=click.Path(file_okay=False, path_type=Path), default=".",
                           show_default=True, help="Folder containing benchmarks/ and agents/.")


@click.group()
def cli() -> None:
    """Agentic harness: run any agent against any benchmark."""


@cli.command()
@click.option("--agent", required=True, help="Agent folder, e.g. agents/mini-swe-agent")
@click.option("--benchmark", required=True, help="Benchmark name or folder, e.g. bash-operations")
@click.option("--task_ids", "--task-ids", default=None, help="Comma-separated task ids (default: all)")
@click.option("--model", default=None, help="Model override, e.g. anthropic/claude-opus-5-5")
@click.option("--attempts", default=1, show_default=True, help="Trials per task (for error bars)")
@click.option("--limit", "limits", multiple=True, metavar="KEY=VALUE",
              help=f"Limit override, repeatable. Keys: {', '.join(sorted(LIMIT_KEYS))}")
@click.option("--expose-eval-files", is_flag=True, help="Let the agent see the tests (default: hidden)")
@click.option("--wait", is_flag=True, help="Run the trials in this process and print results")
@root_option
def run(agent: str, benchmark: str, task_ids: str | None, model: str | None, attempts: int,
        limits: tuple[str, ...], expose_eval_files: bool, wait: bool, root: Path) -> None:
    """Run an agent against a benchmark."""
    paths = service.Paths.from_root(root)
    spec = RunSpec(
        agent=agent, benchmark=benchmark,
        task_ids=[t.strip() for t in task_ids.split(",")] if task_ids else None,
        model=model, attempts=attempts, limits=_parse_limits(limits),
        policy=Policy(expose_eval_files=expose_eval_files),
    )
    run_id = service.submit(paths, spec)
    click.echo(f"run_id: {run_id}")
    if not wait:
        click.echo("queued. start a worker with `harness worker`, then `harness status --run_id " + run_id + "`")
        return

    def show(result: dict[str, Any], status: str | None) -> None:
        click.echo("  " + _trial_line(result, status))

    service.work(paths, run_id=run_id, on_result=show)
    _print_summary(results_mod.summarize(Store(paths.db), run_id))


@cli.command()
@click.option("--run_id", "--run-id", default=None, help="A run id; omit to list active runs")
@root_option
def status(run_id: str | None, root: Path) -> None:
    """Check the status of a benchmark run (or list active runs)."""
    store = Store(service.Paths.from_root(root).db)
    if run_id is None:
        rows = store.runs(active_only=True)
        if not rows:
            click.echo("no active runs")
        for r in rows:
            spec = json.loads(r["spec_json"])
            click.echo(f"{r['id']}  {spec['agent']} × {spec['benchmark']}  {_counts(store.status_counts(r['id']))}")
        return
    s = results_mod.summarize(store, run_id)
    click.echo(f"{run_id}: {s['status']}  {_counts(s['counts'])}")
    for t in s["trials"]:
        click.echo(f"  {t['task_id']:<12} a{t['attempt']}  {t['status']}")


@cli.command()
@click.option("--run_id", "--run-id", required=True)
@click.option("--format", "fmt", type=click.Choice(["json", "table", "summary"], case_sensitive=False),
              default="json", show_default=True)
@root_option
def results(run_id: str, fmt: str, root: Path) -> None:
    """Get results from a completed run."""
    s = results_mod.summarize(Store(service.Paths.from_root(root).db), run_id)
    if fmt == "json":
        click.echo(json.dumps(s, indent=2, default=str))
    elif fmt == "table":
        click.echo(f"{'task':<12} {'att':>3}  {'status':<12} {'pass':<5} {'exit':<11} {'cmds':>4} {'cost':>7} {'secs':>6}")
        for t in s["trials"]:
            m, a = t["metrics"] or {}, t["agent"] or {}
            passed = "" if t["grade"] is None else ("yes" if t["grade"]["passed"] else "no")
            click.echo(f"{t['task_id']:<12} {t['attempt']:>3}  {t['status']:<12} {passed:<5} "
                       f"{a.get('exit_status', ''):<11} {m.get('n_commands', ''):>4} "
                       f"{m.get('cost_usd', 0):>7.3f} {m.get('duration_s', 0):>6.1f}")
        _print_summary(s)
    else:
        _print_summary(s)


@cli.command()
@click.option("--once", is_flag=True, help="Exit when the queue is empty instead of waiting for work")
@root_option
def worker(once: bool, root: Path) -> None:
    """Start a worker that executes queued trials."""
    paths = service.Paths.from_root(root)
    click.echo("worker started" + (" (exits when queue is empty)" if once else "; Ctrl-C to stop"))
    service.work(paths, stop_when_empty=once,
                 on_result=lambda r, st: click.echo("  " + r["run_id"] + "  " + _trial_line(r, st)))


@cli.command()
@click.option("--run_id", "--run-id", required=True)
@root_option
def cancel(run_id: str, root: Path) -> None:
    """Cancel a run's queued trials (running trials finish normally)."""
    n = service.cancel(service.Paths.from_root(root), run_id)
    click.echo(f"cancelled {n} queued trial(s) of {run_id}")


# ---------------------------------------------------------------------- helpers


def _parse_limits(pairs: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs:
        key, sep, value = p.partition("=")
        if not sep:
            raise click.BadParameter(f"expected KEY=VALUE, got {p!r}", param_hint="--limit")
        out[key] = value  # pydantic coerces "30" -> 30 when the Limits are built
    return out


def _counts(c: dict[str, int]) -> str:
    return " ".join(f"{k}={v}" for k, v in sorted(c.items()))


def _trial_line(r: dict[str, Any], status: str | None) -> str:
    if status is None:
        return f"{r['task_id']:<12} a{r['attempt']}  lost its lease; result dropped (another worker owns it)"
    if status == "queued":
        return f"{r['task_id']:<12} a{r['attempt']}  infra_error, requeued: {r['error']['message'][:80]}"
    if r["status"] == "infra_error":
        return f"{r['task_id']:<12} a{r['attempt']}  INFRA_ERROR [{r['error']['kind']}] {r['error']['message'][:80]}"
    agent = r.get("agent") or {}
    mark = "PASS" if r["grade"]["passed"] else "fail"
    return (f"{r['task_id']:<12} a{r['attempt']}  {mark}  agent={agent.get('exit_status')} "
            f"cmds={r['metrics'].get('n_commands', 0)} {r['metrics']['duration_s']:.1f}s")


def _print_summary(s: dict[str, Any]) -> None:
    acc = s["accuracy"]
    if acc["mean"] is None:
        line = "accuracy: n/a (no graded trials)"
    else:
        sem = f" ± {acc['sem']:.3f}" if acc["sem"] is not None else ""
        line = f"accuracy: {acc['mean']:.3f}{sem}  (graded={acc['n_graded']}, per attempt={[round(x, 3) for x in acc['per_attempt']]})"
    click.echo(f"\n{s['run_id']}  {s['spec']['agent']} × {s['spec']['benchmark']}  [{s['status']}]")
    click.echo(line)
    click.echo(f"trials: {_counts(s['counts'])}   cost: ${s['cost_usd']:.4f}"
               + (f"   wall: {s['duration_s']}s" if s["duration_s"] is not None else ""))


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
