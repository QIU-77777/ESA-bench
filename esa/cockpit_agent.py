"""座舱 Harbor agent：驱动用户自研座舱 agent 跑完对话，产物与 ESAAgent 一致。

与 ESAAgent 的差异只有两处，都在驱动配置层面：

1. setup 只上传 driver.mjs，不上传 agent.json（座舱分支不使用 ReAct 定义）；
2. 驱动配置删除 ``agentPayloadPath`` 键并改用 ``agentProtocol: "turn"``，
   同时带入 ``expectedModelId`` 供驱动在开跑前比对 agent 上报的模型标识。

证据组装、ATIF 轨迹、参考预检与产物逻辑全部继承 ESAAgent。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, override

from esa.esa_agent import DRIVER_PATH, PACKAGE_DIR, WORK_DIR, ESAAgent


class CockpitAgent(ESAAgent):
    """座舱评测 agent：逐轮驱动座舱 agent，证据与批量评测一致。"""

    @staticmethod
    @override
    def name() -> str:
        return "esa-cockpit"

    @override
    def version(self) -> str:
        return "1.0.0"

    @override
    async def setup(self, environment) -> None:
        await self._require_node(environment)
        driver = PACKAGE_DIR / "driver.mjs"
        if not driver.is_file():
            raise FileNotFoundError(f"找不到驱动脚本：{driver}")
        await self._exec(environment, f"mkdir -p {WORK_DIR}")
        await environment.upload_file(driver, DRIVER_PATH)

    @override
    def _write_driver_config(self, payload: dict[str, Any]) -> Path:
        # driver.mjs 座舱分支不读取 agentPayloadPath，键在而文件不在会直接失败。
        payload.pop("agentPayloadPath", None)
        payload["agentProtocol"] = "turn"
        if self.model_name:
            payload["expectedModelId"] = self._model_id()
        return super()._write_driver_config(payload)
