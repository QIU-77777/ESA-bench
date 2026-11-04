# 选择距离最近的可用快充站

用户输入见 [instruction.md](<instruction.md>)。环境与用户脚本见 [initial_state.json](<environment/esa/initial_state.json>)。

## 参考接口

[reference.py](<solution/reference.py>) 导出 `solve(ctx)`。`ctx.user_messages` 为元组，`ctx.tools` 为当前 MCP 工具名称与 schema 的字典；`ctx.call(name, args, expect='success', call_id=None)` 返回实际 `structuredContent`，响应丢失时返回 `None`；`ctx.say(text)` 记录回复；`ctx.ask(question)` 返回固定用户脚本应答并建立新轮次。

参考程序使用已交付的用户消息与实际工具返回取得参数，不读取 seed、评分标准或隐藏答案。状态目标在 [rubric.json](<tests/rubric.json>) 独立定义。任务镜像仅复制初态。

Harbor 原生 Oracle 在执行阶段上传 `solution/`，通过 [solve.sh](<solution/solve.sh>) 调用 [runner.py](<solution/runner.py>)，使用 [reference_runtime.py](<solution/reference_runtime.py>) 执行参考函数。Oracle 运行入口只读取初态和参考程序，并访问实际 MCP，不读取 rubric 或执行错误测试。verifier 在评分阶段上传 `tests/`。普通模型不接收参考解或评分答案。

## 错误答案测试

[negative_tests.py](<tests/negative_tests.py>) 导出 `CASES`：

- `farther_station`：从实际返回中选择较远的可用快充站。
- `extra_vehicle_change`：完成正确选择后额外修改音量。

所有错误函数必须完成执行。完整评分拒绝才构成有效负例，抛出异常不构成有效负例。

## 验收条件

筛选实际候选的 power_kw>=30、available>0，并按 (distance_m,id) 选择最小值。数据中的独立标准目标为400003。

使用 `state`、`tool_use`、`reply` 的确定性评分。规则检查明确硬特征，语义正确性需要人工审核。`program_failure` 非空，或证据包含 `evidence_error`、`cleanup_error` 时，评分无效且 reward 为零。即使目标状态已经达成，程序异常也不能通过。

[正式评分入口](<tests/test.sh>)使用 `ESA_RUBRIC_PATH=/tests/rubric.json`。任务评分与参考执行器、Oracle 运行入口副本由[同步脚本](<../../scripts/sync-task-scorers.py>)维护，与[统一评分](<../../esa/score.py>)和共享运行代码逐字节一致。

## 原生 Oracle 验收

在仓库根目录执行：

```bash
.venv/bin/python scripts/sync-task-scorers.py
.venv/bin/python scripts/sync-task-scorers.py --check
.venv/bin/python scripts/validate_task.py --all --local-http --json
STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
  .venv/bin/harbor run -c configs/jobs/esa-oracle-acceptance.yaml --job-name UNIQUE -y
.venv/bin/python scripts/test-oracle-failure.py
```

[原生验收配置](<../../configs/jobs/esa-oracle-acceptance.yaml>)使用 Harbor 原生 `oracle` 和内置 Docker 环境运行七题。模型地址与密钥显式设置为不可用地址和占位值，Oracle 不调用模型或 judge，无需加载真实凭据。新增或修改任务必须通过原生验收，自定义 Agent 与临时 HTTP 预检不能替代。

[七题原生验收结果](<../../jobs/esa-native-oracle-standard-20261103/result.json>)记录各项 reward 为 1。[异常后达标回归](<../../jobs/esa-native-oracle-failure-a3b231e311d8/failure-regression.json>)确认达到目标后的程序异常得到零分。新增任务同时加入[benchmark 配置](<../../configs/bench/esa.yaml>)和原生验收配置。

