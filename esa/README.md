# ESA 本地车机评测

## 组件
- [规范](../docs/esa-local-eval-spec.md)：环境与 runtime 契约。
- [工具配置](../configs/tools.json)：工具启用状态；模型与环境共用 session 权限。
- [工具目录](../environment/catalog.json)：V2.0 参数定义。
- [ReAct runtime](../runtime/README.md)：独立 Go 服务。
- [环境服务](../environment/README.md)：本地数据、Redis 状态和工具执行。
- [驱动](driver.mjs)：创建 session、运行脚本对话、收集状态证据。
- [Harbor agent](esa_agent.py)：生成 ATIF 与 verifier 素材。
- [状态评分](state_score.py)：终态谓词、禁止变更和有序过程约束。

## 构建与运行
在仓库根目录执行：

```bash
bash scripts/build-esa-local.sh
# 配置 .env 后加载变量，详情见仓库根README。
set -a
source .env
set +a
.venv/bin/python scripts/render-job.py --bench esa --harness esa --model stepfun/step-5-preview --output /tmp/esa-job.yaml
PYTHONPATH="$PWD" .venv/bin/harbor run -c /tmp/esa-job.yaml --job-name esa-local -y
```

业务工具默认不访问外部服务。Docker daemon 必须可用。五个任务提供样例，用例不控制环境实现。

## 编写题目
任务初始文件字段：case_id、instruction 或 user_messages、context、initial_state、dataset_version、seed、data_overlays、tool_profile、faults。user_messages 是完整输入序列，取代单条 instruction。答案保存在独立 rubric，由 verifier读取。

rubric 支持：
- expected_state：字段路径与值的对象，或 {path,op,value,description} 数组。
- forbidden_changes：业务路径数组，同时检查执行途中修改后恢复。
- process_constraints：ordered_tools / max_calls / no_failed_calls。
- tool_use、reply、judge：现有参数、表达和LLM判断。
- weights：每个评分维度的权重。

状态谓词支持 eq/ne/exists/contains/in/gt/gte/lt/lte。缺失快照评分为0。禁止变化或过程约束失败，状态维度为0。事件截断时不能判定完整过程。

## 试验产物
Harbor日志包含 driver-result.json、trajectory.json、tool-calls.json、reply.txt、snapshot-initial.json、snapshot-final.json、state-events.json、environment-manifest.json。调用与结果共用 call_id，usage 缺失时标记 coverage。状态变化带路径和版本，不依据成功文案判断任务完成。

## 本地验证
```bash
(cd runtime && go test -race -cover ./...)
node --test esa/test-driver.mjs
.venv/bin/python -m unittest esa.test_state_score -v
```
