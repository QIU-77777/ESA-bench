# 查询公司地址并启动导航

座舱链路示例任务：单轮导航。用户输入见 [instruction.md](<instruction.md>)。座舱初态、工具集合与用户脚本见 [initial_state.json](<environment/esa/initial_state.json>)。

本任务跑在座舱链路上：`mock-system` 是 MCP 记录代理，`cockpit-mcp` 是用户 MCP server（默认镜像为 `cockpit/fake_mcp.py`），`cockpit-agent` 是被测座舱 agent（默认镜像为 `cockpit/fake_agent.py`）。证据结构与评分实现与 ESA 任务一致。

## 参考接口

[reference.py](<solution/reference.py>) 导出 `solve(ctx)`。`ctx.user_messages` 为元组，`ctx.tools` 为当前 MCP 工具名称与 schema 的字典；`ctx.call(name, args, expect='success', call_id=None)` 返回实际 `structuredContent`，响应丢失时返回 `None`；`ctx.say(text)` 记录回复；`ctx.ask(question)` 返回固定用户脚本应答并建立新轮次。

参考程序使用已交付的用户消息与实际工具返回取得参数，不读取 seed、评分标准或隐藏答案。状态目标在 [rubric.json](<tests/rubric.json>) 独立定义。任务镜像仅复制初态。

Harbor 原生 Oracle 在执行阶段上传 `solution/`，通过 [solve.sh](<solution/solve.sh>) 调用 [runner.py](<solution/runner.py>)，使用 [reference_runtime.py](<solution/reference_runtime.py>) 执行参考函数。Oracle 运行入口只读取初态和参考程序，并访问 recorder 转发的实际 MCP，不读取 rubric 或执行错误测试。verifier 在评分阶段上传 `tests/`。普通模型不接收参考解或评分答案。

## 错误答案测试

[negative_tests.py](<tests/negative_tests.py>) 导出 `CASES`：

- `wrong_destination`：查询住宅地址并导航到住宅。
- `skip_query`：不查询地点，直接硬编码地点串启动导航。

所有错误函数必须完成执行。完整评分拒绝才构成有效负例，抛出异常不构成有效负例。

## 验收条件

先查询公司地点，使用实际返回的 id、name、lon、lat 启动导航。终态目的地为公司，回复时间与距离来自导航返回。

使用 `state`、`tool_use`、`reply` 的确定性评分。规则检查明确硬特征，语义正确性需要人工审核。`program_failure` 非空，或证据包含 `evidence_error`、`cleanup_error` 时，评分无效且 reward 为零。即使目标状态已经达成，程序异常也不能通过。

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
.venv/bin/python scripts/test-oracle-failure.py --task tasks/esa-cockpit-navi --goal-path navigation.active
```

座舱 bench 与 esa bench 分开，结果与分数不做直接比较：环境身份、工具集合与状态结构不同。接入契约见[座舱接入文档](<../../docs/esa-cockpit-integration.md>)。
