# ESA 验证记录

## 运行环境
宿主Go1.27.2；runtime镜像Go1.23.12构建；环境镜像Python3.12.9；Redis7.4.11容器；DockerDesktop29.8.2。

## 验证范围
- 工具目录、handler和manifest共61项，默认启用59项。
- 宿主环境35项测试全部通过，包含实际Redis7、canonical来源与目录生成验证。环境容器35项测试执行，31项通过，4项外部来源与示例测试跳过。
- 环境测试覆盖目录与权限、固定数据、读写状态、失败回滚、幂等、故障、Redis并发和TTL、HTTP协议与任务文件隔离。
- runtime race测试与go vet通过，覆盖84.8%；镜像内Go1.23.12测试与构建通过。
- 状态评分、ATIF、verifier：8项通过。
- Node驱动证据测试及真实环境/Go runtime端到端导航通过。
- 两个独立镜像构建成功。
- Harbor本地验收 `esa-61-tool-acceptance`：1项试验、异常0、reward/state/tool_use/reply均1.0。模型使用本地确定性接口，结果仅验证集成流程。

## 复现离线Harbor验收
在仓库根目录执行：

```bash
.venv/bin/python esa/fake_responses.py --port 19090
# 另一个终端，先构建两个独立镜像
STEPFUN_OPENAI_BASE_URL=http://host.docker.internal:19090/v1 STEPFUN_API_KEY=offline-placeholder PYTHONPATH="$PWD" .venv/bin/harbor run -c configs/jobs/esa-offline-acceptance.yaml --job-name UNIQUE_JOB_NAME -y
```

真实模型与judge的表现需使用实际模型配置另行评测。
