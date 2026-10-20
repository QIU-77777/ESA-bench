import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# Standalone verifier uses sibling state_score import in task containers.
import sys
from esa import state_score
sys.modules["state_score"] = state_score
from esa import score

class VerifierTests(unittest.TestCase):
    def test_state_only_missing_or_valid_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);agent=root/"agent";agent.mkdir();verifier=root/"verifier";rubric=root/"rubric.json"
            rubric.write_text(json.dumps({"weights":{"state":1},"expected_state":{"navigation.active":True}}))
            with patch.multiple(score,AGENT_DIR=agent,VERIFIER_DIR=verifier,RUBRIC_PATH=rubric,TOOL_CALLS_PATH=agent/"tool-calls.json",REPLY_PATH=agent/"reply.txt"), patch.object(score,"score_judge",return_value=(None,{"available":False,"reason":"disabled"})):
                score.main();self.assertEqual(0,json.loads((verifier/"reward.json").read_text())["reward"])
                (agent/"snapshot-initial.json").write_text(json.dumps({"state":{}}))
                (agent/"snapshot-final.json").write_text(json.dumps({"state":{"navigation":{"active":True}}}))
                score.main();self.assertEqual(1,json.loads((verifier/"reward.json").read_text())["reward"])
if __name__=="__main__":unittest.main()
