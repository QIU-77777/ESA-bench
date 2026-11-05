座舱评测组件：MCP 记录代理、默认假件与镜像构建入口。

| 文件 | 职责 |
|---|---|
| `recorder.py` | MCP 记录代理。对外实现与 `environment/server.py` 一致的路由，对内把 `tools/call` 与会话创建转发给用户 MCP server；按会话 tool_profile 执行工具权限，注入故障，每次转发前后各读一次座舱状态并计算逐事件差异 |
| `recorder_test.py` | recorder 单元与集成测试。默认使用内存存储与进程内假 MCP server；设置 `ESA_TEST_REDIS_URL` 后追加真实 Redis 事务与隔离测试 |
| `fake_mcp.py` | 确定性假座舱 MCP server，默认 MCP 镜像来源。按接入契约的七项能力实现，提供示例任务使用的工具与会话状态 |
| `fake_agent.py` | 确定性假座舱 agent，默认 agent 镜像来源。实现 `/v1/health` 与 `/v1/turn`，经 recorder 调用 MCP |
| `Dockerfile` | recorder 镜像 `esa-cockpit-recorder:1.0.0` |
| `Dockerfile.mcp` | 默认 MCP 镜像 `esa-cockpit-mcp:1.0.0` |
| `Dockerfile.agent` | 默认座舱 agent 镜像 `esa-cockpit-agent:1.0.0` |

## 本地运行

```bash
.venv/bin/python -m cockpit.fake_mcp --port 18091
.venv/bin/python -m cockpit.recorder --port 18093 --mcp-url http://127.0.0.1:18091 --store memory-test
.venv/bin/python -m cockpit.fake_agent --port 18090
```

recorder 生产默认使用 Redis（`REDIS_URL`），`--store memory-test` 只用于单进程测试。配置项见 [docs/esa-cockpit-integration.md](../docs/esa-cockpit-integration.md)。

三个组件加驱动脚本的本地全链路自检（导航与空调两个场景各跑一遍，不依赖 Docker）：

```bash
.venv/bin/python scripts/check-cockpit-driver.py
```

## 测试

```bash
.venv/bin/python -m unittest cockpit.recorder_test -v
ESA_TEST_REDIS_URL=redis://127.0.0.1:6379/0 .venv/bin/python -m unittest cockpit.recorder_test -v
```

## 镜像构建

```bash
bash scripts/build-esa-local.sh
```

用户资产就绪后，以 `COCKPIT_AGENT_IMAGE` 与 `COCKPIT_MCP_IMAGE` 指向用户构建的镜像即可覆盖默认假件，任务配置不需要改。
