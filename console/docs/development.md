# Console 开发文档

- 版本：v1.0（开发前规格）
- 日期：2026-10-27
- 关联文档：`console/docs/requirements.md`（需求文档）

## 1. 架构总览

```text
┌─────────────────────────────────────────────────────┐
│ console UI（Jinja2 模板 + Tailwind + Alpine.js）      │
│ 数据集 / 评测对象 / Scorer / 实验 / 分析 / Review / Loop │
└──────────────────────┬──────────────────────────────┘
                       │ HTTP（同源）
┌──────────────────────▼──────────────────────────────┐
│ console API（FastAPI）                                │
│ services: dataset / scorer / experiment / judge /     │
│           review / loop / dashboard                   │
└──────┬───────────────────────┬───────────────────────┘
       │ 调用                   │ 读写
┌──────▼──────────────┐  ┌─────▼──────┐  ┌────────────┐
│ scripts/run.sh      │  │ SQLite     │  │ workspace/ │
│ scripts/render-job  │  │ console.db │  │ 生成物归档  │
└──────┬──────────────┘  └────────────┘  └────────────┘
       │ harbor run
┌──────▼──────────────────────────────────────────────┐
│ Harbor 编排 → trial 执行 Agent → jobs/<job-name>/     │
└──────────────────────────────────────────────────────┘
       ▲
       │ iframe 内嵌（反向代理同源）
┌──────┴──────────┐
│ Harbor view     │  轨迹详情
└─────────────────┘
```

职责边界：

- console 负责编排输入（数据集、Scorer、Agent 参数）与结果分析。
- Harbor 负责 trial 执行与轨迹记录。
- Harbor view 负责轨迹渲染，console 通过同源反向代理内嵌。
- Harbor 编排代码与 Harbor view 读取逻辑零改动。

## 2. 目录结构

```text
console/
├── docs/
│   ├── requirements.md
│   └── development.md
├── app/
│   ├── main.py                 # FastAPI 入口、路由注册、Harbor view 反向代理
│   ├── api/                    # REST 接口，按领域分文件
│   │   ├── datasets.py
│   │   ├── scorers.py
│   │   ├── experiments.py
│   │   ├── reviews.py
│   │   ├── dashboards.py
│   │   ├── loop.py
│   │   └── settings.py
│   ├── services/               # 领域逻辑，不依赖 HTTP
│   │   ├── dataset_service.py
│   │   ├── scorer_service.py
│   │   ├── experiment_service.py
│   │   ├── judge_service.py    # 评分执行服务：verifier 与试评共用
│   │   ├── review_service.py
│   │   ├── loop_service.py
│   │   └── harbor_adapter.py   # 调 run.sh / 解析 jobs/
│   ├── scorers/                # 评分函数层（纯逻辑，verifier 与试评同源）
│   │   ├── base.py
│   │   ├── llm_judge.py
│   │   └── code_runner.py
│   ├── render/
│   │   ├── csv_loader.py       # CSV 解析与校验
│   │   └── task_renderer.py    # 数据集版本 → Harbor tasks 目录
│   ├── store/
│   │   ├── models.py           # SQLAlchemy 模型
│   │   └── migrations/         # schema 版本迁移
│   ├── templates/              # Jinja2 页面模板
│   └── static/
│       ├── console.css         # Harbor view 对齐的样式变量与组件
│       └── console.js          # Alpine.js 交互
├── prompts/                    # judge 与 Loop-Assistant 提示词模板
├── workspace/                  # 运行产物（gitignore）
│   ├── console.db
│   ├── generated/              # 渲染出的 bench 配置与 tasks 目录
│   ├── snapshots/              # 实验快照归档
│   └── reports/                # Loop-Assistant 报告
└── tests/
```

## 3. 技术选型

| 层 | 选型 | 理由 |
| --- | --- | --- |
| 后端 | FastAPI + Uvicorn | 与 Harbor 的依赖栈一致（Harbor 已依赖 FastAPI），无新增依赖冲突。 |
| 存储 | SQLite（SQLAlchemy） | 单人单机，零运维。单文件备份。评测产物不进库，只存 `jobs/` 路径引用。 |
| 页面 | Jinja2 服务端渲染 | 薄工作台不需要前端构建链。Harbor 升级、样式复制都不涉及前端工具链。 |
| 交互 | Alpine.js（本地静态资源） | 表格筛选、表单联动、进度轮询所需的轻量交互。 |
| 图表 | ECharts（本地静态资源） | Dashboards 的直方图与分布图。主题色取平台样式变量。 |
| 样式 | Tailwind（本地静态资源）+ 自定义变量文件 | 直接复制 Harbor view 的 CSS 变量达到视觉一致。前端依赖全部下载到 `console/app/static/vendor/`，不依赖 CDN，离线可用。 |

## 4. 数据模型

主键统一为自增整数，外部引用使用字符串 id。时间统一 UTC ISO 8601。

### 数据集

```text
datasets
  id, name(唯一), description, env_template, created_at

dataset_versions
  id, dataset_id, version_no, change_note, content_hash, row_count, created_at
  约束：(dataset_id, version_no) 唯一；行内容只读
  说明：内部快照机制，界面不提供版本管理。每次保存写入新快照，
  实验引用快照 id 保证复现；env_template 随快照记录。

dataset_rows
  id, dataset_version_id, row_id, input, expected, tags(json), metadata(json), rubric
  约束：(dataset_version_id, row_id) 唯一
```

### 评分器

```text
scorers
  id, name, type(llm|code), created_at

scorer_versions
  id, scorer_id, version_no, config(json), change_note, created_at
  llm config: {model_id, prompt_template, output_schema, score_min, score_max}
  code config: {script_source, entry_function, timeout_sec}

scorer_tryouts
  id, scorer_draft(json), trace_ref, output(json), duration_ms, created_at
  试评记录，不关联正式版本
```

### 评测对象

```text
eval_profiles
  id, name, harness, model_id, params(json), version_no, created_at
  params 示例：{thinking: "medium", max_turns: 32}
  修改产生新版本，旧版本随实验快照保留
```

### 实验

```text
experiments
  id, name, status(draft|running|completed|failed|aborted),
  dataset_version_id, scorer_version_id, eval_profile_id,
  snapshot(json), baseline_flag(bool), error(text),
  created_at, started_at, finished_at

experiment_tasks
  id, experiment_id, row_id, harbor_trial_name, harbor_trial_path,
  status(pending|done|trial_failed|judge_failed),
  duration_ms, n_input_tokens, n_output_tokens, created_at

scores
  id, experiment_task_id, scorer_version_id,
  value(real), verdict(pass|fail|suspect), failure_category, reason(json), created_at
  不可变；从 trial 的 /logs/verifier/score.json 解析入库
```

### 人工复核

```text
settings
  key, value(json)
  review_dimensions：复核维度名列表，默认四维（正确性、完整性、安全性、格式符合度）
  blind_review：盲审开关，固定 false
  loop_agent / loop_model：Loop-Assistant 的分析代理命令与模型

reviews
  id, experiment_task_id(唯一), dims(json), overall_verdict(pass|suspect|fail),
  note(text), created_at, updated_at
  dims 为空表示待复核；一个任务一条复核记录

review_history
  id, review_id, dims(json), overall_verdict, note, created_at
  每次提交追加一条，保留变更历史
```

### 基线与回流

```text
baselines
  dataset_id(唯一), experiment_id, set_at
  同一数据集只保留一个当前基线，不绑定数据集快照；跨快照对比按 row_id 匹配共同任务

loop_runs
  id, experiment_id, status(running|done|failed), agent, model, work_dir,
  report(json), error, created_at, finished_at

loop_drafts
  id, loop_run_id, name, rows(json), status(draft|merged),
  target_dataset_id, created_at, confirmed_at

failure_categories
  id, name(唯一), created_at
  失败类别登记表，随 Loop-Assistant 报告累积
```

## 5. 与现有资产的集成

### 5.1 CSV 到 Harbor 任务的渲染

数据集版本渲染为 Harbor tasks 目录：

```text
workspace/generated/datasets/<dataset>/<version>/tasks/<row_id>/
├── task.toml               # 任务元信息、超时、网络策略
├── instruction.md          # 渲染 input 字段
├── environment/            # 按数据集的环境模板复制
└── tests/                  # 恒过 stub verifier
```

渲染规则：

- `instruction.md` 内容为行的 `input`。
- `task.toml` 的任务名使用 `row_id`，来源信息写入注释（数据集名、版本、行号）。
- 环境模板由数据集属性决定：普通任务复用 `tasks/hello-opencode/environment` 形态，esa 任务复用 `tasks/esa-sc-a1/environment` 形态（含 mock-system、agent-api、redis 服务）。
- 渲染结果按 `content_hash` 缓存，同版本重复实验直接复用。

### 5.2 评分在 Harbor verifier 内执行

评分逻辑统一放在 Harbor verifier，形态与 workbuddy-bench 的 `CompositeVerifier` 一致：自定义 `BaseVerifier` 子类，在 trial 生命周期内完成评分。

两层结构保证评分逻辑单一来源：

```text
console/app/scorers/             评分函数层（纯逻辑，不依赖 Harbor）
├── base.py                      score(input, expected, trace, metadata) -> ScoreResult
├── llm_judge.py                 LLM judge 实现
└── code_runner.py               Code 评分器执行器

harbor_test_verifier.py          Harbor 适配壳 ConsoleVerifier(BaseVerifier)
                                 收集 trial 产物 → 调评分函数 → 写 reward
```

执行机制：

- 正式运行：Harbor trial 结束时自动执行 `ConsoleVerifier`，与任务自带 `tests/` 同属 verifier 生命周期。
- 试评与离线重评：console 直接调评分函数对已有 Trace 执行，逻辑与 verifier 内完全同源。
- Scorer 配置传递：实验启动时把选定的 Scorer 版本渲染为配置文件（`workspace/generated/scorers/<experiment_id>.json`），verifier 的 kwargs 指向该文件；LLM judge 的密钥与端点经 `--verifier-env` 注入。
- reward 约定：`reward` 记总分（数值）。`verdict`、失败类别、理由等细分写入 `/logs/verifier/score.json`，console 结果表与 Dashboards 从该文件读取。
- 硬门禁：任务自带 `tests/` 可叠加 gate（触发时 reward 直接记 0），与 Scorer 分数并行生效。

限制：需要读取容器内产物文件的 Code 评分器只能在 verifier 内执行，换口径对照历史实验时需重跑 trial。只依赖 Trace 与输出文本的评分器可被试评和离线重评覆盖。

### 5.3 调用现有脚本

`scripts/render-job.py` 增加一个参数：

```text
--bench-file <path>    直接读取指定 bench 配置，跳过 configs/bench/<slug> 查找
```

`scripts/run.sh` 增加透传参数 `--bench-file`。其余预检、`.env` 映射、`--job-name`、临时配置渲染逻辑不变。

console 运行实验的调用序列：

```text
1. 生成 bench 配置到 workspace/generated/benches/<experiment_id>.yaml
   （tasks 指向渲染目录，n_concurrent_trials 取实验配置）
2. 渲染 Scorer 版本配置到 workspace/generated/scorers/<experiment_id>.json
3. 渲染任务的 task.toml 写入 [verifier] import_path = harbor_test_verifier:ConsoleVerifier
4. 执行：
   scripts/run.sh --bench-file <生成的bench> \
     --harness <harness> --model <provider/model-id> \
     --job-name exp-<实验编号>
   （追加 --verifier-kwarg scorer_config=<配置路径>
        --verifier-env 注入 LLM judge 的密钥与端点）
5. 轮询 jobs/exp-<实验编号>/ 读取进度（rollout 与评分进度合并，verifier 随 trial 完成）
6. 结束后解析 trial result 与 /logs/verifier/score.json 入库
```

`--job-name` 使用 `exp-` 前缀加实验编号，不含冒号与斜线（Docker bind mount 限制，run.sh 已校验）。

### 5.4 Harbor view 内嵌

FastAPI 增加反向代理路由：

```text
/harbor-view/<path>  →  转发到本机 Harbor view 服务
```

Trace 详情页以 iframe 内嵌 `harbor-view/jobs/<job-name>/<trial>` 的 trial 页面。同源转发避免 iframe 跨源限制。

Harbor view 服务由 console 启动时检查并拉起（`harbor_view_service.ensure_running`）：探测 127.0.0.1:8080-8089 中已有实例，未运行时以独立会话启动 viewer 并把代理目标改为实际端口。启动命令带 litellm 预热导入——Harbor view 的 pricing 端点惰性导入 litellm，并发请求下该导入会抛 KeyError 且不被 Harbor 捕获，导致服务卡死；预热后请求直达模块缓存。代理转发失败时若实例由 console 拉起过，会终止旧实例并重启（60 秒冷却），随后重试一次。

### 5.5 密钥与模型

- 密钥继续只在 `.env`，console 读取 `.env` 注入 judge 与 Loop-Assistant 的模型调用，不落库、不回显（只显示末四位）。
- judge 模型从 `configs/models/` 选择，连接机制与 `run.sh` 的模型组映射一致（复用 `render-job.py` 的变量映射逻辑）。

## 6. 模块实现要点

### 6.1 数据集（FR-1）

- 上传解析：Python `csv` 模块逐行读取，校验在内存完成，全量通过后一次性事务写入。
- 保存即写内部快照：版本号自增，`content_hash` 为全部行规范化后的 SHA-256，供实验复现引用。
- 编辑状态判定：前端以初始快照为基线实时比对内容，有改动才显示保存与另存。
- 另存：当前编辑内容写入新数据集，含首个快照。
- 导出：按原字段顺序输出 CSV，`tags` 逗号连接，`metadata` 输出 JSON 字符串。

### 6.2 评测对象（FR-2）

- harness 列表读取 `configs/harnesses/*.yaml`，模型列表读取 `configs/models/**/*.yaml`。
- 参数编辑生成新 `eval_profiles` 版本行，不覆盖历史。

### 6.3 Scorer（FR-3）

LLM judge 输出契约（prompt 中固定要求 JSON）：

```json
{
  "score": 0.0,
  "verdict": "pass | fail | suspect",
  "failure_category": "string | null",
  "reason": "string",
  "evidence": ["string"]
}
```

Code 评分器函数签名约定：

```python
def score(input: str, expected: str, trace: dict, metadata: dict) -> dict:
    # 返回 {"score": float, "verdict": str, "failure_category": str|None,
    #       "reason": str, "context": dict}
```

执行受控：子进程运行、超时上限 60 秒、临时目录隔离、禁止网络（执行前设置无代理环境变量并校验脚本无 socket 导入）。

Harbor 适配壳 `ConsoleVerifier`（`harbor_test_verifier.py`，仓库根模块，与 `harbor_test_agents.py` 同级）：

- `verify()` 从 trial 环境收集 Agent 输出与轨迹，读取 kwargs 指定的 Scorer 配置文件，调用评分函数，写 `reward` 与 `/logs/verifier/score.json`。
- LLM judge 失败重试最多两次，仍失败时该 trial 的 score.json 记 `judge_failed`，reward 记 0，不阻塞批次。
- 任务自带 `tests/` 与本 verifier 可叠加：gate 触发时 reward 优先记 0。

试评流程：选择实验的 Trace（读取 `jobs/` 下 trajectory 与输出）→ 调用同一评分函数 → 展示对照（输入、Agent 输出、Scorer 输出、耗时）→ 保存为版本。试评与正式运行的评分逻辑同源，区别只在结果标注。

### 6.4 实验运行（FR-4）

状态机与执行器：

```text
draft ──run──> running（rollout + 评分）──> completed
                   │
                   ├──────> failed（整体性错误）
                   └──────> aborted（用户中止）
```

- 执行器为后台线程池任务，run.sh 以子进程执行，stdout/stderr 落到 `workspace/reports/run-<experiment_id>.log`。
- 进度轮询：API 读取 `jobs/<job-name>/` 下 trial 目录数与 result.json 完成数。
- 快照生成时机：状态离开 draft 的瞬间。快照 JSON 写入 `workspace/snapshots/<experiment_id>.json`，同时存库。内容见需求 FR-4 第 6 条。
- 中止：给 run.sh 子进程发终止信号，调用 `scripts/cleanup.sh` 清理残留容器。

### 6.5 结果分析（FR-5）

- 结果表数据源：`experiment_tasks` + `scores` 联查。
- Dashboards 聚合在 SQL 层完成（分组均值、通过率、失败类别计数）。
- 回归排序：以 `row_id` 全外连接当前实验与基线实验的分数，`delta = 当前 - 基线` 升序排列，新增失败标记 `new_fail`，修复项标记 `fixed`。
- 内嵌轨迹：Trace 详情页左侧面板展示平台侧数据，右侧 iframe 展示 Harbor view trial 页。

### 6.6 Review（FR-6）

- 标记存疑：Trace 详情按钮写入队列（`reviews` 行，维度分为空表示待复核）。
- 复核表单维度从 `settings` 的 `review_dimensions` 动态生成，每项 1 到 5 分，总体判定三档。
- 每次提交写入 `review_history` 一条记录（维度分、判定、备注、时间），主记录随之更新；待复核与已复核由维度分是否为空区分。
- 汇总：Review 队列页按实验汇总人工判定；实验结果表的 Review 列与 Dashboards 的「人工判定」分组读取同一数据。
- 盲审：`settings` 中 `blind_review` 固定 false。界面保留开关（禁用态），启用逻辑留待多人版本。

### 6.7 Loop-Assistant（FR-7）

Loop-Assistant 由独立配置的 coding agent 在本机执行（agent 命令与模型在设置中单独指定，默认 opencode + 强分析模型），与评测对象配置分离。agent 以非交互模式运行：平台组装分析任务简报，agent 把结果写入约定文件，平台解析入库。

执行流程：

```text
1. 平台收集失败任务
   判定非 pass 的任务，附任务输入、预期结果、Agent 输出（截断）、
   Scorer 判定与理由，连同失败类别清单写成任务简报

2. 启动 Loop-Assistant 的 coding agent 执行
   agent 配置：设置中的 agent 命令与模型（默认 opencode run --model <model>）
   运行目录：workspace/generated/loop/run-<id>（子进程同步 cwd 与 PWD）
   产出约定文件：report.json
     overall          整体情况概括
     categories       失败归类 [{name, count, confidence, summary, typical_cases, actions}]
     draft_rows       新数据集草案行（字段与 FR-1 一致）

3. 平台解析产出
   报告存 loop_runs.report 并在页面渲染
   草案行写 loop_drafts，进入待确认列表
   新类别登记进 failure_categories，随后续简报回传
```

- 任务简报模板存 `prompts/loop_task.md`，失败明细由服务追加在模板之后。
- 产出解析失败时按更严格指令重试一次，仍失败则记录错误可重新发起分析；stdout 文本作为兜底解析来源。
- 草案编辑：页面可改行、剔除行，保存后合并；合并时字段 `id` 映射为内部 `row_id`，写入目标数据集生成新快照，`change_note` 自动引用来源实验编号。

## 7. API 设计

页面路由服务端渲染，数据接口返回 JSON。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 实验列表页 |
| GET | `/datasets` | 数据集列表页 |
| GET | `/datasets/{id}` | 数据集详情（即编辑页，基本信息与任务行） |
| POST | `/datasets` | 创建数据集（含 CSV 上传） |
| POST | `/datasets/{id}/versions/rows` | 保存（元数据与任务行，写内部快照） |
| POST | `/datasets/{id}/save-as` | 另存为新数据集 |
| POST | `/datasets/{id}/delete` | 删除数据集 |
| GET | `/datasets/{id}/versions/{v}/export` | 导出 CSV |
| GET | `/review` | Review 队列页（按实验、状态筛选） |
| POST | `/api/reviews/flag` | 标记存疑进队列 |
| POST | `/api/reviews/submit` | 提交结构化复核（写变更历史） |
| GET | `/settings` / POST | 复核维度、盲审开关、Loop-Assistant 代理与模型 |
| GET | `/scorers` | Scorer 列表页 |
| POST | `/api/scorers` | 创建 Scorer |
| POST | `/api/scorers/{id}/tryout` | 试评（真实日志） |
| POST | `/api/scorers/{id}/versions` | 保存版本 |
| GET | `/eval-profiles` | 评测对象配置页 |
| POST | `/api/eval-profiles/{id}/versions` | 参数新版本 |
| POST | `/api/experiments` | 创建实验（草稿） |
| POST | `/api/experiments/{id}/run` | 预检并启动 |
| POST | `/api/experiments/{id}/abort` | 中止 |
| GET | `/api/experiments/{id}/progress` | 进度轮询 |
| GET | `/experiments/{id}` | 实验详情页（快照、任务、分数） |
| GET | `/experiments/{id}/logs` | Logs 页（Trace 列表） |
| GET | `/experiments/{id}/traces/{row_id}` | Trace 详情页（内嵌 Harbor view） |
| GET | `/experiments/{id}/dashboard` | Dashboards 页 |
| GET | `/api/experiments/{id}/regression?baseline=` | 回归对比数据 |
| POST | `/api/experiments/{id}/baseline` | 标记基线 |
| GET | `/experiments/{id}/loop` | Loop-Assistant 页（运行、报告、草案） |
| POST | `/experiments/{id}/loop` | 发起分析 |
| GET | `/api/loop/runs/{run_id}` | 运行状态轮询 |
| POST | `/api/loop/drafts/{draft_id}/rows` | 保存草案行 |
| POST | `/api/loop/drafts/{draft_id}/merge` | 草案合并进数据集 |
| GET | `/api/settings` / PUT | 设置（盲审预留、judge 并发） |
| GET | `/harbor-view/<path>` | Harbor view 反向代理 |

## 8. 页面与视觉设计规范

目标：内嵌 Harbor view 的轨迹页与平台页面并排时无风格割裂。全部样式值从 Harbor view 的 `root-*.css` 提取，写入 `console/static/console.css`。

### 8.1 设计变量（与 Harbor view 一致）

```css
:root {
  --background: oklch(100% 0 0);
  --foreground: oklch(14.1% .005 285.823);
  --card: oklch(100% 0 0);
  --card-foreground: oklch(14.1% .005 285.823);
  --muted: oklch(96.7% .001 286.375);
  --muted-foreground: oklch(55.2% .016 285.938);
  --border: oklch(92% .004 286.32);
  --primary: oklch(21% .006 285.885);
  --primary-foreground: oklch(98.5% 0 0);
  --secondary: oklch(96.7% .001 286.375);
  --destructive: oklch(57.7% .245 27.325);
  --ring: oklch(55.2% .016 285.938);
  --radius: 0rem;
}
.dark { /* 深色值同 Harbor view 的 .dark 块 */ }
```

### 8.2 排版

- 字体：`"Google Sans Code"` 为主字体与等宽字体（与 Harbor view 相同回退链）。数据列、ID、模型名、分数使用 `font-mono`。
- 字号阶梯：`text-xs`（0.75rem，辅助信息）、`text-sm`（0.875rem，正文与表格）、`text-base`（标题）。
- 数字使用 `tabular-nums`，右对齐。

### 8.3 组件

| 组件 | 规范 |
| --- | --- |
| 卡片 | `border bg-card`，无阴影，直角 |
| 表格 | 表头 `sticky top-* bg-card`，行 `hover:bg-muted`，单元格 `px-4 py-3`，外框 `border` |
| 按钮 | `rounded-none shadow-none border`，变体：primary（黑底白字）、outline、destructive |
| 输入框 | `border bg-background px-3 py-2 text-sm`，聚焦 `ring-2 ring-ring` |
| 标签徽章 | `border px-2 py-0.5 text-xs font-mono` |
| 空状态 | 居中图标 + `text-muted-foreground text-sm` |
| 进度 | 横向条 `bg-muted` 底 + `bg-foreground` 填充，或数字进度（同 Harbor 的 trial 计数风格） |

### 8.4 页面布局

- 顶部：面包屑导航（左侧）+ 页面标题（大号 `text-lg`）+ 右侧操作按钮，下方 1px 边框分隔。与 Harbor view 的 job 页头一致。
- 内容区：单列布局，最大宽度自适应，内边距 `px-4 py-6`。
- 工具条：表格上方粘性工具条（筛选、搜索、列设置），形态对齐 Harbor view 的 results 工具条。
- 状态展示：分数热力用 `color-mix(in oklch, var(--foreground) N%, var(--card))`（N 为分数百分比），与 Harbor view 的 waffle 色阶方法一致。失败用 `--destructive`。

### 8.5 内嵌 Harbor view

- iframe 无边框，宽度占满内容区，高度按视口自适应（最小 600px）。
- 内嵌页与平台页共享顶部导航时，iframe 页面内部的 Harbor view 自带面包屑，平台侧不再重复展示 trial 名。
- 深浅主题跟随平台设置，通过反向代理注入主题参数（Harbor view 遵循系统主题，平台主题切换时同步）。

## 9. 不可变快照实现

快照 JSON 结构：

```json
{
  "experiment_id": "exp-20261027-001",
  "created_at": "2026-10-27T10:00:00Z",
  "dataset": {
    "name": "code-tasks",
    "version_no": 3,
    "content_hash": "sha256:...",
    "row_count": 5
  },
  "scorer": {
    "name": "default-judge",
    "type": "llm",
    "version_no": 2,
    "config": {}
  },
  "eval_profile": {
    "harness": "pi",
    "harness_image": "harbor-test/harness/pi:1.2.0",
    "model_id": "deepseek/deepseek-flash",
    "params": {}
  },
  "rendered_job_config": "...Harbor YAML 全文...",
  "run_command": "scripts/run.sh --bench-file ... --verifier-kwarg scorer_config=...",
  "environment": {
    "git_commit": "ae955a9",
    "harbor_version": "0.24.0"
  }
}
```

快照写入后实验行不再提供任何修改接口。数据集、Scorer、评测对象后续版本变化不影响历史实验展示（历史页从快照读取配置展示）。

## 10. 开发里程碑

| 里程碑 | 范围 | 验收 |
| --- | --- | --- |
| M1 打通链路 | 数据集 CRUD 与保存、评测对象、实验创建、rollout 运行、快照 | 上传 CSV 跑完一次实验，jobs/ 产出 trial，快照落盘 |
| M2 评分 | `ConsoleVerifier` 适配壳、LLM judge、Code 评分器、试评、结果表 | 试评通过后保存 Scorer；trial 内评分产出 reward 与 score.json；结果表出分 |
| M3 分析 | Logs、Trace 详情内嵌、Dashboards、基线对比、导出 | 端到端验收场景第 4、6 步通过 |
| M4 复核 | Review 队列、结构化打分、变更历史、汇总列 | 端到端验收场景第 5 步通过 |
| M5 回流 | Loop-Assistant（独立 coding agent + 强模型）归类、方案报告、草案合并 | 端到端验收场景第 7 步通过 |

每个里程碑发布前执行端到端验收场景中对应步骤，M5 完成后执行全链路。

## 11. 测试策略

- 单元测试：CSV 解析校验、快照哈希、Scorer 输出 schema 校验、回归排序算法、Loop 草案合并。
- 接口测试：FastAPI TestClient 覆盖全部 API 的正常与失败路径。
- 集成测试：用 3 行小数据集 + 便宜模型跑一次完整实验（rollout + judge），断言状态机流转与快照内容。
- 冒烟脚本：`console/tests/smoke.sh` 一键执行集成测试，作为每次发布前检查。

## 12. 架构决策记录

| 编号 | 决策 | 理由 |
| --- | --- | --- |
| ADR-001 | 评分逻辑在 Harbor verifier 内执行 | 复用 Harbor 的 verifier 生命周期、reward 记录与结果展示（workbuddy-bench 的 CompositeVerifier 同款形态）。评分函数独立成层，试评与正式评分同源。 |
| ADR-002 | SQLite 存元数据，评测产物留 `jobs/` | 单人单机零运维；轨迹文件大且 Harbor 已管理，只存引用。 |
| ADR-003 | 服务端渲染 + Alpine.js，不引前端构建链 | 薄工作台，视觉对齐靠 CSS 变量复制即可达成，降低维护面。 |
| ADR-004 | 反向代理同源内嵌 Harbor view | 避免 iframe 跨源限制；Harbor view 轨迹能力保持零改动复用。 |
| ADR-005 | CSV 版本渲染为 tasks 目录，按内容哈希缓存 | 复用 Harbor 原生任务机制；同版本实验共享渲染结果。 |
| ADR-006 | run.sh 增加 `--bench-file` 透传 | 保持 run.sh 为唯一运行入口（预检、密钥映射、job-name 校验全部复用）。 |
| ADR-007 | 密钥只存 `.env`，平台只显示末四位 | 与现有安全约定一致，平台不成为新的密钥落点。 |

## 13. 风险与注意事项

1. Harbor 升级风险：console 只依赖 `run.sh`、`jobs/` 目录结构与 Harbor view URL 形态。升级 Harbor 后先跑冒烟脚本核对这三点。
2. CSV 大文件：万行以上 CSV 上传解析占用内存，第一版限制单文件 5MB、行数 5 万，超出拒绝并提示拆分。
3. 评分输出不稳定：LLM judge 在 verifier 内做输出 schema 校验与重试（最多两次），仍失败记 `judge_failed`、reward 记 0，不阻塞批次。
4. iframe 高度自适应受浏览器同源策略约束，反向代理保证同源后可用 `postMessage` 修正高度；若 Harbor view 页面不发消息，退化为固定高度滚动。
5. 并发控制：run.sh 的并发数与 judge 并发数分开配置，避免模型 API 限流。默认 judge 并发 4。
6. 时区统一：全部时间戳存 UTC，界面展示本地时区，导出保持 UTC。
