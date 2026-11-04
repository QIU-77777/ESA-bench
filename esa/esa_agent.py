"""ESA 评测 agent：驱动本地 ReAct runtime 跑完一轮对话，产出轨迹与判分素材。

单次试验的流程：

1. setup 阶段把驱动脚本与 agent 定义上传到主容器；
2. run 阶段执行驱动脚本，建立 mock 会话、发起一轮对话、按迭代收集事件流；
3. run 阶段把驱动输出转成 ATIF 轨迹与判分素材。

驱动脚本自己负责与 mock-system 和本地 runtime 通信，Harbor 侧只做文件搬运与结果整理。
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shlex
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, override

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities
from harbor.environments.base import BaseEnvironment, ExecResult
from harbor.models.agent.context import AgentContext

# 容器内的工作目录。驱动脚本与结果文件都放在这里。
WORK_DIR = "/tmp/esa"
DRIVER_PATH = f"{WORK_DIR}/driver.mjs"
CONFIG_PATH = f"{WORK_DIR}/driver-config.json"
AGENT_PAYLOAD_PATH = f"{WORK_DIR}/agent.json"
RESULT_PATH = f"{WORK_DIR}/driver-result.json"

# 任务目录把自己的用例素材放到这个路径，由任务镜像复制进容器。
TASK_DIR = "/opt/esa"
INITIAL_STATE_PATH = f"{TASK_DIR}/initial_state.json"

# 评测服务在 compose 里的地址。
DEFAULT_MOCK_SYSTEM_URL = "http://mock-system:8003"
DEFAULT_AGENT_API_URL = "http://agent-api:8080"
DEFAULT_EVAL_PREFIX = "/eval"

# 驱动脚本与冻结的 agent 定义随包分发。
PACKAGE_DIR = Path(__file__).resolve().parent

# 上游用量字段到 ATIF 指标字段的映射。
USAGE_TO_METRICS = {
    "input_tokens": "prompt_tokens",
    "output_tokens": "completion_tokens",
    "cached_tokens": "cached_tokens",
}


def driver_program_failure(driver_output: dict[str, Any]) -> str | None:
    """把驱动层的执行失败映射成评分失效标记，字段名与 Oracle 证据一致。

    只覆盖真正的执行异常：runtime 返回错误或轮次未完成。``user_script``
    正则不匹配属于模型回答不符合预期，由 ``user_script_complete`` 走模型
    失败路径，不能算执行无效，因此不在此处标记。
    """
    errors = driver_output.get("errors") or []
    if errors:
        return f"driver execution failed ({len(errors)} error(s))"
    status = driver_output.get("turnStatus")
    if status != "completed":
        return f"driver turn did not complete: {status}"
    return None


# This bridge executes only HTTP requests. Reference code and grading files stay
# in the host process; neither is interpolated into this script.
HTTP_BRIDGE = """import base64,json,sys,urllib.request,urllib.error
packet=json.loads(base64.b64decode(sys.argv[1]))
data=None if packet['body'] is None else json.dumps(packet['body'],ensure_ascii=False,allow_nan=False).encode()
request=urllib.request.Request(packet['url'],data=data,method=packet['method'],headers={'Content-Type':'application/json'})
try:
    with urllib.request.urlopen(request,timeout=30) as response:
        raw=response.read().decode('utf-8')
        if raw:
            json.loads(raw)
            print(raw)
        else:
            print('{}')
except urllib.error.HTTPError as error:
    print('HTTP bridge failed: status='+str(error.code),file=sys.stderr)
    sys.exit(1)
except Exception:
    print('HTTP bridge failed',file=sys.stderr)
    sys.exit(1)
"""


class PreflightError(RuntimeError):
    """A local, non-secret task contract error."""


class EnvironmentTransport:
    """Synchronous transport used from the reference worker thread."""

    def __init__(self, environment: BaseEnvironment, loop, base_url: str):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError("参考验证需要无凭据的 HTTP 服务地址")
        self.environment = environment
        self.loop = loop
        self.base_url = base_url.rstrip("/")

    def request(self, method: str, path: str, body=None):
        if method not in {"GET", "POST", "DELETE"} or not path.startswith("/") or path.startswith("//"):
            raise ValueError("参考 HTTP 请求的 method/path 不合法")
        packet = {"method": method, "url": self.base_url + path, "body": body}
        encoded = base64.b64encode(json.dumps(packet, ensure_ascii=False, allow_nan=False).encode()).decode()
        command = "python3 -c " + shlex.quote(HTTP_BRIDGE) + " " + shlex.quote(encoded)
        future = asyncio.run_coroutine_threadsafe(
            self.environment.exec(command=command, timeout_sec=45), self.loop
        )
        try:
            result = future.result(timeout=60)
        except Exception:
            future.cancel()
            raise RuntimeError("参考 HTTP 桥接执行失败") from None
        if result.return_code != 0:
            # Never embed raw stderr, response bodies or command parameters in errors.
            raise RuntimeError("参考 HTTP 桥接请求失败")
        try:
            return json.loads(result.stdout or "")
        except (TypeError, json.JSONDecodeError):
            raise RuntimeError("参考 HTTP 桥接返回了非法 JSON") from None


class ESAAgent(BaseAgent):
    """驱动 ESA agent 完成一轮对话并收集轨迹。"""

    capabilities = AgentCapabilities(atif=True)

    @staticmethod
    @override
    def name() -> str:
        return "esa"

    @override
    def version(self) -> str:
        return "1.0.0"

    @override
    async def setup(self, environment: BaseEnvironment) -> None:
        await self._require_node(environment)

        driver = PACKAGE_DIR / "driver.mjs"
        payload = PACKAGE_DIR / "agent.json"
        if not driver.is_file():
            raise FileNotFoundError(f"找不到驱动脚本：{driver}")
        if not payload.is_file():
            raise FileNotFoundError(
                f"找不到 agent 定义：{payload}。恢复仓库内的 esa/agent.json。"
            )

        await self._exec(environment, f"mkdir -p {WORK_DIR}")
        await environment.upload_file(driver, DRIVER_PATH)
        await environment.upload_file(
            self._write_agent_payload(payload), AGENT_PAYLOAD_PATH
        )

    @override
    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        task_state = await self._load_task_state(environment)
        effective_instruction = (instruction or "").strip() or (task_state.get("instruction") or "")
        await self._reference_preflight(environment, task_state, effective_instruction)
        config = {
            "caseId": task_state.get("case_id") or self._case_id(),
            "instruction": effective_instruction,
            "initialContext": task_state.get("initial_context", task_state.get("context", {})) or {},
            "initialState": task_state.get("initial_state") or {},
            "dataOverlays": task_state.get("data_overlays") or {},
            "toolProfile": task_state.get("tool_profile"),
            "faults": task_state.get("faults") or [],
            "seed": task_state.get("seed", 0),
            "datasetVersion": task_state.get("dataset_version"),
            "userMessages": task_state.get("user_messages"),
            "userScript": task_state.get("user_script"),
            "mockSystemUrl": self._env_or("ESA_MOCK_URL", DEFAULT_MOCK_SYSTEM_URL),
            "mockSystemInternalUrl": self._env_or(
                "ESA_MOCK_INTERNAL_URL", self._env_or("ESA_MOCK_URL", DEFAULT_MOCK_SYSTEM_URL)
            ),
            "agentApiUrl": self._env_or("ESA_AGENT_API_URL", DEFAULT_AGENT_API_URL),
            "agentId": self._env_or("ESA_AGENT_ID", "esa_eval"),
            "agentPayloadPath": AGENT_PAYLOAD_PATH,
            "evalPrefix": self._env_or("ESA_EVAL_PREFIX", DEFAULT_EVAL_PREFIX),
            "turnTimeoutSec": int(self._env_or("ESA_TURN_TIMEOUT_SEC", "240")),
            "outputPath": RESULT_PATH,
        }

        await self._exec(environment, f"mkdir -p {WORK_DIR}")
        await environment.upload_file(self._write_driver_config(config), CONFIG_PATH)

        turn_count = (len(config["userScript"]) + 1 if config["userScript"] is not None
                      else max(1, len(config.get("userMessages") or [])))
        timeout = int(config["turnTimeoutSec"]) * turn_count + 120
        result = await environment.exec(
            command=f"node {DRIVER_PATH} --config {CONFIG_PATH}",
            timeout_sec=timeout,
        )
        self._log_exec(result)

        driver_output = await self._read_result(environment)
        self._write_artifacts(driver_output)
        self._populate_context(context, driver_output)
        if driver_output.get("errors") or driver_output.get("turnStatus") != "completed":
            raise RuntimeError(f"ESA 执行失败：{driver_output.get("errors") or driver_output.get("turnStatus")}")

    async def _reference_preflight(self, environment, task_state, instruction):
        """Validate trusted host material before a model session is created."""
        output_dir = self._logs_dir().parent / "reference"
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            from esa.reference import validate_task

            environment_dir = getattr(environment, "environment_dir", None)
            if environment_dir is None:
                raise PreflightError("环境缺少宿主 environment_dir，无法认证任务")
            task_path = Path(environment_dir).resolve().parent
            required = ("solution/solve.sh", "solution/reference.py", "solution/reference_runtime.py", "solution/runner.py", "tests/negative_tests.py", "tests/rubric.json",
                        "environment/esa/initial_state.json")
            if any(not (task_path / name).is_file() for name in required):
                raise PreflightError("正式 ESA 任务缺少可信参考程序、错误答案测试或评分标准")
            host_initial = json.loads((task_path / required[-1]).read_text("utf8"))
            if host_initial != task_state:
                raise PreflightError("宿主任务初态与当前容器加载的初态不一致")
            if not task_state.get("user_messages") and instruction != (task_state.get("instruction") or ""):
                raise PreflightError("模型指令与参考任务指令不一致")
            transport = EnvironmentTransport(environment, asyncio.get_running_loop(),
                self._env_or("ESA_MOCK_URL", DEFAULT_MOCK_SYSTEM_URL))
            report = await asyncio.to_thread(validate_task, task_path, transport, output_dir=output_dir)
            if not isinstance(report, dict):
                raise RuntimeError("参考验证没有返回结构化报告")
            if report.get("task_input_sha256"):
                from esa.reference import digest
                if report["task_input_sha256"] != digest(task_state):
                    raise PreflightError("参考验证使用的任务输入与模型初态不一致")
        except Exception as error:
            # Exception text may include tool data or credentials. Preserve a
            # supplied structured report; unexpected errors get a generic label.
            report = getattr(error, "report", None)
            if not isinstance(report, dict):
                report = {"status": "failed", "passed": False,
                          "failure": str(error) if isinstance(error, PreflightError) else "参考验证失败",
                          "error_type": type(error).__name__}
            report = self._redact_reference_report(report)
            self._save_reference_report(output_dir, report)
            raise RuntimeError("ESA 参考验证失败，模型未执行") from None
        report = self._redact_reference_report(report)
        self._save_reference_report(output_dir, report)
        if report.get("status") != "pass" or report.get("passed") is not True:
            raise RuntimeError("ESA 参考验证未通过，模型未执行")

    def _save_reference_report(self, output_dir, report):
        golden = output_dir / "golden-run.json"
        if golden.is_file():
            try:
                contents = self._redact_reference_report(json.loads(golden.read_text("utf8")))
            except (OSError, ValueError):
                contents = {"status": "failed", "passed": False, "failure": "参考证据无法解析"}
            golden.write_text(json.dumps(contents, ensure_ascii=False, indent=2), "utf8")
        (output_dir / "report.json").write_text(json.dumps(self._redact_reference_report(report), ensure_ascii=False, indent=2), "utf8")

    def _redact_reference_report(self, value):
        def sensitive(key):
            name = str(key).lower()
            return name in {"api_key", "secret", "password", "authorization", "access_token"} or name.endswith(("_api_key", "_secret", "_token", "_password"))

        secrets = [item for key, item in {**os.environ, **getattr(self, "_extra_env", {})}.items()
                   if sensitive(key) and isinstance(item, str) and len(item) >= 4]

        def redact(item):
            if isinstance(item, dict):
                return {key: "<redacted>" if sensitive(key) else redact(child) for key, child in item.items()}
            if isinstance(item, list):
                return [redact(child) for child in item]
            if isinstance(item, str):
                for secret in secrets:
                    item = item.replace(secret, "<redacted>")
                return re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]{6,}", "<redacted>", item)
            return item
        return redact(value)

    # ---- 文件与配置 ----

    def _logs_dir(self) -> Path:
        """本地日志目录的绝对路径。

        Harbor 传进来的是相对工作目录的路径，直接交给 compose 的上传下载命令会
        被解析到任务目录下，因此统一转成绝对路径。
        """
        return self.logs_dir.resolve()

    def _model_id(self) -> str:
        return (self.model_name or "").split("/", 1)[-1]

    def _write_agent_payload(self, source: Path) -> Path:
        """按本次试验的模型名改写 agent 定义里的模型。

        评测要横向比较不同模型，模型名由 job 配置给出，不写死在定义文件里。
        """
        definition = json.loads(source.read_text("utf8"))
        if self.model_name:
            react = definition.setdefault("react", {})
            model = react.setdefault("model", {})
            model["model_id"] = self._model_id()

        target = (self._logs_dir() / ".." / "esa-agent.json").resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(definition, ensure_ascii=False, indent=2), "utf8")
        return target

    def _case_id(self) -> str:
        return self._env_or("ESA_CASE_ID", "unknown")

    async def _load_task_state(self, environment: BaseEnvironment) -> dict[str, Any]:
        """从容器里读任务自带的初始车辆状态。"""
        local = (self._logs_dir() / "initial-state.json").resolve()
        try:
            await environment.download_file(INITIAL_STATE_PATH, local)
        except Exception as error:  # 任务镜像没带上素材时直接失败，避免用空状态跑出一份无意义的轨迹
            raise RuntimeError(
                f"读不到任务的初始状态 {INITIAL_STATE_PATH}：{error}。"
                "任务镜像需要把 initial_state.json 复制到该路径。"
            ) from error
        return json.loads(local.read_text("utf8"))

    def _write_driver_config(self, payload: dict[str, Any]) -> Path:
        """把驱动配置写进本次试验自己的日志目录。

        多个试验并发运行，共用一个固定路径会互相覆盖，必须按试验分开。
        """
        logs = self._logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        target = logs / "driver-config.json"
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf8")
        return target

    def _env_or(self, key: str, default: str) -> str:
        value = self._get_env(key)
        return value if value else default

    async def _require_node(self, environment: BaseEnvironment) -> None:
        result = await environment.exec(command="command -v node")
        if result.return_code != 0:
            raise RuntimeError(
                "主容器里没有 node，驱动脚本无法运行。请在任务镜像里安装 Node.js 22 以上版本。"
            )

    async def _exec(self, environment: BaseEnvironment, command: str) -> ExecResult:
        result = await environment.exec(command=command, timeout_sec=60)
        if result.return_code != 0:
            raise RuntimeError(
                f"命令执行失败（返回码 {result.return_code}）：{command}\n"
                f"标准输出：{result.stdout}\n标准错误：{result.stderr}"
            )
        return result

    async def _read_result(self, environment: BaseEnvironment) -> dict[str, Any]:
        """把驱动结果读回宿主。"""
        local = (self._logs_dir() / "driver-result.json").resolve()
        await environment.download_file(RESULT_PATH, local)
        if not local.is_file():
            raise RuntimeError(f"驱动没有产出结果文件：{RESULT_PATH}")
        return json.loads(local.read_text("utf8"))

    # ---- 产物 ----

    def _write_artifacts(self, driver_output: dict[str, Any]) -> None:
        """写出 ATIF 轨迹与判分素材。"""
        logs = self._logs_dir()
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "tool-calls.json").write_text(
            json.dumps(
                {
                    "case_id": driver_output.get("caseId"),
                    "session_id": driver_output.get("sessionId"),
                    "calls": driver_output.get("toolCalls", []),
                    "events": driver_output.get("toolEvents", []),
                    "truncated": (driver_output.get("eventMetadata") or {}).get("truncated", False),
                    "dialogue": driver_output.get("dialogue", []),
                    "user_script_complete": driver_output.get("user_script_complete"),
                    "turn_status": driver_output.get("turnStatus"),
                    "program_failure": driver_program_failure(driver_output),
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf8",
        )
        (logs / "reply.txt").write_text(driver_output.get("reply") or "", "utf8")
        (logs / "dialogue.json").write_text(json.dumps(driver_output.get("dialogue", []), ensure_ascii=False, indent=2), "utf8")
        for name, key in [("snapshot-initial.json", "initialSnapshot"), ("snapshot-final.json", "finalSnapshot"),
                          ("state-events.json", "eventEvidence"), ("environment-manifest.json", "manifest")]:
            (logs / name).write_text(json.dumps(driver_output.get(key), ensure_ascii=False, indent=2), "utf8")
        (logs / "trajectory.json").write_text(
            json.dumps(self._build_trajectory(driver_output), ensure_ascii=False, indent=2),
            "utf8",
        )

    def _build_trajectory(self, driver_output: dict[str, Any]) -> dict[str, Any]:
        """把驱动输出转成 ATIF 轨迹。

        一次 ReAct 迭代对应一个 step：该次迭代的推理、工具调用与工具结果放在一起。
        用户指令是第一个 step。
        """
        iterations = driver_output.get("iterations") or []
        user_dialogue = [item for item in driver_output.get("dialogue", []) if item.get("role") == "user"]
        messages = ([item["content"] for item in user_dialogue] if user_dialogue else
                    driver_output.get("userMessages") or [driver_output.get("instruction") or ""])
        steps: list[dict[str, Any]] = [
            {
                "step_id": 1,
                "timestamp": driver_output.get("startedAt"),
                "source": "user",
                "message": messages[0],
                "extra": {
                    "case_id": driver_output.get("caseId"),
                    "session_id": driver_output.get("sessionId"),
                    "turn_index": 0,
                },
            }
        ]

        current_turn = 0
        for iteration in iterations:
            turn_index = iteration.get("turnIndex", 0)
            while current_turn < turn_index:
                current_turn += 1
                steps.append({"step_id": len(steps) + 1, "timestamp": iteration.get("receivedAt"),
                              "source": "user", "message": messages[current_turn] if current_turn < len(messages) else "",
                              "extra": {"turn_index": current_turn}})
            steps.append(self._iteration_step(len(steps) + 1, iteration))

        trajectory: dict[str, Any] = {
            "schema_version": "ATIF-v1.7",
            "session_id": driver_output.get("sessionId"),
            "agent": {
                "name": self.name(),
                "version": self.version(),
                "model_name": self._model_id(),
                "extra": self._prune(
                    {
                        "case_id": driver_output.get("caseId"),
                        "thread_id": driver_output.get("threadId"),
                        "turn_id": driver_output.get("turnId"),
                        "turn_status": driver_output.get("turnStatus"),
                    }
                ),
            },
            "steps": steps,
        }
        final_metrics = self._final_metrics(iterations, driver_output)
        if final_metrics:
            trajectory["final_metrics"] = final_metrics
        return trajectory

    def _iteration_step(self, step_id: int, iteration: dict[str, Any]) -> dict[str, Any]:
        tool_calls = []
        results = []
        for index, call in enumerate(iteration.get("toolCalls") or []):
            call_id = call.get("callId") or f"call-{step_id}-{index}"
            tool_calls.append(
                {
                    "tool_call_id": call_id,
                    "function_name": call.get("name") or "",
                    "arguments": self._parse_arguments(call.get("arguments")),
                }
            )
            results.append(
                {
                    "source_call_id": call_id,
                    "content": call.get("output") or "",
                    "extra": self._prune(
                        {
                            "status": call.get("status"),
                            "latency_ms": call.get("latencyMs"),
                            "output_chars": call.get("outputChars"),
                            "override_hit": call.get("overrideHit"),
                        }
                    ),
                }
            )

        step: dict[str, Any] = {
            "step_id": step_id,
            "timestamp": iteration.get("receivedAt"),
            "source": "agent",
            "message": iteration.get("message") or "",
            "reasoning_content": iteration.get("reasoning") or "",
            "model_name": self._model_id(),
            "llm_call_count": 1,
            "extra": self._prune(
                {
                    "iteration": iteration.get("iteration"),
                    "response_id": iteration.get("responseId"),
                    "trace_id": iteration.get("traceId"),
                    "chat_id": iteration.get("chatId"),
                    "model_call_id": iteration.get("modelCallId"),
                    "latency_ms": iteration.get("latencyMs"),
                    "status": iteration.get("status"),
                    "fallback": iteration.get("fallback") or None,
                    "stop_reason": ("error" if iteration.get("status") in {"failed", "error"} else "tool_use" if tool_calls else "end_turn"),
                    "turn_index": iteration.get("turnIndex", 0),
                }
            ),
        }
        if tool_calls:
            step["tool_calls"] = tool_calls
            step["observation"] = {"results": results}
        metrics = self._step_metrics(iteration)
        if metrics:
            step["metrics"] = metrics
        return step

    def _step_metrics(self, iteration: dict[str, Any]) -> dict[str, Any] | None:
        usage = iteration.get("usage") or {}
        metrics = {
            target: usage[source]
            for source, target in USAGE_TO_METRICS.items()
            if isinstance(usage.get(source), int)
        }
        extra = self._prune(
            {
                "reasoning_tokens": usage.get("reasoning_tokens"),
                "total_tokens": usage.get("total_tokens"),
                "latency_ms": iteration.get("latencyMs"),
            }
        )
        if extra:
            metrics["extra"] = extra
        return metrics or None

    def _final_metrics(
        self, iterations: list[dict[str, Any]], driver_output: dict[str, Any]
    ) -> dict[str, Any] | None:
        totals = {"prompt": 0, "completion": 0, "cached": 0, "reasoning": 0}
        latency = 0
        has_usage = False
        for iteration in iterations:
            usage = iteration.get("usage") or {}
            if usage:
                has_usage = True
            totals["prompt"] += usage.get("input_tokens") or 0
            totals["completion"] += usage.get("output_tokens") or 0
            totals["cached"] += usage.get("cached_tokens") or 0
            totals["reasoning"] += usage.get("reasoning_tokens") or 0
            latency += iteration.get("latencyMs") or 0

        metrics: dict[str, Any] = {
            "total_steps": len(iterations) + len(driver_output.get("userMessages") or [driver_output.get("instruction")]),
            "extra": self._prune(
                {
                    "total_latency_ms": latency or None,
                    "tool_call_count": len(driver_output.get("toolCalls") or []) or None,
                    "total_reasoning_tokens": totals["reasoning"] if has_usage else None,
                    "usage_coverage": sum(bool(item.get("usage")) for item in iterations) / len(iterations) if iterations else None,
                }
            ),
        }
        if has_usage:
            for source, target, total in [("input_tokens", "total_prompt_tokens", "prompt"),
                                          ("output_tokens", "total_completion_tokens", "completion"),
                                          ("cached_tokens", "total_cached_tokens", "cached")]:
                covered = sum(source in (item.get("usage") or {}) for item in iterations)
                if covered:
                    metrics[target] = totals[total]
                    metrics["extra"][source + "_coverage"] = covered / len(iterations)
        return metrics

    @staticmethod
    def _parse_arguments(arguments: Any) -> dict[str, Any]:
        if isinstance(arguments, dict):
            return arguments
        if isinstance(arguments, str) and arguments.strip():
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                return {"raw": arguments}
            return parsed if isinstance(parsed, dict) else {"raw": arguments}
        return {}

    @staticmethod
    def _prune(values: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in values.items() if value is not None}

    def _populate_context(self, context: AgentContext, driver_output: dict[str, Any]) -> None:
        iterations = driver_output.get("iterations") or []
        context.metadata = {
            "case_id": driver_output.get("caseId"),
            "session_id": driver_output.get("sessionId"),
            "turn_status": driver_output.get("turnStatus"),
            "iteration_count": len(iterations),
            "tool_call_count": len(driver_output.get("toolCalls") or []),
            "driver_errors": driver_output.get("errors") or [],
        }

    def _log_exec(self, result: ExecResult) -> None:
        if self.logger is None:
            return
        if result.return_code == 0:
            self.logger.debug("驱动执行完成")
            return
        self.logger.error(
            "驱动执行失败，返回码 %s：%s", result.return_code, (result.stderr or "")[-2000:]
        )
