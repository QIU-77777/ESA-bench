# ESA-bench 座舱智能体评测

在 Harbor 上评测车机智能体。每个用例是一次单轮对话：给定车辆初始状态和用户指令，
看智能体是否选对工具、传对参数、给出合规回复。

## 结构

- `esa/driver.mjs`：驱动脚本。创建 mock 会话、注入智能体定义、跑一轮对话、取回工具事件。
- `esa/freeze_agent.py`：从 agent-runtime 的生产配置复制一份智能体定义，裁剪工具清单后冻结成 JSON。
- `esa/agent.json`：冻结后的智能体定义。评测时由驱动随会话注入，不改动 agent-runtime 的配置。
- `esa/esa_agent.py`：Harbor 智能体实现。负责上传素材、启动驱动、写出轨迹与判分素材。
- `esa/score.py`：判分脚本。按评分标准从工具调用、回复文本、模型评审三路取分。
- `tasks/esa-*`：五个用例。`environment/esa/initial_state.json` 是车辆初始状态，
  `environment/esa/rubric.json` 是评分标准。

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
.venv/bin/python scripts/render-job.py --bench esa --harness esa --model stepfun/step-5-preview --output /tmp/job.yaml
PYTHONPATH="$PWD" .venv/bin/harbor run -c /tmp/job.yaml --job-name esa -y
```

结果写在 `jobs/<job-name>/`。每个试验的 `verifier/reward.json` 是多项得分，
`verifier/score-detail.json` 是逐条判定明细。

## 三处必须保持一致

工具清单在三处出现，改动任何一处都要同步另外两处：

- `/Users/jyxc-dz-0101032/qiguang/mcp/configs/eval_tools.toml` 的 `[eval.tools]` 决定 mock 服务对外暴露哪些工具；
- `esa/agent.json` 的 `react.tools` 决定智能体声明哪些工具，多一个少一个都会让会话初始化失败；
- `esa/freeze_agent.py` 的 `EXPOSED` 与 `EXTRA_TOOLS` 决定冻结时保留哪些工具。

## 上下文续接设置

冻结的定义里 `context_continuity = "replay"`。

默认的 `persist` 模式在 ReAct 循环内用 `previous_response_id` 续接上下文。
上游网关接受这个字段但不回带历史，第二次迭代的请求里只有一个 `function_call_output`，
模型把它当成用户新发来的消息。实测两个模型三次重复，推理内容都是"用户说成功查询到公司地址"。
`store` 设为 true 或 false 都一样。

`replay` 模式不发 `previous_response_id`，每次请求把完整对话作为输入项重放：
用户消息、`function_call`、`function_call_output` 依次排列。同样的内容直接发给网关时，
模型正确识别工具结果并继续下一步工具调用。

## 轨迹

`<trial>/agent/trajectory.json` 是 ATIF-v1.7 轨迹。一次 ReAct 迭代对应一个 step：

- 第一个 step 的 `source` 是 `user`，带用户指令；
- 其后每个 step 的 `source` 是 `agent`，带该次迭代的 `reasoning_content`、`tool_calls`、
  `observation`、`model_name`、`llm_call_count` 与 `metrics`；
- `extra` 里有 `iteration`、`response_id`、`trace_id`、`latency_ms` 与 `stop_reason`；
- 顶层 `final_metrics` 汇总 token 用量、总耗时与工具调用次数。

工具调用项与工具结果项在事件流里用的是两套编号，驱动按出现顺序配对。
