#!/bin/sh
out=$(cd / && env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin greet vals 2>&1)
if [ "$out" = "hello, vals" ]; then echo 1 > /logs/verifier/reward.txt; else echo 0 > /logs/verifier/reward.txt; fi
echo "got: $out"
