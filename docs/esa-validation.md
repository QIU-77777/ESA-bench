# ESA 验证记录

日期：2026-11-03。Harbor 版本：0.24.0。

## Harbor 原生 Oracle

[七题原生 Oracle 结果](../jobs/esa-native-oracle-standard-20261103/result.json)：

- 使用 Harbor 内置 oracle Agent 和内置 Docker 环境，未指定自定义 import_path。
- 七项试验完成，异常数量 0。
- 每题 reward、state、tool_use、reply 均为 1.0。
- Harbor 上传标准 solution 目录并执行 solve.sh；参考程序调用实际 Docker 环境的 HTTP/MCP 服务，状态存储使用 Redis。
- Oracle 仅读取环境初态和参考程序，保存完整工具、回复、对话、快照与事件证据。Oracle 不读取 rubric，不执行错误答案测试，不写奖励。
- Harbor 在 Oracle 完成后上传 tests，执行统一 verifier。
- 模型接口配置为不可用地址与测试凭据；本次没有调用真实模型或外部 judge。

[程序异常回归](../jobs/esa-native-oracle-failure-a3b231e311d8/failure-regression.json)：参考程序完成正确导航状态后抛出异常，原生 Oracle 保留证据，正式 verifier 判定 valid=false、reward=0。

[输入隔离检查](../jobs/esa-native-oracle-20261103/input-isolation.json)：实际任务镜像包含环境初态，不包含 solution、tests 或根目录参考与错误程序。ESAAgent 预检回归确认普通模型运行只接收 HTTP 桥接请求，不上传参考与评分文件。

## 参考任务验收

[七题 Docker 参考认证](../jobs/esa-reference-test-3ec23f03e236/summary.json)：

- 七个参考程序全部通过完整评分。
- 14 个错误答案全部正常结束并被有效评分拒绝。
- 两个合法替代解全部通过。
- 每个程序使用独立会话，保留初始和最终快照、完整调用、事件、对话与评分。
- 全部会话的实现、数据和工具指纹一致。
- 独立 Docker 环境使用 esa-local-environment:2.0.0 与专用 Redis。

## 回归测试

| 范围 | 结果 |
|---|---|
| 评分、参考、Oracle、Agent、环境、交互命令、Console | 200 项测试：181 项通过，19 项 Redis 测试在默认内存运行中跳过 |
| 专用真实 Redis 环境测试 | 37 项全部通过，无跳过 |
| Node 驱动 | 五项全部通过 |
| Go runtime | go test ./... 通过 |
| ESA 运行脚本 | 使用显式测试连接变量，--dry-run 通过 |
| 评分与 Oracle 执行器副本 | 七个任务与共享实现逐字节一致 |
| Git 差异格式 | git diff --check 通过 |

Oracle 回归覆盖：Harbor 原生入口发现、验收配置覆盖全部 benchmark 任务、独立标准库执行、Oracle 无需 tests、程序完成目标后异常、证据缺失、会话清理和凭据脱敏。

任务生成器回归覆盖：标准 solution 骨架、合法 TOML、独立 rubric、参考未实现与错误答案未编写时在 HTTP 前拒绝认证。

参考回归覆盖：源码误读限制、SystemExit/GeneratorExit、执行期限、线程跟踪器恢复、重复 call_id、入口覆盖、错误解异常、快照与清理失败、缺失和变更的环境身份、执行期间源码变化、评分与 Oracle 副本字节差异、旧认证报告失效。

环境回归覆盖：schema、权限、确定性数据、读写状态、失败回滚、幂等、故障、HTTP、Redis 并发和 TTL。写入响应丢失后的同标识恢复不会重复播放。

测试容器和 Redis 均已清理，已有开发服务保持运行。没有提交代码。

## 复现

```bash
.venv/bin/python scripts/sync-task-scorers.py --check
.venv/bin/python scripts/validate_task.py --all --local-http --json
.venv/bin/python scripts/test-reference-docker.py

STEPFUN_OPENAI_BASE_URL=http://127.0.0.1:1/v1 \
STEPFUN_API_KEY=oracle-no-model PYTHONPATH="$PWD" \
.venv/bin/harbor run -c configs/jobs/esa-oracle-acceptance.yaml \
  --job-name UNIQUE_JOB_NAME -y

.venv/bin/python scripts/test-oracle-failure.py
```

所有正式 ESA 模型运行都会重新执行实际部署参考认证。证据保留 review_required；指令语义、信息来源与未覆盖合法路径仍需人工审核。
