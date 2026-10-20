# 本地车机工具环境

服务使用 Python 标准库 HTTP、只读 SQLite 和 Redis。依赖固定在 requirements.txt。

## 启动

在仓库根目录执行：

```sh
python3 -m venv environment/.venv
environment/.venv/bin/pip install -r environment/requirements.txt
environment/.venv/bin/python -m environment.server --redis-url redis://127.0.0.1:6379/0
```

默认端口为 8003。默认存储为 Redis。Redis 不可连接时启动失败。单进程测试可以显式传入 --store memory-test。生产部署使用 Redis。

SQLite 由 seed-v1.json 创建。业务查询使用 mode=ro 连接。元数据保存 dataset_version 和 seed_sha256。已有数据库的版本或内容哈希不一致时启动失败。使用 --db 指定独立数据库文件。使用 --seed-file 或 ESA_SEED_FILE 指定新增版本资料，输入必须满足 seed-schema.json 和实体关联校验。任务文件目录使用 ESA_WORKSPACE_ROOT，默认 /tmp/esa-workspaces。

## HTTP 与 MCP

- GET /health：健康状态。
- POST /eval/api/v1/sessions：创建会话。接收 type=car、context/initial_context、initial_state、data_overlays、tool_profile、faults、seed、dataset_version。
- POST /eval/mcp/{id}：JSON-RPC 2.0 tools/list 和 tools/call。tools/call 使用 params.name、params.arguments、params.call_id。arguments._call_id 作为兼容输入，验证前删除。
- GET /api/v1/rl/sessions/{id}/snapshot：固定 context、业务 state、initial_state、版本、种子与完整 manifest。
- GET /api/v1/rl/sessions/{id}/events：返回 events、truncated、total_events、state_version。支持 after 与 call_id 查询。
- POST /eval/api/v1/sessions/{id}/round：开始新轮次，重置屏幕焦点并原子保存状态差异事件。
- POST /eval/api/v1/sessions/{id}/reset：恢复初态，清除幂等缓存与故障计数，原子保存差异事件。
- DELETE /eval/api/v1/sessions/{id}：删除会话。
- GET /eval/api/v1/manifest：完整能力清单。

snapshot/events/reset/round 也支持 /eval/api/v1/sessions/{id}/...。

## 工具权限与业务能力

catalog.json 保留 源V2.0的61个名称、描述和 inputSchema。source_sha256 由源文件原始字节计算。manifest.json 为逐个能力记录，包含读取来源、写入字段、约束、结果、限制和 availability。重新生成目录：

```sh
python3 environment/generate_catalog.py /path/to/tools_v2.0.golden
```

configs/tools.json 保留全部 61 项。默认启用 59 项。session tool_profile 是启用名称的唯一子集。tools/list 和 tools/call 使用同一权限记录。未知名称、重复名称、遗漏名称、无法执行的启用配置在启动时失败。

61项工具具有明确处理器。默认关闭两个文件工具。list_directory/read_file 可以显式启用；仅允许会话目录相对路径，拒绝绝对路径、父级路径和符号链接，文件最大 64KiB，目录最大 200 项。

导航使用固定路线资料或确定性地理距离模型。所有路线明确标记 simulated。开始导航需要 D/R 挡位。地图搜索按关键词、距离、价格、评分、充电功率、品牌、路线范围和 limit 筛选。搜索结果写入 display.candidates，供 display_candidate_cards 使用。歌曲检索读取本地曲目，播放验证名称与歌手，未知歌曲明确失败。收藏、记忆、车辆控制、日程和本地服务任务写入实际会话状态。

天气、百科、财经、生态、车辆知识、笑话、故事、技能与视觉使用固定资料。没有匹配资料时返回明确缺失。服务任务保留每轮交互，不调用额外模型，不提交外部预订、交易、维修或救援。日程保存经过严格检查的 pending 任务，不提供后台时钟触发器。

旧 fixture 的 longitude/latitude、vehicle.window/door/remaining_*、navigation.total_*、user_addresses、memories、navi_history 会转换为标准业务状态。固定 context 不保留动态业务字段。

## 原子状态与故障

Redis 用一个会话记录保存 state、state_version、events 和 idempotency。每次执行使用 WATCH/MULTI/EXEC 提交整个记录。冲突重试有上限。整个记录统一 TTL。snapshot 使用单次 GET。

成功读取同样保存事件。状态没有变化时不增加 state_version。异常、未知工具、权限拒绝和 Schema 错误保存失败事件且保持业务状态。相同 call_id 和参数返回缓存，避免重复副作用。相同 ID 使用不同请求时保存拒绝事件并保留原缓存。事件包含完整参数、结果、序号、前后版本和 JSON Pointer 差异。事件默认保留最后 10000 项，发生截断时 truncated=true，total_events 保留累计计数。

faults 项包含 tool、arguments 匹配字段、times 和 kind。kind 支持 reject、missing_field（需 field 点路径）、response_lost。故障次数与工具执行原子提交。response_lost 在提交成功后断开 HTTP 连接。相同 call_id 重试可以读取已提交结果。expected、评分字段和 tool_overrides 拒绝进入服务。

## 测试

```sh
ESA_TEST_REDIS_URL=redis://127.0.0.1:6379/0 \
environment/.venv/bin/python -m unittest environment.test_environment -v
```

设置 ESA_GOLDEN_SOURCE 可以额外校验外部 canonical 文件与 checked-in catalog 完全相同。测试覆盖全部 61 项目录、全部 59 项默认工具行为、文件隔离、启用配置拒绝、查询筛选、写后读取、失败回滚、权限、幂等、故障、并发、TTL、版本事件、HTTP 协议和旧五个样例初态。

Dockerfile 使用仓库根目录作为构建上下文。服务以 UID 10001 运行。镜像仅复制明确的 Python 与数据文件，不复制本地虚拟环境。独立镜像构建及Python3.12容器测试通过；Harbor离线容器试验异常0且状态评分1.0。
