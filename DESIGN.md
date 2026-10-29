# Vals Agentic Harness — Design Spec (draft v1)

Status: draft for discussion. **(proposed)** marks decisions still open to change; §15 lists open questions. Diagram: `architecture.excalidraw`.

## 0. Design philosophy

1. **Protect the experiment.** The task image is the experiment: never modified for sidecar agents, and every modification by an installed agent is recorded. Tests never enter the chamber; a fresh verifier re-overlays them, so the agent can't read or tamper with them. When a grader *runs* code from the workspace, only the declared submission (`submission_paths`) is carried over, so planted files (`pytest.py`, `conftest.py`) can't hijack grading; `pass_requires` makes a pass also show the grader's own evidence (pytest's summary), so a submission can't fake one by exiting 0.
2. **Grade outcomes, record everything.** Scores come from the final workspace, not from what the agent says. Every action and model call is kept for transcript analysis.
3. **Separate the brain from the hands.** The agent decides; the harness executes. Agents act only through the Chamber API, at the tool level, never with direct access to the chamber.
4. **Agent code never sits next to privilege.** Third-party agent code runs in its own container, never in the worker process that holds Docker and the DB.
5. **Protocol first, transport later.** Everything between harness and agent is a versioned message (`vals-agent/1`). Stdio today, WebSocket tomorrow; nothing depends on shared files or local processes.
6. **Ports and adapters.** Every external dependency sits behind an interface with one v1 implementation (§5.3). Growth = new implementation, not a core change.
7. **Each party owns its config.** Benchmark authors own BenchmarkSpec, agent authors own AgentSpec, the experimenter owns RunSpec. They merge into a frozen manifest that can reproduce the trial.
8. **Honest numbers.** Agent failures count; harness failures are retried and excluded — and nothing an agent does can make its failure look like a harness failure. Accuracy is reported with error bars over repeated attempts, the way Vals reports.
9. **Follow the pros; deviate on purpose.** Claude Code installed in the chamber (Harbor, Inspect SWE); mini-swe-agent as the fixed harness (Vals). Sidecar for external agents is our deliberate deviation, for isolation.
10. **Simplest infrastructure that works on one machine.** SQLite as queue and DB, worker on the host, one child process per trial. Constraints are tested, not assumed (the Docker Desktop socket test led to stdio).
11. **Consistency over invisibility.** Eval awareness can't be eliminated. Aim for no harness artifacts and a consistent world the agent can't catch contradicting itself.
12. **Prove it at $0.** Oracle, noop and cheater agents test the full pipeline, including anti-cheating, before any model spend.
13. **Say what you mean.** `Sandbox` is the thing, the chamber is the role; "env" only means environment variables.

### 0.1 Decision log

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Where external agents run | Own container (sidecar) | Host process | Agent code isolated from Docker + DB; per-agent dependencies; one shape for all agents |
| Where Claude Code runs (v1) | Installed in the chamber | Sidecar + shell relay | Industry standard (Harbor, Inspect SWE); its tools are hardwired to the local machine. Sidecar kept as stretch |
| Agent ↔ harness channel | JSON-RPC over the agent container's stdio | Unix socket, TCP to host | Host sockets don't reach containers on Docker Desktop (tested); stdio needs no ports, tokens or networking |
| Agent output | Protocol messages (`event`, `artifact`, `finish`) | Files in a shared `/out` volume | Works unchanged for remote agents |
| Where grading runs | Fresh verifier container | Same container as the agent; host | Agent can't tamper; agent-written code never runs on the host |
| Test visibility | Hidden by default, overlaid at verify | Left in the workspace | Prevents reading and editing tests; flag `expose_eval_files` if a benchmark intends them visible |
| What the grader sees | Pristine workspace + declared `submission_paths` + eval files | The agent's whole workspace | Found in phase 1: the cheater agent scored 100% on python-tasks by planting `pytest.py` (grade.py runs `python -m pytest` from the workspace). With `submission_paths: ["solution.py"]` it scores 0% |
| Agent-caused errors | Always graded (score 0) | Recorded as infra_error | Phase 1 review: venv symlinks, Docker's error text in output, a deleted workspace and malformed protocol params all became infra_error, i.e. excluded from accuracy. Fault is now decided by `Sandbox.alive()`, never by agent-controlled output |
| Faked pass | `pass_requires` (grader's own evidence must appear) | Trust grade.py's exit code | `os._exit(0)` at import scored 100%. Residual risk: code running inside the grader can still print a fake summary; a grader that doesn't execute the submission in-process is the full fix |
| Concurrency safety | Fencing token per claim; per-claim labels and artifact folders | Lease only | A stale worker could overwrite results and delete the new owner's containers |
| Stuck agents | recv deadline + watchdog that kills the agent process | recv deadline only | An agent that stops reading blocked the worker's write forever |
| Claude Code as root | `--allowedTools` pre-approval | `--dangerously-skip-permissions` as non-root user | Claude Code refuses skip-permissions as root; switching user would change the task environment (many tasks need root) |
| Installed runtime | Image layer FROM pinned task image | `npm install` inside a live chamber | Cached, reproducible (pinned id), no slow install inside a running trial |
| Benchmark scripts | Run in a tooling container | Run on the host | `prepare.py` installs packages; host stays clean |
| Shell choice (`sh` vs `bash`) | The `Sandbox` | Each agent adapter | It's a property of the image; solved once for all agents |
| Job unit | Trial | Run | Parallelism, per-trial retry, partial results early |
| Queue + DB | SQLite (worker, CLI, DB all on host) | Redis, Postgres | Zero infra on one machine; Postgres behind the same `Queue` interface later |
| Worker model | Process with N slots, child process per trial | Threads; container per worker | Crash isolation, simple timeouts (kill child + remove labeled containers) |
| Model | `claude-opus-5-5` for both agents | — | Single model, set per run; dev uses $0 test agents |
| Remote agents | Tool-level protocol (+ MCP/ACP adapters later) | Networked filesystem | Split-brain runtimes, latency, POSIX edge cases |
| Names | `Sandbox` / chamber / Chamber API | `Environment` / Env API | "Environment" collided with env vars and mini's own term |

## 1. Goals and non-goals

**Goals**
- Run any agent against any benchmark through one CLI: `harness run | status | results`.
- Add a benchmark or agent by adding a folder + spec (+ adapter if needed), never by editing the core.
- Asynchronous execution: submit without blocking, background workers, bounded concurrency, persisted results.
- Ship two agents: mini-swe-agent (external, in a sidecar by default) and Claude Code (installed in v1; sidecar with shell relay as stretch).
- The harness never imports or runs agent code on the host: an agent is an image plus a spec.
- Every trial is isolated, reproducible from its recorded manifest, and gradeable without trusting the agent.
- Report what Vals reports: accuracy with error bars over repeated runs, latency, cost, tool-use stats, error types.

**Non-goals (v1)** — designed-for but not built: multi-host workers, cloud sandboxes, web UI, metrics stack, model-API proxy, remote (vendor-hosted) agents.

## 2. Prior art and what we take from it

| Source | Practice | Our take |
|---|---|---|
| Vals methodology | Model evals use one fixed harness for every model (mini-swe-agent on SWE-bench and Terminal-Bench 4.0). Agent evals use the model's native agent (Claude Code, Codex). Multi-run benchmarks: 3 runs, SEM over per-run scores. | The model is a run parameter; the agent is fixed. `--attempts` + SEM over attempts. |
| Harbor (Terminal-Bench) | Claude Code is **installed in the task container**: installs curl, bash, nodejs, npm (on alpine via `apk` + `npm i -g`), passes model/key/base URL as env vars, pipes the instruction via stdin, runs `claude --output-format=stream-json --print`, redirects `CLAUDE_CONFIG_DIR` into the logs dir and converts session JSONL to a trajectory. | v1 Claude Code adapter follows this pattern. |
| Inspect SWE (UK AISI ecosystem) | Agent runs **inside the sandbox**; model calls are proxied back to the host. | The model proxy is the standard way to observe and swap models behind installed agents (v2). |
| Anthropic, "Demystifying evals for AI agents" | Clean environment per trial, grade outcomes not paths, record full transcripts. | §3 principles. |

Nobody standard runs Claude Code as a sidecar. The pros accept a modified task image and record it.

## 3. Vocabulary

| Term | Meaning |
|---|---|
| Benchmark | A dataset of tasks + how to prepare and grade them. |
| Task | One problem: instruction, task image, eval files, grading command. |
| Agent | Model + scaffold. Placement: **sidecar** (own agent container, reaches the chamber via the Chamber API; default), **installed** (inside the chamber), or **host** (agent runner as a host subprocess, same protocol; dev only). |
| Chamber API | The worker-side service that exposes `Sandbox` (exec / put / get) to an agent container over that container's stdin/stdout. The only path from an agent container into a chamber. |
| Bridge | How a sidecar agent's actions reach the Chamber API: **api** (agent calls it natively, e.g. mini-swe-agent) or **shell_relay** (agent's hardwired shell is pointed at a relay binary, e.g. Claude Code). |
| Run | One `harness run`: agent × benchmark × tasks × attempts × config. |
| Trial | One attempt at one task. The unit of work, retry and grading. |
| Chamber | The container where the agent acts: task image + agent-visible workspace (+ agent runtime if installed). |
| Sandbox | Any isolated container the harness can `exec` / `put` / `get` in. The chamber, the verifier and the tooling container are all sandboxes. |
| Verifier | A fresh container that grades the final workspace. The agent never touches it. |
| Worker | A host process that claims trials from the queue and executes them. |
| Slot | Capacity for one concurrently running trial. A worker has N slots. |

## 4. Trial lifecycle

```
          host (worker → trial subprocess)
 ┌──────────────────────────────────────────────────────────────────────┐
 │ 1 PREPARE   tooling container runs the benchmark's own setup script  │
 │             → workdir/visible/   (goes into chamber)                 │
 │             → workdir/eval/      (never enters chamber by default)   │
 │                                                                      │
 │ 2 CHAMBER   docker run <task image>, copy visible/ → /workspace      │
 │             sidecar/host: image unmodified, --network none           │
 │             installed: agent runtime added, egress allowed (v1)      │
 │                                                                      │
 │ 3 AGENT     sidecar: start agent container, Chamber API over stdio,  │
 │               agent acts on chamber via Chamber API (§5.1)           │
 │             installed: install into chamber, run CLI, copy logs out  │
 │             host (dev): agent as host subprocess, same protocol      │
 │                                                                      │
 │ 4 COLLECT   docker cp /workspace → snapshot.tar; stop + remove       │
 │                                                                      │
 │ 5 VERIFY    fresh verifier container: snapshot + eval/ overlaid      │
 │             (eval files overwrite agent-made copies) → grading cmd   │
 │             → result parser → Grade                                  │
 │                                                                      │
 │ 6 RECORD    TrialResult → DB; artifacts → artifacts/<run>/<trial>/   │
 └──────────────────────────────────────────────────────────────────────┘
```

Principles:
- **Benchmark code never runs on the host.** Setup and grading scripts run in the benchmark's tooling image.
- **Task images are modified only by an installed agent's declared runtime**, and every modification is recorded in the manifest. Sidecar agents modify nothing.
- **Agent code runs only in containers.** The worker holds Docker access and the DB; agent code never shares a process or filesystem with them.
- **Eval files never enter the chamber** unless the run policy exposes them. They are always re-overlaid at verify time, so tampering is impossible.
- **The verifier is always fresh**, and is the only place agent-produced code runs for grading.

## 5. Core interfaces

```python
class Sandbox(Protocol):              # sandbox/__init__.py; any isolated container, the chamber is one
    id: str
    shell: list[str]                  # ["sh", "-c"] for alpine, ["bash", "-lc"] otherwise (probed once)
    def alive(self) -> bool: ...      # asks Docker; decides harness-vs-agent fault, never output text
    def exec(self, cmd: str, *, timeout: int, cwd: str = "/workspace",
             env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult: ...
    def put(self, path: str, data: bytes) -> None: ...
    def get(self, path: str) -> bytes: ...
    def put_dir(self, local_dir: Path, dest: str) -> None: ...
    def snapshot(self, path: str, tar_path: Path) -> None: ...
    def stop(self) -> None: ...

class SandboxProvider(Protocol):
    def ensure_image(self, image: str | None = None, *, dockerfile: Path | None = None) -> str: ...  # -> image id
    def create(self, image: str, *, labels: dict[str, str], network: str = "none",
               cpus: float = 1.0, memory: str = "1g") -> Sandbox: ...
    def reap(self, labels: dict[str, str]) -> int: ...          # never raises
    def reap_stale(self, live_claims: set[str]) -> int: ...     # dead workers' leftovers

class AgentLauncher(Protocol):        # launchers.py; Host today, Docker/Remote later
    def launch(self, agent: AgentSpec, stderr_path: Path) -> AgentSession: ...   # transport + wait/kill

class BenchmarkAdapter:               # benchmarks.py; one YAML-driven class for script-based benchmarks
    def prepare(self, task, workdir, labels) -> PreparedTask: ...                 # visible/ + eval/
    def verify(self, task, snapshot_tar, prepared, workdir, labels, timeout) -> tuple[Grade, ExecResult]: ...
```

- **The shell belongs to `Sandbox`.** The agent says what to run; the sandbox knows how its image runs it.
- **Sidecar agents act only through the Chamber API**, so the harness records every action.
- **Installed agents** use `Sandbox.exec` to install and launch (instruction via stdin, never argv), then `get` to pull their native transcript.
- **Agent tools are not modified.** Adapters translate mechanics only and record what they did.
- A YAML spec covers the common case; a benchmark or agent with unusual needs supplies a Python class implementing the same protocol.

### 5.1 Sidecar placement and the Chamber API (default for external agents)

Every sidecar agent runs in its own container. The only way into the chamber is the Chamber API, served by the worker over the agent container's stdin/stdout (never reachable from the chamber).

```
 agent container (sidecar)                    host worker                 chamber (task image, untouched)
 ┌──────────────────────────────┐ JSON-RPC  ┌──────────────────────┐ docker ┌──────────────────────┐
 │ mini ─ ChamberEnvironment    │ ──stdio─▶ │ Chamber API          │ ─exec─▶│ sh -c "<cmd>"        │
 │   (bridge: api)              │           │  → Sandbox.exec      │        │ --network none       │
 │ egress: model API only       │           │  → logs every action │        │                      │
 └──────────────────────────────┘           └──────────────────────┘        └──────────────────────┘
```

**Chamber API transport: stdio.** The worker starts the agent container with `docker run -i` and keeps its stdin/stdout pipes. Those pipes carry the Chamber API as newline-delimited JSON-RPC (like MCP's stdio transport). The agent's free-form logs go to stderr (captured by the worker); structured output goes over the protocol (§5.2).
- No sockets, ports, tokens or host networking. Identical on macOS and Linux. The agent container has no route to the worker except its own pipes.
- Scoped to one trial by construction: each pipe pair belongs to one container.
- The runner reserves the real stdout for RPC at startup and redirects `sys.stdout` to stderr, so a stray `print` can't corrupt the channel.
- Requests are handled one at a time per trial (agents act sequentially).
- `shell_relay` (stretch): the relay binary runs inside the agent container, so it connects to the in-container runner over a local unix socket, and the runner forwards over stdio. That socket never crosses the Docker VM boundary.

| Method | Params | Result |
|---|---|---|
| `exec` | `{cmd, cwd?, env?, timeout_s?, stdin?}` | `{output, returncode, timed_out, truncated, duration_s}` — stdout+stderr combined (what mini expects) |
| `put` | `{path, data_b64}` | `{}` |
| `get` | `{path}` | `{data_b64}` |

- A non-zero exit code is a normal result. Error replies: `-32602` bad params, `-32601` unknown method, `-32001` not found, `-32002` cancelled (deadline passed) — all the agent's problem, the session continues. Only `-32000` (chamber broken, confirmed via `Sandbox.alive()`) aborts the trial as `infra_error`.
- Output is capped (default 1 MB) and flagged `truncated`.
- The worker applies the image's shell, per-command timeout and logging; the agent can't bypass them.

**Why sidecar is the default for external agents**
- Agent code (possibly third-party or proprietary) never runs next to the worker's Docker access and DB.
- Each agent brings its own dependencies; the harness no longer depends on mini-swe-agent as a Python package.
- One uniform shape for every agent: an image + a spec + a bridge.
- The API key lives in the agent container, never in the chamber.

**Bridges**
- **api** (v1, mini-swe-agent): the agent image ships `ChamberEnvironment`, a ~40-line implementation of mini's environment interface (`execute(action)`) that calls the Chamber API. Mini's loop, prompts and tools are unchanged. The shell (`sh -c` vs `bash -lc`) is applied by the worker's `Sandbox`, not by mini. **Gotcha:** mini detects "task done" inside its environment class: output whose first line is `COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT` raises `Submitted`. `ChamberEnvironment` must keep that check, or mini runs to its step limit.
- **shell_relay** (stretch, Claude Code): Claude Code's Bash tool is pointed at a static Rust relay binary that forwards to the Chamber API. Read/Edit act on shared `/workspace` and `/tmp`. The sidecar image is built `FROM` the task image so Read and Bash see the same world. Runs `--read-only`, agent as non-root, relay root-owned (the agent's file tools can reach it). Precondition: a spike logging exactly what Claude Code sends to its shell.

**Costs:** one extra container per trial; a socket hop per action (negligible next to model latency); the agent image must be built per agent version.

### 5.2 Agent session protocol

The whole interface between the harness and an agent's code. **Everything travels as protocol messages, nothing through shared files or env-only conventions**, so the same agent works whether it runs in a local container (stdio) or on someone else's server (§5.3).

JSON-RPC 2.0, newline-delimited. W = worker, A = agent.

| Step | Direction | Message | Payload |
|---|---|---|---|
| 1 | A → W | `hello` (request) | `{protocol: "vals-agent/1", agent: {name, version}, capabilities: ["exec", "files", "events"]}` |
| 2 | W → A | `hello` result | `{protocol: "vals-agent/1", trial_id}` — mismatched major version → `agent_error` (graded 0: the agent's adapter is wrong) |
| 3 | W → A | `start` (notification) | `{instruction, config: {model, step_limit, cost_limit_usd, …}, workspace: "/workspace", deadline_unix}` |
| 4 | A → W | `exec` / `put` / `get` (requests) | Chamber API (§5.1) |
| 4 | A → W | `event` (notification, optional) | `{type, data}` — progress, step summaries, model usage; streamed into the `events` table |
| 4 | A → W | `artifact` (request) | `{name, data_b64}` — native trajectory, logs; stored under `agent_logs/` |
| 5 | A → W | `finish` (request) | `{exit_status, steps, input_tokens, output_tokens, cost_usd}` |
| — | W → A | `cancel` (notification) | `{reason}` — agent should stop and `finish`; worker kills it after a grace period |

- **Config travels in `start`, not env vars.** Only secrets stay in the agent's own environment (they never cross the protocol).
- **Process exit** after `finish` is normal. Exit or disconnect without `finish` = `agent_error` (graded 0), unless the worker can attribute it to the harness → `infra_error`. Deadline passed → worker sends `cancel`, then kills → `timeout`.
- The instruction never appears in argv or env.

### 5.3 Extension seams

Each seam is an interface with one v1 implementation. Making agents remote, or moving sandboxes to the cloud, means adding an implementation, not changing the core.

| Interface | Responsibility | v1 implementation | Future implementations |
|---|---|---|---|
| `Transport` | Carry protocol messages: `send(msg)`, `recv() -> msg`, `close()` | `StdioTransport` (pipes of `docker run -i`) | `WebSocketTransport` (TLS + per-trial bearer token), `HttpLongPollTransport` |
| `AgentLauncher` | Start an agent for a trial and hand back a session: `launch(agent_spec, trial) -> AgentSession`; `AgentSession` = transport + `wait()` + `kill()` | `DockerLauncher` (sidecar), `InstalledLauncher`, `HostLauncher` (dev) | `RemoteLauncher`: POST to the vendor's `/sessions`, get a WebSocket URL, agent dials back |
| Agent session protocol | Handshake, task, actions, events, artifacts, finish (§5.2) | `vals-agent/1` | additive methods; major bump only for breaking changes |
| `SandboxProvider` | Create chambers and verifiers: `create(image, limits, network, labels) -> Sandbox` | `DockerSandboxProvider` | Modal, Daytona, Firecracker, remote Docker host |
| `Sandbox` | Act on one sandbox: `exec`, `put`, `get`, `snapshot`, `stop` | `DockerSandbox` | one per `SandboxProvider` |
| `ModelGateway` | How an agent reaches its model: `credentials_for(trial) -> {base_url, api_key}` | `DirectGateway` (pass the declared secret) | `ProxyGateway`: host proxy, per-trial scoped key, logs every model call; remote agents bringing their own model just report usage in `finish` |
| `Queue` | `enqueue`, `claim(worker_id) -> trial`, `heartbeat`, `complete`, `requeue_expired` | `SqliteQueue` | `PostgresQueue` (`FOR UPDATE SKIP LOCKED`), managed queue |
| `ResultStore` / `ArtifactStore` | Persist rows / blobs | SQLite / local filesystem | Postgres / S3 |
| `TrialRunner` | `run(TrialSpec) -> TrialResult`, the whole lifecycle | in-process child per slot | remote workers on other machines |

**What a remote agent looks like with these seams:**

```
 worker ──RemoteLauncher──▶ vendor API: POST /sessions {trial_id, token}
   ▲                                         │
   └──────── WebSocketTransport ◀────────────┘ agent dials back with token
             same vals-agent/1 messages: hello · start · exec/put/get · event · artifact · finish
```

The vendor's code never enters our infrastructure and our chamber never leaves it. The agent only ever sees protocol messages. Only `RemoteLauncher` and `WebSocketTransport` are new; the protocol, Chamber API, grading and results are unchanged.

**Rules that keep this possible (apply from v1):**
1. Agents never depend on shared volumes, the host filesystem or Docker. Everything goes through the protocol.
2. The worker never assumes the agent is a local process: it talks to an `AgentSession`.
3. Every message carries no transport-specific data (no file paths outside the chamber, no socket names).
4. Protocol is versioned from day one (`hello`).

### 5.4 Remote agents: where the line is drawn

A remote agent is split into a **brain** (model + loop, on the vendor's side) and **hands** (tool execution, on ours). The interface sits at the **tool level**, not the OS level: we don't fake a machine, we execute the agent's tool calls.

| Agent's tools | How it connects | Notes |
|---|---|---|
| Pluggable (custom tools, MCP) | Its tools call `exec` / `put` / `get` directly | Preferred. We could also expose the Chamber API as an **MCP server** (`bash`, `read_file`, `write_file`), so any MCP-capable agent plugs in with no vendor-side code. **ACP** (Agent Client Protocol) is the same idea: the client provides the agent's file system and terminal. |
| Interactive terminal (tmux-style, e.g. Terminus) | Protocol capability `pty`: `pty_open`, `pty_write`, `pty_read`, `pty_close` | Keystrokes in, screen out. Add when needed; advertised in `hello`. |
| Hardwired to a local machine | Shell relay over the network + networked filesystem for `/workspace` | Last resort. Their binaries and runtimes still run on their side, so `python` there ≠ `python` in the chamber (split brain again, now across the internet). Many round trips for `grep` / `git status`; POSIX edge cases (locks, symlinks, permissions). Prefer asking the vendor to make tools pluggable. |

- **Latency:** each action crosses the internet (~50–200 ms), still small next to model calls.
- **Trust runs both ways.** The vendor protects its agent; Vals protects its benchmarks. Task content is already visible to the model provider in any API-based eval, so remote mode adds no new kind of exposure. What changes: possibly an extra party (an agent vendor that isn't the model provider), possibly weaker data terms than the provider's API agreement, and tasks arriving pre-packaged with full trajectories. Tests and graders never leave our verifier in any mode. `policy.allow_remote_agents` per benchmark records whether a given vendor may see that benchmark under its terms.

## 6. Specs: who owns what

Three layers, each owned by a different party, composed into one frozen `TrialSpec` per trial.

| Question | Owner | Where |
|---|---|---|
| Task fields, setup, which files are eval-only, verifier image + command, result parser | benchmark adapter author | `BenchmarkSpec` |
| Agent placement, runtime it needs, how to launch it, defaults | agent adapter author | `AgentSpec` |
| Which agent/benchmark/tasks/model, attempts, limits, whether eval files are exposed, network | the experimenter (Vals) | `RunSpec` |
| Everything above, resolved and pinned (image digests, versions) | the harness | `TrialSpec` (= manifest) |

### 6.1 BenchmarkSpec — `benchmarks/<name>/harness.yaml`

```yaml
name: python-tasks
version: "1"
dataset: dataset.json
fields: { id: id, instruction: prompt, image: docker_image }
tooling_image: vals/python-tasks-tooling   # built from python:3.11-slim + pytest; used for prepare + verify
prepare: ["python", "prepare.py", "{task_id}", "--workspace", "/out"]
eval_paths: ["test_solution.py"]           # moved from visible/ to eval/
submission_paths: ["solution.py"]          # only these agent files reach the verifier ("**" = all)
pass_requires: ['=+ \d+ passed in [0-9.]+s']  # a pass must show pytest's own summary
verify: ["python", "grade.py", "{task_id}", "--workspace", "/workspace"]
result_parser: json_stdout                 # json_stdout | exit_code
defaults: { timeout_s: 600 }
```

```yaml
name: bash-operations
version: "1"
dataset: dataset.json
fields: { id: task_id, instruction: problem_statement, image: docker_image }
tooling_image: python:3.11-slim
prepare: ["python", "setup.py", "--workspace-dir", "/out", "--task", "{task_id}"]
eval_paths: []
verify: ["python", "evaluate.py", "--workspace-dir", "/workspace", "--task", "{task_id}"]
result_parser: json_stdout
```

### 6.2 AgentSpec — `agents/<name>/agent.yaml`

```yaml
name: mini-swe-agent
version_cmd: "mini --version"
placement: sidecar                         # sidecar | installed | host (dev only)
bridge: api
image: vals/agent-mini-swe-agent           # built from agents/mini-swe-agent/Dockerfile: python + mini + ChamberEnvironment
entrypoint: ["python", "-m", "vals_mini_runner"]   # reads instruction from stdin, config from env
secrets: [MODEL_API_KEY]                   # into the agent container only
defaults: { model: anthropic/claude-opus-5-5, step_limit: 50, cost_limit: 1.0 }   # litellm model string
```

```yaml
name: claude-code
version: "2.1.312"
placement: installed
installed:
  dockerfile: install.Dockerfile            # ARG BASE / FROM ${BASE}: built FROM the pinned task image
  run: >-                                   # stdin = instruction; output = stream-json
    claude -p --output-format stream-json --verbose --model {model} --max-turns {max_turns}
    --allowedTools "Bash Read Write Edit Glob Grep"
  env:
    CLAUDE_CONFIG_DIR: /tmp/vals/agent      # session logs, outside /workspace, kept as an artifact
    ANTHROPIC_CUSTOM_HEADERS: "anthropic-workspace-id: ${ANTHROPIC_WORKSPACE_ID}"   # only if set
  output_parser: claude_stream_json
secrets: [ANTHROPIC_API_KEY]
defaults: { model: claude-opus-5-5, step_limit: 50 }   # step_limit -> --max-turns
```

How installed agents run (`installed.py`):
- **Runtime as an image layer, never installed live.** `install.Dockerfile` is built `FROM` the pinned task image (tagged `vals/base:<id>` so it can't move), cached by content hash, and pinned as `agent_image`. The chamber is created from it. Alpine (apk, musl) and Debian (apt) both work.
- **Driven from the worker with `Sandbox.exec`**: instruction written to `/tmp/vals/instruction.txt` and piped on stdin, output streamed to a file, then parsed (`tool_use` events → transcript; the `result` event → exit status, turns, tokens incl. cache, cost).
- **Root, without skipping permissions.** Claude Code refuses `--dangerously-skip-permissions` as root (and 2.1.312 ignores `IS_SANDBOX`), so its tools are pre-approved with `--allowedTools`. The chamber stays root, as the task expects.
- **Secrets** travel as `docker exec -e NAME` through the CLI's own environment, never argv. They are visible to the agent inside the chamber (v1 risk, documented; v2 model proxy with a dummy key).
- **Network:** the chamber gets egress because the CLI calls its model from there (v2: proxy only).
- **Observed:** Opus 5.5 via Claude Code scored 5/5 on bash-operations, 2/3 on python-tasks. On py-003 it answered in chat instead of writing `solution.py`: the benchmark's prompt never names the file. Correctly graded 0; the harness doesn't rewrite benchmark prompts.

### 6.3 RunSpec — built by `harness run`, stored in `runs.spec_json`

```yaml
agent: agents/mini-swe-agent
benchmark: bash-operations
task_ids: [bash-001, bash-003]             # null = all
model: anthropic/claude-opus-5-5             # overrides agent default
attempts: 3                                # trials per task, for SEM over runs
limits: { timeout_s: 600, command_timeout_s: 30, verify_timeout_s: 120, step_limit: 50, cost_limit_usd: 1.0, cpus: 1, memory: 1g }
policy:
  expose_eval_files: false
  network: none                            # none | egress (forced to egress for installed agents in v1)
  infra_retries: 2
```

Precedence: RunSpec > BenchmarkSpec/AgentSpec defaults > harness defaults.

### 6.4 TrialSpec — resolved at enqueue, written to `manifest.json`

The fully resolved RunSpec for one `(task_id, attempt)`, plus `inputs_digest`: content hashes of the benchmark folder (dataset, scripts, grader) and the agent folder. `manifest_hash` covers all of it but excludes per-run ids and host paths, so **same hash = same experiment**, and editing `grade.py` changes it. At run time `manifest.json` also records `pins`: the task and tooling **image ids**, and containers are created from those ids, not tags.

## 7. Workers and concurrency

**A worker is a process; a slot is one running trial; containers belong to the trial, not the worker.**

**Topology: the worker runs on the host**, talking to Docker through the normal CLI/SDK. The Chamber API uses each agent container's stdio (§5.1), which avoids a Docker Desktop for Mac limitation found in testing: a unix socket created on the macOS host and bind-mounted into a container is visible but refuses connections. Because the worker, CLI and DB all live on the host, SQLite stays safe.

```
harness worker --slots 4                      (supervisor; the only writer of final results)
   │  loop: collect finished children → fill free slots (claim) → renew leases every 10 s
   ├─ slot 1 → python -m vals_harness.trial_child <job> <result>   ─▶ tooling → chamber + agent → verifier
   ├─ slot 2 → …                                  (own process group; result written atomically to a file)
   └─ trial wall clock: kill process group + reap by claim label → infra_error (timeout_harness)
```

- **Slot = the resource unit.** One trial runs at most 2 containers at once (chamber + sidecar, or chamber alone), each capped by `limits.cpus/memory`. Size slots so `slots × per-trial limits ≤ machine`.
- **Each trial runs in a child process** in its own process group. A crash or hang is isolated to that trial: a child that exits without a result becomes `infra_error` (`adapter`); one that overruns `trial_budget_s` (default: agent timeout + verify timeout + 15 min for image builds; override with `--limit trial_timeout_s=…`) is killed with its whole group, its containers are reaped by claim label, and it's recorded as `timeout_harness`.
- **Clean shutdown.** On Ctrl-C the supervisor kills its children, reaps their containers and *releases* their claims back to the queue (fenced, no infra attempt charged).
- **Multiple workers are fine.** Each claims atomically from SQLite (WAL mode). Multi-host would need Postgres; out of scope.
- **Leases.** A claimed trial has `lease_until`, renewed every 10 s. An expired lease (dead worker) lets another worker reclaim it, counting one infra attempt; past `policy.infra_retries` the trial is closed as `infra_error` (`worker_lost`) instead of being handed out forever.
- **Fencing tokens.** Every claim gets a fresh `claim_token`; every later write (`set_status`, `heartbeat`, `finish`) must present it. A worker that stalled past its lease is fenced out: its writes fail (`LostLease`), its result is dropped, and because containers and artifact folders are per claim (`vals.claim` label, `try<N>/` folder) it can't delete or overwrite the new owner's.
- **Startup reaper.** A worker removes `vals.*` containers whose claim isn't live (`reap_stale`).
- **The worker loop never dies on a trial.** Any exception becomes that trial's `infra_error`; cleanup (`reap`) never raises.
- **Optional:** a per-model concurrency cap to stay under provider rate limits.

`harness run --wait` runs an in-process worker for the run's trials — convenient for development, same code path.

## 8. Results

Trial states: `queued → preparing → running_agent → verifying → graded | infra_error | cancelled`.

**Error taxonomy**
- Agent failed → `graded`, score 0, counts toward accuracy. This includes everything the agent controls: wrong answer, step/cost limit, timeout, gave up, crashed, malformed protocol messages, a deleted workspace (empty submission), unsafe links in its snapshot (dropped and reported), a submission that hangs the grader.
- Harness/infra failed → `infra_error`: image pull, Docker failure confirmed by `Sandbox.alive()`, adapter bug, grader output we can't parse, lost worker. Retried up to `policy.infra_retries`; excluded from accuracy, reported separately.
- **Rule:** nothing the agent can do may produce an `infra_error`, or an agent could drop its own failures from the denominator. The vandal test agent enforces this.
- `cancelled`: `harness cancel --run_id` cancels queued trials; running trials finish normally.

### 8.1 TrialResult

```json
{
  "trial_id": "t_…", "run_id": "r_…", "task_id": "bash-001", "attempt": 1,
  "status": "graded",
  "grade": { "passed": true, "score": 1.0, "details": { } },
  "agent": { "exit_status": "submitted", "steps": 4 },
  "metrics": { "duration_s": 41.2, "agent_s": 35.0, "verify_s": 3.1,
               "input_tokens": 8120, "output_tokens": 640, "cost_usd": 0.012, "n_commands": 4 },
  "error": null,
  "artifacts": "artifacts/r_…/t_…/",
  "manifest_hash": "sha256:…"
}
```

`error` when `infra_error`: `{ "kind": "image" | "docker" | "prepare" | "adapter" | "verifier" | "worker_lost" | "timeout_harness", "message": "…" }` (`timeout_harness` = the trial wall clock ran out). Results also carry `try` (1 + infra retries) and per-trial `spent_usd` summed over every try.

### 8.2 RunResult (`harness results`)

```json
{
  "run_id": "r_…", "spec": { },
  "status": "completed",
  "counts": { "graded": 14, "infra_error": 1, "cancelled": 0, "queued": 0, "running": 0 },
  "accuracy": { "mean": 0.80, "sem": 0.04, "per_attempt": [0.8, 0.86, 0.73] },
  "cost_usd": 0.41, "duration_s": 312.0,
  "tasks": [ { "task_id": "bash-001", "passed": 3, "graded": 3, "infra_error": 0 } ],
  "trials": [ { "trial_id": "…", "status": "graded", "tries": 1, "spent_usd": 0.02, "grade": { }, "…": "…" } ]
}
```

- `accuracy.mean` = mean of per-attempt accuracies; each per-attempt accuracy = passed / graded for that attempt.
- `sem` = standard error over per-attempt scores (Vals' method for multi-run benchmarks); over task-level scores if `attempts = 1`.

## 9. Data and observability

The worker is the only writer and pushes as it goes.

**Hot (SQLite)**
- `runs(id, spec_json, created_at)` — run status is derived from its trials, never stored
- `trials(id, run_id, task_id, attempt, status, tries, infra_attempts, max_infra, spent_usd, spec_json, manifest_hash, claim_token, worker_id, lease_until, created_at, started_at, finished_at)`
- `results(trial_id, passed, score, grade_json, agent_json, metrics_json, error_json, artifacts)`
- `events(trial_id, try_no, ts, type, payload_json)` — append-only: phases, each agent command (cmd, rc, duration), agent events, infra errors, skipped snapshot entries

**Cold (filesystem)** — `artifacts/<run_id>/<trial_id>/try<N>/` (one folder per try, so retries never mix): `manifest.json`, `transcript.jsonl`, `agent_logs/`, `workspace.tar`, `verifier.log`, `result.json`.

The run report is a SQL aggregation over `runs ⋈ trials ⋈ results`.

**Transcript capture:** external agents — the harness records every `Sandbox.exec` + the agent's own trajectory. Installed agents — native transcript copied out (Claude Code stream-json + session JSONL). v2: host-side model proxy for a uniform transcript and token accounting.

## 10. Isolation and security

- Chamber: `--network none` for sidecar agents; egress for installed agents in v1 (documented), proxy-only in v2.
- Agent container: egress to the model API only (v1: allow egress; v2: model proxy). Gets its Chamber API pipes and its declared `secrets`, nothing else.
- Secrets never enter the chamber for sidecar agents. Installed agents receive only their declared `secrets`.
- Workspace moves via `docker cp` / tar, not bind mounts.
- Eval files absent from the chamber by default; always re-overlaid at verify.
- Containers labeled `vals.trial_id`, `vals.run_id`, `vals.claim`; reaped per claim in `finally` and on worker start.
- Every sandbox is hardened: `--init` (reaps zombies), `--pids-limit 512`, `--security-opt no-new-privileges`, `--cap-drop ALL` plus only the file-ownership caps tasks need (CHOWN, DAC_OVERRIDE, FOWNER, FSETID, SETUID, SETGID, KILL). Disk isn't capped: overlay2 on Docker Desktop doesn't support `--storage-opt size`.
- Host memory is bounded: exec output streams with a 1 MB cap, `get` 10 MB, snapshot 1 GB (streamed to disk), protocol messages 32 MB.
- Agent snapshots are unpacked with tarfile's `data` filter; rejected members and links escaping the workspace are dropped and listed in the grade's `harness.skipped_files`.
- Eval files are copied over the graded workspace without ever following an agent-planted symlink.

## 11. CLI and phases

```
harness run --agent agents/mini-swe-agent --benchmark bash-operations [--task_ids a,b] [--model …] [--attempts 3] [--wait [--slots N]]
harness status [--run_id ID]        # no id → list active runs
harness results --run_id ID [--format json|table|summary]
harness worker [--slots N] [--once]
harness prune [--dry-run]          # outdated vals/* images + dead workers' containers
harness cancel --run_id ID
```

1. ✅ `Sandbox` (Docker) + both benchmark adapters + mini-swe-agent on `host` placement + `run --wait` + SQLite + results.
2. ✅ `DockerLauncher`: every agent runs in its own sidecar container (`docker run -i`, same stdio protocol). Agent images are built in PREPARE from the agent folder + SDK (default Dockerfile, or the agent's own), tagged by content hash, and pinned by id in the manifest. Secrets passed as `-e NAME` (never on the command line); the container has no mounts, egress for model calls, the standard hardening, and is removed by name on kill.
3. ✅ Worker supervisor: N slots, one child process per trial, trial wall clock, clean release on shutdown; `harness prune`.
4. ✅ Claude Code, installed (Harbor pattern): runtime layer FROM the pinned task image, headless stream-json, logs and transcript collected; a fake-CLI test covers the path at $0.
5. Stretch: Claude Code via `shell_relay` (spike first), model proxy, network lockdown.

## 12. Timeouts

| Limit | Default | Enforced by | On expiry |
|---|---|---|---|
| Per command | 30 s (mini's default) | Chamber API / `Sandbox.exec` | command returns `timed_out: true`; agent continues |
| Agent wall clock | 10 min | recv deadline → `cancel` + 10 s grace (only finish/event/artifact accepted); a watchdog kills the agent process 5 s later even if the worker is blocked writing to an agent that stopped reading | `exit_status: timeout`, graded |
| Verify | 2 min | in-container `timeout` | graded 0 (the submission runs inside the grader, so a hang is the agent's) |
| Trial wall clock | agent + verify + 15 min (`trial_timeout_s`) | supervisor kills the trial's process group + reaps by claim label | `infra_error` (kind `timeout_harness`) |
| Lease | 60 s, renewed every 10 s | DB | trial requeued |

## 13. Testing without a model

Built-in test agents make the whole pipeline testable deterministically at $0 model spend:
- **oracle** — replays a known-good command list per task (`agents/oracle/solutions/<task_id>.sh`; we write these for the 8 tasks). Expect 100%.
- **noop** — does nothing. Expect 0%; proves graders don't pass empty workspaces.
- **cheater** — tries every grading attack found so far: recon for tests, a fake `test_solution.py`, a planted `pytest.py` and `conftest.py`, a submission that calls `os._exit(0)` at import, absolute / escaping / overlay-redirect symlinks, and Docker's error text in its output. Expect 0% and no infra errors.
- **vandal** — tries to turn its own failure into an infra error: malformed protocol messages, garbage `finish` numbers, `rm -rf /workspace`. Expect 0% and no infra errors.

They run as sidecar containers like any other agent; a further integration test proves a sidecar agent sees no host files, none of the worker's environment, and only its declared secrets. Unit tests cover specs, parsers, graded-workspace safety, the protocol (malformed params, an agent that stops reading), the store (fencing, retry caps, spend) and aggregation; integration tests run every test agent against both benchmarks in real Docker.

## 14. Repo layout and image builds

```
vals-harness/
├── src/vals_harness/       cli.py · service.py · specs.py · sandbox/ (Sandbox, docker.py) · benchmarks.py · protocol.py
│                           launchers.py · chamber_api.py · installed.py · trial.py · trial_child.py · store.py · results.py · errors.py
├── src/vals_agent_sdk/     client.py (agent side of vals-agent/1, stdlib only)
├── agents/                 mini-swe-agent/ · claude-code/ · oracle/ · noop/ · cheater/ · vandal/
├── benchmarks/             bash-operations/ · python-tasks/   (starter files untouched + harness.yaml)
├── docker/                 tooling images
├── tests/
└── README.md · DESIGN.md · architecture.excalidraw
```

- Agent and tooling images are built from their folders, tagged by a hash of the folder contents (`vals/agent-mini-swe-agent:<hash>`), built lazily at enqueue if missing. `harness build` builds all ahead of time.
- Task images are pulled once and pinned by digest in each manifest.

## 15. Open questions

1. ~~Model and provider.~~ **Decided: Anthropic, `claude-opus-5-5`** ($4 / $20 per M tokens) for both agents. Pipeline development runs at $0 via the oracle/noop/cheater agents (§13); model runs only when testing agents themselves. Verify before the first model run: (a) mini-swe-agent's config sends no `temperature` (the API has rejected sampling params on every model since 2026-10-21); (b) litellm can price `claude-opus-5-5`, or mini's cost limit can't work.
2. **Expose tests to the agent?** Default hidden via `policy.expose_eval_files`.
3. **Installed-agent egress in v1.** Accept unrestricted egress, or block everything but the model API host?
