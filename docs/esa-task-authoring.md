# 编写 ESA 车机评测题目

## 查看能力
[工具目录](../environment/catalog.json)包含名称和参数。[能力清单](../environment/manifest.json)包含数据来源、副作用和约束。[配置](../configs/tools.json)控制工具启用。目录包含61个工具，默认59个可用。目录读取和文件读取可配置，仅允许访问任务工作目录。

## 初始化环境
题目可使用现有seed实体，或者通过环境 --seed-file 指定符合 [数据schema](../environment/seed-schema.json)的新版本资料。每次试验创建独立session。data_overlays只覆盖当前数据已有ID和字段，不改变公共数据库。

示例：

	{
	  "case_id":"navigation-example",
	  "dataset_version":"esa-local-v1",
	  "seed":0,
	  "context":{"location":{"id":"100001","lon":121.445,"lat":31.21},"time":"2026-10-20T09:00:00+08:00"},
	  "initial_state":{"vehicle":{"gear":"D"}},
	  "tool_profile":["search_poi_by_keyword","start_navi","get_navi_info"],
	  "user_messages":["导航去科技园 A 座","现在的目的地是什么？"]
	}

user_messages包括完整用户序列，取代instruction。model历史和业务state共享至本次试验结束。时钟固定，环境不会自动推进车辆位置或触发定时任务。

## 验证结果
答案仅在rubric中定义：

	{
	  "expected_state":{"navigation.active":true,"navigation.destination.id":"200001"},
	  "forbidden_changes":["vehicle.volume"],
	  "process_constraints":[{"type":"max_calls","tool":"start_navi","count":1}],
	  "weights":{"state":1}
	}

字段路径针对snapshot.state，数组下标使用数字。过程约束只检查实际事件；未知规则使verifier失败。禁止修改会检查完整执行历史，即使最终恢复原值也失败。

## 测试题目
先使用确定性的脚本工具调用验证初态、目标状态和约束是否成立，再调用待测模型。不同有效工具序列可得到相同终态，不要求与参考动作逐项相同。模型失败时保留trace与快照，不正常评分。

## 环境限制
路线为固定资料或确定性地理模型。天气和公开资料为本地固定记录。视觉返回分发的固定描述。外部服务工具创建本地任务和交互记录，没有真实预订、支付或远端agent。日程工具保存pending任务，无后台触发器。开始导航按V2.0要求D/R状态；P状态可规划与估算。三项文件工具默认禁用。
