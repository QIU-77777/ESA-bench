# Console 座舱对话台规范

状态：已批准设计，实施依据。版本：1.0。日期：2026-11-02。

## 目标与范围

把现在只能通过命令行（`esa/cli.py chat`）进行的座舱 Agent 交互搬进 Console，提供带会话管理、对话流与配置面板的网页对话台。交互对象是 esa 座舱 Agent（Go ReAct runtime + mock-system 工具服务），与批量评测相互独立：不评分、不产生 experiment。

## 核心机制

网页不直连 Agent。Console 后端充当 Agent 客户端，调用路径与命令行完全一致：

```text
浏览器 ──fetch/SSE──▶ Console 后端 ──HTTP/SSE──▶ runtime（18080 /v1/run）
                          │                            │
                          └──────HTTP────▶ mock-system（8003，车辆状态与工具）
```

三个已核实的事实构成设计基础：

1. runtime 无状态。每轮请求携带完整 `transcript`，返回更新后的 `transcript`。车辆状态与工具集保存在 mock-system 的 session 中。Console 在数据库存会话与对话历史，即是完整的 Agent 客户端。
2. runtime 以 SSE 流式输出，事件共六类：`reasoning_delta`、`text_delta`、`tool_call`、`tool_result`、`turn_end`、`run_end`。Console 逐事件转发给浏览器。
3. Agent 定义随请求传递。`/v1/run` 的 `agent` 字段携带 `react.instructions`（系统提示词）、`react.model`（model_id、temperature、top_p、reasoning_effort）、`react.model_params`。基线取 `esa/agent.json` 原样传递，其中的 `${...}` 环境变量占位符由 runtime 自行解析。

## 硬约束

runtime 校验每轮请求的 `toolProfile` 必须与 session 创建时的工具列表完全一致（`runtime/protocol.go`）。因此：

- 系统提示词与模型参数逐请求生效，面板修改下一轮即生效。
- 工具集在会话创建时固定。更换工具集等于创建新会话，车辆状态与对话历史一并重置。面板呈现该机制，不伪造「热切换」。

## 服务依赖

对话台依赖 esa dev stack：mock-system（127.0.0.1:8003）与 runtime（127.0.0.1:18080）。进入对话页时探测两个健康端点（`/health` 与 `/v1/health`）：

- 均就绪：正常使用。
- 未就绪：页面显示就绪状态与「启动服务」按钮。点击后由 Console 执行 `docker compose -p esa-dev -f docker-compose.dev.yaml up -d` 并轮询就绪（120 秒超时）。模型端点与密钥经 `.env` 注入 compose 环境，与命令行一致。

runtime 容器经 compose 网络访问 mock-system，请求体中的 `mockSystemUrl` 固定传 `http://mock-system:8003`（Docker 模式内部地址）。

## 后端设计

新增 `console/app/services/chat_service.py`。

### 会话

```text
list_sessions() / get_session(id)
create_session(name, agent_config) -> 建 mock-system 会话并落库
delete_session(id) / rename_session(id, name)
send_message(session_id, text) -> SSE 流
```

`create_session` 向 mock-system 提交 `{type: "car", context, initial_state, data_overlays, faults, seed, tool_profile, dataset_version}`，默认不带任务初始数据（与命令行不传 `--task` 一致）；`tool_profile` 取会话配置中的工具集，缺省为全部启用工具。创建后拉取一次 snapshot 作为初始状态。

### Agent 配置覆盖

基线读 `esa/agent.json`，覆盖层按会话存库：

```json
{
  "model_id": "step-5-preview",
  "temperature": 0.7,
  "top_p": 0.3,
  "reasoning_effort": "low",
  "instructions": "……",
  "max_iterations": 16,
  "turn_timeout": 240
}
```

发请求时深拷贝基线，用覆盖层改写 `react.model.*` 与 `react.instructions`；未覆盖的字段保持基线值。

### 发消息与流式转发

```text
POST /v1/run
  agent（基线 + 覆盖）, user_messages: [text], transcript（库中历史）,
  mockSystemUrl, sessionId, maxIterations, turnTimeoutSec
```

httpx 流式读取 runtime 的 SSE，逐事件写入浏览器的 SSE 响应。同时缓存本轮事件：`run_end` 携带完整 result（含更新后的 transcript、每轮迭代、token 用量）。消息落库规则：

- 用户消息：发送即落库（role=user）。
- agent 消息：`run_end` 后落库（role=assistant），content 为最终回复，events 为本轮思考、工具调用与结果，usage 为 token 与耗时。
- transcript 更新为 result 中的版本。

### 证据文件

每轮结束把会话证据写入 `~/.esa/sessions/<mock_session_id>/`，格式与命令行一致，共六个文件：`transcript.json`、`snapshot-initial.json`、`snapshot-final.json`、`state-events.json`、`turn-results.json`、`meta.json`。`snapshot-final.json` 与 `state-events.json` 每轮从 mock-system 重新拉取；`meta.json` 记录会话标识、模型、起止时间、轮数与两个服务地址。

## 数据模型

```sql
chat_sessions
  id, name, mock_session_id, agent_config(json), evidence_dir, created_at, updated_at

chat_messages
  id, session_id, role(user|assistant), content, events(json), usage(json), created_at
```

## 界面设计

新页面 `/chat`，侧边导航增加「Chat」入口并置首位。三栏布局，视觉规范见 `docs/design-spec.md`。

### 左栏 会话

- 「＋ 新会话」按钮置顶。
- 会话条目显示名称与首条消息摘要。右键条目弹出操作菜单（重命名、删除），菜单为指针处浮层，不改变列表项高度。
- 点击切换会话，中栏与右栏随之刷新。

### 中栏 对话流

- 用户消息靠右，agent 消息靠左。
- 每轮 agent 消息内嵌可折叠步骤：思考（逐字流式）、工具调用（名称、参数、结果、耗时）。按迭代分组，组间横线分隔，组标签显示「第 N 步」与迭代耗时。
- 步骤排版不用描边框：思考是灰色 reasoning 小字标签加单行省略正文；工具调用是工具名小字、浅灰参数 Chip、右侧灰色元信息（耗时、字符数），展开箭头用细线 Chevron。
- 入参与工具结果先逐层解析 JSON 再格式化输出，解出 `content[].text` 内层；结果限高 160px 内部滚动，超 8000 字符标注截断。
- 工具失败与取消态在摘要中用 `--destructive` 色标注；`turn_end` 携带错误时渲染独立错误块。
- 消息尾部展示 token 用量与总耗时。
- 底部输入框，Enter 发送、Shift+Enter 换行；发送期间输入框禁用。

### 右栏 会话配置

- 可编辑项：model_id、reasoning_effort、temperature、max_iterations、turn_timeout。修改保存后下一轮生效。留空或选择「默认」时使用 `esa/agent.json` 的取值。
- 字段单列布局。标签只放英文字段名，中文含义以 12px 灰色小字跟在标签右侧；model_id 与 temperature 的占位符直接显示 `esa/agent.json` 的默认值。
- 字段分三组：模型、运行限制、提示词。保存按钮为右对齐浅灰次级按钮。
- SYSTEM PROMPT 多行编辑，同样为会话覆盖值。
- 工具区展示当前会话启用的工具。标题与工具名相同时只显示工具名，不重复排列。
- 「更换工具配置」按钮点击后确认「将创建新会话，车辆状态与对话历史重置」，确认后带当前配置（含新工具集）建新会话并切换。
- 工具清单来自 mock-system 快照的 manifest 与 `environment/catalog.json` 的描述合并。

### 就绪状态

服务未就绪时，中栏区域显示就绪说明与「启动服务」按钮，不使用对话功能。

## 验收标准

1. 就绪状态下新建会话，发送消息，中栏实时出现思考逐字、工具调用与结果、最终回复与用量。
2. 刷新页面后会话列表、对话历史（含折叠步骤与用量）完整还原。
3. 右栏修改 temperature 或系统提示词，下一轮回复体现新配置；恢复基线值后行为回到基线。
4. 点击「更换工具配置」选择工具子集并确认，产生新会话，工具清单为所选子集，对话历史为空。
5. 每轮结束后 `~/.esa/sessions/<mock_session_id>/` 下六个证据文件内容更新，transcript 与页面历史一致。
6. 服务未就绪时页面显示就绪说明，点击「启动服务」后等待就绪可正常对话。

## 边界

1. 不做评分、不产生 experiment；对话台与评测管线隔离。
2. 不做多人与并发隔离，单机单人使用。
3. 不与命令行的会话证据互通：Console 自建 mock-system 会话，证据目录同址但互不读写对方会话。
4. 工具集热切换不做，以新建会话替代。
