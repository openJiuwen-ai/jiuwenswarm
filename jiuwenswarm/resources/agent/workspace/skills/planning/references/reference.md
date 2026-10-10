# Reference（规划模块内部 sub-agent 接口）

> 本文件定义规划模块**内部** 3 个 sub-agent 之间的 **4 个流转接口**。
> 这些接口**不对外**（不进模块三/四），只用于 MethodArchitect / MethodCritic / ExperimentPlanner 之间的协作。
>
> - **方法阶段** 3 个接口：method_draft / method_review / method_final（构成反思循环）
> - **实验阶段** 1 个接口：experiment_final（无反思循环，靠确定性校验 + Planner 自我修正 + 用户裁决）
>
> 4 个接口：method_draft / method_review / method_final / experiment_final。

---

## 0. 通用约定

- 这 4 个接口是**结构化数据**（dict），不是 prompt 字符串。
- Agent 派发前，按本文件字段组装 payload，注入 prompt。
- Agent 返回时，按本文件字段解析回 dict。
- 与 [planning-schemas.md](planning-schemas.md) 的关系：内部 `method_review` 字段**包含**外部 MethodReview 的全部字段（+ 上下文）。

---

## 1. method_draft（Architect → Critic）

| 字段 | 类型 | 说明 |
|---|---|---|
| `upstream_context` | dict | 上游 8 个输入的完整 dict：`research_question` / `hypotheses` / `gap_report` / `key_papers` / `references` / `research_frontier` / `resource_constraints` / `domain` |
| `draft_method_design` | dict | MethodDesign 全字段（见 planning-schemas.md §2.1） |
| `round` | int | 第几轮（1 = 初稿，2+ = 反思后重写） |
| `previous_feedback` | str | ⬜ 上一轮 MethodReview.feedback（仅 round ≥ 2 时有） |

**发出方**：MethodArchitect（每轮）
**接收方**：MethodCritic

---

## 2. method_review（Critic → Architect，反馈）

| 字段 | 类型 | 说明 |
|---|---|---|
| `review` | dict | MethodReview 全字段（见 planning-schemas.md §2.2） |
| `target_method_design` | dict | Critic 评审的 Architect 上一轮草稿（供 Architect 比对、保留已通过部分） |
| `target_fields` | list[str] | 反馈具体指向哪些字段（如 `["innovation_points[0].claim", "components[2].novelty_degree"]`）—— 让 Architect 定向改，不是整篇重写 |

**发出方**：MethodCritic（每轮）
**接收方**：MethodArchitect（仅当 `review.passed = False` 时）
**循环轮数** ≤ `MAX_METHOD_ROUNDS = 3`（见 planning-schemas.md §3）。超轮仍未过 → 接收当前最优版本 + 记 limitations，交 Leader 汇总。

---

## 3. method_final（Architect → Planner）

| 字段 | 类型 | 说明 |
|---|---|---|
| `final_method_design` | dict | MethodDesign 全字段 |
| `passed_review` | dict | 最近一轮通过的 MethodReview（`passed = True`） |
| `rounds_used` | int | 反思循环实际跑了多少轮（1-3） |

**发出方**：MethodArchitect（仅当最近一轮 `MethodReview.passed = True`）
**接收方**：ExperimentPlanner

---

## 4. experiment_final（Planner → 汇总）

| 字段 | 类型 | 说明 |
|---|---|---|
| `experiment_plan` | dict | ExperimentPlan 全字段（见 planning-schemas.md §2.3） |
| `data_plan` | dict | DataPlan 全字段（见 planning-schemas.md §2.4） |
| `rounds_used` | int | Planner 自我修正了几轮（0 = 一次过，1 = 校验失败后补 1 轮） |
| `validation_results` | dict | 校验结果汇总：`{validate_experiment_matrix: pass/fail, validate_data_plan: pass/fail, check_hypothesis_coverage: pass/fail}` |
| `compute_tier` | int | 算力档位（0=精算 / 1=消减 / 2=核心优先，见 planning-schemas.md §4） |

**发出方**：ExperimentPlanner
**接收方**：Leader 汇总（产生 4 个对外产物：method_design / method_review / experiment_plan / data_plan）

**注**：实验阶段**没有**反思循环。失败处理走两条路：
1. 确定性校验（§7 A 档的 `validate_experiment_matrix` / `validate_data_plan` / `check_hypothesis_coverage`）失败 → Planner 自我修正 1 轮
2. 仍然失败 → 提交 Leader 汇总，由用户裁决（不是 LLM 反思）—— 见设计方案 §6.3 `optional human()` 确认点
