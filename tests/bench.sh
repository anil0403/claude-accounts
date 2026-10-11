#!/bin/bash
# Time a status line script: N runs per fixture, mean/max in ms.
# Usage: tests/bench.sh [script] [runs]   (default: this repo's status line, 10 runs)
here=$(cd "$(dirname "$0")" && pwd)
script=${1:-$here/../statusline/statusline.sh}; runs=${2:-10}
HOME=$(mktemp -d); trap 'rm -rf "$HOME"' EXIT; export HOME   # usage files go here, not in your real ~
python3 - "$script" "$runs" "$here"/fixtures/*.json <<'PY'
import subprocess, sys, time
script, runs, fixtures = sys.argv[1], int(sys.argv[2]), sys.argv[3:]
allt = []
for f in fixtures:
    data = open(f, "rb").read()
    ts = []
    for _ in range(runs):
        t = time.perf_counter()
        subprocess.run([script], input=data, capture_output=True)
        ts.append((time.perf_counter() - t) * 1000)
    allt += ts
    print(f"{f.rsplit('/',1)[-1]:22} mean {sum(ts)/len(ts):7.0f} ms   max {max(ts):7.0f} ms")
mean = sum(allt) / len(allt)
print(f"{'ALL':22} mean {mean:7.0f} ms   (budget 50 ms)")
sys.exit(1 if mean > 50 else 0)
PY
