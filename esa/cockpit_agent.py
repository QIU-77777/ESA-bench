"""座舱评测 agent：驱动 agent-runtime 跑完一轮对话，产出轨迹与判分素材。

单次试验的流程：

1. setup 阶段把驱动脚本、冻结的 agent 定义与任务初始状态上传到主容器；
2. run 阶段执行驱动脚本，建立 mock 会话、发起一轮对话、收集事件流与工具调用记录；
3. run 阶段把驱动输出的 JSON 落成 ATIF 轨迹与判分素材。

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
WORK_DIR = "/tmp/cockpit"
DRIVER_PATH = f"{WORK_DIR}/driver.mjs"
CONFIG_PATH = f"{WORK_DIR}/driver-config.json"
AGENT_PAYLOAD_PATH = f"{WORK_DIR}/agent.json"
RESULT_PATH = f"{WORK_DIR}/driver-result.json"

# 任务目录把自己的用例素材放到这个路径，由任务镜像复制进容器。
TASK_DIR = "/opt/cockpit"
INITIAL_STATE_PATH = f"{TASK_DIR}/initial_state.json"

# 评测服务在 compose 里的地址。
DEFAULT_MOCK_SYSTEM_URL = "http://mock-system:8003"
DEFAULT_AGENT_API_URL = "http://agent-api:8080"
DEFAULT_EVAL_PREFIX = "/eval"

# 驱动脚本与冻结的 agent 定义随包分发。
PACKAGE_DIR = Path(__file__).resolve().parent


class CockpitAgent(BaseAgent):
    """驱动座舱 agent 完成一轮对话并收集轨迹。"""

    capabilities = AgentCapabilities(atif=True)

    @staticmethod
    @override
    def name() -> str:
        return "cockpit"

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
            "mockSystemUrl": self._env_or("COCKPIT_MOCK_URL", DEFAULT_MOCK_SYSTEM_URL),
            "mockSystemInternalUrl": self._env_or(
                "COCKPIT_MOCK_INTERNAL_URL", self._env_or("COCKPIT_MOCK_URL", DEFAULT_MOCK_SYSTEM_URL)
            ),
            "agentApiUrl": self._env_or("COCKPIT_AGENT_API_URL", DEFAULT_AGENT_API_URL),
            "agentId": self._env_or("COCKPIT_AGENT_ID", "cockpit_eval"),
            "agentPayloadPath": AGENT_PAYLOAD_PATH,
            "evalPrefix": self._env_or("COCKPIT_EVAL_PREFIX", DEFAULT_EVAL_PREFIX),
            "turnTimeoutSec": int(self._env_or("COCKPIT_TURN_TIMEOUT_SEC", "240")),
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

    def _write_agent_payload(self, source: Path) -> Path:
        """按本次试验的模型名改写 agent 定义里的模型。

        评测要横向比较不同模型，模型名由 job 配置给出，不写死在定义文件里。
        """
        definition = json.loads(source.read_text("utf8"))
        if self.model_name:
            model_id = self.model_name.split("/", 1)[-1]
            react = definition.setdefault("react", {})
            model = react.setdefault("model", {})
            model["model_id"] = model_id

        target = self._logs_dir() / ".." / "cockpit-agent.json"
        target = target.resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(definition, ensure_ascii=False, indent=2), "utf8")
        return target

    def _case_id(self) -> str:
        return self._env_or("COCKPIT_CASE_ID", "unknown")

    async def _load_task_state(self, environment: BaseEnvironment) -> dict[str, Any]:
        """从容器里读任务自带的初始车辆状态与评分标准。"""
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
        (logs / "reply.txt").write_text(
            driver_output.get("reply") or "", "utf8"
        )
        (logs / "trajectory.json").write_text(
            json.dumps(self._build_trajectory(driver_output), ensure_ascii=False, indent=2),
            "utf8",
        )

    def _build_trajectory(self, driver_output: dict[str, Any]) -> dict[str, Any]:
        """把驱动输出转成 ATIF 轨迹。

        一轮对话对应一个 step：用户输入、模型回复与工具调用都放在这个 step 里。
        """
        tool_calls = []
        for index, call in enumerate(driver_output.get("toolCalls", []) or []):
            arguments = call.get("arguments")
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments}
            tool_calls.append(
                {
                    "tool_call_id": call.get("callId") or f"call-{index}",
                    "function_name": call.get("name") or "",
                    "arguments": arguments or {},
                }
            )

        observation = None
        if tool_calls:
            observation = {
                "results": [
                    {
                        "source_call_id": call["tool_call_id"],
                        "content": self._tool_output(driver_output, call["tool_call_id"]),
                    }
                    for call in tool_calls
                ]
            }

        return {
            "schema_version": "ATIF-v1.7",
            "session_id": driver_output.get("sessionId"),
            "agent": {"name": self.name(), "version": self.version()},
            "steps": [
                {
                    "step_id": 1,
                    "timestamp": driver_output.get("startedAt"),
                    "source": "agent",
                    "message": driver_output.get("reply") or "",
                    "reasoning_content": "\n".join(driver_output.get("reasoning") or []),
                    "tool_calls": tool_calls,
                    "observation": observation,
                }
            ],
        }

    @staticmethod
    def _tool_output(driver_output: dict[str, Any], call_id: str) -> str:
        for call in driver_output.get("toolCalls", []) or []:
            if call.get("callId") == call_id:
                return call.get("output") or ""
        return ""

    def _populate_context(self, context: AgentContext, driver_output: dict[str, Any]) -> None:
        context.metadata = {
            "case_id": driver_output.get("caseId"),
            "session_id": driver_output.get("sessionId"),
            "turn_status": driver_output.get("turnStatus"),
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
