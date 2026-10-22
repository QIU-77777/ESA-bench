"""ESA 交互式命令行端到端冒烟测试。

设置 ESA_CLI_INTEGRATION=1 启用。测试自行启动假模型接口、Redis、环境服务和 runtime，
以脚本化标准输入驱动 esa chat，验证回复、业务状态和证据文件。
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

MODEL_PORT = 19091
REDIS_PORT = 16398
MOCK_PORT = 18003
RUNTIME_PORT = 18080


def wait_health(url, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except Exception:
            time.sleep(0.3)
    return False


def read_log(path):
    if not path.is_file():
        return ""
    return path.read_text("utf-8", "replace")[-800:]


@unittest.skipUnless(os.getenv("ESA_CLI_INTEGRATION") == "1", "设置 ESA_CLI_INTEGRATION=1 启用端到端冒烟测试")
class CliIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.processes = []
        cls.logs = {}
        cls.log_files = []
        if shutil.which("redis-server") is None:
            raise unittest.SkipTest("缺少 redis-server 可执行文件")
        if not (REPO_ROOT / "environment" / ".venv" / "bin" / "python").is_file():
            raise unittest.SkipTest("缺少环境服务虚拟环境")
        if shutil.which("go") is None:
            raise unittest.SkipTest("缺少 go 可执行文件")
        try:
            cls.spawn("model", [sys.executable, str(REPO_ROOT / "esa" / "fake_responses.py"), "--port", str(MODEL_PORT)])
            cls.spawn("redis", ["redis-server", "--port", str(REDIS_PORT), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"])
            cls.spawn("environment", [
                str(REPO_ROOT / "environment" / ".venv" / "bin" / "python"), "-m", "environment.server",
                "--host", "127.0.0.1", "--port", str(MOCK_PORT),
                "--redis-url", "redis://127.0.0.1:" + str(REDIS_PORT) + "/0",
                "--db", str(cls.root / "esa.sqlite3"),
            ], cwd=REPO_ROOT)
            binary = cls.root / "esa-runtime"
            build = subprocess.run(["go", "build", "-o", str(binary), "."], cwd=str(REPO_ROOT / "runtime"), capture_output=True, text=True)
            if build.returncode != 0:
                raise unittest.SkipTest("runtime 构建失败: " + build.stderr[-300:])
            env = dict(os.environ)
            env["PORT"] = str(RUNTIME_PORT)
            env["RESPONSES_API_BASE_URL"] = "http://127.0.0.1:" + str(MODEL_PORT) + "/v1"
            env["RESPONSES_API_KEY"] = "offline"
            cls.spawn("runtime", [str(binary)], env=env)
            if not wait_health("http://127.0.0.1:" + str(MOCK_PORT) + "/health", 60):
                raise unittest.SkipTest("环境服务未就绪: " + read_log(cls.logs["environment"]))
            if not wait_health("http://127.0.0.1:" + str(RUNTIME_PORT) + "/v1/health", 30):
                raise unittest.SkipTest("runtime 未就绪: " + read_log(cls.logs["runtime"]))
        except unittest.SkipTest:
            cls.tearDownClass()
            raise

    @classmethod
    def spawn(cls, name, command, env=None, cwd=None):
        log_path = cls.root / (name + ".log")
        cls.logs[name] = log_path
        log = log_path.open("w")
        cls.log_files.append(log)
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env, cwd=str(cwd) if cwd else None)
        cls.processes.append(process)
        return process

    @classmethod
    def tearDownClass(cls):
        for process in getattr(cls, "processes", []):
            if process.poll() is None:
                process.terminate()
        for process in getattr(cls, "processes", []):
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for handle in getattr(cls, "log_files", []):
            handle.close()
        if hasattr(cls, "tmp"):
            cls.tmp.cleanup()

    def test_scripted_chat_session(self):
        root = self.root
        session_dir = root / "sessions"
        script = "导航去公司\n现在的目的地是什么\n/state\n/quit\n"
        command = [
            sys.executable, "-m", "esa.cli", "chat",
            "--no-stack",
            "--mock-url", "http://127.0.0.1:" + str(MOCK_PORT),
            "--mock-internal-url", "http://127.0.0.1:" + str(MOCK_PORT),
            "--runtime-url", "http://127.0.0.1:" + str(RUNTIME_PORT),
            "--session-dir", str(session_dir),
            "--turn-timeout", "60",
        ]
        result = subprocess.run(command, input=script, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=240)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ESA 交互会话", result.stdout)
        self.assertIn("已开始导航", result.stdout)
        self.assertIn("ESA >", result.stdout)
        directories = sorted(path for path in session_dir.iterdir() if path.is_dir())
        self.assertEqual(len(directories), 1)
        evidence = directories[0]
        for name in ("transcript.json", "snapshot-initial.json", "snapshot-final.json", "state-events.json", "meta.json"):
            self.assertTrue((evidence / name).is_file(), name)
        final = json.loads((evidence / "snapshot-final.json").read_text("utf-8"))
        state = final["state"]
        self.assertTrue(state["navigation"]["active"])
        self.assertEqual(state["navigation"]["destination"]["id"], "200001")
        transcript = json.loads((evidence / "transcript.json").read_text("utf-8"))
        self.assertGreaterEqual(len(transcript), 6)
        events = json.loads((evidence / "state-events.json").read_text("utf-8"))["events"]
        tool_names = [event["tool"] for event in events if not str(event["tool"]).startswith("session/")]
        self.assertIn("get_favorite_poi", tool_names)
        self.assertIn("start_navi", tool_names)


if __name__ == "__main__":
    unittest.main()
