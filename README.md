# Francisco's Take-home assignment
NOTE: This document is human-written; the Setup and Run sections were added, and the Phantom Box section updated, with Claude's help to match the current code.

## Setup

**Requirements:** macOS or Linux, [Docker](https://docs.docker.com/get-docker/) with the daemon running (tested on Docker Desktop), and [uv](https://docs.astral.sh/uv/) (it fetches Python 3.11 itself). No Rust toolchain is needed: the phantom binary is built inside Docker.

```bash
git clone <this repo> vals && cd vals
uv sync
```

**API key** (only for the real agents; the test agents cost $0). Create `.env` in the repo root:

```bash
ANTHROPIC_API_KEY=sk-ant-...
# only if your key isn't scoped to a workspace:
ANTHROPIC_WORKSPACE_ID=wrkspc_...
```

`.env` is git-ignored, and a value only reaches agents that declare it in their `agent.yaml`.

**Check the install** ($0, no API key needed):

```bash
uv run harness run --agent agents/oracle --benchmark bash-operations --wait   # expect accuracy 1.000
uv run pytest -q                                                              # ~2.5 min, needs Docker
```

The first run of each agent builds its image (mini-swe-agent ≈ 5 min, Claude Code layers ≈ 1–2 min, test agents seconds); later runs reuse them.

## Run

**Run and wait** (an in-process worker; good for trying things):

```bash
uv run harness run --agent agents/mini-swe-agent --benchmark bash-operations --wait
uv run harness run --agent agents/claude-code-phantom --benchmark python-tasks --task_ids py-002 --wait --follow
```

`--follow` prints every command as it runs in the chamber; `--slots N` runs N trials at once.

**Queue, then work** (the asynchronous path):

```bash
uv run harness run --agent agents/mini-swe-agent --benchmark python-tasks --attempts 3   # prints a run_id, returns at once
uv run harness run --agent agents/claude-code --benchmark system-tasks
uv run harness worker --slots 4 --once --follow    # --once: exit when the queue is empty; omit to keep serving
uv run harness status                              # active runs
uv run harness status --run_id <run_id>            # one run, per trial
uv run harness results --run_id <run_id> --format table   # json (default) | table | summary
uv run harness cancel --run_id <run_id>            # cancel queued trials
uv run harness prune --dry-run                     # outdated images and leftover containers
```

Several workers can run at once (e.g. in two terminals); they share one queue.

**Agents** (`--agent agents/<name>`):

| Agent | Placement | Cost |
|---|---|---|
| `mini-swe-agent` | sidecar container | ≈ $0.02 / task (Opus 5.5) |
| `claude-code` | installed in the chamber | ≈ $0.04–0.06 / task |
| `claude-code-phantom` | phantom box (Chamber API over the network) | ≈ $0.04–0.06 / task |
| `oracle` · `noop` · `cheater` · `vandal` · `saboteur` | sidecar test agents | $0 (expect 100% · 0% · 0% · 0% · 0%) |

**Benchmarks** (`--benchmark <name>`): `bash-operations` and `python-tasks` (the starter's, unchanged) and `system-tasks` (Harbor-style task folders: a running service, an asset file, a command on PATH).

**Useful options:** `--model anthropic/claude-haiku-5-5` (mini-swe-agent takes litellm names; Claude Code takes `claude-haiku-5-5`), `--limit timeout_s=300 --limit step_limit=20` (repeatable), `--expose-eval-files`.

**Environment variables:**

| Variable | Default | Effect |
|---|---|---|
| `VALS_MAX_ACTIVE_TRIALS` | unset | cap on running trials across all workers (each trial runs ≤ 3 containers) |
| `VALS_HOME` | `.vals/` | where the database and artifacts live |
| `VALS_CHAMBER_API_BIND` / `VALS_CHAMBER_API_PUBLIC` | `127.0.0.1` / `host.docker.internal` | where the phantom box's Chamber API listens / what address the box dials |

**Where results go:** `.vals/harness.db` (runs, trials, results, events) and one folder per try, `.vals/artifacts/<run>/<trial>/try<N>/`, holding `manifest.json`, `transcript.jsonl`, `agent_logs/`, `workspace.tar`, `verifier.log` and `result.json`.

## Eval harness: design philosofy
**Best effort isolation**: I took the desing in the direction of making agents be able to be run inside their own sandboxes, given that some of the agents one might test would require dependencies not normally found inside the specified images.

For example, if a lab requests `alpine` as image, but asks to test on claude code, then we'd be polluting the stock alpine image in order to make claude code run. Or, the grading logic could require python, which is again not found in the default alpine image.

This system does a best effort to not corrupt or modify the specified image, even if it calls for more computational resources to isolate an agent, or its subsequent grading.

Under an "intended" run where only the model is the secret (e.g not Claude Code's sourcecode) there would be a total of four kinds of containers:

1. tooling container (for setup)
2. agent container (optional)
3. test chamber
4. grading container (optional)

The harness circumvents the different shcemas of the benchmarks by mandating a wrapping .yaml
to map a benchmark's fields to a common schema.

NOTE: This system dubs a container in which a trial happens as a "chamber".

There's a total of 4 manifest files needed to adapt a benchmark and it's agent to this system:

### BenchmarkSpec
- Defines dataset fields
- Eval paths
- Grader network access and secrets
- Tooling
- Dockerfile image
- Preparation
- Result parser
- Passing requirements
etc. 

### AgentSpec
- Placement of the agent e.g sidecar, in-chamber, remote, host
- Runtime secrets
- Limits
- Model
- Phantom

### RunSpec
- Cost limits
- Attempts
- Network 
- Policy (expose eval files, infra retries)
- Retries

### TrialSpec
- Merge of the prior 3 specs
- 

This design allowed for the execution of Claude Code, which wouldn't normally run in the requested alpine image without compromising it:
- If installed, it would require node

## Phantom Box
Labs and vendors might want to keep unreleased harnesses to themselves, which is not something that the take-home assingment requirement-abiding system would normally allow. 

My exploration on this problem is the Phantom Box: a container that runs a stock agent CLI (Claude Code here) next to the chamber, and that:
1. is built from the chamber's own image (plus the agent's runtime), so the agent sees the same OS and files
2. replaces `sh`/`bash` with a "phantom shell" (a small Rust binary) that streams every shell command to the real, Vals-hosted chamber
3. talks to the chamber only through the Chamber API, over the network (TCP plus a one-time token per trial)
4. keeps its own copy of the workspace instead of sharing a filesystem, synced with the chamber at every command boundary: pull everything at start, push the agent's own edits before each command, pull the chamber's changes after it, push once more at exit.

The chamber itself stays the untouched task image, offline, with no secrets: the API key and the network access live only in the phantom box.

Today the box runs on the same Docker host as the worker, but nothing ties it there. Pointed at a reachable Chamber API (behind TLS), the same box could run on a vendor's own infrastructure. That would let a vendor run Vals benchmarks without sharing a secret harness, while Vals keeps the chamber, the tests and the grading on its side.

Known gaps: one sync message is capped at 24 MB (big repos would need incremental sync or a FUSE mirror), and the box has bash while alpine chambers only have `sh`, which is flagged in the results rather than patched.
