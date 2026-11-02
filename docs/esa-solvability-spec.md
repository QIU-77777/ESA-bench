# ESA-bench 任务可解性保证：需求与技术规格（Solvability Spec）

> 版本 v1.0，2026-10-29。需求/技术规格文档。
> 解决的问题：**模拟环境出题，怎么保证每道题有标准解（可用的工具链路 + 可用的返回结果），让「解不出来」确定是 agent/模型的问题，而不是评测环境的问题。**
> 上游：[构造原则 P5](esa-benchmark-principles.md)（golden run 前置）在本文细化为可实施合同；执行细节回流 [构造方案 §4](esa-benchmark-plan.md)。

---

## 0. 术语

| 术语 | 定义 |
|---|---|
| **GT 链路（ground-truth chain）** | 每题附带的标准工具调用序列：具体工具 + 具体参数（绑定 seed 实体 ID）+ 步骤间参数链 + 期望效果。是「该题有解」的机器可验证见证 |
| **可解性合同（solvability contract）** | GT 链路 + 环境重放证据（golden-run.json）的组合。出厂前置条件 |
| **工具契约图（tool contract graph）** | 由工具的 reads/writes/requires/参数来源构成的依赖图。对标 CAR-bench 的 API graph |
| **见证解 vs 唯一解** | GT 链路是**一个**合法解（见证）。评分认终态不认路径（原则 P6），等价链路同样得分——「有标准答案」指「有可达成的期望终态 + 至少一条机器验证过的链路」，不指路径唯一 |

## 1. 问题定义

模拟环境 benchmark 的归因风险：seed 数据缺实体、工具返回空/错、前置条件（挡位等）不满足、检索排序不稳定、工具契约与 handler 实现不符——这些**环境缺陷全部表现为「模型没做出来」**，与模型能力缺陷不可区分。CAR-bench 论文的对策是构造期保证 + 试跑迭代 + 人工验证，但**没有出厂前的自动可解性证明**（论文 §3.5 依赖 LLM 试跑修正 + manual validation）。

**目标**：
1. 每道题入库前必须携带机器可验证的可解性证明（GT 链路确定性重放通过）。
2. 归因协议：GT 链路重放通过的题，模型失败 = agent 责任；重放失败 = 题目/数据/工具问题，该题报废或修复，**不进评测结果**。
3. 构造期发现的工具/数据缺陷形成反哺清单，驱动 MCP server 模拟返回与工具设计优化。

**非目标**：
- 不保证指令无歧义（归双人互审 + pilot 校准，原则 P12）——重放证明「环境有解」，不证明「话术只有一种理解」。
- 不强制唯一路径（P6）。
- 不做自动题目生成器（v1 人工出题，生成器是 v2 演进项）。

## 2. CAR-bench 机制调研结论

### 2.1 它怎么做（论文 + 代码双重确认）

**构造期（论文 §3.5）**：三层——
1. **API graph**：「linking all tools through their parameters, accessed states, and state changes, incorporating interdependencies such as parameter chaining and policy-based triggers」。边 = 参数链式依赖 + 读写状态 + 策略触发。
2. **从图采样轨迹**：在 API graph 上采样 multi-step 轨迹（1–9 actions），独立均匀生成初态，**LLM agent 反向生成导向该轨迹的用户指令**，再用 LLM agent 在环境里试跑、修正 GT 的可行性与自然度。
3. **人工验证**：歧义措辞、指令完整性、动作正确性、执行可行性、**唯一终态**。迭代测试修正「任务规格错误导致的失败」。

**GT 的数据形态（`car_bench/types.py`）**：
```python
class Action(BaseModel):
    name: str
    kwargs: Dict[str, Any]
    index: Optional[int] = None
    dependent_on_action_index: Optional[Union[int, List[int]]] = None   # ← API graph 的参数链残留

class Task(BaseModel):
    task_id, calendar_id, actions: List[Action], persona, instruction,
    context_init_config, task_type,
    disambiguation_element_internal / _user / _note, removed_part
```
`dependent_on_action_index` 显式声明「本动作的参数依赖前序动作输出」——参数链（parameter chaining）的落点。

**评分期的 GT 重放（`reward_calculators.calculate_state_based_reward`）**——可解性机制的核心：
```python
gt_context_state = ContextState()
context_state.get().update_state(**task.context_init_config)   # 装初态
gt_state_hashes = [hash(initial)]
for action in task.actions:                                     # 逐个重放 GT 动作
    step_fn(action, messages)                                   # ← 走真实工具实现
    gt_state_hashes.append(hash(current))
gt_vehicle_state_hash = hash(final)                             # 金标准终态
r_actions_final = agent_final_hash == gt_vehicle_state_hash
r_actions_intermediate = agent_states ⊆ gt_states               # 顺序无关的中间态集合检查
```
即：**金标准不是预先算好存起来的，而是评分时用 GT 动作序列在真实工具实现上重放出来的**。工具改了，金标准跟着变；GT 动作被工具拒绝，重放当场暴露。`r_tool_subset` 只要求 GT 中的 get 类工具被覆盖（忽略参数，允许多调）——等价路径的机械化处理。

**不可满足题的派生（`tool_manipulation.py`）**：`removed_part` 用点记法（`tool` / `tool.param` / `tool.param.sub`）在运行时从工具 schema 删除元素（`remove_tool_elements`），并用 `check_hallucinated_removed_part` 检测 agent 是否使用了被删元素。派生是**机械的**：基题可解 → 删关键路径元素 → 不可满足，无需重新出题。

### 2.2 借鉴与不照搬

| CAR-bench 做法 | 判定 | 落到 ESA |
|---|---|---|
| GT = 显式动作序列（工具+具体参数+依赖索引） | **借** | `ground_truth.json` GT 链路，参数链用 `$binds` 引用 |
| 评分期用真实工具实现重放 GT 产生金标准 | **借并前移** | 同一套重放机制，但前移到**出厂前校验**（CI 门禁），评分期只比对 expected_state |
| 终态比对（hash）+ 顺序无关中间态检查 | **部分借** | 终态比对（ESA 的 expected_state 谓词，比 hash 更细）；中间态检查用 `forbidden_changes`（比 CAR-bench 的子集检查宽容——CAR-bench 惩罚任何 GT 之外的中间态，对多路径不公，ESA 不采纳，见 §5.3） |
| API graph 构造期采样 | **借思想，降规模** | 工具契约图（§4.2）做**校验**（链路连贯、旁路检测），不做自动采样生成器（v2） |
| 派生删除（tool/param/result） | **借** | `removal_spec` 机械派生 + 旁路封死检查（§7） |
| LLM 试跑 + 人工验证保可解 | **降级为兜底** | 确定性重放替代其可解性职能；LLM 试跑只留难度校准（pilot）；人工只留措辞审 |
| 唯一终态要求 | **不借** | ESA 认终态不认路径（P6）；「唯一」指期望终态可判定，不指动作序列唯一 |
| 用户模拟器控制词判 hallucination | **不借** | ESA 用 reply_checks + judge + removal 元数据（P8/P9 决策 D2） |

**关键差异**：CAR-bench 的可解性靠「构造期图采样 + 评分期重放 + 人工」三重，其中重放在评分期才发生——**题目缺陷在跑完模型后才暴露**。ESA 把同一套重放做成**入库前门禁**，这正是「解不出来一定是 agent 的问题」的成立前提。

## 3. 总体方案：三层结构

```
┌─ A. 数据契约 ─────────────────────────────────────┐
│  ground_truth.json（GT 链路）+ 工具契约图 + 题目卡片 │
└──────────────────┬───────────────────────────────┘
                   ▼
┌─ B. 确定性校验器（出厂门禁）───────────────────────┐
│  L0 静态检查（schema/ID/参数/权限）                  │
│  L1 GT 重放（真实 handler 执行，产出 golden-run.json）│
└──────────────────┬───────────────────────────────┘
                   ▼
┌─ C. 归因与反哺 ────────────────────────────────────┐
│  重放过 → 模型失败=agent 责任                       │
│  重放挂 → 三类归因：题目错/数据缺/工具缺陷 → 修复清单 │
└───────────────────────────────────────────────────┘
```

## 4. 数据契约

### 4.1 ground_truth.json（每题一个，与 rubric 同级）

```json
{
  "schema_version": "1.0",
  "case_id": "NAVI-L2-03",
  "solution_class": "multi_path",
  "task_type": "T-execute",
  "chain": [
    {
      "step": 0,
      "tool": "search_memory",
      "args": {"query": "川菜馆"},
      "depends_on": [],
      "expect": {"status": "success", "result_contains": ["MEM-0007"]},
      "binds": {"dest_poi": "$.result.items[0].poi_id"}
    },
    {
      "step": 1,
      "tool": "plan_navi_route",
      "args": {"destination": "$binds.dest_poi"},
      "depends_on": [0],
      "expect": {"status": "success"}
    }
  ],
  "expected_state": {"navigation.planned": true, "navigation.destination.id": "200001"},
  "faults": [],
  "derivation": null
}
```

字段语义：
- **chain[].args**：必须是具体值（seed 实体 ID / 字面量）或 `$binds.x` 引用前序输出路径（参数链，对标 `dependent_on_action_index`）。禁止模糊值。
- **chain[].expect**：该步最小断言——status、result_contains（后续 binds 依赖的字段必须出现在这里，双保险）。
- **solution_class**：`unique_chain`（该链是唯一合理路径，process_constraints 用 ordered_tools 锁定）或 `multi_path`（GT 仅为见证，评分认终态）。
- **faults**：与环境 `faults` 字段同构（`{tool, arguments, times, kind: reject|missing_field|response_lost, field, message}`，已由 `Environment.validate_faults` 支持）。O7 题把故障注入声明在此，GT 链须包含**期望的恢复行为**（如实报告/重试/降级）对应的话术断言放 rubric reply/judge。
- **derivation**：派生题填 `{"from": "NAVI-L2-03", "removal_spec": {...}}` 或 `{"from": "...", "ambiguity_spec": {...}}`，记录派生来源（对照组）。

### 4.2 工具契约图（catalog.json 扩展）

每个工具增加机器可读契约（`environment/catalog.json` 每工具新增 `contract` 字段，`generate_manifest` 同步进 manifest）：

```json
{
  "name": "plan_navi_route",
  "contract": {
    "reads": ["state.navigation", "data.pois", "data.routes"],
    "writes": ["state.navigation.planned", "state.navigation.destination", "state.navigation.waypoints"],
    "requires": [{"state": "state.vehicle.gear", "in": ["P", "D", "R", "N"]}],
    "param_sources": {"destination": "data.pois.id", "waypoints[]": "data.pois.id"},
    "result_schema": {"route_id": "str", "distance_m": "int", "duration_s": "int"},
    "returns_state_fields": true
  }
}
```

用途（按优先级）：
1. **L0 静态校验**：GT 链参数能在 `param_sources` 对应的 seed 集合或前序 `binds` 中找到；`requires` 与任务初态一致（挡位等）；`tool_profile` 覆盖链上工具。
2. **旁路检测**（§7）：派生题删元素后，在图上搜索「被删功能是否还有替代路径」（等价工具/委派工具）。
3. **覆盖分析**：域×工具覆盖报表（场景设计文档 §3 的机器化）。
4. v2 自动出题（图采样）的输入——不在本期。

**维护约定**：contract 与 handler 实现必须同 commit 修改；`generate_manifest` 重生成后 `git diff --exit-code`（沿用现有开发验证流程）。

### 4.3 题目卡片补充字段

卡片（scenario-difficulty §6 流程）新增：`gt_ref: ground_truth.json 路径`、`critical_path: 链上步骤索引`（供 removal_spec 只删关键路径元素）、`solvability_notes`（自由文本，记录出题时的环境依赖假设）。

## 5. 确定性校验器（golden-run validator）

### 5.1 形态与接口

- 入口：`scripts/validate_task.py --task tasks/<id> [--all] [--seed-file ...]`
- 实现：**in-process** 复用 `environment.service.Environment(store, dataset, config)`（与 `environment.test_environment` 同路），不走 Docker/HTTP——CI 快、无网络变量。
- 复用件：状态谓词判定直接调 `esa.state_score.score_state`（与正式评分同一实现，避免「校验用一套判分用另一套」）。
- 输出：
  - exit code：0=通过；10=题目定义错误；20=seed/数据缺口；30=工具/环境缺陷（分类见 §6）
  - `golden-run.json`（入库证据）：session 初态快照、每步调用与返回摘要、每步后状态 hash、终态快照、forbidden 检查结果、耗时

### 5.2 校验分级

**L0 静态检查**（无执行）：
1. rubric / ground_truth / initial_state 的 schema 合法（含 rubric 路径可解析到 state 结构）；
2. GT 链工具全部在 catalog 且在 `tool_profile` 内；参数类型/枚举符合 catalog schema；
3. 字面量 ID 存在于 seed 或 data_overlays；`$binds` 引用先定义后使用、路径存在；
4. `requires` 前置条件被初态满足（挡位、导航中/未导航等）；
5. expected_state 的 path 与值类型和 seed/状态 schema 一致；派生题的 removal_spec 目标在 critical_path 上。

**L1 GT 重放**（确定性执行）：
1. `Environment.create(initial_state.json 内容 + faults)` 成功；
2. 逐步 `execute(sid, tool, args_resolved, call_id)`：断言 status 符合 `expect`、`result_contains` 命中、binds 路径可解析且类型正确；
3. 链末断言：`score_state(rubric, initial, final, events)` 的 expected_state 全过、forbidden_changes 未触发、process_constraints（若有）满足；
4. 事件记录未截断；幂等性抽查（可选：同 call_id 重放末步返回一致）；
5. T-clarify/T-boundary 的附加断言（§6）。

**L2 等价性抽查**（可选，人工触发）：对 `solution_class=multi_path` 的题，跑 1–2 条已知等价链（卡片 `solvability_notes` 里列出），确认终态一致——防「只有 GT 那条路能走通」的隐性唯一性。

### 5.3 与正式评分的关系

- 校验期重放 = **可解性证明**（存在性）；评分期只比对 expected_state / forbidden / process / tool_checks / reply / judge（已有引擎）。
- 不采纳 CAR-bench 的「中间态集合 ⊆ GT 中间态」严格检查：它惩罚一切非 GT 副作用（哪怕等价链合法），对多路径不公。ESA 用 `forbidden_changes`（白名单外的状态改动即挂，P6）达到同等安全效果且不锁路径。
- `unique_chain` 题用 `ordered_tools` + `max_calls` 在评分侧锁定路径特征，与 GT 呼应。

## 6. 分题型可解性定义（判定标准不同）

| 题型 | 「有解」的定义 | L1 附加断言 |
|---|---|---|
| T-execute | GT 链重放全绿 + expected_state 满足 | 无 |
| T-clarify | 决策点前**无状态改动**；internal 解：ambiguity_spec 指向的偏好/记忆条目在 seed 中存在且可导出目标值；user 解：GT 链含分支应答（agent 问→预写答案→后续动作），分支在链上以 `{"step": "branch", "on": "澄清问题", "user_reply": "..."}` 形式声明 | internal：验证 seed 依据存在；user：分支后续动作重放通过 |
| T-boundary | **反向有解**：(a) 基题（derivation.from）GT 链校验通过；(b) removal 后不可满足性成立——被删元素在 critical_path 上、tool_profile 无旁路（含委派工具封死）；(c) 期望终态 = 初态无破坏性变化 + 承认话术（rubric reply/judge 承载） | 旁路检测跑图；断言「不存在 tool_profile 内的替代链」 |
| T-knowledge | 信息链（get 类）重放返回中**可导出** judge 条目判真所需的事实 | result_contains 覆盖 judge 条目的事实依据 |

**O7 故障题**（环境 `faults` 已支持三类：reject / missing_field / response_lost）：GT 链声明注入（faults 字段）+ 期望恢复行为（重试/降级/如实报告），L1 断言恢复行为对应的状态/事件形态。

## 7. 派生题（R8/R9）的可解性保证

1. **只删关键路径**：`removal_spec` 的目标必须落在基题 GT 链 `critical_path` 上——删一个不在链上的元素不构成不可满足（假题）。
2. **旁路封死**：删除后在工具契约图上做可达性搜索（等价工具、上位委派工具如 multi_task_agent、组合替代），发现旁路则：要么从 `tool_profile` 封死并声明，要么改删别的元素。
3. **删除物可验证**：`remove_tool_elements` 式的运行时删除（对标 CAR-bench dot notation）落到 ESA 的 `tool_profile`（整工具）或 catalog 参数裁剪（参数粒度，需要环境支持 per-task 参数覆盖——列入环境需求 E8）；`result` 粒度（删返回字段）由 data_overlays/handler 配置实现。
4. **歧义题双解可验**：ambiguity_spec 的 `resolvable_by=internal` 必须给出 seed 依据 ID（L0 校验存在）；`resolvable_by=user` 的应答必须预写进 user_messages/分支（L1 重放走通）。

## 8. 环境与工具优化需求（反哺清单）

| # | 需求 | 触发 | 优先级 |
|---|---|---|---|
| E1 | **确定性约束**：检索类工具排序稳定（并列时按 ID tie-break）、无未声明随机、时钟固定（已具备）、同一输入恒等返回 | 「挑最近的/最便宜的」类题的期望可判定 | 高 |
| E2 | **seed 引用完整性**：扩展 seed-schema 校验（poi↔routes↔favorites、contacts↔memory、services↔pois 的 ID 引用），`generate_manifest` 一并检查 | GT 链 ID 依赖 | 高 |
| E3 | **工具契约落地**：catalog.json 增 `contract`（§4.2），handler 与契约一致性抽查（每工具 smoke case，`environment.test_environment` 扩展） | L0 校验 | 高 |
| E4 | faults 已支持（`Environment.validate_faults`，kind: reject/missing_field/response_lost）→ **O7 题可进 v1**；补：GT 链 fault 场景的事件记录语义（fault 计数进 state-events） | O7 构造 | 中 |
| E5 | 委派工具隔离：tool_profile 已能封 multi_task_agent/a2a_invoke（scenario §6.3）；校验器检查 GT 只含 profile 内工具 | 旁路封死 | 高 |
| E6 | **返回结果可用性**：查询工具返回必须含 GT 后续 binds 所需字段——由 L1 重放自动保证；同时在 catalog `result_schema` 文档化 | 参数链 | 高 |
| E7 | 环境缺陷出口：校验器 30 类失败输出「工具名+调用+缺失字段」的最小复现，直接可转 issue | 反哺闭环 | 中 |
| E8 | per-task 参数裁剪（参数粒度 removal）：会话创建支持 `param_overrides`（对标 CAR-bench `remove_tool_elements` 的 param 级删除） | R8 参数粒度派生 | 中（v1 可用「tool 整删+结果字段删」降级） |

## 9. 出题工作流与归因协议

**SOP 更新**（嵌入 scenario-difficulty §6 流程，替换原「golden run」步骤）：

```
题目卡片 → 初态/rubric/GT 链路 → validate_task（L0+L1）→ 双人互审 → pilot 校准 → 入库
                                     │
                                     ├─ 10 题目错 → 改卡片/rubric/GT
                                     ├─ 20 数据缺 → 扩 seed/data_overlays（E2）
                                     └─ 30 工具缺陷 → 环境 issue（E7）→ 修 handler/catalog → 重生成 manifest → 重新校验
```

**归因协议（写进评测运行手册）**：
1. 只有携带有效 `golden-run.json`（校验通过且 seed_sha256/manifest 版本与运行环境一致）的题目参与评测；
2. 模型失败 → 记 agent 失败（归因到 M1–M4 维度）；
3. 运行中发现环境异常（工具报错超出 faults 声明、返回缺字段）→ 该 trial 标记 `env_suspect`，不计入分数，转 E7 流程；
4. 环境/seed 任何变更后，全量重跑 `validate_task --all`（golden-run 失效重建）。

## 10. 验收标准

- [ ] 40 题试点的每题：L0+L1 通过，golden-run.json 入库，失败分类可用
- [ ] GT 链路字段（binds/depends_on/expect）全部有解析器与报错路径
- [ ] 工具契约覆盖全部 61 工具，generate_manifest 纳入契约校验
- [ ] T-clarify（internal/user）与 T-boundary 各 ≥2 题走通反向/分支断言
- [ ] O7 故障题 ≥2 题走通 faults 重放
- [ ] 归因协议在一次真实试跑中演练：至少 1 个环境缺陷被 30 类出口拦截、未混入模型分
- [ ] validate_task 单题校验 < 10s（in-process），40 题全量 < 5min

## 11. 里程碑

| 阶段 | 内容 | 对应 |
|---|---|---|
| S1 | ground_truth.json schema + validate_task L0/L1 + golden-run.json | 40 题试点前置（I3 seed v2 并行） |
| S2 | 工具契约 E3 + seed 完整性 E2 + 确定性 E1 | 批量出题前置 |
| S3 | 分题型断言（clarify/boundary/faults）+ 派生旁路检测 | L3 批量构造前置 |
| S4 | 归因协议演练 + E7 缺陷出口 + 文档收口 | 报告可信度声明 |

**工作量预估**：S1 是核心（validator + schema，约 2–3 个工作日）；S2 随 seed v2 一起做；S3/S4 跟随试点扩量。
