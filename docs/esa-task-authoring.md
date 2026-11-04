# 编写 ESA 评测任务

## 标准任务目录

完整合同见[参考验证规格](<esa-solvability-spec.md>)。示例见[导航参考](<../tasks/esa-navi-execute/solution/reference.py>)、[澄清参考](<../tasks/esa-veh-clarify/solution/reference.py>)和[故障参考](<../tasks/esa-med-response-loss/solution/reference.py>)。

```text
tasks/<id>/
  instruction.md
  task.toml
  environment/Dockerfile
  environment/docker-compose.yaml
  environment/esa/initial_state.json
  solution/solve.sh
  solution/reference.py
  solution/reference_runtime.py
  solution/runner.py
  tests/negative_tests.py
  tests/rubric.json
  tests/score.py
  tests/state_score.py
  tests/test.sh
```

所有活动任务必须兼容 Harbor 原生 `OracleAgent`。[标准入口](<../tasks/esa-navi-execute/solution/solve.sh>)执行 Python [运行入口](<../esa/oracle.py>)，再使用[共享参考执行器](<../esa/reference.py>)调用实际 MCP。

Dockerfile 仅复制初态。Oracle 在原生执行阶段上传 `solution/`；运行入口读取初态和参考程序，不读取 rubric 或错误测试。verifier 在评分阶段上传 `tests/`。普通模型运行不接收 `solution/` 或评分答案。

## 定义环境输入

[工具目录](<../environment/catalog.json>)定义名称、描述和输入 schema。[能力清单](<../environment/manifest.json>)记录数据来源与副作用。[工具配置](<../configs/tools.json>)控制默认权限。

```json
{
  "case_id": "example",
  "dataset_version": "esa-local-v1",
  "seed": 0,
  "user_messages": ["导航去公司"],
  "initial_state": {"vehicle": {"gear": "D"}},
  "tool_profile": ["get_home_or_company_address", "start_navi", "get_navi_info"]
}
```

公司地址必须在会话资料中存在，并能由开放工具返回。参考函数不能直接读取初态或 seed。`data_overlays` 只覆盖已知实体的已有字段。新增数据需更新 `dataset_version`，并同步任务版本。

POI 使用 `id`、`name`、`lon`、`lat`。坐标必须为数字。状态路径使用点分隔，数组下标使用数字，例如 `media.playlist.0.id`。

## 编写参考程序

[参考示例](<../tasks/esa-navi-execute/solution/reference.py>)导出 `solve(ctx)`。`ctx.call` 通过实际 MCP 调用工具并返回 `structuredContent`。参数从已提供的用户消息和返回字段计算。

```python
def solve(ctx):
    result = ctx.call("get_home_or_company_address", {"poi_type": "company"})
    assert result["found"] and result["poi"]
    p = result["poi"]
    navigation = ctx.call("start_navi", {
        "poi_list": [f"{p['id']}:{p['name']}:{p['lon']},{p['lat']}:false"]
    })
    assert navigation["active"]
    ctx.say(f"已启动导航，预计 {navigation['duration_s'] / 60:.0f} 分钟。")
```

`ctx.tools` 提供本轮实际 `tools/list`。`ctx.user_messages` 只包含已经交付的用户输入。程序可使用普通 Python 条件、循环、排序、断言，并导入 `math` 或 `re`。不得读取文件、seed、rubric 或 `ctx` 私有属性。代码审核必须检查信息来源，源代码检查不能代替人工审核。

已知合法替代解可通过 `ALTERNATIVES = {"名称": function}` 声明。没有业务顺序要求时，不使用工具序列白名单限制等价路径。

## 独立编写 rubric

评分答案放在[评分目录示例](<../tasks/esa-navi-execute/tests/rubric.json>)。作者根据题意独立定义目标，不将参考终态自动保存为答案。

```json
{
  "case_id": "example",
  "weights": {"state": 0.6, "tool_use": 0.2, "reply": 0.2},
  "expected_state": {"navigation.active": true, "navigation.destination.id": "200001"},
  "forbidden_changes": ["vehicle", "addresses"],
  "tool_checks": [{"id": "lookup", "any_tool": ["get_home_or_company_address", "get_favorite_poi"]}],
  "reply_checks": [{"id": "started", "pattern": "已.*导航"}]
}
```

[统一评分](<../esa/score.py>)提供 `evaluate(rubric, evidence, judge_runner=None)`。参考预检和正式 verifier 调用同一个实现，Oracle 仅保存该实现需要的执行证据。

- `expected_state` 支持对象或 `path/op/value` 数组。
- `tool_checks` 默认 `required=true`，只接受成功调用；`any_tool` 同样检查参数，`arguments_match` 使用结构化子集匹配。
- `tool_forbidden` 支持 `before_check` 或 `before_turn`。确认前禁止执行可使用 `before_turn: 1`。
- `reply_checks` 可指定 `turn_index`，只检查对应轮次的助手回复。
- `forbidden_changes` 检查全部事件，修改后恢复仍然失败。
- 禁止工具、禁止修改与过程违规使总分归零。
- `program_failure` 非空，或证据包含 `evidence_error`、`cleanup_error` 时，评分无效且 reward 为零。即使目标状态已达成，程序异常也不能通过。
- 未知规则、必要证据缺失和启用的 judge 缺席使评分无效；正式 verifier 返回非零。

正则只检查明确硬特征。语义条件需要人工审核；启用 judge 时必须完成正确与错误回复校准。

## 编写错误答案测试

[错误测试示例](<../tasks/esa-navi-execute/tests/negative_tests.py>)导出 `CASES = {"错误名称": function}`。错误测试使用独立命名空间，可以引用原参考 `solve(ctx)`，不能替换入口。每题至少两个错误解。

错误函数必须正常结束。工具错误用 `expect="error"` 明确接收。抛出异常、工具不可见或接口故障不算有效错误答案。测试应覆盖错误目标、额外副作用、确认前执行或编造回复，并检查统一评分能够拒绝。

Console 生成标准目录骨架、未实现的 `solve(ctx)` 和 `CASES = {}`。作者必须完成参考和错误测试；空的 `CASES` 会在创建 HTTP 会话前被拒绝。

## 澄清与故障

澄清题只提供一条初始 `user_messages`，再声明 `user_script`：

```json
{"user_script": [{"reply_pattern": "多少.*度|几度", "user_reply": "24度"}]}
```

`ctx.ask(question)` 与正式 driver 使用相同分支匹配。模型未提出匹配问题时，不提供后续答案。每轮重新执行 `round` 与 `tools/list`，保留完整对话和状态。

`faults` 支持 `reject`、`missing_field` 和 `response_lost`。参考函数通过 `expect="error"` 或 `expect="response_lost"` 接收声明的失败，再核查并恢复。响应丢失必须有真实已提交事件，普通连接错误不能冒充故障。

`recover_response(mode="retry")` 检查丢失响应后的同工具、同参数成功重试。对已提交写入不能盲目重复。当前 Agent 验收使用只读查询重试，同标识幂等写入恢复由环境专项测试验证。

## 同步、预检与原生验收

所有命令在仓库根目录执行：

```bash
.venv/bin/python scripts/sync-task-scorers.py
.venv/bin/python scripts/sync-task-scorers.py --check
.venv/bin/python scripts/validate_task.py --all --local-http --json
.venv/bin/python scripts/validate_task.py --all --mock-url http://127.0.0.1:8003 --json

STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
  .venv/bin/harbor run -c configs/jobs/esa-oracle-acceptance.yaml --job-name UNIQUE -y
.venv/bin/python scripts/test-oracle-failure.py
```

[同步脚本](<../scripts/sync-task-scorers.py>)维护标准入口、共享运行入口、参考执行器与评分副本。`--check` 检查逐字节一致性。临时 HTTP 服务使用显式 `MemoryStore`，只用于集成测试。

新增或修改任务后，必须通过[原生 Oracle 配置](<../configs/jobs/esa-oracle-acceptance.yaml>)的实际 Docker 验收。自定义 Agent 验证与临时预检不能替代该验收。命令使用不可用模型地址和占位密钥，Oracle 不调用模型或 judge，无需加载真实凭据。[失败回归脚本](<../scripts/test-oracle-failure.py>)检查达到目标后程序异常仍然被正式 verifier 拒绝。

预检证据写入 `jobs/reference/<task>/golden-run.json`。人工审核指令、信息充分性和合法替代解后，将新任务同时加入[benchmark 配置](<../configs/bench/esa.yaml>)与[原生验收配置](<../configs/jobs/esa-oracle-acceptance.yaml>)。

[七题原生验收结果](<../jobs/esa-native-oracle-standard-20261103/result.json>)与[异常后达标回归结果](<../jobs/esa-native-oracle-failure-a3b231e311d8/failure-regression.json>)记录实际 Docker 验证。正式 ESAAgent 在每次部署中重新预检，通过后创建独立模型会话。模型失败保留完整证据；存在环境或评分缺陷的试验不用于正常模型比较。
