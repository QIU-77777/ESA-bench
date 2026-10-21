import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from esa import cli


def make_settings(tmp, no_stack=True, **overrides):
    parser = cli.build_parser()
    arguments = ["chat", "--session-dir", str(tmp / "sessions")]
    if no_stack:
        arguments.append("--no-stack")
    for key, value in overrides.items():
        flag = "--" + key.replace("_", "-")
        arguments.append(flag)
        if not isinstance(value, bool):
            arguments.append(str(value))
    args = parser.parse_args(arguments)
    env_path = Path(args.env)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    return cli.Settings(args, cli.load_env_file(env_path))


class EnvAndTaskTests(unittest.TestCase):
    def test_load_env_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "test.env"
            path.write_text("# comment\nSTEPFUN_API_KEY=secret\nQUOTED=\"value\"\nEMPTY=\n\nBAD LINE\n", "utf-8")
            values = cli.load_env_file(path)
            self.assertEqual(values, {"STEPFUN_API_KEY": "secret", "QUOTED": "value", "EMPTY": ""})
            self.assertEqual(cli.load_env_file(Path(tmp) / "missing.env"), {})

    def test_load_task_directory_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            task_dir = root / "task"
            (task_dir / "environment" / "esa").mkdir(parents=True)
            payload = {"case_id": "T", "instruction": "导航去公司", "initial_state": {"vehicle": {"gear": "D"}}}
            (task_dir / "environment" / "esa" / "initial_state.json").write_text(json.dumps(payload), "utf-8")
            self.assertEqual(cli.load_task(str(task_dir)), payload)
            self.assertEqual(cli.load_task(str(task_dir / "environment" / "esa" / "initial_state.json")), payload)
            with self.assertRaises(cli.CliError):
                cli.load_task(str(root / "missing"))

    def test_first_task_message(self):
        self.assertEqual(cli.first_task_message({"instruction": "导航去公司"}), "导航去公司")
        self.assertEqual(cli.first_task_message({"user_messages": ["第一条", "第二条"]}), "第一条")
        self.assertIsNone(cli.first_task_message({}))


class RenderHelperTests(unittest.TestCase):
    def test_compact_arguments(self):
        self.assertEqual(cli.compact_arguments(json.dumps({"a": 1})), '{"a":1}')
        self.assertEqual(cli.compact_arguments("not json"), "not json")
        long_value = "x" * 400
        self.assertTrue(cli.compact_arguments(json.dumps({"v": long_value})).endswith("..."))

    def test_summarize_output(self):
        envelope = json.dumps({"content": [{"text": "多行\n结果"}], "isError": False})
        self.assertEqual(cli.summarize_output(envelope), "多行 结果")
        error_envelope = json.dumps({"content": [{"text": "拒绝"}], "isError": True})
        self.assertEqual(cli.summarize_output(error_envelope), "错误: 拒绝")
        self.assertEqual(cli.summarize_output(""), "")
        long_text = "y" * 400
        self.assertTrue(cli.summarize_output(json.dumps({"content": [{"text": long_text}]})).endswith("..."))


class SettingsTests(unittest.TestCase):
    def test_docker_mode_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            self.assertEqual(settings.mock_url, "http://127.0.0.1:8003")
            self.assertEqual(settings.mock_internal_url, "http://mock-system:8003")
            self.assertEqual(settings.runtime_url, "http://127.0.0.1:8080")

    def test_local_mode_defaults_and_model_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), local=True, model="other-model")
            self.assertEqual(settings.mock_url, "http://127.0.0.1:18003")
            self.assertEqual(settings.mock_internal_url, "http://127.0.0.1:18003")
            self.assertEqual(settings.runtime_url, "http://127.0.0.1:18080")
            self.assertEqual(settings.model_id, "other-model")
            self.assertEqual(settings.agent["react"]["model"]["model_id"], "other-model")


class ChatTurnTests(unittest.TestCase):
    def build_chat(self, settings):
        chat = cli.VehicleChat(settings, session_id="session-1", transcript=[])
        return chat

    def test_turn_sends_transcript_and_stores_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            chat = self.build_chat(settings)
            chat.transcript = [{"role": "user", "content": "上一轮"}]
            returned = [
                {"role": "user", "content": "上一轮"},
                {"type": "function_call", "call_id": "c1", "name": "echo", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "done"},
                {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "本轮回复"}]},
            ]
            with patch.object(cli, "http_json", return_value={"status": "completed", "reply": "本轮回复", "transcript": returned}) as mocked:
                result = chat.turn("新消息")
            self.assertEqual(result["reply"], "本轮回复")
            self.assertEqual(chat.transcript, returned)
            self.assertEqual(chat.turn_count, 1)
            body = mocked.call_args.args[2]
            self.assertEqual(body["user_messages"], ["新消息"])
            self.assertEqual(body["transcript"], [{"role": "user", "content": "上一轮"}])
            self.assertEqual(body["mockSystemUrl"], settings.mock_internal_url)
            self.assertEqual(body["sessionId"], "session-1")
            self.assertNotIn("model", body)

    def test_turn_includes_model_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp), model="override-model")
            chat = self.build_chat(settings)
            with patch.object(cli, "http_json", return_value={"status": "completed", "reply": "", "transcript": []}) as mocked:
                chat.turn("消息")
            self.assertEqual(mocked.call_args.args[2]["model"], "override-model")


class EvidenceTests(unittest.TestCase):
    def test_write_evidence_and_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            chat = cli.VehicleChat(settings, session_id="session-2", transcript=[{"role": "user", "content": "历史"}])
            chat.initial_snapshot = {"state": {"vehicle": {"gear": "D"}}, "state_version": 1}
            chat.turn_count = 2

            def fake_http(url, method="GET", body=None, timeout=30):
                if url.endswith("/snapshot"):
                    return {"state": {"navigation": {"active": True}}, "state_version": 3}
                return {"events": [{"sequence": 1, "tool": "start_navi"}], "truncated": False}

            with patch.object(cli, "http_json", side_effect=fake_http):
                cli.write_evidence(chat)
            directory = chat.evidence_dir
            for name in ("transcript.json", "snapshot-initial.json", "snapshot-final.json", "state-events.json", "meta.json"):
                self.assertTrue((directory / name).is_file(), name)
            final = json.loads((directory / "snapshot-final.json").read_text("utf-8"))
            self.assertTrue(final["state"]["navigation"]["active"])
            meta = json.loads((directory / "meta.json").read_text("utf-8"))
            self.assertEqual(meta["session_id"], "session-2")
            self.assertEqual(meta["turns"], 2)
            cli.archive_evidence(chat, 0)
            self.assertTrue((directory / "reset-0" / "transcript.json").is_file())
            self.assertEqual(cli.read_meta(directory)["session_id"], "session-2")

    def test_write_evidence_survives_service_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            chat = cli.VehicleChat(settings, session_id="session-3", transcript=[])
            chat.initial_snapshot = {"state": {}}
            with patch.object(cli, "http_json", side_effect=cli.CliError("服务不可达")):
                cli.write_evidence(chat)
            self.assertTrue((chat.evidence_dir / "snapshot-final.json").is_file())


class ResumeTests(unittest.TestCase):
    def test_resume_requires_evidence_and_live_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            with self.assertRaises(cli.CliError):
                cli.resume_session(settings, "absent")
            directory = settings.session_dir / "session-4"
            directory.mkdir(parents=True)
            (directory / "transcript.json").write_text(json.dumps([{"role": "user", "content": "旧消息"}]), "utf-8")
            with patch.object(cli, "http_json", side_effect=cli.CliError("404")):
                with self.assertRaises(cli.CliError):
                    cli.resume_session(settings, "session-4")
            with patch.object(cli, "http_json", return_value={"state": {}, "state_version": 2}):
                chat = cli.resume_session(settings, "session-4")
            self.assertEqual(chat.transcript, [{"role": "user", "content": "旧消息"}])
            self.assertEqual(chat.initial_snapshot["state_version"], 2)


class ControllerTests(unittest.TestCase):
    def build_controller(self, settings, chat):
        return cli.ChatController(settings, chat)

    def test_dispatch_commands(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            chat = cli.VehicleChat(settings, session_id="session-5", transcript=[])
            controller = self.build_controller(settings, chat)
            snapshots = {"state": {"navigation": {"active": True}}, "manifest": {"tools": [{"name": "a", "enabled": True}, {"name": "b", "enabled": False}]}}

            def fake_http(url, method="GET", body=None, timeout=30):
                if url.endswith("/snapshot"):
                    return snapshots
                if url.endswith("/events"):
                    return {"events": [{"sequence": 1, "tool": "start_navi", "status": "ok", "before_version": 1, "after_version": 2, "changes": [{"path": "navigation.active"}]}], "truncated": False}
                return {}

            with patch.object(cli, "http_json", side_effect=fake_http):
                self.assertTrue(controller.dispatch("/state"))
                self.assertTrue(controller.dispatch("/events"))
                self.assertTrue(controller.dispatch("/tools"))
            self.assertFalse(controller.dispatch("/quit"))
            self.assertFalse(controller.dispatch("/exit"))
            self.assertTrue(controller.dispatch("/unknown"))

    def test_reset_archives_and_clears_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            chat = cli.VehicleChat(settings, session_id="session-6", transcript=[{"role": "user", "content": "旧"}])
            chat.initial_snapshot = {"state": {}, "state_version": 1}
            controller = self.build_controller(settings, chat)
            calls = []

            def fake_http(url, method="GET", body=None, timeout=30):
                calls.append((method, url))
                if url.endswith("/reset"):
                    return {"reset": True, "state_version": 1}
                return {"state": {}, "state_version": 1}

            with patch.object(cli, "http_json", side_effect=fake_http), patch.object(cli, "archive_evidence") as archive:
                controller.command_reset()
            archive.assert_called_once()
            self.assertEqual(chat.transcript, [])
            self.assertTrue(any(url.endswith("/reset") for _, url in calls))


class ServiceStartupTests(unittest.TestCase):
    def test_missing_endpoint_reports_configuration_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "empty.env").write_text("", "utf-8")
            settings = make_settings(root, no_stack=False, env=str(root / "empty.env"))
            with patch.dict(os.environ, {}, clear=True), patch.object(cli, "health_ok", return_value=False):
                with self.assertRaisesRegex(cli.CliError, "未配置模型端点"):
                    cli.ensure_services(settings, root / "logs")

    def test_configured_endpoint_starts_stack(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "chat.env").write_text("STEPFUN_OPENAI_BASE_URL=https://example.invalid/v1\nSTEPFUN_API_KEY=offline\n", "utf-8")
            settings = make_settings(root, no_stack=False, env=str(root / "chat.env"))
            with patch.dict(os.environ, {}, clear=True), patch.object(cli, "health_ok", return_value=False), patch.object(cli, "start_docker_stack", return_value=[]) as start:
                cli.ensure_services(settings, root / "logs")
            start.assert_called_once()

    def test_healthy_services_skip_startup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            with patch.dict(os.environ, {}, clear=True), patch.object(cli, "health_ok", return_value=True):
                self.assertEqual(cli.ensure_services(settings, root / "logs"), [])


class CommandBoxTests(unittest.TestCase):
    def capture(self, prefix):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            cli.command_box(prefix)
        return stream.getvalue()

    def test_slash_shows_all_commands(self):
        output = self.capture("/")
        self.assertIn("可用命令:", output)
        for name, _ in cli.COMMANDS:
            self.assertIn(name, output)

    def test_prefix_filters_commands(self):
        output = self.capture("/st")
        self.assertIn("/state", output)
        self.assertNotIn("/events", output)

    def test_no_match_shows_all_commands(self):
        output = self.capture("/zz")
        self.assertIn("没有以 /zz 开头的命令", output)
        for name, _ in cli.COMMANDS:
            self.assertIn(name, output)


if __name__ == "__main__":
    unittest.main()
