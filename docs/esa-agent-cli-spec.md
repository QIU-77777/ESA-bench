# ESA 交互式命令行规范

状态：已批准设计，实施依据。版本：1.0。日期：2026-10-21。

## 目标与范围

提供交互式命令行入口，供人工直接与车载 Agent 对话，用于体验和问题发现，与批量评测相互独立。批量评测的驱动、评分和任务流程不改变。

交互式命令行与批量评测共用同一 Agent 实现：命令行不执行 ReAct 循环，不直接调用模型，全部经过 runtime。

## 组件与协议

- `esa/cli.py`：Python 命令行，入口为 `python -m esa.cli chat`。仅使用 Python 标准库。
- `runtime/`：Go ReAct runtime。本规范增加对话历史传递，是唯一服务端改动。
- `docker-compose.dev.yaml`：开发服务组合，发布环境服务与 runtime 端口，Redis 仅在内部网络。
- 证据目录：默认 `~/.esa/sessions/<会话标识>/`。

现有服务接口不变：环境服务继续提供会话创建、快照、事件、重置；runtime 继续提供 `/v1/run`。

## runtime 接口变更

### 请求

`RunRequest` 增加 `transcript` 字段，类型为对话项数组，缺省为空。对话项共四种，与 runtime 内部重放格式一致：

1. 用户消息：`{"role":"user","content":"<字符串>"}`。
2. 助手消息：`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"<字符串>"}]}`，`content` 元素也可以是 `{"type":"refusal","refusal":"<字符串>"}`。
3. 工具调用：`{"type":"function_call","call_id":"<非空字符串>","name":"<非空字符串>","arguments":"<字符串>"}`。
4. 工具结果：`{"type":"function_call_output","call_id":"<非空字符串>","output":"<字符串>"}`。

`transcript` 非空时作为对话历史起点，`user_messages` 或 `instruction` 提供的新消息追加在其后。批量评测不发送该字段，行为与当前完全一致。

### 响应

`RunResult` 增加 `transcript` 字段，内容为本次运行结束时的完整对话项，包含请求带入的历史和本次新增的全部项。凭据脱敏对该字段同时生效。

### 行为规则

- 历史中出现的工具调用 `call_id` 加入去重集合。模型在新一轮重复使用历史中的 `call_id` 时判定失败，原因与现有重复判定一致。
- 上下文预算、16MB 请求上限、每轮超时、迭代上限沿用现有机制。历史计入上下文预算，超限时明确失败，不静默截断历史。
- 每轮开始仍先调用环境服务 `/round` 并拉取工具清单，与批量评测一致。

### 校验

对话项结构非法时返回 HTTP 400，错误信息指明具体原因。非法情况包括：对话项不是对象；用户消息缺少字符串 `content`；助手消息角色错误或内容数组为空；内容元素类型不受支持；工具调用缺少 `call_id`、`name` 或 `arguments` 不是字符串；工具结果缺少 `call_id` 或 `output` 不是字符串；对话项类型不受支持。

## 开发服务组合

`docker-compose.dev.yaml` 位于仓库根目录，项目名 `esa-dev`：

- `mock-system`：镜像 `${MOCK_SYSTEM_IMAGE:-esa-local-environment:1.0.0}`，端口绑定 `127.0.0.1:8003`，依赖 Redis 健康。
- `agent-api`：镜像 `${AGENT_API_IMAGE:-esa-react-runtime:1.0.0}`，端口绑定 `127.0.0.1:8080`，依赖环境服务健康。环境变量 `RESPONSES_API_BASE_URL` 与 `RESPONSES_API_KEY` 由宿主环境传入。
- `redis`：镜像 `redis:7-alpine`，不发布端口。

### 服务启动模式

`esa chat` 启动时检测环境服务 `/health` 与 runtime `/v1/health`：

- 两个服务均可达：直接连接。
- 不可达且未指定 `--no-stack`：执行 `docker compose -p esa-dev -f docker-compose.dev.yaml up -d`，然后轮询健康状态，默认等待 120 秒，超时明确失败并输出服务日志路径提示。
- `--local`：改用宿主进程。依次检查并启动宿主 Redis（`redis-server`，端口 16379）、环境服务（`environment/.venv`，端口 18003）、runtime（`go run .`，端口 18080）。日志写入证据目录的 `logs/`。退出时终止这三个进程。缺少对应可执行文件时明确失败。
- `--no-stack`：不启动任何服务，不可达时直接报错并给出启动命令提示。
- `--ephemeral`：退出时执行 `docker compose -p esa-dev down`。默认退出时保留服务运行。

`--local` 与 Docker 模式的默认地址不同：Docker 模式命令行自身访问 `http://127.0.0.1:8003`，传递给 runtime 的环境服务地址为 `http://mock-system:8003`；`--local` 模式两者均为 `http://127.0.0.1:18003`。两个地址都可用参数覆盖。

模型端点使用本机地址时，命令行提示改用 `host.docker.internal`，否则 runtime 容器内无法访问。

## 命令行行为

### 启动流程

1. 解析参数，加载 `.env`（`--env` 可指定其他文件）。
2. 加载 Agent 配置 `esa/agent.json`，`--model` 可覆盖其中的模型标识。
3. 按上述规则确保服务就绪。
4. 创建车辆会话，请求体与批量驱动一致：`type`、`context`、`initial_state`、`data_overlays`、`tool_profile`、`faults`、`seed`、`dataset_version`。
5. 指定 `--task` 时，从任务目录或 `initial_state.json` 读取上述字段，并把任务的首条用户消息作为第一轮自动发出。
6. 打印会话标识、模型、启用工具数量、证据目录，进入交互循环。

### 每轮交互

1. 读取用户输入。空输入跳过；以 `/` 开头按斜杠命令处理。
2. 向 `/v1/run` 发送请求：`agent`、`model`（有覆盖时）、`user_messages` 为单条新消息、`transcript` 为已保存的历史、`mockSystemUrl`、`sessionId`、`maxIterations`、`turnTimeoutSec`。
3. 等待期间显示提示。
4. 渲染本次返回的迭代：每次工具调用显示工具名、参数、结果摘要、耗时与状态；`--verbose` 时追加显示模型思考内容。随后显示助手回复。状态不是 `completed` 时显示对应标签与错误信息。
5. 用响应中的 `transcript` 更新已保存历史。

### 斜杠命令

| 命令 | 作用 |
|---|---|
| `/help` | 显示命令列表 |
| `/state` | 显示当前车辆状态快照 |
| `/events` | 显示工具调用记录与状态变化，最多最近 50 条 |
| `/tools` | 显示当前会话启用的工具 |
| `/reset` | 调用环境服务重置接口，清空对话历史，用同一任务配置继续 |
| `/resume` | 列出已保存的交互会话，选择后续聊 |
| `/quit`、`/exit` | 写出证据并退出 |

未知斜杠命令显示提示，不中断会话。

### 中断与退出

- 等待模型期间按 `Ctrl+C`：取消当前轮，打印取消提示，会话保留，可继续输入。已执行的工具调用可能已改变业务状态，但结果未计入对话历史，命令行提示这一情况。
- 输入状态按 `Ctrl+C`：清空当前行，返回新提示符。
- `Ctrl+D` 或 `/quit`：写出证据后退出。
- 会话不存在或已过期时，相关命令明确报错。

### 续聊

`/resume` 与启动参数 `--resume <会话标识>` 从证据目录读取 `transcript.json` 续聊。续聊前检查环境会话是否存活，过期时明确告知。runtime 无服务端状态，续聊只依赖命令行保存的历史。

## 任务加载

`--task` 接受任务目录（读取其中的 `environment/esa/initial_state.json`）或直接指向该文件。字段映射与批量驱动相同。首条用户消息取自任务的 `instruction`，自动作为第一轮发出；多轮任务后续消息由人工输入，不自动发出。`/reset` 使用同一任务配置重建会话状态。

## 证据

证据目录默认为 `~/.esa/sessions/<会话标识>/`，`--session-dir` 可改为其他位置。首次创建会话时建立目录，退出与每次 `/reset` 时写出：

- `transcript.json`：完整对话历史。
- `snapshot-initial.json`：会话初态快照。
- `snapshot-final.json`：写出时刻的状态快照。
- `state-events.json`：工具调用与状态变化事件。
- `meta.json`：会话标识、模型、任务、起止时间、轮数、服务地址。

`--local` 模式的进程日志写在 `logs/` 子目录。文件名与批量评测证据一致，检查方式相同。

## 安全边界

- 模型密钥只存在于 runtime 进程环境与本地 `.env`。命令行不读取密钥值用于展示，不写入任何证据文件。runtime 对响应内容做凭据脱敏。
- 证据文件包含工具输出，属于本地文件，不自动上传。
- Docker 模式端口绑定 `127.0.0.1`，不对外暴露。
- 命令行不执行宿主命令，不访问任务工作目录之外的文件。

## 验收

1. runtime 单元测试覆盖：历史前置、响应回传、结构校验拒绝、历史 `call_id` 去重；现有测试全部保持通过。
2. `docker compose -f docker-compose.dev.yaml config` 校验通过。
3. 命令行单元测试覆盖：`.env` 解析、任务加载、请求构造、响应处理、斜杠命令、证据写出。
4. 端到端冒烟：假模型接口加环境服务与 runtime，以脚本化标准输入驱动命令行，断言回复内容、业务状态变化、证据文件齐全。
5. `--local` 模式冒烟通过。
6. README 增加交互式使用章节，命令可直接复制执行。

## 不在第一版范围

语音输入、轮次中实时工具渲染、评分与模型评审、故障注入、多会话并行。

## 实施顺序

规范 → runtime 改动与测试 → 开发服务组合 → 命令行实现 → 单元测试与集成冒烟 → 文档。

## 验证状态

2026-10-21：规范与实现完成。

- runtime：`transcript` 请求与响应字段、结构校验、历史 `call_id` 去重均有单元测试；`go vet` 与 race 测试通过，覆盖率 84.0%。现有测试全部保持通过，批量评测路径不变。
- `docker compose -f docker-compose.dev.yaml config` 校验通过。
- 命令行单元测试 14 项通过，覆盖 `.env` 解析、任务加载、请求构造、响应处理、斜杠命令、证据写出、续聊。
- 端到端冒烟通过：假模型接口加环境服务与 runtime，脚本化标准输入驱动 `esa chat`，断言回复内容、业务状态变化和证据文件齐全。
- Docker 服务组合模式冒烟通过：命令自动拉起服务、完成对话、`--ephemeral` 退出时销毁。
- `--local` 宿主进程模式冒烟通过：自动启动宿主 Redis、环境服务和 runtime，完成对话，退出时按进程组清理。
- README 增加交互式使用章节。

验证中发现并修复的问题：本地 runtime 进程需要接收 `RESPONSES_API_BASE_URL` 与 `RESPONSES_API_KEY`，而非 `.env` 中的 `STEPFUN_*` 名称；`go run` 派生的子进程需要按进程组终止，否则退出后占用端口；会话目录判定使用 `meta.json`，排除开发数据库与日志子目录。

启动服务前校验模型端点：`.env` 与 shell 环境均未提供 `STEPFUN_OPENAI_BASE_URL` 时直接报错并说明配置方法，不在首轮对话时才失败。启动横幅显示解析到的端点，便于确认。Docker 模式修改端点后重新执行命令会重建 runtime 容器，已实测确认。
