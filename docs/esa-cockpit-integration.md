# 座舱 Agent 评测接入契约

状态：实施依据。日期：2026-11-04。范围：用户自研座舱 agent 与用户设计的本地 MCP server 如何接入 ESA-bench 座舱评测。改造方案见 [esa-cockpit-agent-eval-plan.md](<esa-cockpit-agent-eval-plan.md>)。

本文档冻结两类用户资产的接入契约：座舱 agent（被测对象）与座舱 MCP server（工具与状态提供方）。评测不连接真实车辆，全部过程在容器内完成；结果由初态与调用序列决定，不依赖外部设备、真实时钟与随机数。

## 1. 链路与角色

```text
Harbor trial 容器组
  driver（主容器，esa/driver.mjs 座舱分支）
    ├─ 创建会话、逐轮 POST agent /v1/turn、拉取事件与快照
  mock-system（cockpit/recorder.py，MCP 记录代理）
    ├─ 对外实现 ESA 环境协议，对内转发 tools/call 到用户 MCP server
    ├─ 按会话 tool_profile 执行工具权限，注入故障，计算逐事件状态差异
  cockpit-agent（用户自研座舱 agent，HTTP 服务）
    └─ 经 recorder 的 /eval/mcp/{sid} 调用用户 MCP server
  cockpit-mcp（用户 MCP server，工具与座舱状态）
  redis（会话记录与事件）
```

驱动不执行 ReAct 循环；模型循环在被测 agent 内部。recorder 录制全部调用并产出与 ESA 任务一致的证据文件，`esa/score.py` 与 `esa/state_score.py` 直接复用。

## 2. MCP server 接入契约

用户的 MCP server 需要提供七项能力。前两项与服务发现相关，其余按会话隔离座舱状态。

| 能力 | 要求 | 说明 |
|---|---|---|
| MCP 工具服务 | 标准 MCP JSON-RPC（`tools/list`、`tools/call`），HTTP 传输 | recorder 经 `/eval/mcp/{sid}` 接收调用后转发。无会话上下文的 `tools/list` 必须返回全量工具定义，recorder 启动时读取一次 |
| 健康检查 | `GET /health` 返回 200，上报 `version` 与 `commit` | 任务 compose 就绪检查使用；两者进入 manifest 指纹 |
| 数据版本 | `/health` 或 `/manifest` 上报 `data_version` 与 `data_sha256` | `data_sha256` 是裸 64 位小写十六进制，不带 `sha256:` 前缀，不能是提交号或版本号；缺失时 recorder 启动失败，任务 `initial_state.json` 的 `dataset_version` 必须与 `data_version` 一致 |
| 会话隔离 | 按 session_id 隔离座舱状态，session_id 出现在 MCP 端点路径或调用参数中 | 默认路径形态：`POST {COCKPIT_MCP_URL}/eval/mcp/{sid}`；参数形态由 `COCKPIT_MCP_SESSION_MODE=param` 选择，recorder 在 JSON-RPC params 里加 `session_id` |
| 初态注入 | 会话创建时接收座舱初态，两种形态任选其一 | HTTP 形态（默认）：`POST {COCKPIT_MCP_URL}/sessions`，body 含 `session_id`、`initial_state`、`context`、`tool_profile`、`seed`、`dataset_version`；MCP 形态：`initialize` 参数带同一批字段，由 `COCKPIT_INJECT_MODE=mcp` 选择 |
| 状态读取 | 二者之一：状态查询工具（默认名 `get_vehicle_state`，可配置）；或 HTTP 状态端点 `GET /state/{sid}` | 快照与逐事件差异的数据来源，每次工具调用前后各读一次；状态查询工具不要求出现在任务 `tool_profile` 里，recorder 内部读取不写事件流 |
| 会话删除 | 按 session_id 删除 server 侧会话：`DELETE {COCKPIT_MCP_URL}/sessions/{sid}` | recorder 的 reset 与 DELETE 都依赖它；参考验证的会话清理也走 DELETE，缺失会使参考预检记 `cleanup_error`、评分无效，因此该项能力为强制 |

工具权限不要求 server 实现：recorder 按会话 `tool_profile` 过滤 `tools/list`，profile 外的 `tools/call` 判 `permission_denied`，对待测 agent 与 server 透明。server 的 `tools/list` 返回全量工具即可。

server 不提供状态读取能力时：`expected_state` 与 `forbidden_changes` 维度不可用，该 server 上的任务只能用 `tool_checks`、`process_constraints`、`reply_checks` 评分。需要终态评分的任务有两条路：推动 server 补充状态查询能力；或在 recorder 中注册该任务的状态投影规则（按调用序列推导座舱状态），规则随任务交付并接受人工评审。

### 2.1 请求与响应细节

`tools/call` 转发保持 MCP 信封原样：server 返回 `{"content": [...], "structuredContent": ..., "isError": bool}`，recorder 把同一信封返回给 agent，并把 `structuredContent` 记进事件。`isError` 为真时事件状态为 `error`。

状态读取的两种形态对应 recorder 配置：

```bash
COCKPIT_STATE_TOOL=get_vehicle_state   # 默认，经 MCP 工具读取
COCKPIT_STATE_URL=http://cockpit-mcp:8091/state/{session_id}   # 或经 HTTP 端点读取，二者二选一
```

recorder 配置项（环境变量注入，compose 里设置）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `COCKPIT_MCP_URL` | 无，必填 | 用户 MCP server 基地址 |
| `REDIS_URL` | `redis://redis:6379/0` | 会话记录与事件存储 |
| `COCKPIT_MCP_SESSION_MODE` | `path` | `path` 或 `param`，见会话隔离 |
| `COCKPIT_INJECT_MODE` | `http` | `http` 或 `mcp`，见初态注入 |
| `COCKPIT_STATE_TOOL` | `get_vehicle_state` | 状态查询工具名 |
| `COCKPIT_STATE_URL` | 空 | HTTP 状态端点，设置后优先于状态查询工具 |
| `COCKPIT_MCP_TIMEOUT` | `60` | 转发与状态读取超时秒数 |
| `COCKPIT_EVENT_LIMIT` | `10000` | 单会话事件上限，超出截断并显式标记 |

## 3. 座舱 agent 接入契约

主路径是 HTTP 服务。端点契约：

| 方法与路径 | 请求 | 响应 |
|---|---|---|
| `GET /v1/health` | 无 | 200，`{"status":"ok","model_id":"实际使用的模型标识"}` |
| `POST /v1/turn` | `{"session_id": "...", "message": "用户消息", "history": [{"role":"user"\|"assistant","content":"..."}]}` | `{"reply": "助手回复", "status": "completed", "usage": {"input_tokens": 0, "output_tokens": 0}}`，usage 可选 |

约束：

1. agent 从环境变量获得 MCP 地址：`ESA_MOCK_URL` 与 session_id 拼出 `/eval/mcp/{session_id}`。agent 每轮先 `tools/list` 再调用。
2. history 由驱动每轮完整传入，agent 可以无状态。多轮共用同一 session_id 与同一座舱会话。
3. agent 不得读取任务目录、rubric、solution 与 tests。容器内本就不放置这些文件，任务镜像只复制初态。
4. 模型选择。compose 注入 `COCKPIT_AGENT_MODEL_ID`，agent 启动时按该变量选择模型，并在 `/v1/health` 上报实际使用的 model_id。模型凭据由 agent 自行管理，经 compose 环境变量注入；密钥不落盘进证据。驱动在发起第一轮对话前比对上报值与 job 模型标识，不一致记执行错误并结束，不发生任何模型调用。
5. agent 镜像来源两种，任选其一：作为任务 compose 服务，由任务 Dockerfile 安装（示例任务采用）；或作为 harness 镜像预构建并只读挂载（参照 `harness/pi` 的模式）。
6. 状态约定：`status` 为 `completed`、`failed`、`timeout` 之一。`failed` 与 `timeout` 由驱动记为执行错误，评分无效。
7. 多轮交互由驱动按 `user_script` 匹配回复后继续，agent 只需回答当前轮。

退化路径：agent 无法提供 HTTP 服务时，可以命令行进程接入，驱动以子进程运行，约定 stdin 收 JSON 行（message 与 history），stdout 回 JSON 行（reply 与 status）。该路径只用于本地试用，批量评测以 HTTP 路径为准。

## 4. 任务定义要点

- `environment/esa/initial_state.json` 不放 agent 字段；`initial_state` 按用户 MCP server 的座舱状态结构编写，经会话创建注入；`dataset_version` 必填并与 server 上报的数据版本一致。
- 座舱 agent 地址只有一份来源：`task.toml` 的 `[environment.env] ESA_AGENT_API_URL`；`ESA_MOCK_URL` 指向 recorder（服务名 `mock-system`，端口 8003）。
- `environment/docker-compose.yaml` 由 mock-system（recorder）、cockpit-agent、cockpit-mcp、redis 组成；模型连接按现有 ESA 任务的写法读 provider 前缀变量（`STEPFUN_OPENAI_BASE_URL`、`STEPFUN_API_KEY`）。
- `[environment.healthcheck]` 是单条链式命令，三个服务全查，写为一条检查；座舱 agent 必须进入健康检查，避免驱动第 0 步遇到连接拒绝。
- `tests/rubric.json` 结构不变：`expected_state` 的点路径指向座舱状态；`forbidden_changes`、`tool_checks`、`tool_forbidden`、`process_constraints`、`reply_checks`、`user_script`、`faults` 全部沿用现有语义。

## 5. 用户资产接入步骤

1. 按第 2 节实现 MCP server，按第 3 节实现座舱 agent；用 `cockpit/fake_mcp.py` 与 `cockpit/fake_agent.py` 作为形状参照。
2. 构建镜像，以 `COCKPIT_AGENT_IMAGE` 与 `COCKPIT_MCP_IMAGE` 指向用户镜像覆盖默认假件，任务配置不需要改：

```bash
COCKPIT_AGENT_IMAGE=<用户 agent 镜像> COCKPIT_MCP_IMAGE=<用户 MCP 镜像> bash scripts/build-esa-local.sh
scripts/run.sh --bench esa-cockpit --harness esa-cockpit --model <provider/model-id> --dry-run
COCKPIT_AGENT_MODEL_ID=<provider/model-id> .venv/bin/python -m esa.cli chat --cockpit
```

3. 模型标识全链：`run.sh` 把 job 的模型标识导出为 `COCKPIT_AGENT_MODEL_ID` 注入 compose，agent 在 `/v1/health` 上报，驱动比对。直接 `harbor run -c` 的路径没有导出方，agent 自报模型与 job 模型双写证据供人工核对。
4. 正式运行：`scripts/run.sh --bench esa-cockpit --harness esa-cockpit --model <provider/model-id> --job-name UNIQUE`。

## 6. 证据与评分

座舱试验产出的证据文件与 ESA 任务完全一致：`tool-calls.json`、`reply.txt`、`dialogue.json`、`snapshot-initial.json`、`snapshot-final.json`、`state-events.json`、`environment-manifest.json`、`trajectory.json`。逐事件差异由 recorder 在每次转发调用前后各读一次状态计算，`forbidden_changes` 因此能发现修改后恢复。

manifest 只放静态字段：`environment_version`、`implementation_sha256`（recorder 实现哈希）、`source_sha256`（server `tools/list` 规范化序列化哈希）、`seed_sha256`（server 的 `data_sha256`）、`dataset_version`（server 的 `data_version`）、tools 名单，外加 `server_version` 与 `server_commit`。调用统计只出现在 recorder 的 `/eval/api/v1/manifest` 路由响应里，不进快照 manifest，避免参考验证误报环境身份不一致。

限制：模型用量指标依赖 agent 透传 usage，agent 不回传时 ATIF 轨迹省略用量字段；模型标识由 agent 自报，recorder 与驱动只比对不验证；并发批量路径按 trial 天然隔离，开发服务组合与 Console 多会话共用同一 MCP server 实例时依赖 server 的会话隔离能力。
