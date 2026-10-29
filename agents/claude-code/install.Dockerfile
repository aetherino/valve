# Agent runtime layer, built FROM the pinned task image (BASE is passed by the harness).
# Works on alpine (apk, musl) and Debian (apt). Version pinned for reproducibility.
ARG BASE
FROM ${BASE}
RUN if command -v apk >/dev/null; then apk add --no-cache nodejs npm bash ripgrep libgcc libstdc++; \
    else apt-get update && apt-get install -y --no-install-recommends nodejs npm ripgrep ca-certificates && apt-get clean; fi \
 && npm install -g --no-fund --no-audit @anthropic-ai/claude-code@2.1.312
