"""Native Oracle executes uploaded solution only; verifier reads tests separately."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from esa import oracle, score
from esa.reference import TransportError
from scripts.validate_task import local_http_environment

ROOT = Path(__file__).resolve().parents[1]
TASK_NAMES = ("esa-navi-execute", "esa-chg-select", "esa-veh-clarify", "esa-inf-knowledge",
              "esa-veh-boundary", "esa-med-retry", "esa-med-response-loss")


def stage_solution(task, target):
    solution = target / "solution"
    shutil.copytree(task / "solution", solution)
    initial = target / "environment/esa/initial_state.json"
    initial.parent.mkdir(parents=True)
    shutil.copyfile(task / "environment/esa/initial_state.json", initial)
    return initial, solution


def verify_artifacts(task, artifacts, verifier_dir):
    with patch.multiple(score, AGENT_DIR=artifacts, VERIFIER_DIR=verifier_dir,
                        RUBRIC_PATH=task / "tests/rubric.json", TOOL_CALLS_PATH=artifacts / "tool-calls.json",
                        REPLY_PATH=artifacts / "reply.txt"):
        result = score.main()
    return result, json.loads((verifier_dir / "reward.json").read_text()), json.loads((verifier_dir / "score-detail.json").read_text())


class OracleIntegrationTests(unittest.TestCase):
    def test_seven_native_solutions_need_no_tests_and_pass_independent_verifier(self):
        session_ids = []
        with tempfile.TemporaryDirectory(prefix="esa-native-oracle-") as tmp, local_http_environment() as transport:
            for name in TASK_NAMES:
                with self.subTest(task=name):
                    task = ROOT / "tasks" / name
                    staged = Path(tmp) / name
                    staged.mkdir()
                    initial, solution = stage_solution(task, staged)
                    self.assertFalse((staged / "tests").exists())
                    self.assertFalse((solution / "negative_tests.py").exists())
                    self.assertFalse((solution / "rubric.json").exists())
                    output = staged / "logs/agent"
                    original_read = Path.read_text
                    def guarded_read(path, *args, **kwargs):
                        self.assertNotIn("tests", path.parts, "Oracle attempted to read verifier material")
                        self.assertNotIn("negative_tests.py", path.parts, "Oracle attempted to read negatives")
                        return original_read(path, *args, **kwargs)
                    with patch.object(Path, "read_text", guarded_read):
                        evidence = oracle.run(initial, solution, output, transport)
                    self.assertIsNone(evidence["program_failure"], evidence)
                    session_ids.append(evidence["session_id"])
                    self.assertFalse((output / "reward.json").exists())
                    tool_data = json.loads((output / "tool-calls.json").read_text())
                    self.assertEqual(tool_data["calls"], evidence["calls"])
                    self.assertEqual(tool_data["events"], evidence["events"])
                    self.assertEqual(tool_data["dialogue"], evidence["dialogue"])
                    self.assertEqual(json.loads((output / "snapshot-final.json").read_text()), evidence["final"])
                    self.assertNotIn("evidence_error", tool_data)
                    self.assertNotIn("cleanup_error", tool_data)
                    result, reward, detail = verify_artifacts(task, output, staged / "logs/verifier")
                    self.assertEqual(result, 0, detail)
                    self.assertEqual(reward["reward"], 1.0, detail)
                    self.assertTrue(detail["valid"])
                    self.assertTrue(detail["passed"])
                    with self.assertRaises(TransportError):
                        transport.request("GET", "/api/v1/rl/sessions/" + evidence["session_id"] + "/snapshot")
            self.assertEqual(len(session_ids), len(set(session_ids)))

    def test_standalone_runner_uses_stdlib_with_only_solution_and_initial_uploaded(self):
        task = ROOT / "tasks/esa-navi-execute"
        with tempfile.TemporaryDirectory(prefix="esa-standalone-oracle-") as tmp, local_http_environment() as transport:
            staged = Path(tmp)
            initial, solution = stage_solution(task, staged)
            # Stage canonical copies so this test is independent of sync timing.
            shutil.copyfile(ROOT / "esa/oracle.py", solution / "runner.py")
            shutil.copyfile(ROOT / "esa/reference.py", solution / "reference_runtime.py")
            output = staged / "logs/agent"
            process = subprocess.run([sys.executable, "-S", str(solution / "runner.py"), "--initial", str(initial),
                                      "--solution", str(solution), "--output-dir", str(output), "--mock-url", transport.base_url],
                                     cwd=staged, env={**os.environ, "PYTHONPATH": ""}, capture_output=True, text=True, timeout=30)
            self.assertEqual(process.returncode, 0, process.stdout + process.stderr)
            self.assertFalse((staged / "tests").exists())
            self.assertEqual(json.loads(process.stdout)["status"], "completed")
            result, reward, detail = verify_artifacts(task, output, staged / "logs/verifier")
            self.assertEqual(result, 0, detail)
            self.assertEqual(reward["reward"], 1.0)

    def test_program_failure_after_correct_state_is_invalid_and_session_deleted(self):
        task = ROOT / "tasks/esa-navi-execute"
        with tempfile.TemporaryDirectory(prefix="esa-oracle-failure-") as tmp, local_http_environment() as transport:
            staged = Path(tmp)
            initial, solution = stage_solution(task, staged)
            reference = solution / "reference.py"
            reference.write_text(reference.read_text() + "\noriginal_solve = solve\ndef solve(ctx):\n    original_solve(ctx)\n    raise RuntimeError('do-not-log-secret')\n")
            output = staged / "logs/agent"
            evidence = oracle.run(initial, solution, output, transport)
            self.assertTrue(evidence["final"]["state"]["navigation"]["active"])
            self.assertIsNotNone(evidence["program_failure"])
            self.assertNotIn("do-not-log-secret", (output / "tool-calls.json").read_text())
            result, reward, detail = verify_artifacts(task, output, staged / "logs/verifier")
            self.assertEqual(result, 1, detail)
            self.assertEqual(reward["reward"], 0.0)
            self.assertFalse(detail["valid"])
            self.assertFalse(detail["passed"])
            with self.assertRaises(TransportError):
                transport.request("GET", "/api/v1/rl/sessions/" + evidence["session_id"] + "/snapshot")

    def test_missing_reference_and_transport_failure_save_failure_artifacts(self):
        task = ROOT / "tasks/esa-navi-execute"
        with tempfile.TemporaryDirectory(prefix="esa-oracle-bad-input-") as tmp:
            staged = Path(tmp)
            initial, solution = stage_solution(task, staged)
            output = staged / "logs/agent"
            class BrokenTransport:
                def request(self, *args, **kwargs):
                    raise RuntimeError("SECRET_HTTP_BODY")
            evidence = oracle.run(initial, solution, output, BrokenTransport())
            self.assertIsNotNone(evidence["program_failure"])
            self.assertNotIn("SECRET_HTTP_BODY", (output / "tool-calls.json").read_text())
            result, reward, detail = verify_artifacts(task, output, staged / "logs/verifier")
            self.assertEqual(result, 1)
            self.assertEqual(reward["reward"], 0.0)
            empty_solution = staged / "empty-solution"
            empty_solution.mkdir()
            evidence = oracle.run(initial, empty_solution, staged / "missing/agent", BrokenTransport())
            self.assertIsNotNone(evidence["program_failure"])
            self.assertTrue((staged / "missing/agent/tool-calls.json").exists())

    def test_cli_exits_nonzero_when_reference_fails(self):
        task = ROOT / "tasks/esa-navi-execute"
        with tempfile.TemporaryDirectory(prefix="esa-oracle-exit-") as tmp:
            initial, solution = stage_solution(task, Path(tmp))
            reference = solution / "reference.py"
            reference.write_text("def solve(ctx):\n    raise RuntimeError('HIDDEN_SECRET')\n")
            with local_http_environment() as transport:
                result = oracle.main(["--initial", str(initial), "--solution", str(solution),
                                      "--output-dir", str(Path(tmp) / "logs/agent"), "--mock-url", transport.base_url])
            self.assertEqual(result, 1)
            self.assertNotIn("HIDDEN_SECRET", (Path(tmp) / "logs/agent/tool-calls.json").read_text())

    def test_sensitive_evidence_uses_shared_redaction(self):
        task = ROOT / "tasks/esa-navi-execute"
        with tempfile.TemporaryDirectory(prefix="esa-oracle-redaction-") as tmp:
            initial, solution = stage_solution(task, Path(tmp))
            evidence = {"initial": {"state": {}}, "final": {"state": {}, "manifest": {}}, "events": [], "calls": [],
                        "dialogue": [], "reply": "shared-secret-value", "program_failure": None, "api_key": "shared-secret-value"}
            with patch.dict(os.environ, {"OPENAI_API_KEY": "shared-secret-value"}), patch("esa.reference.run_program", return_value=evidence):
                output = Path(tmp) / "logs/agent"
                result = oracle.run(initial, solution, output, object())
            self.assertEqual(result["reply"], "<redacted>")
            self.assertEqual(result["api_key"], "<redacted>")
            self.assertNotIn("shared-secret-value", (output / "reply.txt").read_text())


if __name__ == "__main__":
    unittest.main()
