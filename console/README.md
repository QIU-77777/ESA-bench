# Console · 评测工作台

覆盖评测全链路的本地 Web 工作台：数据集、评分器、实验、结果分析、人工复核与闭环回流。

## 启动

```bash
nohup .venv/bin/python -m uvicorn console.app.main:app --host 127.0.0.1 --port 8899 &
```

浏览器打开 `http://127.0.0.1:8899`。Harbor view 由 Console 启动时自动拉起，轨迹页内嵌展示，无需单独开启。

## 页面

| 路径 | 内容 |
|---|---|
| `/experiments` | 实验列表、创建、运行、中止、复制、删除 |
| `/datasets` | 数据集管理与任务行编辑 |
| `/eval-profiles` | harness、模型与运行参数 |
| `/scorers` | LLM judge 与 Code 评分器，试评与版本 |
| `/review` | 人工复核队列与结构化打分 |
| `/settings` | 复核维度、盲审开关、Loop-Assistant 分析代理与模型 |
| `/experiments/{id}` | 实验详情、任务表、Harbor view 内嵌 |
| `/experiments/{id}/logs` · `/dashboard` · `/loop` | 任务与运行日志、分析看板、闭环回流 |

## 技术形态

FastAPI + 标准库 SQLite + Jinja2 服务端渲染 + 原生 JavaScript，无前端构建链，静态资源全部本地。实现约定见 [docs/requirements.md](docs/requirements.md)（产品需求）与 [docs/development.md](docs/development.md)（架构与实现）。
