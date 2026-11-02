# 评测对象能力透明化规范

状态：已批准设计，实施依据。版本：1.0。日期：2026-10-29。

## 目标与范围

评测对象配置页当前只暴露 harness 与模型的名称下拉，克隆仓库的用户无法知道所选 harness 配了什么、环境里有什么。本规范让评测对象页透出 harness 与交互环境的完整能力信息，并把交互环境的选择从数据集层移到评测对象层。

范围包含：能力登记服务、`eval_profiles` 增加环境字段、渲染链路的环境来源变更、配置页界面。不包含：在界面上编辑 harness 或环境的定义文件。

## 设计原则

1. 界面不写死任何能力信息。提示词、工具清单、参数默认值、数据规模全部来自仓库现有文件，用户改造文件后刷新即生效。
2. 读不到的内容如实说明。镜像内 harness 的提示词与工具列表在仓库侧不存在，卡片标注来源，不虚构。
3. 环境是评测配置的一部分，归属评测对象。数据集只描述任务数据。

## 数据源

### Harness 侧

| 文件 | 读取字段 |
|---|---|
| `configs/harnesses/<id>.yaml` | id、name、protocol、import_path、manifest、kwargs、可选的 param_docs |
| `harness/<id>/manifest.yaml` | version、npm_package、image_repository、mount_path |
| `esa/agent.json` | display_name、description、capabilities、react.instructions、react.model、react.context_continuity |

`param_docs` 为 `configs/harnesses/<id>.yaml` 中新增的可选字段，结构为参数名到说明文字的映射。缺失时参数表只显示键值。

esa 之外的三类 harness（pi、opencode、claude-code）的 CLI 与提示词在镜像内，仓库侧无定义文件。它们的卡片展示 manifest 与 kwargs，并附说明「提示词与工具由镜像内 CLI 决定，改造需重新构建镜像」。

### 环境侧

| 文件 | 读取字段 |
|---|---|
| `environment/catalog.json` | 每项工具的 name、title、description |
| `configs/tools.json` | 每项工具的 enabled 开关 |
| `environment/manifest.json` | environment_version、seed_sha256、工具可用性 |
| `environment/seed-v1.json` | 各类实体计数（pois、tracks、documents、routes、weather、contacts、visions、services） |

standard 环境无上述文件，卡片内容为内置描述：`node:22-alpine` 基础容器，预装 bash、curl、python3、pytest，无 MCP 工具，Agent 通过 shell 直接操作文件系统。

环境清单本身保持显式登记（standard、esa 两项），新增环境需在登记处增加条目；条目之下的全部内容来自文件读取。

## 服务层

新增 `console/app/services/capability_registry.py`，仅读文件，不写库。

```text
harness_detail(harness_id) -> dict | None
  id, name, protocol, import_path
  image: {repository, version, mount_path, npm_package}
  params: [{key, value, doc}]
  agent: {display_name, description, capabilities, instructions, model_defaults} | None
  notes: [str]

environment_detail(env_id) -> dict | None
  id, name, description
  tools: {total, enabled, items: [{name, title, description, enabled}]}
  data: {entities: [{type, label, count}], seed_sha256}
  version

available_environments() -> list[str]
```

文件缺失或解析失败时返回 None，界面提示「无法读取该 <harness|环境> 的定义」，不阻塞表单其他部分。

## 数据模型变更

```sql
ALTER TABLE eval_profiles ADD COLUMN environment TEXT NOT NULL DEFAULT 'standard';
```

迁移加入 `db.py` 的 `_MIGRATIONS`。历史行的 environment 为 standard，运行时回退到数据集的 env_template，行为不变。

实验快照的 `eval_profile` 段增加 `environment` 字段。

## 运行链路变更

`experiment_service._execute` 渲染任务时，环境模板来源改为：

```text
profile.environment → 缺失时回退 dataset.env_template
```

`task_renderer.render_tasks` 的入参含义不变，仍接受环境模板标识。数据集创建表单保留环境模板字段，作为该数据集的默认提示；数据集详情页在该字段旁标注「运行时环境由评测对象决定」。

## 界面设计

### 表单结构

评测对象表单字段顺序：名称、Harness、模型、交互环境、运行参数。切换 Harness 时运行参数按该 harness 的 kwargs 默认值填充（现有行为不变）。切换 Harness 或环境时，下方两张能力卡同步刷新。

### Harness 能力卡

分区展示：基础信息（协议、镜像仓库、版本、挂载点、CLI 包）、系统提示词（esa 展示 agent.json 全文，折叠区块，默认收起）、模型默认参数（esa 的 temperature、top_p、reasoning_effort）、可调参数表（kwargs 键值 + param_docs 说明）、备注（镜像内 harness 的来源说明）。

### 环境能力卡

分区展示：环境描述、工具统计（总数与启用数）、工具清单（名称、标题、描述、启用状态，61 项用可折叠列表）、数据规模（按实体分类计数）、环境版本与 seed 哈希。standard 环境展示内置容器描述，工具区显示「无 MCP 工具」。

### 刷新机制

两张卡片由 HTML 片段端点渲染，避免在 JavaScript 里重复渲染逻辑：

```text
GET /eval-profiles/harness-card/<harness_id>    -> HTML 片段
GET /eval-profiles/environment-card/<env_id>    -> HTML 片段
```

页面加载时两张卡片按当前选中项渲染；下拉变更时 fetch 对应片段替换容器内容。片段端点只读文件，不写库。

## 验收标准

1. 评测对象页选择 esa 时，Harness 卡展示系统提示词全文、模型默认参数与可调参数表；选择 pi 时展示 manifest 信息与镜像内来源说明。
2. 评测对象页选择 esa 环境时，环境卡展示 61 项工具清单（含启用状态）、seed 数据规模分类计数与环境版本；选择 standard 时展示空容器描述。
3. 修改 `configs/tools.json` 中某工具的启用状态后刷新页面，环境卡的启用数与工具状态随之变化。
4. 新建评测对象选择 esa 环境并保存后，实验快照的 eval_profile 段含 environment 字段；运行实验时 trial 使用座舱环境。
5. 改造 `configs/harnesses/pi.yaml` 的 kwargs 或新增 param_docs 后，Harness 卡的参数表随之变化。
6. 历史评测对象（无 environment 字段值）运行实验时行为与改造前一致。

## 边界

1. 不在界面上编辑 harness 或环境定义文件；改造走文件编辑。
2. 环境清单保持显式登记，新增环境类型需要在登记处增加条目。
3. 工具的参数 schema 不在界面平铺，清单展示名称、标题、描述、启用状态四项。
4. 镜像内 harness 的提示词与工具列表不做探测，如实标注来源。
