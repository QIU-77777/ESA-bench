#!/bin/bash
# 判分入口：读取驱动产物与评分标准，输出 reward.json。
set -uo pipefail

mkdir -p /logs/verifier

python3 /tests/score.py
status=$?

if [ ! -f /logs/verifier/reward.json ]; then
  printf '{"reward": 0.0, "tool_use": 0.0, "reply": 0.0}' > /logs/verifier/reward.json
fi

exit "$status"
