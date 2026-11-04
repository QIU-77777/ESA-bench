# ESA 本地车机评测

## 组件

- [共享参考执行器](<reference.py>)：通过实际 HTTP/MCP 执行 Python 参考、合法替代解和错误答案测试。
- [原生 Oracle 运行入口](<oracle.py>)：执行参考程序，保存正式 verifier 所需的证据，不读取 rubric 或调用模型。
- [Harbor Agent](<esa_agent.py>)：正式模型运行前验证任务，生成 ATIF 与评分证据。
- [驱动](<driver.mjs>)：创建独立模型会话，执行固定或澄清对话，保存状态与调用。
- [统一评分](<score.py>)：提供 `evaluate(rubric, evidence, judge_runner=None)`，供参考验证与正式 verifier 共用。
- [状态评分](<state_score.py>)：终态、禁止修改和过程约束。
- [Agent 定义](<agent.json>)、[工具目录](<../environment/catalog.json>)、[环境服务](<../environment/README.md>)与[Go runtime](<../runtime/README.md>)。

本文命令均在仓库根目录执行。

## 原生 Oracle 与任务目录

所有活动任务必须兼容 Harbor 原生 `OracleAgent`。任务提供标准 [solve.sh](<../tasks/esa-navi-execute/solution/solve.sh>)，它调用 [runner.py](<../tasks/esa-navi-execute/solution/runner.py>)，再使用 [reference_runtime.py](<../tasks/esa-navi-execute/solution/reference_runtime.py>) 执行 [reference.py](<../tasks/esa-navi-execute/solution/reference.py>)。

原生 Oracle 在执行阶段上传 `solution/`。运行入口只读取环境初态与参考程序，并访问实际 MCP 服务；它不读取评分答案或错误答案测试。verifier 在评分阶段接收 `tests/`。普通模型运行不接收 `solution/`，任务环境镜像仅复制初态。

任务的共享运行入口、参考执行器与评分副本必须和宿主实现逐字节一致。[同步脚本](<../scripts/sync-task-scorers.py>)同步这些副本；`--check` 检查一致性。目录与接口见[任务编写指南](<../docs/esa-task-authoring.md>)和[参考验证规格](<../docs/esa-solvability-spec.md>)。

## 参考预检

每题提供可信侧参考程序、独立 rubric 和非空错误答案测试。参考程序仅使用已交付的用户消息、`tools/list` 与实际 `structuredContent`，不直接读取初态、答案或 seed。

正式 ESAAgent 核对宿主与容器初态，通过容器 HTTP 桥接访问本次部署环境。正例和已知合法替代解必须通过全部必需条件。错误解必须正常结束并被有效评分拒绝。全部程序使用独立会话，完成后删除。预检通过后创建模型会话。

全部变体的初始和最终环境身份必须一致。副本过期、证据缺失、程序异常或必需 judge 不可用时拒绝认证。参考通过表示指定环境中有一个已验证解；信息来源、指令语义与未覆盖的合法路径仍需人工审核。

Console 生成的任务包含标准目录骨架、未实现的 `solve(ctx)` 和空的 `CASES`。作者必须完成参考程序与错误测试。错误测试为空时，预检在创建 HTTP 会话前拒绝认证。

## 构建与原生验收

```bash
bash scripts/build-esa-local.sh
.venv/bin/python scripts/sync-task-scorers.py
.venv/bin/python scripts/sync-task-scorers.py --check
.venv/bin/python scripts/validate_task.py --all --local-http --json

STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
  .venv/bin/harbor run -c configs/jobs/esa-oracle-acceptance.yaml --job-name UNIQUE -y
.venv/bin/python scripts/test-oracle-failure.py
```

[原生验收配置](<../configs/jobs/esa-oracle-acceptance.yaml>)使用 Harbor 原生 `oracle` 和内置 Docker 环境运行七题。命令显式覆盖模型地址与密钥为不可用地址和占位值，避免 Docker Compose 使用本地凭据。工具服务和 Redis 正常运行；Oracle 不调用模型或 judge。无需执行凭据加载命令。

[失败回归脚本](<../scripts/test-oracle-failure.py>)创建独立原生 Oracle 试验，检查目标状态已经达成后抛出异常仍然得到 `reward=0`、`valid=false`，随后清理资源。

[七题原生验收结果](<../jobs/esa-native-oracle-standard-20261103/result.json>)包含七项完成试验、零项异常，各项 reward 为 1。[异常后达标回归结果](<../jobs/esa-native-oracle-failure-a3b231e311d8/failure-regression.json>)确认程序异常被拒绝。

临时 HTTP/MCP 预检和自定义 Agent 验证不能替代原生 Oracle 实际环境验收。公共镜像为 `esa-react-runtime:1.0.0` 与 `esa-local-environment:2.0.0`。业务工具使用本地固定资料，不控制真实车辆。

## 正式模型运行

设置模型凭据后执行：

```bash
set -a
source .env
set +a
.venv/bin/python scripts/render-job.py --bench esa --harness esa --model stepfun/step-5-preview --output /tmp/esa-job.yaml
PYTHONPATH="$PWD" .venv/bin/harbor run -c /tmp/esa-job.yaml --job-name esa-model-run -y
```

新增任务必须同时加入 [benchmark 配置](<../configs/bench/esa.yaml>)和[原生验收配置](<../configs/jobs/esa-oracle-acceptance.yaml>)，并通过原生 Oracle 验收。

## 评分与对话

- `expected_state`：独立终态谓词。
- `forbidden_changes`：检查所有事件，修改后恢复仍然失败。
- `tool_checks`：成功调用、参数匹配与合法替代工具。
- `tool_forbidden`：全程禁止、`before_check` 或 `before_turn`。
- `reply_checks`：明确硬特征，可指定 `turn_index`。
- `process_constraints`：`ordered_tools`、`max_calls`、`no_failed_calls` 与 `recover_response(mode="retry")`。
- `user_script`：助手问题匹配后才交付后续用户回答。

禁止修改、禁止工具、过程违规和未完成澄清分支使总分归零。`program_failure` 非空，或证据包含 `evidence_error`、`cleanup_error` 时，评分无效且总分为零。未知规则、必要证据缺失和必需 judge 不可用同样使评分无效；正式 verifier 返回非零。普通模型证据没有这些执行错误字段时保持正常评分。

当前七题采用确定性规则评分。语义 judge 的参考认证需要注入同一个可用 `judge_runner`。默认参考预检和原生 Oracle 不调用 judge。正式 verifier 使用 `JUDGE_API_BASE_URL`、`JUDGE_API_KEY` 和 `JUDGE_MODEL_ID`；语义任务必须完成正确与错误答案校准。

## 执行证据

Harbor 保存模型轨迹、完整调用、对话、初始与最终快照、状态事件和环境清单。原生 Oracle 保存同样的正式评分证据。可信侧参考目录保存完整预检报告，包含源码、输入和环境摘要，并对凭据脱敏。

## 开发验证

```bash
.venv/bin/python scripts/sync-task-scorers.py --check
.venv/bin/python -B -m unittest esa.test_score esa.test_state_score esa.test_verifier esa.test_trajectory esa.test_reference esa.test_reference_agent esa.test_oracle environment.test_reference_fingerprint environment.test_environment
node --test esa/test-driver.mjs
(cd runtime && go test ./...)
.venv/bin/python scripts/test-reference-docker.py
```

[Docker 参考验证](<../scripts/test-reference-docker.py>)创建独立环境服务和专用 Redis，执行参考与环境测试并清理资源。[离线导航配置](<../configs/jobs/esa-offline-acceptance.yaml>)使用[确定性模型接口](<fake_responses.py>)检查模型驱动链路。各项验收范围见[验证记录](<../docs/esa-validation.md>)。

## 交互式对话

```bash
.venv/bin/python -m esa.cli chat
.venv/bin/python -m esa.cli chat --task tasks/esa-navi-execute
```

[命令行](<cli.py>)复用同一个 runtime 与工具环境，支持状态、事件、工具与会话续接。该入口用于人工对话，不执行任务认证。接口见[命令行规范](<../docs/esa-agent-cli-spec.md>)。
