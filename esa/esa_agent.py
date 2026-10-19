"""ESA 评测 agent：驱动 agent-runtime 跑完一轮对话，产出轨迹与判分素材。

单次试验的流程：

1. setup 阶段把驱动脚本与冻结的 agent 定义上传到主容器；
2. run 阶段执行驱动脚本，建立 mock 会话、发起一轮对话、按迭代收集事件流；
3. run 阶段把驱动输出转成 ATIF 轨迹与判分素材。

驱动脚本自己负责与 mock-system 和 agent-runtime 通信，Harbor 侧只做文件搬运与结果整理。
"""

from __future__ import annotations

import json
from pathlib import Path
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
                f"找不到 agent 定义：{payload}。先运行 freeze_agent.py 生成。"
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
        config = {
            "caseId": task_state.get("case_id") or self._case_id(),
            "instruction": (instruction or "").strip() or (task_state.get("instruction") or ""),
            "initialContext": task_state.get("context") or {},
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

        timeout = int(config["turnTimeoutSec"]) + 120
        result = await environment.exec(
            command=f"node {DRIVER_PATH} --config {CONFIG_PATH}",
            timeout_sec=timeout,
        )
        self._log_exec(result)

        driver_output = await self._read_result(environment)
        self._write_artifacts(driver_output)
        self._populate_context(context, driver_output)

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
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf8",
        )
        (logs / "reply.txt").write_text(driver_output.get("reply") or "", "utf8")
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
        steps: list[dict[str, Any]] = [
            {
                "step_id": 1,
                "timestamp": driver_output.get("startedAt"),
                "source": "user",
                "message": driver_output.get("instruction") or "",
                "extra": {
                    "case_id": driver_output.get("caseId"),
                    "session_id": driver_output.get("sessionId"),
                },
            }
        ]

        for index, iteration in enumerate(iterations):
            steps.append(self._iteration_step(index + 2, iteration))

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
                    "stop_reason": "tool_use" if tool_calls else "end_turn",
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
            "total_steps": len(iterations) + 1,
            "extra": self._prune(
                {
                    "total_latency_ms": latency or None,
                    "tool_call_count": len(driver_output.get("toolCalls") or []) or None,
                    "total_reasoning_tokens": totals["reasoning"] if has_usage else None,
                }
            ),
        }
        if has_usage:
            metrics["total_prompt_tokens"] = totals["prompt"]
            metrics["total_completion_tokens"] = totals["completion"]
            metrics["total_cached_tokens"] = totals["cached"]
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
