# Console 设计规范

Console 的视觉规范对齐 Harbor view。token 与组件规则从 Harbor view 源码与运行实例实测提取，不自行发明。Harbor view 的样式表位于 `.venv/lib/python3.12/site-packages/harbor/viewer/static/assets/root-*.css`，运行实例默认在 `127.0.0.1:8080`。

## 核心原则

浅灰填充与浅灰描边从不同时出现在同一个组件上。区分层级只用三种手段之一：

1. 浅灰填充（无描边）
2. 浅灰描边（无填充）
3. 字体颜色、字号、字重

## 色彩 token

`console/app/static/console.css` 的 `:root` 与 Harbor view 完全一致，包含亮色与暗色两套。常用项：

| token | 亮色值 | 用途 |
|---|---|---|
| `--background` | `oklch(100% 0 0)` | 页面底色 |
| `--foreground` | `oklch(14.1% .005 285.823)` | 正文与强调 |
| `--muted-foreground` | `oklch(55.2% .016 285.938)` | 辅助文字 |
| `--muted` | `oklch(96.7% .001 286.375)` | 浅灰填充、hover 底色 |
| `--border` / `--input` | `oklch(92% .004 286.32)` | 描边、表格行线 |
| `--primary` | `oklch(21% .006 285.885)` | 主按钮底色、选中强调 |
| `--destructive` | `oklch(57.7% .245 27.325)` | 失败、错误 |
| `--success` | `oklch(59.5% .16 146)` | 通过 |
| `--radius` | `0rem` | 全部组件直角 |

`:root` 之外不新增颜色值。需要新颜色时先确认 Harbor view 是否已有对应 token。

## 字号

三档，不新增中间值：

| 字号 | 用途 |
|---|---|
| `0.75rem` | 辅助文字、标签、元信息、轨迹细节 |
| `0.875rem` | 正文、输入框、按钮、表格 |
| `1.125rem` | 页面标题、弹窗标题 |

## 组件规范

### 输入框、下拉、文本域

1px `--input` 描边，透明底，14px（`0.875rem`），内边距 `0.25rem 0.75rem`，焦点态边框变 `--ring` 加 3px 光环。textarea 内边距 `0.5rem 0.75rem`。

### 按钮

| 变体 | 类 | 样式 |
|---|---|---|
| 主按钮 | 默认 `button` | `--primary` 底、`--primary-foreground` 字、无描边 |
| 次按钮 | `.soft` | `--muted` 底、无描边 |
| 描边按钮 | `.outline` | 1px `--border` 描边、透明底 |
| 危险按钮 | `.destructive` | `--destructive` 底、无描边 |

高度 `2.25rem`，内边距 `0 0.75rem`，字重 500。hover 只改底色，不加阴影。

### 分段切换

容器 1px `--border` 描边、内边距 2px；选中项 `--muted` 填充、无描边；未选中项透明底、`--muted-foreground` 字。

### 表格

无外框、无竖线。行间 1px `--border` 横线，末行无横线。表头无底色、字重 500。单元格内边距 `0.5rem 1rem`，行高 `2.75rem`。hover 行 `color-mix(in oklch, var(--muted) 50%, transparent)`。

### 页内 tab 栏

容器只留底部 1px 横线，无底色、无上边线。tab 透明底，未选中 `--muted-foreground` 字，选中 `--foreground` 字加底部 2px `--foreground` 线，均无填充。

### 内容块

`pre.snapshot` 用 `--muted` 填充、无描边，承载 JSON、代码、日志、任务输入与预期结果。限高 600px，内部滚动。对话轨迹内的工具块用 `pre.snapshot.chat-tool-block`，限高 160px，字号 `0.75rem`。

### 徽章

`.badge` 用 1px 描边、透明底，状态只改描边与文字颜色（`.ok` 绿、`.warn` 黄、`.fail` 红、`.dim` 灰）。

### 行内代码

`code.chip` 用 `--muted` 填充、无描边，字号 `0.75rem`。

### 浮层

弹出菜单（`.session-menu`、`.menu-pop`、`.c-select-pop`、`.modal`）用白底加 1px `--border` 描边，可带轻阴影。菜单项透明底，hover `--accent`。

### 侧边栏导航

链接透明底，hover 与选中态用 `--sidebar-accent` 填充，无描边。选中项字重 500。

### 分组标题

`.section-title` 用 `0.75rem`、`--muted-foreground`、大写、字距加宽，下方内容用留白分隔，不加分隔线。

## 对话轨迹

- 迭代分组：每轮迭代一个 `.chat-iter`，组间 1px 横线，组标签「第 N 步 · 耗时」用 12px 灰色。
- 思考与工具调用不用描边框，不用展开箭头。思考是灰色 `reasoning` 小字标签，摘要不显示，点击展开看全文；工具调用是工具名小字、浅灰参数 Chip、右侧灰色元信息（耗时、字符数）。
- 可展开通过 hover 暗示：步骤行 hover 时工具名与元信息从 `--muted-foreground` 变为 `--foreground`，指针为 pointer。失败行 hover 保持 `--destructive` 色。
- 展开箭头用细线 Chevron，不用浏览器默认三角。
- 失败态只把工具名与元信息改为 `--destructive` 色。

## 禁止事项

- 浅灰填充与浅灰描边同时使用。
- 新增字号档位。
- 新增 `:root` 之外的颜色值。
- 给按钮、输入框加投影（浮层除外）。
- 用圆角（`--radius` 为 0）。

## 规范同步方式

Harbor view 升级后，重新实测其 token 与组件样式，更新本文件与 `console.css`。实测方法：浏览器打开 Harbor view 实例，对目标元素执行 `getComputedStyle`，读取 `backgroundColor`、`border`、`fontSize`、`padding` 后对照本文件。
