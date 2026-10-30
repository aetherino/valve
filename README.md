# Francisco's Take-home assignment
NOTE: This document is human-written; the Phantom Box section was updated with Claude's help to match the current code.

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
