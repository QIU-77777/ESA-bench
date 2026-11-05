#!/bin/bash
set -euo pipefail
mkdir -p /logs/verifier
export ESA_RUBRIC_PATH=/tests/rubric.json
python3 /tests/score.py
