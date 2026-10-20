# ESA 本地车机评测环境规范

状态：已批准范围，实施依据。版本：1.0。日期：2026-10-20。

## 目标与写入范围
本仓库提供独立最小 Go ReAct runtime、Python 本地车机工具服务、只读 SQLite 数据和 Redis 会话状态。生产 agent-runtime、mcp、car-bench 仓库不修改。Harbor 是调度及评分层。五个现有用例是回归样例，不限定环境能力。

## 组件与协议
- runtime/：独立 Go module，HTTP 8080，GET /v1/health，POST /v1/run。无 Voice/ASR/A2A/VIN/前置分类器。仅标准库及确有需要的依赖。
- environment/：Python HTTP 8003，本地 MCP JSON-RPC tools/list 和 tools/call；GET /health；POST /eval/api/v1/sessions；GET /api/v1/rl/sessions/{id}/snapshot 和 /events；DELETE /eval/api/v1/sessions/{id}。
- esa/driver.mjs：创建会话，通过 /v1/run 发起脚本化单轮或多轮，收集实际 iterations、状态及事件；不依赖 WebSocket 或生产服务。
- configs/tools.json：唯一启用配置，61个源V2.0工具。默认启用59个，文件目录/读取默认禁用。unknown/duplicate/unimplemented enabled在启动时失败。
- 模型可见 schema 与服务执行许可来自同一 session tools/list。agent.json 中的旧工具白名单不再控制默认工具集合；用例可通过 tool_profile 子集限制且服务执行同样限制。

## 模型循环
请求发送 instructions + tools + 完整 transcript，不发送 previous_response_id；store=false。支持 Responses SSE与非流式，保留 reasoning 在日志，不重放推理或 response IDs。重放 user/assistant/function_call/function_call_output，调用和结果使用同一 call_id。串行工具执行，错误作为工具结果进入模型，最大12次迭代及总超时可配置。取消/断流关闭未完成工具调用，报告失败，不伪造正常终止。多轮 user_messages按顺序执行，共用transcript和业务session。上下文预算超限明确失败；不静默丢弃历史。
每个模型调用输出 iteration、message、reasoning、toolCalls、usage、latencyMs、status、responseId。失败尝试也有记录。runtime不保存API key到产物。请求模型覆盖来自Harbor job。

## 工具契约
environment/catalog.json从现有tools_v2.0.golden生成保留61个名称、描述、inputSchema，保存来源hash，不要求构建时依赖其他仓库。实现manifest逐个描述数据读取、状态写入、约束、结果和限制。JSON Schema在执行前验证，工具扩展通过完整schema+实现注册，禁止静默通用成功fallback。
导航/车控/音乐/收藏/记忆/日程等在本地计算或提交真实状态。联网/金融/生态/知识读取固定资料。视觉读分发资产的固定描述，明确模拟结果。技能读本地技能资料。a2a/multi-task工具使用明确的本地服务任务状态，不启动额外LLM，不声称真实远端操作。
文件工具启用时局限独立任务工作目录，不允许绝对路径、父级路径和符号链接逃逸。tools/list与tools/call共同执行会话工具权限。

## 数据与状态
固定SQLite数据由版本化JSON seed构建：pois、routes、tracks、documents、weather、contacts及关联实体。查询使用参数、过滤、排序、limit、位置计算，缺失明确返回，不联网。路线可由可复现本地地理模型生成且结果标记模拟，无真实交通预测。session data_overlays只能覆盖固定业务表已知字段，不含expected答案。
固定context：时间、规格等。可变state：vehicle/navigation/addresses/memory/media/display/map/schedules/service_tasks/chat，以结构化JSON保存；兼容旧initial context字段。初始snapshot保存，初态可重置。每个session带dataset_version、seed、tool_profile、state_version。
Redis是持久化权威：每次Execute在WATCH/MULTI事务中原子提交state/version/event/idempotency cache。读操作也生成事件但不增加state_version；失败不改业务state；重复call_id和相同请求返回缓存不重复副作用，不同请求同ID拒绝。所有调用包括异常/unknown/schema错误均留痕。并发冲突重试有上限。TTL统一，snapshot一次读取同一原子记录。事件有sequence、call_id、工具、完整参数/结果、status、before/after版本、JSON路径changes。上限截断必须显式标记。
数据库只读，不复制一份可变状态到SQLite。可选in-memory测试store须显式配置，仅用于单进程单元测试，生产默认Redis，绝不自动fallback。

## 题目与故障
用例字段：dataset_version、seed、initial_context/initial_state、data_overlays、tool_profile、user_messages、faults。评分expected_state/forbidden_changes/process_constraints/reply/judge仅verifier读取。
正常业务不使用按工具名固定成功文本的override。旧tool_overrides转换为数据及明确fault；如果仍出现则启动报错，防止伪成功。fault支持执行前拒绝、字段缺失、响应丢失（后者已提交事件且可通过call_id查询），匹配工具/参数/次数，确定性。

## 评分与产物
ATIF保留每轮user step和每次模型agent step、reasoning、calls/results、tokens/latency/status及汇总。snapshot-initial.json、snapshot-final.json、state-events.json、environment manifest随trial保存。新增代码评分支持点路径/比较谓词、禁止变更和有序过程约束；与现有reply/judge分数独立。无usage时省略，部分usage标记coverage，不将缺失算零成本。参考动作只用于题目验证，可在全新session重放，不污染actual。

## 验收
1 所有61个schema有实现及独立能力记录；默认启用59。
2 读参数改变结果；导航后可查询；播放后可查询session snapshot；收藏/记忆写后可读。
3 状态失败回滚、调用幂等、未知工具拒绝、权限一致、并发隔离、版本事件一致。
4 本地假模型测试SSE/多轮/工具错误/上限/超时/usage；不依赖真实API。
5 SQLite关联校验、固定seed可复现、fault严格匹配；任务目录、文件路径与符号链接逃逸测试。
6 Harbor五个样例转换，docker compose配置校验，集成独立镜像。真实模型测试仅显式使用用户已有凭据，报告与离线测试分开。

## 版本与部署
独立runtime/environment镜像，Redis 7。数据seed和schema/config纳入git，生成SQLite不需联网。依赖固定版本。工具服务和runtime可外置共享，通过session隔离；默认compose仍可自包含。更换环境后旧分数不作直接模型比较。

## 验证状态
2026-10-20：独立runtime、61工具环境与Harbor驱动已实现。runtime通过Go1.23.12镜像构建内测试及宿主race测试，覆盖84.8%。两个独立镜像构建成功。环境、状态评分、ATIF与驱动回归测试通过。Harbor离线容器试验异常0，reward/state/tool_use/reply均1.0。真实模型与judge表现需独立运行评测。

## 实施顺序
规范与能力表 → runtime及环境并行实现 → driver/评分/示例集成 → 单元与Redis集成 → 容器验收 → 文档。Docker未启动或Go缺失不阻止源代码及离线验证，容器验收须明确未完成，不能标记整体完成。
