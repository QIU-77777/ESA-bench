#!/usr/bin/env python3
"""ESA 交互式命令行：与车载 Agent 直接对话。

入口：python -m esa.cli chat
命令行不执行 ReAct 循环，不直接调用模型，全部经过 runtime，与批量评测走同一实现。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
AGENT_PAYLOAD_PATH = REPO_ROOT / "esa" / "agent.json"
DEV_COMPOSE_PATH = REPO_ROOT / "docker-compose.dev.yaml"
ENV_VENV_PYTHON = REPO_ROOT / "environment" / ".venv" / "bin" / "python"
RUNTIME_DIR = REPO_ROOT / "runtime"
QUOTE_CHARS = "\"'"

DOCKER_MOCK_URL = "http://127.0.0.1:8003"
DOCKER_MOCK_INTERNAL_URL = "http://mock-system:8003"
DOCKER_RUNTIME_URL = "http://127.0.0.1:8080"
LOCAL_MOCK_URL = "http://127.0.0.1:18003"
LOCAL_RUNTIME_URL = "http://127.0.0.1:18080"
LOCAL_REDIS_PORT = 16379
READY_TIMEOUT_SEC = 120.0

STATUS_LABELS = {
    "completed": "完成",
    "failed": "失败",
    "limit_exceeded": "达到迭代上限",
    "timeout": "超时",
    "cancelled": "已取消",
}


class CliError(Exception):
    """可直接展示给用户的错误。"""


def load_env_file(path):
    values = {}
    if not path.is_file():
        return values
    for line in path.read_text("utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip(QUOTE_CHARS)
    return values


def http_json(url, method="GET", body=None, timeout=30):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", "replace")[:500]
        raise CliError(method + " " + url + " 返回 " + str(error.code) + ": " + detail) from error
    except urllib.error.URLError as error:
        raise CliError("无法连接 " + url + ": " + str(error.reason)) from error
    return json.loads(raw) if raw else {}


def health_ok(url):
    try:
        http_json(url, timeout=3)
        return True
    except CliError:
        return False


def port_open(port, host="127.0.0.1", timeout=1.0):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def load_task(path_text):
    path = Path(path_text)
    if path.is_dir():
        path = path / "environment" / "esa" / "initial_state.json"
    if not path.is_file():
        raise CliError("任务文件不存在: " + str(path))
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict):
        raise CliError("任务文件必须是 JSON 对象")
    return data


def first_task_message(task):
    instruction = task.get("instruction")
    if isinstance(instruction, str) and instruction.strip():
        return instruction.strip()
    messages = task.get("user_messages")
    if isinstance(messages, list) and messages and isinstance(messages[0], str):
        return messages[0]
    return None


class Settings:
    def __init__(self, args, env_values):
        self.local = bool(args.local)
        self.no_stack = bool(args.no_stack)
        self.ephemeral = bool(args.ephemeral)
        self.verbose = bool(args.verbose)
        self.model = args.model
        self.turn_timeout = float(args.turn_timeout)
        self.max_iterations = int(args.max_iterations)
        self.task_path = args.task
        self.resume = args.resume
        self.session_dir = Path(args.session_dir).expanduser()
        self.runtime_url = args.runtime_url or (LOCAL_RUNTIME_URL if self.local else DOCKER_RUNTIME_URL)
        self.mock_url = args.mock_url or (LOCAL_MOCK_URL if self.local else DOCKER_MOCK_URL)
        self.mock_internal_url = args.mock_internal_url or (LOCAL_MOCK_URL if self.local else DOCKER_MOCK_INTERNAL_URL)
        self.env = env_values
        self.agent = json.loads(AGENT_PAYLOAD_PATH.read_text("utf-8"))
        react = self.agent.setdefault("react", {})
        model = react.setdefault("model", {})
        if self.model:
            model["model_id"] = self.model
        self.model_id = model.get("model_id", "")
        self.task = load_task(self.task_path) if self.task_path else {}


def wait_ready(settings):
    deadline = time.monotonic() + READY_TIMEOUT_SEC
    while time.monotonic() < deadline:
        if health_ok(settings.mock_url + "/health") and health_ok(settings.runtime_url + "/v1/health"):
            return
        time.sleep(1.0)
    raise CliError("服务在 " + str(int(READY_TIMEOUT_SEC)) + " 秒内未就绪。请执行 docker compose -p esa-dev -f docker-compose.dev.yaml logs 查看日志。")


def start_docker_stack(settings):
    if shutil.which("docker") is None:
        raise CliError("未找到 docker 可执行文件。请安装 Docker Desktop，或使用 --local 模式。")
    base = settings.env.get("STEPFUN_OPENAI_BASE_URL", "")
    if base and ("127.0.0.1" in base or "localhost" in base):
        print("提示：模型端点使用本机地址，容器内无法访问。请改用 host.docker.internal。")
    env = dict(os.environ)
    env.update(settings.env)
    command = ["docker", "compose", "-p", "esa-dev", "-f", str(DEV_COMPOSE_PATH), "up", "-d"]
    result = subprocess.run(command, env=env, capture_output=True, text=True)
    if result.returncode != 0:
        raise CliError("启动开发服务失败: " + result.stderr.strip()[:500])
    wait_ready(settings)
    return []


def start_local_stack(settings, log_dir):
    processes = []
    log_dir.mkdir(parents=True, exist_ok=True)
    if not port_open(LOCAL_REDIS_PORT):
        if shutil.which("redis-server") is None:
            raise CliError("未找到 redis-server，且本机 Redis 未运行。请安装 Redis 或使用 Docker 模式。")
        log = (log_dir / "redis.log").open("w")
        processes.append(subprocess.Popen(
            ["redis-server", "--port", str(LOCAL_REDIS_PORT), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
    if not health_ok(LOCAL_MOCK_URL + "/health"):
        if not ENV_VENV_PYTHON.is_file():
            raise CliError("未找到环境服务虚拟环境: " + str(ENV_VENV_PYTHON))
        db_path = settings.session_dir / "dev-data" / "esa-seed-v1.sqlite3"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        log = (log_dir / "environment.log").open("w")
        processes.append(subprocess.Popen(
            [str(ENV_VENV_PYTHON), "-m", "environment.server", "--host", "127.0.0.1", "--port", "18003",
             "--redis-url", "redis://127.0.0.1:" + str(LOCAL_REDIS_PORT) + "/0", "--db", str(db_path)],
            cwd=str(REPO_ROOT), stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
    if not health_ok(LOCAL_RUNTIME_URL + "/v1/health"):
        if shutil.which("go") is None:
            raise CliError("未找到 go 可执行文件，且 runtime 未运行。请安装 Go 或使用 Docker 模式。")
        env = dict(os.environ)
        env.update(settings.env)
        env["PORT"] = "18080"
        base = model_endpoint(settings)
        key = settings.env.get("STEPFUN_API_KEY") or os.environ.get("STEPFUN_API_KEY", "")
        if base:
            env["RESPONSES_API_BASE_URL"] = base
        if key:
            env["RESPONSES_API_KEY"] = key
        log = (log_dir / "runtime.log").open("w")
        processes.append(subprocess.Popen(["go", "run", "."], cwd=str(RUNTIME_DIR), env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
    wait_ready(settings)
    return processes


def model_endpoint(settings):
    """解析模型端点。来源顺序：.env 文件、shell 环境变量。"""
    return settings.env.get("STEPFUN_OPENAI_BASE_URL") or os.environ.get("STEPFUN_OPENAI_BASE_URL") or os.environ.get("RESPONSES_API_BASE_URL") or ""


def ensure_services(settings, log_dir):
    if health_ok(settings.mock_url + "/health") and health_ok(settings.runtime_url + "/v1/health"):
        return []
    if settings.no_stack:
        raise CliError("环境服务或 runtime 不可达。请先启动服务，或去掉 --no-stack 由命令自动启动。")
    if not model_endpoint(settings):
        raise CliError("未配置模型端点。请在 .env 中设置 STEPFUN_OPENAI_BASE_URL 与 STEPFUN_API_KEY，或导出同名环境变量后重试。变量名前缀不限服务提供方，端点需兼容 OpenAI Responses API。")
    if settings.local:
        return start_local_stack(settings, log_dir)
    return start_docker_stack(settings)


def stop_processes(processes):
    for process in processes:
        if process.poll() is None:
            _terminate(process, signal.SIGTERM)
    for process in processes:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate(process, signal.SIGKILL)
            process.wait()


def _terminate(process, sig):
    """终止整个进程组，覆盖 go run 派生的子进程。"""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError, OSError):
        process.terminate()


def compact_arguments(raw):
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        text = str(raw)
    else:
        text = json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))
    return text if len(text) <= 300 else text[:297] + "..."


def summarize_output(raw):
    text = str(raw or "")
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        summary = text
    else:
        contents = parsed.get("content") if isinstance(parsed, dict) else None
        if isinstance(contents, list) and contents:
            first = contents[0]
            summary = first.get("text", "") if isinstance(first, dict) else str(first)
        else:
            summary = text
        if isinstance(parsed, dict) and parsed.get("isError"):
            summary = "错误: " + summary
    summary = " ".join(summary.split())
    return summary if len(summary) <= 200 else summary[:197] + "..."


def render_turn(result, verbose=False):
    for iteration in result.get("iterations") or []:
        for call in iteration.get("toolCalls") or []:
            marker = "完成 " if call.get("status") == "completed" else "失败 "
            print("  " + marker + str(call.get("name")) + " " + compact_arguments(call.get("arguments")))
            print("      " + summarize_output(call.get("output")) + " (" + str(call.get("latencyMs")) + "ms)")
        if verbose and iteration.get("reasoning"):
            print("  思考: " + " ".join(str(iteration["reasoning"]).split())[:500])
    reply = result.get("reply") or ""
    if reply:
        print("")
        print("ESA > " + reply)
        print("")
    status = result.get("status")
    if status != "completed":
        label = STATUS_LABELS.get(status, str(status))
        error = result.get("error") or ""
        print("[本轮" + label + (": " + error if error else "") + "]")
        print("")


class VehicleChat:
    def __init__(self, settings, session_id=None, transcript=None):
        self.settings = settings
        self.session_id = session_id
        self.transcript = transcript if isinstance(transcript, list) else []
        self.initial_snapshot = None
        self.turn_count = 0
        self.started_at = datetime.now(timezone.utc).isoformat()

    @property
    def evidence_dir(self):
        return self.settings.session_dir / (self.session_id or "pending")

    def create_session(self):
        task = self.settings.task
        body = {
            "type": "car",
            "context": task.get("context") or {},
            "initial_state": task.get("initial_state") or {},
            "data_overlays": task.get("data_overlays") or {},
            "faults": task.get("faults") or [],
            "seed": task.get("seed", 0),
        }
        if task.get("tool_profile") is not None:
            body["tool_profile"] = task["tool_profile"]
        if task.get("dataset_version"):
            body["dataset_version"] = task["dataset_version"]
        created = http_json(self.settings.mock_url + "/eval/api/v1/sessions", "POST", body)
        self.session_id = created.get("session_id")
        if not self.session_id:
            raise CliError("环境服务没有返回 session_id")
        self.initial_snapshot = self.snapshot()

    def snapshot(self):
        return http_json(self.settings.mock_url + "/api/v1/rl/sessions/" + self.session_id + "/snapshot")

    def events(self):
        return http_json(self.settings.mock_url + "/api/v1/rl/sessions/" + self.session_id + "/events")

    def reset(self):
        http_json(self.settings.mock_url + "/eval/api/v1/sessions/" + self.session_id + "/reset", "POST", {})

    def turn(self, message):
        body = {
            "agent": self.settings.agent,
            "user_messages": [message],
            "transcript": self.transcript,
            "mockSystemUrl": self.settings.mock_internal_url,
            "sessionId": self.session_id,
            "maxIterations": self.settings.max_iterations,
            "turnTimeoutSec": self.settings.turn_timeout,
        }
        if self.settings.model:
            body["model"] = self.settings.model
        result = http_json(self.settings.runtime_url + "/v1/run", "POST", body, timeout=self.settings.turn_timeout + 30)
        if isinstance(result.get("transcript"), list):
            self.transcript = result["transcript"]
        self.turn_count += 1
        return result


def write_evidence(chat):
    directory = chat.evidence_dir
    directory.mkdir(parents=True, exist_ok=True)
    files = {"transcript.json": chat.transcript, "snapshot-initial.json": chat.initial_snapshot or {}}
    try:
        files["snapshot-final.json"] = chat.snapshot()
    except CliError:
        files["snapshot-final.json"] = chat.initial_snapshot or {}
    try:
        files["state-events.json"] = chat.events()
    except CliError:
        files["state-events.json"] = {}
    for name, payload in files.items():
        (directory / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
    meta = {
        "session_id": chat.session_id,
        "model": chat.settings.model_id,
        "task": str(chat.settings.task_path) if chat.settings.task_path else None,
        "started_at": chat.started_at,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "turns": chat.turn_count,
        "runtime_url": chat.settings.runtime_url,
        "mock_url": chat.settings.mock_url,
    }
    (directory / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), "utf-8")


def read_meta(directory):
    path = directory / "meta.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text("utf-8"))
    except ValueError:
        return {}


def archive_evidence(chat, index):
    directory = chat.evidence_dir
    target = directory / ("reset-" + str(index))
    target.mkdir(parents=True, exist_ok=True)
    for name in ("transcript.json", "snapshot-initial.json", "snapshot-final.json", "state-events.json", "meta.json"):
        source = directory / name
        if source.is_file():
            (target / name).write_text(source.read_text("utf-8"), "utf-8")


def resume_session(settings, session_id):
    directory = settings.session_dir / session_id
    transcript_path = directory / "transcript.json"
    if not transcript_path.is_file():
        raise CliError("会话证据不存在: " + str(directory))
    transcript = json.loads(transcript_path.read_text("utf-8"))
    if not isinstance(transcript, list):
        raise CliError("对话历史格式错误: " + str(transcript_path))
    try:
        snapshot = http_json(settings.mock_url + "/api/v1/rl/sessions/" + session_id + "/snapshot", timeout=5)
    except CliError as error:
        raise CliError("该会话在环境服务中不可用，无法续聊: " + str(error)) from error
    chat = VehicleChat(settings, session_id=session_id, transcript=transcript)
    chat.initial_snapshot = snapshot
    return chat


def print_banner(chat, settings):
    snapshot = chat.initial_snapshot or {}
    manifest = snapshot.get("manifest") or {}
    enabled = sum(1 for tool in manifest.get("tools") or [] if tool.get("enabled"))
    print("ESA 交互会话")
    print("  会话标识: " + str(chat.session_id))
    print("  模型: " + str(settings.model_id))
    endpoint = model_endpoint(settings)
    if endpoint:
        print("  模型端点: " + endpoint)
    print("  启用工具: " + str(enabled) + " 项")
    if settings.task_path:
        print("  任务: " + str(settings.task_path))
    print("  证据目录: " + str(chat.evidence_dir))
    print("输入消息开始对话，/help 查看命令。")
    print("")


COMMANDS = [
    ("/state", "显示当前车辆状态快照"),
    ("/events", "显示工具调用记录与状态变化"),
    ("/tools", "显示当前会话启用的工具"),
    ("/reset", "重置车辆状态并清空对话历史"),
    ("/resume", "列出已保存的交互会话并续聊"),
    ("/quit", "写出证据并退出"),
    ("/help", "显示命令列表"),
]


def command_box(prefix):
    """按前缀过滤显示命令列表。输入 / 时显示全部命令。"""
    matched = [item for item in COMMANDS if item[0].startswith(prefix)]
    if matched:
        print("可用命令:")
    else:
        print("没有以 " + prefix + " 开头的命令。全部命令:")
    for name, description in matched or COMMANDS:
        print("  " + name.ljust(9) + description)
    print("普通输入直接作为用户消息发送。")


def print_help():
    command_box("/")


class ChatController:
    def __init__(self, settings, chat):
        self.settings = settings
        self.chat = chat
        self.reset_count = 0

    def respond(self, message):
        print("ESA 思考中…", flush=True)
        result = self.chat.turn(message)
        render_turn(result, self.settings.verbose)

    def command_state(self):
        snapshot = self.chat.snapshot()
        text = json.dumps(snapshot.get("state") or {}, ensure_ascii=False, indent=2)
        if len(text) > 4000:
            text = text[:4000] + "\n... (已截断)"
        print(text)

    def command_events(self):
        data = self.chat.events()
        events = data.get("events") or []
        for event in events[-50:]:
            changes = event.get("changes") or []
            paths = ", ".join(str(change.get("path", "")) for change in changes[:5])
            if len(changes) > 5:
                paths += " 等 " + str(len(changes)) + " 项"
            line = "  #" + str(event.get("sequence")) + " " + str(event.get("tool")) + " " + str(event.get("status")) + " v" + str(event.get("before_version")) + "→v" + str(event.get("after_version"))
            if paths:
                line += "  变化: " + paths
            print(line)
        if data.get("truncated"):
            print("  (事件记录已截断)")

    def command_tools(self):
        snapshot = self.chat.snapshot()
        tools = [tool["name"] for tool in (snapshot.get("manifest") or {}).get("tools") or [] if tool.get("enabled")]
        print("启用 " + str(len(tools)) + " 项: " + ", ".join(tools))

    def command_reset(self):
        archive_evidence(self.chat, self.reset_count)
        self.reset_count += 1
        try:
            self.chat.reset()
        except CliError as error:
            print("重置失败: " + str(error))
            return
        self.chat.transcript = []
        self.chat.initial_snapshot = self.chat.snapshot()
        print("已重置。当前状态版本 v" + str(self.chat.initial_snapshot.get("state_version")) + "。")

    def command_resume(self):
        root = self.settings.session_dir
        if not root.is_dir():
            print("没有已保存的会话。")
            return
        entries = sorted((path for path in root.iterdir() if path.is_dir()), key=lambda path: path.stat().st_mtime, reverse=True)[:20]
        if not entries:
            print("没有已保存的会话。")
            return
        for index, path in enumerate(entries, start=1):
            meta = read_meta(path)
            print("  " + str(index) + ". " + path.name + "  模型: " + str(meta.get("model") or "-") + "  轮数: " + str(meta.get("turns", "-")))
        choice = input("选择会话编号（回车取消）: ").strip()
        if not choice.isdigit() or not 1 <= int(choice) <= len(entries):
            print("已取消。")
            return
        try:
            self.chat = resume_session(self.settings, entries[int(choice) - 1].name)
        except CliError as error:
            print("续聊失败: " + str(error))
            return
        print("已恢复会话 " + self.chat.session_id + "，历史 " + str(len(self.chat.transcript)) + " 项。")

    def dispatch(self, line):
        command = line.split()[0].lower()
        if command in ("/quit", "/exit"):
            return False
        if command == "/help":
            print_help()
        elif command == "/state":
            self.command_state()
        elif command == "/events":
            self.command_events()
        elif command == "/tools":
            self.command_tools()
        elif command == "/reset":
            self.command_reset()
        elif command == "/resume":
            self.command_resume()
        else:
            print("未知命令 " + command + "。")
            command_box(command)
        return True


def run_repl(controller):
    settings = controller.settings
    history_path = settings.session_dir / "chat_history"
    readline = None
    try:
        import readline as readline_module

        readline = readline_module
        readline.set_history_length(1000)
        if history_path.is_file():
            readline.read_history_file(str(history_path))
    except (ImportError, OSError):
        readline = None
    print_banner(controller.chat, settings)
    first_message = first_task_message(settings.task) if settings.task else None
    if first_message:
        print("你 > " + first_message)
        try:
            controller.respond(first_message)
        except CliError as error:
            print("本轮失败: " + str(error))
        except KeyboardInterrupt:
            print("已取消当前轮。")
    while True:
        try:
            line = input("你 > ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("")
            continue
        if not line:
            continue
        if line.startswith("/"):
            if line.strip() == "/":
                command_box("/")
                continue
            if not controller.dispatch(line):
                break
            continue
        try:
            controller.respond(line)
        except CliError as error:
            print("本轮失败: " + str(error))
        except KeyboardInterrupt:
            print("已取消当前轮。如业务状态已变化但未计入对话历史，可用 /reset 重新开始。")
    if readline is not None:
        try:
            readline.write_history_file(str(history_path))
        except OSError:
            pass


def run_chat(settings):
    processes = ensure_services(settings, settings.session_dir / "logs")
    chat = None
    try:
        if settings.resume:
            chat = resume_session(settings, settings.resume)
        else:
            chat = VehicleChat(settings)
            chat.create_session()
        controller = ChatController(settings, chat)
        run_repl(controller)
    finally:
        if chat is not None and chat.session_id:
            try:
                write_evidence(chat)
            except CliError:
                pass
        stop_processes(processes)
        if settings.ephemeral and not settings.local and not settings.no_stack:
            subprocess.run(["docker", "compose", "-p", "esa-dev", "-f", str(DEV_COMPOSE_PATH), "down"], capture_output=True)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="esa", description="ESA 座舱 Agent 交互式命令行")
    subparsers = parser.add_subparsers(dest="command")
    chat = subparsers.add_parser("chat", help="启动交互式对话")
    chat.add_argument("--task", help="任务目录或 initial_state.json 路径")
    chat.add_argument("--model", help="覆盖模型标识")
    chat.add_argument("--env", default=".env", help="环境变量文件路径")
    chat.add_argument("--runtime-url", help="runtime 地址")
    chat.add_argument("--mock-url", help="环境服务地址（命令行自身访问）")
    chat.add_argument("--mock-internal-url", help="传递给 runtime 的环境服务地址")
    chat.add_argument("--turn-timeout", type=float, default=240.0, help="单轮超时秒数")
    chat.add_argument("--max-iterations", type=int, default=12, help="单轮最大迭代次数")
    chat.add_argument("--local", action="store_true", help="使用宿主进程而非 Docker 服务组合")
    chat.add_argument("--no-stack", action="store_true", help="不自动启动服务")
    chat.add_argument("--ephemeral", action="store_true", help="退出时销毁 Docker 服务组合")
    chat.add_argument("--session-dir", default=str(Path.home() / ".esa" / "sessions"), help="证据目录")
    chat.add_argument("--verbose", action="store_true", help="显示模型思考内容")
    chat.add_argument("--resume", help="续聊已保存的会话标识")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv if argv is not None else sys.argv[1:])
    if not args.command:
        parser.print_help()
        return 2
    env_path = Path(args.env)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path
    env_values = load_env_file(env_path)
    settings = Settings(args, env_values)
    try:
        return run_chat(settings)
    except CliError as error:
        print("错误: " + str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已退出。")
        return 130


if __name__ == "__main__":
    sys.exit(main())
