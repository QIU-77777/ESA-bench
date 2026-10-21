"""ESA 交互式命令行服务组合冒烟测试。

覆盖 --local 宿主进程模式与默认 Docker 服务组合模式。设置 ESA_CLI_INTEGRATION=1 启用。
测试自行启动假模型接口作为模型端点。
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
MODEL_PORT = 19092


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


def wait_port(port, timeout):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def read_log(path):
    if not path.is_file():
        return ""
    return path.read_text("utf-8", "replace")[-600:]


@unittest.skipUnless(os.getenv("ESA_CLI_INTEGRATION") == "1", "设置 ESA_CLI_INTEGRATION=1 启用服务组合冒烟测试")
class StackChatTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name)
        cls.processes = []
        cls.logs = {}
        cls.log_files = []
        model_log = (cls.root / "model.log").open("w")
        cls.log_files.append(model_log)
        cls.model = subprocess.Popen(
            [sys.executable, str(REPO_ROOT / "esa" / "fake_responses.py"), "--port", str(MODEL_PORT)],
            stdout=model_log, stderr=subprocess.STDOUT)
        cls.processes.append(cls.model)
        if not wait_port(MODEL_PORT, 15):
            cls.tearDownClass()
            raise unittest.SkipTest("假模型接口未就绪: " + read_log(cls.root / "model.log"))

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

    def write_env_file(self, name, base_url):
        path = self.root / name
        path.write_text("STEPFUN_OPENAI_BASE_URL=" + base_url + "\nSTEPFUN_API_KEY=offline\n", "utf-8")
        return path

    def run_cli(self, session_dir, env_file, extra_arguments, script):
        command = [
            sys.executable, "-m", "esa.cli", "chat",
            "--env", str(env_file),
            "--session-dir", str(session_dir),
            "--turn-timeout", "60",
        ] + list(extra_arguments)
        return subprocess.run(command, input=script, capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=300)

    def assert_chat_succeeded(self, result, session_dir):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("ESA 交互会话", result.stdout)
        self.assertIn("已开始导航", result.stdout)
        directories = sorted(path for path in session_dir.iterdir() if path.is_dir() and (path / "meta.json").is_file())
        self.assertEqual(len(directories), 1)
        evidence = directories[0]
        for name in ("transcript.json", "snapshot-initial.json", "snapshot-final.json", "state-events.json", "meta.json"):
            self.assertTrue((evidence / name).is_file(), name)
        final = json.loads((evidence / "snapshot-final.json").read_text("utf-8"))
        self.assertTrue(final["state"]["navigation"]["active"])
        self.assertEqual(final["state"]["navigation"]["destination"]["id"], "200001")

    def test_local_stack_chat(self):
        if shutil.which("redis-server") is None or shutil.which("go") is None:
            self.skipTest("缺少 redis-server 或 go 可执行文件")
        if not (REPO_ROOT / "environment" / ".venv" / "bin" / "python").is_file():
            self.skipTest("缺少环境服务虚拟环境")
        env_file = self.write_env_file("local.env", "http://127.0.0.1:" + str(MODEL_PORT) + "/v1")
        session_dir = self.root / "sessions-local"
        result = self.run_cli(session_dir, env_file, ["--local"], "导航去公司\n现在的目的地是什么\n/quit\n")
        self.assert_chat_succeeded(result, session_dir)

    def test_docker_stack_chat(self):
        if shutil.which("docker") is None:
            self.skipTest("缺少 docker 可执行文件")
        for image in ("esa-react-runtime:1.0.0", "esa-local-environment:1.0.0"):
            inspect = subprocess.run(["docker", "image", "inspect", image], capture_output=True)
            if inspect.returncode != 0:
                self.skipTest("缺少镜像 " + image)
        env_file = self.write_env_file("docker.env", "http://host.docker.internal:" + str(MODEL_PORT) + "/v1")
        session_dir = self.root / "sessions-docker"
        result = self.run_cli(session_dir, env_file, ["--ephemeral"], "导航去公司\n现在的目的地是什么\n/quit\n")
        self.assert_chat_succeeded(result, session_dir)
        remaining = subprocess.run(["docker", "compose", "-p", "esa-dev", "ps", "--status", "running"], capture_output=True, text=True)
        self.assertNotIn("agent-api", remaining.stdout)


if __name__ == "__main__":
    unittest.main()
