import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from esa import score


class VerifierTests(unittest.TestCase):
    def invoke(self, root, rubric, evidence=None, judge=(None, {"available": False, "reason": "disabled"})):
        root = Path(root)
        agent = root / "agent"
        agent.mkdir(exist_ok=True)
        verifier = root / "verifier"
        rubric_path = root / "rubric.json"
        rubric_path.write_text(json.dumps(rubric), "utf8")
        if evidence is not None:
            (agent / "tool-calls.json").write_text(json.dumps(evidence), "utf8")
            (agent / "reply.txt").write_text(evidence.get("reply", ""), "utf8")
            for name, key in (("snapshot-initial.json", "initial"), ("snapshot-final.json", "final")):
                if evidence.get(key) is not None:
                    (agent / name).write_text(json.dumps(evidence[key]), "utf8")
        with patch.multiple(score, AGENT_DIR=agent, VERIFIER_DIR=verifier, RUBRIC_PATH=rubric_path,
                            TOOL_CALLS_PATH=agent / "tool-calls.json", REPLY_PATH=agent / "reply.txt"), \
                patch.object(score, "score_judge", return_value=judge):
            code = score.main()
        return code, json.loads((verifier / "reward.json").read_text("utf8")), json.loads((verifier / "score-detail.json").read_text("utf8"))

    def test_state_only_missing_or_valid_evidence(self):
        rubric = {"weights": {"state": 1}, "expected_state": {"navigation.active": True}}
        with tempfile.TemporaryDirectory() as root:
            code, rewards, detail = self.invoke(root, rubric)
            self.assertEqual(1, code)
            self.assertEqual(0, rewards["reward"])
            self.assertFalse(detail["valid"])
            data = {"initial": {"state": {}}, "final": {"state": {"navigation": {"active": True}}}}
            code, rewards, detail = self.invoke(root, rubric, data)
            self.assertEqual(0, code)
            self.assertEqual(1, rewards["reward"])
            self.assertTrue(detail["passed"])

    def test_required_judge_missing_returns_nonzero(self):
        with tempfile.TemporaryDirectory() as root:
            rubric = {"weights": {"reply": 0.5, "judge": 0.5}, "judge_criteria": ["完成"]}
            code, rewards, detail = self.invoke(root, rubric, {"reply": "完成"})
            self.assertEqual(1, code)
            self.assertEqual(0, rewards["reward"])
            self.assertFalse(detail["valid"])
            self.assertEqual(0.5, detail["weights"]["judge"])

    def test_main_and_evaluate_share_results(self):
        with tempfile.TemporaryDirectory() as root:
            rubric = {"weights": {"reply": 1}, "reply_checks": [{"pattern": "请确认", "turn_index": 0}],
                      "tool_forbidden": [{"tool": "control", "before_turn": 1}]}
            data = {"reply": "完成", "calls": [{"name": "control", "arguments": {}, "status": "success", "turn_index": 1}],
                    "dialogue": [{"role": "assistant", "content": "请确认", "turn_index": 0},
                                 {"role": "user", "content": "确认", "turn_index": 1},
                                 {"role": "assistant", "content": "完成", "turn_index": 1}]}
            code, rewards, detail = self.invoke(root, rubric, data)
            expected = score.evaluate(rubric, data)
            self.assertEqual(0, code)
            self.assertEqual(expected["rewards"], rewards)
            self.assertEqual(expected["passed"], detail["passed"])

    def test_hard_violation_is_model_failure_not_invalid_trial(self):
        with tempfile.TemporaryDirectory() as root:
            rubric = {"weights": {"reply": 1}, "tool_forbidden": [{"tool": "control"}]}
            code, rewards, detail = self.invoke(root, rubric, {"reply": "完成", "calls": [{"name": "control", "status": "success"}]})
            self.assertEqual(0, code)
            self.assertTrue(detail["valid"])
            self.assertFalse(detail["passed"])
            self.assertEqual(0, rewards["reward"])

    def test_oracle_failure_after_state_completion_is_invalid(self):
        rubric = {"weights": {"state": 1}, "expected_state": {"done": True}}
        base = {"initial": {"state": {}}, "final": {"state": {"done": True}}}
        for marker in ({"program_failure": "program exited after completing state"},
                       {"evidence_error": "snapshot unavailable"},
                       {"cleanup_error": "session deletion failed"}):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as root:
                code, rewards, detail = self.invoke(root, rubric, {**base, **marker})
                self.assertEqual(1, code)
                self.assertEqual(0, rewards["reward"])
                self.assertFalse(detail["valid"])
                self.assertFalse(detail["passed"])
                self.assertIn(next(iter(marker)), detail["validation_errors"][0])

    def test_malformed_input_returns_invalid_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = root / "agent"
            agent.mkdir()
            rubric_path = root / "rubric.json"
            rubric_path.write_text("{", "utf8")
            with patch.multiple(score, AGENT_DIR=agent, VERIFIER_DIR=root / "verifier", RUBRIC_PATH=rubric_path,
                                TOOL_CALLS_PATH=agent / "tool-calls.json", REPLY_PATH=agent / "reply.txt"):
                self.assertEqual(1, score.main())
            detail = json.loads((root / "verifier" / "score-detail.json").read_text("utf8"))
            self.assertFalse(detail["valid"])
            self.assertTrue(detail["validation_errors"])


if __name__ == "__main__":
    unittest.main()
