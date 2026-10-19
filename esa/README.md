# ESA-bench 座舱智能体评测

在 Harbor 上评测车机智能体。每个用例是一次单轮对话：给定车辆初始状态和用户指令，
看智能体是否选对工具、传对参数、给出合规回复。

## 结构

- `esa/driver.mjs`：驱动脚本。创建 mock 会话、注入智能体定义、跑一轮对话、取回工具事件。
- `esa/freeze_agent.py`：从 agent-runtime 的生产配置复制一份智能体定义，裁剪工具清单后冻结成 JSON。
- `esa/agent.json`：冻结后的智能体定义。评测时由驱动随会话注入，不改动 agent-runtime 的配置。
- `esa/cockpit_agent.py`：Harbor 智能体实现。负责上传素材、启动驱动、写出轨迹与判分素材。
- `esa/score.py`：判分脚本。按评分标准从工具调用、回复文本、模型评审三路取分。
- `tasks/cockpit-*`：五个用例。`environment/cockpit/initial_state.json` 是车辆初始状态，
  `environment/cockpit/rubric.json` 是评分标准。

## 五个用例

| 用例 | 指令 | 考察点 |
| --- | --- | --- |
| SC-A1 | 导航去公司 | 从常用地址取到公司本身，一轮完成导航 |
| SC-B1 | 导航去上次那个地方 | 指代消歧，给出候选并附到访时间 |
| SC-A3 | 找一下附近能用的快充桩，挑最近的去 | 多条件筛选，跳过检修与慢充站点 |
| SC-A4 | 放一首周杰伦的《晴天》 | 同名曲目里选对原唱版本 |
| SC-C3 | 帮我看看明天回老家全程会不会堵车 | 承认无法预测未来路况 |

## 运行

```bash
set -a; . /Users/jyxc-dz-0101032/qiguang/agent-runtime/.env; set +a
export STEPFUN_OPENAI_BASE_URL=https://api.stepfun.com/v1
export STEPFUN_API_KEY="$RESPONSES_API_KEY"
export STEPFUN_OPENAI_MODEL_ID=step-5-preview
.venv/bin/python scripts/render-job.py --bench cockpit --harness cockpit --model stepfun/step-5-preview --output /tmp/job.yaml
PYTHONPATH="$PWD" .venv/bin/harbor run -c /tmp/job.yaml --job-name cockpit -y
```

结果写在 `jobs/<job-name>/`。每个试验的 `verifier/reward.json` 是多项得分，
`verifier/score-detail.json` 是逐条判定明细。

## 两处必须保持一致

工具清单在三处出现，改动任何一处都要同步另外两处：

- `/Users/jyxc-dz-0101032/qiguang/mcp/configs/eval_tools.toml` 的 `[eval.tools]` 决定 mock 服务对外暴露哪些工具；
- `esa/agent.json` 的 `react.tools` 决定智能体声明哪些工具，多一个少一个都会让会话初始化失败；
- `esa/freeze_agent.py` 的 `EXPOSED` 与 `EXTRA_TOOLS` 决定冻结时保留哪些工具。

## 上游续接设置

冻结的定义里 `store_responses = false`。网关按响应编号续接上下文时不回带历史，
模型只能看到工具结果的文本，会把结果当成用户新发来的消息，导致每轮只调用一个工具就停下。
关闭后每次请求回放完整历史，多轮工具调用正常。
