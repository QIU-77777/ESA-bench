#!/bin/bash

# Reference solution, used by `harbor task run --oracle` to validate the task
# itself, never shown to the evaluated agent.

cat > /app/solve.py <<'PY'
from pathlib import Path

seen = set()
total = 0
for line in Path("/app/data/numbers.txt").read_text().splitlines():
    line = line.strip()
    if not line:
        continue
    value = int(line)
    if value < 0 or value in seen:
        continue
    seen.add(value)
    total += value

Path("/app/result.txt").write_text(f"{total}\n")
PY

python3 /app/solve.py
