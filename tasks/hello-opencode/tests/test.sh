#!/bin/bash

# Runs inside the task container after the agent finishes.
# /tests/test_outputs.py is copied in by Harbor; reward is written to
# /logs/verifier/reward.txt.

python3 -m pytest /tests/test_outputs.py -rA
if [ $? -eq 0 ]; then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
