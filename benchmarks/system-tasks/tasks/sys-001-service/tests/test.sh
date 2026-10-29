#!/bin/sh
# Pass iff something on :8000 answers /ping with exactly "pong".
body=$(python3 -c 'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/ping", timeout=5).read().decode().strip())' 2>/dev/null)
if [ "$body" = "pong" ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
echo "got: ${body:-<no response>}"
