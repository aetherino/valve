# Francisco's Take-home assignment
NOTE: This document is entirely human-written.

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

There's a total of 3 manifest files needed to adapt a benchmark and it's agent to this system:

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
- Superset of the prior two
- Cost limits
- Attempts
- Network 
- Policy (expose eval files, infra retries)
- Retries



This design allowed for the execution of Claude Code, which wouldn't normally run in the requested alpine image without compromising it:
- If installed, it would require node

## Phantom Box
Labs and vendors might want to keep unreleased harnesses to themselves, which is not something that the take-home assingment requirement-abiding system would normally allow. 

My exploration on this problem is the Phantom Box; a vendor-hosted but Vals-provided container setup that:
1. replicates the chamber's image
2. streams all shell commands to the real' vals-hosted chamber container
3. mounts a networked filesystem.

This ultimately allows a vendor to run Vals benchmarks without sharing a secret harness, while allowing Vals to control where benchmarks happen.
