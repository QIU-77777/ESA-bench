# Harbor + 多 agent harness Benchmark

Harbor 0.24.0（`~/qiguang/harbor_test/.venv`），用同一个任务分别驱动 OpenCode CLI、
pi CLI 和 Claude Code CLI，在同一批任务上对比不同 agent harness 的得分与轨迹。
三个 CLI 都采用「预构建 harness 镜像 +
只读挂载」：镜像构建一次，每个 trial 挂载进来，不重复打包。
运行参数收敛在 job 配置里，模型、端点、API 密钥收敛在 `.env`（由 job 配置顶部的 `model: provider/model-id` 行选择），入口是 `scripts/run.sh`，起跑前先做预检。

## 目录

```
.env                              # 连接配置唯一源头：模型 ID + 端点 + API 密钥（.gitignore 排除，权限 600）
.env.example                      # 不含密钥的连接配置示例
configs/
├── opencode-harness.yaml         # job 配置：OpenCode harness（与另两份配置同任务，对比用）
├── pi-harness.yaml               # job 配置：pi harness
└── claude-code-harness.yaml      # job 配置：Claude Code harness
harness/
├── opencode/
│   ├── manifest.yaml            # 版本单一来源：版本号、镜像标签、挂载路径
│   └── Dockerfile               # harness 镜像：@opencode/cli@2.0.28
├── pi/
│   ├── manifest.yaml            # 同上，pi 版本单一来源
│   └── Dockerfile               # harness 镜像：@earendil-works/pi-coding-agent@1.2.0
└── claude-code/
    ├── manifest.yaml            # 同上，Claude Code 版本单一来源
    └── Dockerfile               # harness 镜像：@anthropic-ai/claude-code@2.1.299
harbor_test_agents.py            # MountedOpenCode / MountedPi / MountedClaudeCode：挂载式 agent（不执行 npm 安装）
harbor_test_env.py               # HarborTestDockerEnvironment：镜像挂载的 API 版本协商
scripts/
├── run.sh                       # 运行入口：预检 + harbor run，--dry-run 只预检
├── manifest-env.sh              # 读取 harness/<name>/manifest.yaml，供其他脚本引用
├── build-harness.sh             # 构建 harness 镜像（参数：harness 名）
└── cleanup.sh                   # 清理中断运行残留的容器和 hb__ 任务镜像
tasks/hello-opencode/
├── task.toml                    # 任务元信息、超时、网络策略
├── instruction.md               # 给 agent 看到的任务说明
├── environment/Dockerfile       # 任务环境（node:22-alpine + python3 + pytest）
├── solution/solve.sh            # 参考答案，用于验证任务本身是否成立
└── tests/
    ├── test.sh                  # 验证入口，写 /logs/verifier/reward.txt
    └── test_outputs.py          # 5 条 pytest 断言
```

## 运行

```bash
cd ~/qiguang/harbor_test
# 复制 .env.example 为 .env，并在 .env 的 *_API_KEY 行填入密钥

./scripts/run.sh --dry-run    # 只预检并打印运行计划，不消耗 docker 资源
./scripts/run.sh              # 预检通过后运行 configs/opencode-harness.yaml

JOB_CONFIG=configs/pi-harness.yaml ./scripts/run.sh --dry-run
JOB_CONFIG=configs/pi-harness.yaml ./scripts/run.sh

JOB_CONFIG=configs/claude-code-harness.yaml ./scripts/run.sh --dry-run
JOB_CONFIG=configs/claude-code-harness.yaml ./scripts/run.sh
```

模型、端点、API 密钥都在 `.env` 里改：`run.sh` 载入 `.env`，按 job 配置的
`model: provider/model-id` 行选择供应商组，并校验模型 ID 与 `.env` 的值一致。
`run.sh` 再生成已解析的临时 job 配置，供 Harbor 保存准确的运行快照；源 YAML 保留
模板结构。job 配置顶部的 `model:` 和 `harness:` 两行由 `run.sh` 读取，harbor 校验时忽略
这两行；换密钥只改 `.env` 里对应组的 `_API_KEY` 行。

先跑参考答案确认任务定义没问题（不消耗模型调用）：

```bash
PYTHONPATH=$PWD .venv/bin/harbor run -c configs/opencode-harness.yaml -a oracle -y
```

多任务、多并发：在 job 配置里改 `tasks:` 列表和 `n_concurrent_trials`。

## 预检（scripts/run.sh 在起容器之前全部检查）

1. job 配置的组合声明齐全且互相一致：`model: provider/model-id` 在 `.env` 里有对应的
   前缀变量组（例如 `model: go/mimo-v2.6-flash` 对应 `GO_*`，本 harness 协议必需的变量
   非空，模型 ID 与 `.env` 一致），`harness:` 与
   `agents[0].import_path` 匹配（opencode/MountedOpenCode、pi/MountedPi、
   claude-code/MountedClaudeCode）；
2. 挂载镜像标签与挂载路径必须和 `harness:` 指向的 `manifest.yaml` 一致，否则报错并指出
   两边要同步修改；
3. harness 镜像存在，且镜像 LABEL `<harness>.version` 等于 manifest 版本，否则提示重新构建
   （`AUTO_BUILD_HARNESS=1` 时自动构建）；
4. Docker daemon 的 API 版本不低于 1.48（镜像挂载的最低要求）；
5. `.env` 已载入、所选组已映射、临时配置中的真实 `model_name` 已写入、密钥非空，
   且用它请求网关 models 端点返回 200，密钥错误在起容器前就失败
   （网关地址按 harness 协议取：OpenCode/pi 用 `OPENAI_BASE_URL`，Claude Code 用
   `ANTHROPIC_BASE_URL`；base URL 不带 `/v1` 时先试 `/v1/models`）；
6. `--dry-run` 额外用 harbor 的 `--print-config` 验证配置能被解析、agent 导入路径可用。

## 版本升级流程

版本号只写在对应 harness 的 `manifest.yaml`：

```bash
# 1. 修改 harness/<name>/manifest.yaml 的 version
# 2. 构建新镜像
./scripts/build-harness.sh <name>     # opencode | pi | claude-code
# 3. 同步 job 配置里 mounts 的 source 标签（预检会强制这一点）
# 4. ./scripts/run.sh --dry-run 确认后运行
```

`harbor_test_agents.py` 的版本校验读同一个 manifest；镜像用 LABEL 记录版本，预检读 LABEL 比对。
版本号在仓库里出现两处（manifest 与 job 配置的镜像标签），预检保证它们不会不一致。

## 自定义类的职责

`run.sh` 会把 job 配置的 `model: provider/model-id` 写入临时配置的 `agents[0].model_name`。
三个类的 `install()` 继续执行各自的版本检查和运行准备，变量缺失直接报错。

**MountedOpenCode**（`harbor_test_agents.py`），Harbor 内置 opencode 适配器的子类：

1. `install()`：内置适配器每次 trial 执行 `npm i -g opencode-ai@latest`，实测 87 秒，且 `@latest` 会让同一批 trial 出现版本漂移。子类改为执行一次 `command -v opencode && opencode --version`，校验挂载进来的确实是 manifest 声明的版本。
2. `run()`：内置适配器拼出 `opencode --model=X run ... --dangerously-skip-permissions`，这是 v1 的写法；v2 CLI 不接受全局 `--model`，也不认识该参数，直接打印 usage 后退出。子类改为 `opencode run --model=X --format=json --thinking --auto -- '<instruction>'`。

**MountedPi**（`harbor_test_agents.py`），Harbor 内置 pi 适配器的子类，只改 `install()`：
内置适配器每次 trial 装 nvm + `npm i -g`，子类改为执行 `command -v pi && pi --version`，
校验挂载进来的版本。pi 的 `run()` 和轨迹逻辑保持原版——它的轨迹来自 `--session-dir`
的会话 JSONL 文件（进程本地写入），stdout 只做兜底统计。

**MountedClaudeCode**（`harbor_test_agents.py`），Harbor 内置 Claude Code 适配器的子类，
只改 `install()`：内置适配器每次 trial 执行 `npm install -g @anthropic-ai/claude-code`
或 bootstrap 脚本，子类改为执行 `command -v claude && claude --version`，校验挂载进来的
版本。`run()` 保持原版（stream-json 输出、`ANTHROPIC_*` 环境变量、权限模式默认
`bypassPermissions`）。

**HarborTestDockerEnvironment**（`harbor_test_env.py`），Docker 环境的子类：

`type: image` 挂载需要 Docker Engine API ≥ 1.48，部分 compose 版本不显式指定 `DOCKER_API_VERSION` 会协商出过低版本，失败表现为 trial 中途的 compose 报错。子类在挂载包含 image 类型时，读取 daemon 自报的 API 版本写入该变量；版本过旧时给出明确错误。

## 模型接入与可调参数

Harbor 按 agent 解析模型连接：端点、模型 ID、API 密钥的值都在 `.env`
（一个供应商一组变量，`run.sh` 按 job 的 `model: provider/model-id` 行选组），job 配置
写入临时配置的 `model_name`；端点以环境变量的
形式进入 harbor 的模型连接解析和 trial 容器。密钥用 `--agent-env` 注入
（`.env` 里的同一个值同时写入 `OPENAI_API_KEY` 和 `ANTHROPIC_API_KEY`），
各适配器再转换成自己 CLI 的原生配置：

| | 端点 | 密钥 | 模型选择 | 推理参数 |
| --- | --- | --- | --- | --- |
| OpenCode | `opencode_config` 的 `options.baseURL`（`{env:OPENAI_BASE_URL}`，容器内取值） | `opencode_config` 里 `{env:OPENAI_API_KEY}` | `model_name`（`provider/model-id`） | `--thinking`（`run()` 写死） |
| pi | 环境变量 `OPENAI_BASE_URL`（写入 `models.json` 的 `baseUrl`） | `models.json` 引用 `$OPENAI_API_KEY` | `model_name`（`provider/model-id`，运行时 provider 换成 harbor-endpoint） | `kwargs.thinking`（off…max）、`kwargs.model_api`（API 风格）、`kwargs.max_turns` |
| Claude Code | 环境变量 `ANTHROPIC_BASE_URL`（不带 `/v1`） | 环境变量 `ANTHROPIC_API_KEY` | `model_name`（临时配置中为 `provider/model-id`，发送给 Claude Code 时使用 model-id） | `kwargs.reasoning_effort`（`--effort`）、`kwargs.max_turns`、`kwargs.max_budget_usd`、`MAX_THINKING_TOKENS` 等 |

网关的模型按协议绑定：`mimo-v2.6-flash` 只在 OpenAI 协议端点上，
`claude-haiku-5-5` 只在 Anthropic 协议端点上；跨协议调用返回
`ModelProtocolUnsupported`。所以 `.env` 的 go 组里 OpenCode/pi 读
`GO_OPENAI_MODEL_ID=mimo-v2.6-flash`，Claude Code 读
`GO_ANTHROPIC_MODEL_ID=claude-haiku-5-5`。换模型前先确认目标 harness
说的协议与网关端点一致。

## 改模型 / 换服务商

- 同供应商换模型：修改 job 配置的 `model: provider/model-id`，并同步修改 `.env` 里该组
  对应协议的 `*_MODEL_ID` 行。`run.sh` 会检查两处模型 ID 是否一致。
- 换密钥：只改 `.env` 里该组的 `*_API_KEY` 一行，其余不动。
- 新增供应商（多供应商共存，不覆盖已有组）：
  1. 在 `.env` 末尾追加一组 `<前缀>_*` 五行（前缀用大写字母、数字、下划线，
     参考文件里的 deepseek 示例组）；
  2. 把目标 job 配置顶部的 `model:` 行改成完整的 `provider/model-id`，例如
     `model: deepseek/deepseek-flash`；
  3. 使用 `./scripts/run.sh --dry-run` 验证模型 ID、端点、密钥和 Harbor 配置。
- 切回旧供应商：把 `model:` 行改回对应的 `provider/model-id`，`.env` 里的供应商组一直保留。
- jobs 列表页读取运行快照中的 `agents[].name`、`agents[].model_name`；`run.sh` 会把完整的
  `provider/model-id` 写入快照。

## 同任务对比三个 harness

```bash
JOB_CONFIG=configs/opencode-harness.yaml ./scripts/run.sh
JOB_CONFIG=configs/pi-harness.yaml       ./scripts/run.sh
JOB_CONFIG=configs/claude-code-harness.yaml   ./scripts/run.sh
```

三个配置指向同一个 `tasks/hello-opencode`。OpenCode 和 pi 使用
`model: go/mimo-v2.6-flash`，Claude Code 使用 `model: go/claude-haiku-5-5`；
三个模型值都在 `.env` 的 go 组，协议限制见上节。
每个 `jobs/<时间戳>/` 目录里可对比：

| 对比项 | 位置 |
| --- | --- |
| 得分 | `verifier/reward.txt` |
| 轨迹完整性（最终回答是否入轨迹、每步工具调用） | `agent/trajectory.json` |
| 原始事件流 / 会话文件 | `agent/opencode.txt` 或 `agent/pi/sessions/*.jsonl` |
| token 统计与版本信息 | `result.json` |

## 分阶段耗时（同一任务实测，OpenCode）

| 阶段 | 旧方式（每 trial npm 安装） | 挂载方式 |
| --- | --- | --- |
| environment_setup | 2.7s | 3.0s / 5.5s |
| agent_setup | 87.1s | 0.6s / 1.2s |
| agent_execution | 65.5s | 36.1s / 126.7s |
| verifier | 0.5s | 0.6s / 0.9s |

`agent_execution` 的波动来自模型网关延迟，与环境无关。

结果目录 `jobs/<时间戳>/<trial>/`（每次运行一个时间戳目录）：

| 文件 | 内容 |
| --- | --- |
| `agent/opencode.txt` | OpenCode `run --format=json` 的原始事件流 |
| `agent/pi.txt` / `agent/pi/sessions/` | pi 的 stdout 输出 / 会话 JSONL（轨迹来源） |
| `agent/claude-code.txt` / `agent/sessions/` | Claude Code 的 stream-json 输出 / 会话文件 |
| `agent/trajectory.json` | 归一化后的轨迹：每步的工具调用、参数、观察结果 |
| `verifier/test-stdout.txt` | 验证器完整输出 |
| `verifier/reward.txt` | 0 或 1 |
| `result.json` | 该次 trial 的汇总，`agent_info.version` 记录实际 CLI 版本 |

## Harness 镜像

```bash
./scripts/build-harness.sh opencode    # 产物 harbor-test/harness/opencode:2.0.28，约 814MB
./scripts/build-harness.sh pi          # 产物 harbor-test/harness/pi:1.2.0，约 389MB
./scripts/build-harness.sh claude-code # 产物 harbor-test/harness/claude-code:2.1.299
```

镜像最终层是 `FROM scratch` 加 `COPY --from=build /usr/local/ /`，因此镜像根目录的内容就是
`/usr/local` 的内容。trial 容器把它只读挂载到 `/usr/local`，得到 node、npm 和 CLI 可执行文件
（`bin/opencode`、`bin/pi` 是相对符号链接，挂载后仍可解析；`pi` 的 shebang 是
`#!/usr/bin/env node`，node 同样来自挂载）。每个镜像只存一份，所有 trial 共用。

opencode 版本必须写在 `@opencode/cli`：`opencode-ai` 这个旧包只发布到 1.x，2.0.28 属于 v2 CLI。
pi 的包在 0.74.0 时从 `@mariozechner/pi-coding-agent` 改名为 `@earendil-works/pi-coding-agent`。

## 残留清理

正常运行会删除自己的 trial 容器。中断的运行会留下 `hb__<hash>` 容器和任务镜像：

```bash
./scripts/cleanup.sh    # 删除 hb__ 前缀的容器和任务镜像，保留 harness 镜像和基础层
```

## 这台机器上的两个环境问题

1. Docker Hub 直连被重置，基础镜像改从 `docker.m.daocloud.io` 拉取后打本地标签：
   `docker tag docker.m.daocloud.io/library/node:22-alpine node:22-alpine`
2. 容器里的 OpenCode 拿不到本机 `auth.json`，因此用 `opencode_config` 定义一个
   openai-compatible provider：baseURL 用 `{env:OPENAI_BASE_URL}`、密钥用
   `{env:OPENAI_API_KEY}`，都由 `--agent-env` 注入（值来自 `.env` 的 go 组）；
   pi 走同样的网关，端点读环境变量
   `OPENAI_BASE_URL`，密钥是 `--agent-env` 注入的 `OPENAI_API_KEY`。

## 任务镜像设计要点

任务镜像用 `node:22-alpine`（musl）：Harbor 的 OpenCode 适配器在 glibc 镜像上会先装 nvm
和 Node 22，实测超过 360 秒的安装超时。改用挂载模式后这一步已经不再执行，基础镜像保留
Alpine 是为了与 harness 镜像的 node 布局一致，两者挂载后路径不冲突。
