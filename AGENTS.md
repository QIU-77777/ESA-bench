# Repository rules

- 所有活动任务必须兼容 Harbor 原生 OracleAgent。
- 参考解必须提供标准 `solution/solve.sh` 入口。辅助 Python 程序放在 `solution/`，评分和错误答案测试放在 `tests/`。
- 新增或修改任务后，必须使用原生 Oracle 执行实际环境验收，并确认统一 verifier 通过。自定义 Agent 验证不能替代此项验收。
- 普通模型运行不得收到参考解或评分答案。Oracle 仅在原生执行阶段接收 `solution/`，verifier 在评分阶段接收 `tests/`。
- 参考验证、Oracle 和正式模型评分必须使用同一个评分实现。程序异常或证据缺失必须使评分无效。
- 更新 ESA 共享执行器后，执行 `scripts/sync-task-scorers.py`，并使用 `--check` 检查副本。新增 ESA 任务须同时加入 benchmark 和原生 Oracle 验收配置。
- 未经用户要求，不提交代码。
