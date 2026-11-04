# ESA 评测任务构造方案

版本：2.0。日期：2026-11-03。

## 任务合同

每题使用 Python 参考程序、独立 rubric 和正常结束的错误答案测试。参考与正式模型调用实际 MCP，参考预检和正式 verifier 使用同一个 `evaluate` 评分入口。

所有活动任务提供标准 `solution/solve.sh` 入口，兼容 Harbor 原生 `OracleAgent`。辅助 Python 文件放在 `solution/`，评分与错误答案测试放在 `tests/`。Oracle 在执行阶段接收参考目录；verifier 在评分阶段接收评分目录。普通模型不接收参考解或评分答案，环境镜像仅复制初态。

Oracle 运行入口读取初态和参考函数，保存真实 MCP 调用、对话、快照和事件。它不读取 rubric 或执行错误答案测试。正式 ESAAgent 在可信宿主侧预检参考、合法替代解和错误解，通过后创建独立模型会话。

目录、接口和保证范围见[参考验证规格](<esa-solvability-spec.md>)。编写步骤见[任务编写指南](<esa-task-authoring.md>)。

## 当前验收任务

| 任务 | 验证内容 | 错误答案 |
|---|---|---|
| [导航](<../tasks/esa-navi-execute/solution/reference.py>) | 查询地址，从返回字段启动导航，接受收藏查询替代解 | 错误目的地、额外车控 |
| [充电选择](<../tasks/esa-chg-select/solution/reference.py>) | 筛选实际候选的功率与可用数量，按距离选择并导航 | 较远候选、额外车控 |
| [车辆澄清](<../tasks/esa-veh-clarify/solution/reference.py>) | 询问温度，获得匹配分支回答，确认后控制 | 未确认就执行、额外音量修改 |
| [车辆信息](<../tasks/esa-inf-knowledge/solution/reference.py>) | 使用查询返回说明电量和续航，不修改状态 | 编造数值、额外车控 |
| [能力边界](<../tasks/esa-veh-boundary/solution/reference.py>) | 根据工具目录说明执行能力缺失 | 编造成功、编造转交处理 |
| [错误重试](<../tasks/esa-med-retry/solution/reference.py>) | 查询失败后重试，使用成功返回播放一次 | 失败后谎称播放、重复播放 |
| [响应丢失](<../tasks/esa-med-response-loss/solution/reference.py>) | 只读搜索丢失响应后用新标识重试，再播放一次 | 未恢复就谎称播放、重复播放 |

七题用于机制验收。模型难度与能力结论需要实际模型试验和人工审核。当前任务使用确定性评分，证据标记 `review_required`。

## 评分与版本

- `expected_state` 独立定义任务目标。
- 必需的 `tool_checks` 检查成功调用与参数，`any_tool` 支持合法替代工具。
- `forbidden_changes`、`tool_forbidden` 和 `process_constraints` 实施总分安全否决。
- `reply_checks` 检查明确硬特征，支持按轮次过滤。
- `user_script` 仅在助手问题匹配后提供用户回答。
- 程序异常、证据收集或清理错误、必要证据缺失、未知规则和必需 judge 不可用使评分无效且总分为零。
- Oracle 达到目标状态后发生异常，也必须被正式 verifier 拒绝。
- 任务运行入口、参考执行器和评分副本与共享实现逐字节一致。输入、源码或环境身份变化后重新验证。

Console 生成未实现的参考函数和空的错误测试集合，作者完成任务合同后才能认证。空的 `CASES` 在创建 HTTP 会话前被拒绝。

## 实施与实际验收

命令在仓库根目录执行：

```bash
.venv/bin/python scripts/sync-task-scorers.py
.venv/bin/python scripts/sync-task-scorers.py --check
.venv/bin/python -m environment.generate_manifest
.venv/bin/python scripts/validate_task.py --all --local-http --json
.venv/bin/python scripts/validate_task.py --all --mock-url http://127.0.0.1:8003 --json

STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
  .venv/bin/harbor run -c configs/jobs/esa-oracle-acceptance.yaml --job-name UNIQUE -y
.venv/bin/python scripts/test-oracle-failure.py
```

[同步脚本](<../scripts/sync-task-scorers.py>)维护共享副本。临时 HTTP 集成模式使用显式 `MemoryStore`，正式部署使用 Redis。参考预检连接本次实际服务，并检查完整评分和错误答案测试。

[原生 Oracle 配置](<../configs/jobs/esa-oracle-acceptance.yaml>)使用内置 Docker 环境执行标准参考入口。模型地址与密钥显式设置为不可用地址和占位值；Oracle 不调用模型或 judge，无需加载真实凭据。新增或修改任务必须通过该验收，自定义 Agent 验证不能替代。

[七题原生验收结果](<../jobs/esa-native-oracle-standard-20261103/result.json>)记录七题全部完成、零项异常、各项 reward 为 1。[异常后达标回归结果](<../jobs/esa-native-oracle-failure-a3b231e311d8/failure-regression.json>)确认程序异常时 reward 为零，目标已达也不能通过。

新增任务同时加入[benchmark 配置](<../configs/bench/esa.yaml>)和原生验收配置。[离线导航配置](<../configs/jobs/esa-offline-acceptance.yaml>)用于检查模型驱动链路，不消耗真实模型额度。

## CAR-bench 参考范围

[CAR-bench 论文](https://arxiv.org/html/2601.22027v1)提供执行、消歧、能力边界和重复成功率的设计参照。当前实现使用普通 Python 控制流绑定工具返回，工具图不作为构建前提。

参考通过证明一个已验证解存在。删除链路中的工具不能证明不存在其他解。每题需要审核替代路径与独立评分，模型失败需要根据实际证据排除环境和评分缺陷。

## 任务审核与集合报告

作者检查指令明确性、可见信息、参考参数来源、错误答案覆盖和合法替代解。评审者检查完整语义、业务策略及评分能否拒绝错误答案。启用语义 judge 时同时校准正确与错误回复。

集合报告分别呈现业务领域、交互类型、必需条件通过率和失败原因。重复运行时区分全部成功与至少一次成功。环境缺陷和评分无效的试验单独报告。
