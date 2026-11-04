# ESA 任务参考验证规格

版本：2.0。日期：2026-11-03。

## 目标与保证范围

每题提供独立评分定义、可执行参考程序和错误答案测试。参考程序使用用户输入、模型可见工具定义和实际工具返回完成任务。

验证通过表示：指定部署环境中有一条合法参考解通过完整评分；已列出的合法替代解通过；已列出的错误解被有效拒绝。指令语义、信息来源和未覆盖的合法路径仍需人工审核。模型失败需要检查实际轨迹，排除工具、环境和评分缺陷后才能归因。

所有活动任务必须兼容 Harbor 原生 `OracleAgent`。新增或修改任务后，必须在实际 Docker 环境中执行原生 Oracle，并确认统一 verifier 通过。参考预检与自定义 Agent 验证不能替代此项验收。

## 标准任务目录

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

- [环境初态示例](<../tasks/esa-navi-execute/environment/esa/initial_state.json>)：`case_id`、用户消息、固定上下文、业务初态、数据覆盖、权限、故障与确定性用户应答。
- [标准入口](<../tasks/esa-navi-execute/solution/solve.sh>)：执行 Python Oracle 运行入口。
- [参考程序](<../tasks/esa-navi-execute/solution/reference.py>)：导出 `solve(ctx)`，可选 `ALTERNATIVES` 字典保存已知合法解法。
- [共享参考执行器副本](<../tasks/esa-navi-execute/solution/reference_runtime.py>)与[Oracle 运行入口副本](<../tasks/esa-navi-execute/solution/runner.py>)：分别与[宿主执行器](<../esa/reference.py>)和[宿主运行入口](<../esa/oracle.py>)逐字节一致。
- [错误答案测试](<../tasks/esa-navi-execute/tests/negative_tests.py>)：导出非空 `CASES`，每个函数正常结束并被统一评分拒绝。
- [独立 rubric](<../tasks/esa-navi-execute/tests/rubric.json>)：目标状态、工具、回复与安全条件。参考程序不得读取。
- [正式评分入口](<../tasks/esa-navi-execute/tests/test.sh>)：调用与[统一评分](<../esa/score.py>)及[状态评分](<../esa/state_score.py>)一致的任务副本。

Dockerfile 仅复制初态。Harbor 原生 Oracle 在执行阶段上传 `solution/`，verifier 在评分阶段上传 `tests/`。普通模型运行不接收参考解或评分答案。Oracle 运行入口只读取初态和参考程序，并访问实际环境服务；不读取 rubric，不执行错误答案测试，不调用模型或 judge。

[同步脚本](<../scripts/sync-task-scorers.py>)维护标准入口、参考执行器、Oracle 运行入口与评分副本。更新共享实现后必须同步，并通过 `--check`。任务副本过期或入口缺失时拒绝认证。

Console 生成标准草稿目录，参考函数尚未实现，错误测试为 `CASES = {}`。作者必须完成这两部分。错误测试为空时，预检在创建 HTTP 会话前拒绝认证。

## 参考程序接口

```python
def solve(ctx):
    address = ctx.call("get_home_or_company_address", {"poi_type": "company"})
    assert address["found"]
    poi = address["poi"]
    navigation = ctx.call("start_navi", {
        "poi_list": [f"{poi['id']}:{poi['name']}:{poi['lon']},{poi['lat']}:false"]
    })
    assert navigation["active"]
    ctx.say(f"已启动导航，预计 {navigation['duration_s'] / 60:.0f} 分钟。")
```

| 接口 | 含义 |
|---|---|
| `ctx.user_messages` | 已经交付的用户消息，不提前暴露未应答分支 |
| `ctx.tools` | 本轮实际 `tools/list` 工具定义 |
| `ctx.call(name, args, expect="success", call_id=None)` | 通过实际 MCP 调用工具，返回 `structuredContent` |
| `ctx.say(text)` | 记录助手回复 |
| `ctx.ask(question)` | 匹配声明的澄清分支，交付回答并开始下一轮 |
| `ctx.next_user()` | 交付固定多轮任务的下一条用户消息 |

`expect` 支持 `success`、`error` 和 `response_lost`。响应丢失必须有真实已提交事件证明，不能凭连接错误推断。每次逻辑调用使用唯一 `call_id`，与模型 runtime 约束一致。

程序可使用普通 Python 条件、循环、排序、断言，并导入 `math` 或 `re`。源代码检查拒绝常见文件读取、动态执行、其他导入与私有上下文访问。参考程序属于可信代码；该检查用于防止作者误用隐藏输入，不提供恶意代码安全隔离。执行设置期限。

关键实体和数值必须来自已提供的用户输入或工具返回。不得使用模型无法取得的目标 ID。人工审核检查信息来源。评分目标由作者独立定义，不从参考终态自动生成答案。

## 实际 MCP 预检

1. 读取任务输入、程序和评分内容，检查标准入口与共享副本。
2. 创建独立会话，仅传入环境配置字段，保存初始快照。
3. 每轮执行 `round` 和 `tools/list`，与 runtime 生命周期一致。
4. 通过 HTTP JSON-RPC `tools/call` 执行参考函数，保存完整参数、信封、返回和状态。
5. 保存最终快照、完整事件与对话，调用统一 `evaluate`。
6. 在独立会话中执行每个已知合法替代解和错误解。
7. 删除验证会话，保存完整证据。正式模型另建会话。

正式 ESAAgent 在可信宿主侧执行预检，通过容器 HTTP 桥接连接本次实际部署环境，不上传参考程序。宿主任务输入必须与模型容器读取的输入相同。

原生 Oracle 使用相同参考执行器直接执行 `solve(ctx)`，保存正式评分证据，再由 Harbor 调用统一 verifier。Oracle 不执行宿主侧的错误答案认证流程。

## 统一评分

接口为 `evaluate(rubric, evidence, judge_runner=None)`。证据包括 `initial`、`final`、`calls`、`events`、`truncated`、`reply` 和 `dialogue`，并可包含执行错误及用户应答完成标记。返回 `rewards`、`detail`、`passed`、`valid`。

- `expected_state` 使用独立目标谓词。
- `forbidden_changes` 检查全部事件，修改后恢复仍然失败。
- `tool_checks` 要求成功调用；任意一次符合工具和参数的调用可以满足条件。`any_tool` 同样检查参数。
- `tool_forbidden` 支持全程禁止，以及 `before_check` 或 `before_turn`。
- `reply_checks` 可指定 `turn_index`，只检查对应轮次的助手回复。
- `process_constraints` 支持有序工具、调用上限、失败限制与 `recover_response`。
- `recover_response(mode="retry")` 检查响应丢失后同工具、同参数的成功重试。
- 禁止工具、禁止状态修改、过程约束失败及未完成澄清分支使总分归零。
- `program_failure` 非空，或证据包含 `evidence_error`、`cleanup_error` 时，`valid=false`、`passed=false`、`reward=0`。目标状态已达成后程序异常也适用此规则。
- 必要证据缺失、未知规则或必需 judge 不可用同样使评分无效，不静默改变评分权重。

成功执行证据省略 `evidence_error` 和 `cleanup_error`；`program_failure` 可以为 `None` 或空字符串。普通模型旧证据不含执行错误字段时正常评分。正式 verifier 遇到无效评分写入零分与错误明细，并返回非零。

加权 reward 用于诊断。`passed` 表示全部必需条件通过，不能仅凭加权分较高认定完成。语义条件由人工审核；启用 judge 时用正确和错误答案校准。当前规则型验收不需要模型凭据。

## 澄清、边界与故障

- 执行任务通过工具达到目标状态。
- 信息任务检查所需事实在工具返回中可用，再验证回复硬特征或语义。
- 澄清任务只提供一条初始消息，`user_script` 在助手回复匹配 `reply_pattern` 后才交付 `user_reply`，并检查确认前未执行。
- 边界任务记录缺失的必要能力，人工检查全部开放工具的替代实现。能力删除和错误测试不自动证明通用不可满足性。
- 故障任务通过 `faults` 声明 `reject`、`missing_field` 或 `response_lost`。只读操作丢失后可按规则重试；写入恢复需要幂等机制或查询确认。环境同标识幂等恢复由独立环境测试验证。

## 证据与版本

每次正式模型运行重新预检，不依赖旧证据缓存。认证报告保存：

- `input_sha256`：初态、rubric、参考与错误程序内容摘要。
- `environment`：部署 manifest，包括 `seed_sha256`、工具定义摘要和 `implementation_sha256`。
- `evaluation_sha256`：任务、环境、共享执行器、Oracle 运行入口与统一评分摘要。
- 初始与最终快照、完整调用、事件、对话、执行错误与失败原因。
- `review_required`：机器验证不能代替人工语义审核。

环境实现摘要覆盖服务 Python 源码、工具 schema、数据 schema 和工具配置。各程序变体的初始与最终环境身份必须一致。执行内容与记录摘要使用同一份读取结果，验证期间内容变化或环境指纹缺失时拒绝认证。升级代码、数据或评分后重新验证。

## 验证命令

在仓库根目录执行：

```bash
.venv/bin/python scripts/sync-task-scorers.py
.venv/bin/python scripts/sync-task-scorers.py --check

# 临时实际HTTP/MCP服务，使用显式MemoryStore，仅用于集成测试
.venv/bin/python scripts/validate_task.py --all --local-http --json

# 本次部署服务
.venv/bin/python scripts/validate_task.py --all --mock-url http://127.0.0.1:8003 --json

# Harbor原生Oracle与内置Docker环境验收
STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
  .venv/bin/harbor run -c configs/jobs/esa-oracle-acceptance.yaml --job-name UNIQUE -y

# 目标达成后异常仍被正式评分拒绝
.venv/bin/python scripts/test-oracle-failure.py
```

原生 Oracle 命令显式覆盖模型地址和密钥，避免 Docker Compose 使用本地真实凭据。复用的 runtime 服务可以启动，Oracle 不调用该服务的模型接口。[失败回归脚本](<../scripts/test-oracle-failure.py>)同样强制使用不可用模型地址与占位密钥。每次运行选择未使用的 job 名称。

预检 CLI 退出码：0 表示全部任务通过，1 表示验证失败，2 表示参数错误。报告保留失败阶段与实际证据，不根据异常消息关键词推断责任。

## 验收条件

- 标准入口与共享副本检查通过。
- 原生 Oracle 实际执行环境验收与统一 verifier 通过。
- 参考通过完整评分，每题至少两个正常结束的错误答案被拒绝，已知合法替代解通过。
- 澄清回答仅在匹配问题后提供，故障真实出现并按规则恢复，禁止行为使总分归零。
- 程序异常、证据错误、未知规则与必需 judge 缺失使评分无效且 reward 为零。
- 普通模型不接收参考解或评分答案，原生上传阶段遵守 `solution/` 与 `tests/` 边界。
- 正式模型预检失败时不启动模型，预检与模型使用独立会话。
- 新任务同时加入[benchmark 配置](<../configs/bench/esa.yaml>)与[原生 Oracle 配置](<../configs/jobs/esa-oracle-acceptance.yaml>)，原始任务归档完整。

[七题原生验收结果](<../jobs/esa-native-oracle-standard-20261103/result.json>)记录七题全部完成且 reward 为 1。[异常后达标回归结果](<../jobs/esa-native-oracle-failure-a3b231e311d8/failure-regression.json>)确认已达到目标的程序异常仍被拒绝。完整执行范围见[验证记录](<esa-validation.md>)。
