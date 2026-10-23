# ESA 验证记录

## 运行环境
宿主Go1.27.2；runtime镜像Go1.23.12构建；环境镜像Python3.12.9；Redis7.4.11容器；DockerDesktop29.8.2。

## 验证范围
- 工具定义、handler和manifest共61项，默认启用59项；catalog-schema校验通过。
- 宿主环境35项测试执行，17项通过，18项Redis集成测试因未设置ESA_TEST_REDIS_URL跳过。
- 环境测试覆盖目录schema与权限、固定数据、读写状态、失败回滚、幂等、故障、Redis并发和TTL、HTTP协议与任务文件隔离。
- 独立副本验证：仅复制本仓库到独立目录，环境测试与镜像构建通过。
- runtime race测试与go vet通过，覆盖84.8%；镜像内Go1.23.12测试与构建通过。
- 状态评分、ATIF、verifier：8项通过。
- Node驱动证据测试及真实环境/Go runtime端到端导航通过。
- 两个独立镜像构建成功。
- Harbor五任务验收 `esa-selfcontained-001`：5项试验、异常0，平均reward 0.7966；a1与a4为1.0，b1为0.8，c3为0.65，a3为0.5333。全部试验含新的catalog哈希与61项manifest。
- b1 记忆文案更新后复测 `esa-b1-memory-001`：1项试验、异常0，reward/state/tool_use/reply/judge均为1.0。

## 复现离线Harbor验收
在仓库根目录执行：

```bash
.venv/bin/python esa/fake_responses.py --port 19090
# 另一个终端，先构建两个独立镜像
STEPFUN_OPENAI_BASE_URL=http://host.docker.internal:19090/v1 STEPFUN_API_KEY=offline-placeholder PYTHONPATH="$PWD" .venv/bin/harbor run -c configs/jobs/esa-offline-acceptance.yaml --job-name UNIQUE_JOB_NAME -y
```

真实模型与judge的表现需使用实际模型配置另行评测。
