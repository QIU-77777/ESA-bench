# ESA local runtime

独立 Go 标准库服务。默认监听 `:8080`。环境会话由调用方创建和销毁。

## 执行和测试

```sh
cd runtime
go test -race -cover -timeout 30s ./...
go run .
docker build -t esa-local-runtime .
```

模型地址从 `RESPONSES_API_BASE_URL` 读取，允许完整 `/responses` 地址或 API base（自动追加 `/responses`）。认证从 `RESPONSES_API_KEY` 读取。已有 `agent.react.model_params.explicit_api_base` 和 `explicit_api_key` 支持 `${ENV_NAME}` 引用。运行过程不写入请求、认证或日志文件。返回结果中移除当前 API key 的字面内容。

`PORT` 设置监听端口。`RUNTIME_CONTEXT_MAX_BYTES` 设置每次完整 Responses JSON 请求的字节预算，默认 1048576。超过预算明确失败，保留完整历史。该预算按序列化字节计算，不估算模型 token 数量。模型供应方的 token 上限错误同样返回失败。

## HTTP 契约

`GET /v1/health` 返回 `{status:"ok",version:"esa-local-runtime/1.0.0"}`。

`POST /v1/run` 使用 JSON：

```json
{
  "agent": {"react": {"instructions": "系统指令", "model": {"model_id": "指定模型"}}},
  "model": "可选覆盖模型",
  "instruction": "用户指令",
  "user_messages": ["第一轮", "第二轮"],
  "mockSystemUrl": "http://mock-system:8003",
  "sessionId": "已创建会话",
  "toolProfile": ["启用工具名称"],
  "maxIterations": 12,
  "turnTimeoutSec": 240
}
```

`user_messages` 非空时依次执行这些用户消息；否则执行 `instruction`。`model` 允许字符串或 `react.model` 结构的对象。读取 `model_id`、`temperature`、`top_p`、`reasoning_effort`、`max_output_tokens`、`stream`。默认 `stream=true`。`maxIterations` 默认12，允许1–100。`turnTimeoutSec` 默认240，允许大于0且不超过3600，限制每轮全部模型及工具调用。请求客户端取消会取消当前上游请求。

返回 `{iterations,toolCalls,reply,status,error?,turns,runtimeVersion}`。`reply` 是最后执行轮次的回复。`turns` 是 `{instruction,iterations,reply,status,error?}` 数组。`iterations` 包含 `iteration`（每轮从1开始）、`turnIndex`（从0开始）、`message`、`reasoning`、`toolCalls`、`usage?`、`latencyMs`、`status`、`responseId?`、`receivedAt`、`error?`。工具调用包含 `callId`、`name`、`arguments`（JSON字符串）、`output`（MCP结果JSON字符串）、`status`、`latencyMs`、`outputChars`。缺失 usage 时省略字段，不补充零值。失败的模型尝试保留 iteration。

终止状态：`completed`、`failed`、`limit_exceeded`、`timeout`、`cancelled`。HTTP 请求格式错误返回400。已开始运行的失败返回200和结果中的失败状态，保留轨迹。
请求带 `Accept: text/event-stream` 时返回 `text/event-stream`，逐事件推送，最后一帧 `run_end` 携带完整结果，内容与 JSON 响应一致。事件类型与字段：

| 事件 | 字段 |
|---|---|
| `turn_start` | `turn_index`、`instruction` |
| `iteration_start` | `turn_index`、`iteration` |
| `reasoning_delta` | `turn_index`、`iteration`、`text` |
| `text_delta` | `turn_index`、`iteration`、`text` |
| `tool_call` | `turn_index`、`iteration`、`call_id`、`name`、`arguments` |
| `tool_result` | `turn_index`、`iteration`、`call_id`、`status`、`output`、`latencyMs` |
| `turn_end` | `turn_index`、`status`、`reply`、`error?` |
| `run_end` | `result`（完整结果对象） |

每帧为 `event:` 加 `data:` 两行。响应不是事件流时返回 JSON，内容与 `run_end` 的 `result` 相同。

## 协议行为

每轮首先调用 `POST /eval/api/v1/sessions/{sessionId}/round`，请求体为 `{}`，环境原子重置本轮屏幕焦点并保存版本事件。初始化失败时终止运行，不调用模型。

环境 MCP 地址为 `POST /eval/mcp/{sessionId}`。每轮请求 `tools/list`，使用其 `name/description/inputSchema` 转换模型工具。`toolProfile` 提供时，要求名称集合与会话工具集合完全相同。工具许可由环境会话执行。agent 旧工具白名单不影响工具集合。

串行调用 `tools/call`，参数为 `{name,arguments:{...,_call_id:callId},call_id:callId}`。工具错误进入 transcript。网络中断可能发生在环境提交之后，runtime不重试副作用，也不声明工具已回滚；调用方应检查会话事件。

Responses 每次发送 `instructions/tools/input/store=false`，重放完整 user、assistant、function_call、function_call_output。`call_id` 成对保留。reasoning 和 response ID 仅进入轨迹。支持 JSON 和 SSE；流必须收到 `response.completed` 才能执行工具。断流记录部分消息和调用，未执行调用标记 `cancelled`。

Dockerfile 使用固定版本 Go 构建镜像和非 root Alpine 运行镜像。离线测试使用 `httptest` 假模型与假 MCP，不需要模型凭据。Docker daemon 不可用时，镜像构建和容器运行验收仍需要在具备 Docker 的机器执行。
