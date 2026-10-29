# mini-swe-agent sidecar image. Build context (assembled by the harness):
#   agent/            this folder
#   vals_agent_sdk/   the agent-side protocol client
FROM python:3.11-slim
# Pinned so a run is reproducible. litellm 1.106 imports `mcp` whenever a request
# has tools but doesn't declare it, so mcp is installed explicitly.
# uv instead of pip: pip's resolver took ~15 min backtracking through litellm's tree.
COPY --from=ghcr.io/astral-sh/uv:0.7.2 /uv /bin/uv
RUN uv pip install --system --no-cache "mini-swe-agent==2.4.6" "litellm==1.106.6" mcp
COPY vals_agent_sdk /opt/vals/vals_agent_sdk
ENV PYTHONPATH=/opt/vals PYTHONUNBUFFERED=1
WORKDIR /agent
COPY agent/ /agent/
