# 询问温度后控制空调

座舱链路示例任务：空调澄清。用户输入见 [instruction.md](<instruction.md>)。座舱初态、工具集合与用户脚本见 [initial_state.json](<environment/esa/initial_state.json>)。

本任务跑在座舱链路上：`mock-system` 是 MCP 记录代理，`cockpit-mcp` 是用户 MCP server（默认镜像为 `cockpit/fake_mcp.py`），`cockpit-agent` 是被测座舱 agent（默认镜像为 `cockpit/fake_agent.py`）。证据结构与评分实现与 ESA 任务一致。

## 参考接口

[reference.py](<solution/reference.py>) 导出 `solve(ctx)`。`ctx.call(name, args, expect='success', call_id=None)` 返回实际 `structuredContent`；`ctx.ask(question)` 记录提问、匹配用户脚本应答并建立新轮次；`ctx.say(text)` 记录回复。

参考程序从用户脚本的真实应答里解析目标温度，不读取评分标准或隐藏答案。状态目标在 [rubric.json](<tests/rubric.json>) 独立定义。任务镜像仅复制初态。

Harbor 原生 Oracle 在执行阶段上传 `solution/`，通过 [solve.sh](<solution/solve.sh>) 调用 [runner.py](<solution/runner.py>)，使用 [reference_runtime.py](<solution/reference_runtime.py>) 执行参考函数。Oracle 运行入口只读取初态和参考程序，并访问 recorder 转发的实际 MCP，不读取 rubric 或执行错误测试。verifier 在评分阶段上传 `tests/`。普通模型不接收参考解或评分答案。

## 错误答案测试

[negative_tests.py](<tests/negative_tests.py>) 导出 `CASES`：

- `control_without_asking`：用户未确认温度就直接控制空调。
- `wrong_temperature`：询问后设置错误温度。

所有错误函数必须完成执行。完整评分拒绝才构成有效负例，抛出异常不构成有效负例。

## 验收条件

先询问目标温度，用户应答后按应答值设置空调。确认前不得调用控制工具（`tool_forbidden` 加 `before_turn`），确认后空调温度等于目标值，两轮回复分别包含询问与确认温度。

使用 `state`、`tool_use`、`reply` 的确定性评分。`program_failure` 非空，或证据包含 `evidence_error`、`cleanup_error` 时，评分无效且 reward 为零。

[正式评分入口](<tests/test.sh>)使用 `ESA_RUBRIC_PATH=/tests/rubric.json`。任务评分与参考执行器、Oracle 运行入口副本由[同步脚本](<../../scripts/sync-task-scorers.py>)维护，与[统一评分](<../../esa/score.py>)和共享运行代码逐字节一致。

## 座舱链路验收

在仓库根目录执行：

```bash
bash scripts/build-esa-local.sh                                        # 构建 recorder 与两个默认镜像
.venv/bin/python -m cockpit.fake_mcp --port 18091 &
.venv/bin/python -m cockpit.recorder --port 18093 --mcp-url http://127.0.0.1:18091 --store memory-test &
.venv/bin/python scripts/validate_task.py --bench esa-cockpit --mock-url http://127.0.0.1:18093 --json
STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
  .venv/bin/harbor run -c configs/jobs/esa-cockpit-oracle-acceptance.yaml --job-name UNIQUE -y
.venv/bin/python scripts/test-oracle-failure.py --task tasks/esa-cockpit-climate --goal-path vehicle.ac_temperature
```

座舱 bench 与 esa bench 分开，结果与分数不做直接比较：环境身份、工具集合与状态结构不同。接入契约见[座舱接入文档](<../../docs/esa-cockpit-integration.md>)。
