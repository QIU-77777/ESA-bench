"""Trusted-side reference preflight tests; no real model or container required."""
import base64
import json
import shlex
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from harbor.environments.base import ExecResult
from esa import score
from esa.esa_agent import ESAAgent, EnvironmentTransport, HTTP_BRIDGE


class FakeEnvironment:
    def __init__(self, task_path, initial):
        self.environment_dir = task_path / "environment"
        self.initial = initial
        self.commands = []
        self.uploads = []
        self.requests = []
        self.model_runs = 0
        self.bridge_failure = False
        self.output = {"caseId": "test", "sessionId": "model-session", "turnStatus": "completed",
                       "reply": "完成", "iterations": [], "toolCalls": [], "toolEvents": [],
                       "errors": [], "dialogue": [{"role": "user", "content": "测试", "turn_index": 0}],
                       "initialSnapshot": {"state": {}}, "finalSnapshot": {"state": {}}}

    async def exec(self, command, timeout_sec=None):
        self.commands.append(command)
        if command.startswith("python3 -c "):
            argv = shlex.split(command)
            if argv[2] != HTTP_BRIDGE:
                raise AssertionError("container executes non-HTTP reference code")
            packet = json.loads(base64.b64decode(argv[3]))
            self.requests.append(packet)
            if self.bridge_failure:
                return ExecResult(return_code=1, stdout="secret response", stderr="api_key=secret-key")
            if packet["url"].endswith("/sessions"):
                return ExecResult(return_code=0, stdout=json.dumps({"session_id": "reference-session"}))
            return ExecResult(return_code=0, stdout=json.dumps({"ok": True}))
        if command.startswith("node "):
            self.model_runs += 1
        return ExecResult(return_code=0, stdout="")

    async def upload_file(self, source, target):
        self.uploads.append((str(source), target, Path(source).read_text()))

    async def download_file(self, source, target):
        payload = self.initial if source.endswith("initial_state.json") else self.output
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        Path(target).write_text(json.dumps(payload))


class ReferenceAgentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.task = self.root / "task"
        initial_path = self.task / "environment/esa/initial_state.json"
        initial_path.parent.mkdir(parents=True)
        self.initial = {"instruction": "测试", "case_id": "test", "dataset_version": "v1"}
        initial_path.write_text(json.dumps(self.initial))
        (self.task / "tests").mkdir()
        (self.task / "solution").mkdir()
        (self.task / "tests/rubric.json").write_text('{"hidden_answer":"REFERENCE_SECRET"}')
        (self.task / "solution/reference.py").write_text("# REFERENCE_SECRET program stays on host\n")
        (self.task / "tests/negative_tests.py").write_text("# hidden negative code\n")
        (self.task / "solution/solve.sh").write_bytes(b"#!/bin/bash\nset -euo pipefail\npython3 /solution/runner.py\n")
        (self.task / "solution/reference_runtime.py").write_text("# mock reference runtime copy\n")
        (self.task / "solution/runner.py").write_text("# mock oracle runner copy\n")
        self.agent = ESAAgent(logs_dir=self.root / "trial/agent", model_name="fake")
        self.env = FakeEnvironment(self.task, self.initial)
        self.context = types.SimpleNamespace(metadata={})

    async def asyncTearDown(self):
        self.tmp.cleanup()

    def module(self, validator):
        return patch.dict("sys.modules", {"esa.reference": types.SimpleNamespace(validate_task=validator)})

    async def test_failure_stops_model_and_redacts_structured_report(self):
        self.agent._extra_env["OPENAI_API_KEY"] = "secret-key"
        def fail(task_path, transport, output_dir=None):
            self.assertEqual(task_path, self.task.resolve())
            report = {"status": "failed", "passed": False, "api_key": "secret-key", "failure": "secret-key"}
            (Path(output_dir) / "golden-run.json").write_text(json.dumps(report))
            return report
        with self.module(fail):
            with self.assertRaisesRegex(RuntimeError, "模型未执行"):
                await self.agent.run("测试", self.env, self.context)
        self.assertEqual(self.env.model_runs, 0)
        self.assertEqual(self.env.uploads, [])
        report = (self.root / "trial/reference/report.json").read_text()
        self.assertNotIn("secret-key", report)
        self.assertIn("<redacted>", report)
        self.assertNotIn("secret-key", (self.root / "trial/reference/golden-run.json").read_text())
        self.assertFalse((self.root / "trial/agent/tool-calls.json").exists())

    async def test_success_uses_actual_bridge_and_independent_model_session(self):
        def passed(task_path, transport, output_dir=None):
            ref_session = transport.request("POST", "/eval/api/v1/sessions", {"type": "car"})
            transport.request("DELETE", "/eval/api/v1/sessions/" + ref_session["session_id"])
            return {"status": "pass", "passed": True, "session_id": ref_session["session_id"]}
        with self.module(passed):
            await self.agent.run("测试", self.env, self.context)
        self.assertEqual(self.env.model_runs, 1)
        self.assertEqual(self.context.metadata["session_id"], "model-session")
        self.assertEqual(self.env.requests[0]["url"], "http://mock-system:8003/eval/api/v1/sessions")
        self.assertNotEqual(json.loads((self.root / "trial/reference/report.json").read_text())["session_id"], self.context.metadata["session_id"])
        for source, target, contents in self.env.uploads:
            self.assertNotIn("reference.py", source)
            self.assertNotIn("rubric.json", source)
            self.assertNotIn("REFERENCE_SECRET", contents)
        for command in self.env.commands:
            self.assertNotIn("REFERENCE_SECRET", command)
            self.assertNotIn("reference.py", command)
        self.assertTrue((self.root / "trial/agent/dialogue.json").is_file())
        payload = json.loads((self.root / "trial/agent/tool-calls.json").read_text())
        self.assertIsNone(payload["program_failure"])
        self.assertEqual(payload["turn_status"], "completed")

    async def test_bridge_error_does_not_leak_response_or_stderr(self):
        self.env.bridge_failure = True
        def passed(task_path, transport, output_dir=None):
            transport.request("GET", "/health")
        with self.module(passed):
            with self.assertRaisesRegex(RuntimeError, "模型未执行"):
                await self.agent.run("测试", self.env, self.context)
        report = (self.root / "trial/reference/report.json").read_text()
        self.assertNotIn("secret-key", report)
        self.assertNotIn("secret response", report)
        self.assertEqual(self.env.model_runs, 0)

    async def test_initial_mismatch_and_missing_reference_fail_closed(self):
        validator_calls = []
        def passed(*args, **kwargs):
            validator_calls.append(args)
            return {"status": "pass", "passed": True}
        self.env.initial = {**self.initial, "seed": 42}
        with self.module(passed):
            with self.assertRaises(RuntimeError):
                await self.agent.run("测试", self.env, self.context)
        self.assertFalse(validator_calls)
        self.env.initial = self.initial
        (self.task / "solution/reference.py").unlink()
        with self.module(passed):
            with self.assertRaises(RuntimeError):
                await self.agent.run("测试", self.env, self.context)
        self.assertFalse(validator_calls)
        self.assertEqual(self.env.model_runs, 0)

    async def test_nonboolean_pass_and_instruction_override_are_not_certified(self):
        with self.module(lambda *args, **kwargs: {"status": "pass", "passed": 1}):
            with self.assertRaises(RuntimeError):
                await self.agent.run("测试", self.env, self.context)
        with self.module(lambda *args, **kwargs: {"status": "pass", "passed": True}):
            with self.assertRaises(RuntimeError):
                await self.agent.run("另一任务", self.env, self.context)
        self.assertEqual(self.env.model_runs, 0)

    async def test_driver_failure_marks_evidence_invalid_for_scoring(self):
        self.env.output.update({"turnStatus": "failed", "errors": ["runtime turn did not complete"]})
        with self.module(lambda *args, **kwargs: {"status": "pass", "passed": True}):
            with self.assertRaisesRegex(RuntimeError, "ESA 执行失败"):
                await self.agent.run("测试", self.env, self.context)
        payload = json.loads((self.root / "trial/agent/tool-calls.json").read_text())
        self.assertTrue(payload["program_failure"])
        self.assertEqual(payload["turn_status"], "failed")
        result = score.evaluate(
            {"weights": {"state": 1}, "expected_state": {"ok": True}},
            {**payload, "reply": "完成", "initial": {"state": {}}, "final": {"state": {"ok": True}}},
        )
        self.assertFalse(result["valid"])
        self.assertFalse(result["passed"])
        self.assertEqual(0, result["rewards"]["reward"])

    async def test_script_mismatch_stays_model_failure_not_execution_failure(self):
        self.env.output.update({"user_script_complete": False, "script_failure": "reply_pattern did not match",
                                "turnStatus": "completed", "errors": []})
        with self.module(lambda *args, **kwargs: {"status": "pass", "passed": True}):
            await self.agent.run("测试", self.env, self.context)
        payload = json.loads((self.root / "trial/agent/tool-calls.json").read_text())
        self.assertIsNone(payload["program_failure"])
        result = score.evaluate(
            {"weights": {"state": 1}, "expected_state": {"ok": True}},
            {**payload, "reply": "完成", "initial": {"state": {}}, "final": {"state": {}}},
        )
        self.assertTrue(result["valid"])
        self.assertFalse(result["passed"])

    async def test_transport_rejects_external_path_and_credential_url(self):
        import asyncio
        with self.assertRaises(ValueError):
            EnvironmentTransport(self.env, asyncio.get_running_loop(), "http://user:secret@host")
        transport = EnvironmentTransport(self.env, asyncio.get_running_loop(), "http://mock-system:8003")
        with self.assertRaises(ValueError):
            transport.request("GET", "//external.example/path")


class BridgeEnvironment(FakeEnvironment):
    """Run the container HTTP bridge locally, against a real test MCP service."""

    async def exec(self, command, timeout_sec=None):
        import asyncio
        if not command.startswith("python3 -c "):
            return await super().exec(command, timeout_sec)
        argv = shlex.split(command)
        if argv[2] != HTTP_BRIDGE:
            raise AssertionError("only the fixed HTTP bridge may execute")
        self.commands.append(command)
        self.requests.append(json.loads(base64.b64decode(argv[3])))
        process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                       stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout_sec)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        return ExecResult(return_code=process.returncode, stdout=stdout.decode(), stderr=stderr.decode())


class ReferenceBridgeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_clarification_and_response_loss_over_real_bridge(self):
        from scripts.validate_task import local_http_environment
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="esa-reference-bridge-") as tmp, local_http_environment() as http:
            for name in ("esa-navi-execute", "esa-veh-clarify", "esa-med-response-loss"):
                with self.subTest(task=name):
                    task = root / "tasks" / name
                    initial = json.loads((task / "environment/esa/initial_state.json").read_text())
                    environment = BridgeEnvironment(task, initial)
                    agent = ESAAgent(logs_dir=Path(tmp) / name / "agent", model_name="fake",
                                     extra_env={"ESA_MOCK_URL": http.base_url})
                    await agent._reference_preflight(environment, initial, initial.get("instruction", ""))
                    report = json.loads((Path(tmp) / name / "reference/report.json").read_text())
                    self.assertEqual(report["status"], "pass", report)
                    self.assertIs(report["passed"], True)
                    self.assertGreater(len(environment.requests), 0)
                    self.assertEqual(environment.model_runs, 0)
                    self.assertEqual(environment.uploads, [])
                    for command in environment.commands:
                        self.assertEqual(shlex.split(command)[2], HTTP_BRIDGE)
                    sessions = [packet["url"] for packet in environment.requests if packet["method"] == "DELETE"]
                    self.assertGreater(len(set(sessions)), 1)  # reference and negative sessions are independent


if __name__ == "__main__":
    unittest.main()
