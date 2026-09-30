# Planning Schemas（规划模块对外接口）

> 本文件定义规划模块（模块二）所有**对外接口**的字段契约。
>
> - **14 个对外接口**：8 个上游输入（来自模块一 / 用户配置）+ 1 个外部输入（模块三 PlanningFeedback，用于 REPLAN 重规划）+ 5 个下游输出（ExecutionConfig / MethodDesign / MethodReview / ExperimentPlan / DataPlan，给模块三 / 模块四 / 内部反思循环）。
> - **4 个内部 sub-agent 接口**（method_draft / method_review / method_final / experiment_final）见 [references/reference.md](reference.md)。
> - **§7 功能函数**（11 个，模块二内部工具）本次不列。
>
> 设计依据：设计方案 §3 输入分析、§4 输出分析、§6.3 反思循环机制、§8 Pydantic 模型。

---

## 0. 通用约定

- 枚举值一律大写（如 `PASS` / `REVISE` / `available`）；未知值视为不合法。
- 列表字段必含至少一条有效项，不允许空列表。
- 缺失值使用 `null`；不得使用"无""未知"等字符串冒充结构化缺失值。
- 每个列表项必带 `id` 或 `name`，供其他表交叉引用。
- 必填项缺一即视为该接口不合法（load_inputs 失败 / write_outputs 拒绝）。

---

## 1. 输入侧（模块一 / 用户配置 → 模块二）

### 1.1 ResearchQuestion（主研究问题）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `topic` | str | ✅ | 主题，一句话、动词化（如"用一个轻量化 X 实现 Y"） |
| `scope` | str | ✅ | 边界：在什么范围内做、什么不做 |
| `success_criteria` | str | ✅ | 成功标准：方法跑通 + 假设被验证 |

来源：模块一 TopicDecomposer。

### 1.2 Hypothesis（假设，`hypotheses[]` 的元素）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | 假设唯一 id，MethodDesign.hypothesis_coverage 引用 |
| `claim` | str | ✅ | 假设陈述 |
| `verifiable` | bool | ✅ | 是否可被实验验证——不可验证者要在规划时改写或删 |

来源：模块一 TopicDecomposer。

### 1.3 GapReport（研究空白）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `research_question` | str | ✅ | 衔接 ResearchQuestion |
| `existing_state` | str | ✅ | 现有方法能做到什么 |
| `missing_capability` | str | ✅ | ★ 现有方法缺什么能力——方法设计的入手点 |
| `opportunities` | list[str] | ⬜ | 候选方向 |

来源：模块一 GapSynthesizer。

### 1.4 PaperRef（关键论文，`key_papers[]` 的元素）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | 论文唯一 id（如 arXiv id） |
| `title` | str | ✅ | 标题 |
| `method_key` | str | ✅ | ★ 核心方法标签——借鉴/查重用 |
| `is_baseline` | bool | ⬜ | 是否作为基线对照 |

来源：模块一 LiteratureScout。

### 1.4.1 Reference（完整参考文献，`references[]` 的元素）

最新版模块一固定交付 10 篇可核验参考文献。模块二将其作为方法溯源、基线候选和
后续写作的补充证据；`key_papers[]` 仍是方法设计优先消费的精简子集。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `id` | str | ✅ | 稳定论文 id，任务内唯一 |
| `title` | str | ✅ | API 核验标题 |
| `authors` | list[str] | ✅ | 作者顺序与来源一致 |
| `year` | int | ✅ | 发表或首次公开年份 |
| `venue` | str | ⬜ | 期刊、会议或预印本平台 |
| `source` | enum | ✅ | `arxiv` / `semantic_scholar` / `openalex` / `crossref` |
| `url` | str | ✅ | 可核验详情页 |
| `citation_text` | str | ✅ | 可读参考文献条目 |
| `relevance` | str | ✅ | 与问题、空白或假设的具体关系 |
| `related_hypothesis_ids` | list[str] | ⬜ | 必须引用已有 `hypotheses[].id` |

兼容约定：历史缓存没有 `references.json` 时，`load_inputs.py` 允许使用
`key_papers.json` 继续规划；新运行必须由 paper-gen 适配层写出该文件。

### 1.5 ResourceConstraints（资源约束）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `gpu_type` | str / null | ⬜ | GPU 类型（如 A100 / 3090），未指定按默认 |
| `gpu_hours` | int | ✅ | 可用 GPU 小时数，**必须 ≥ 0** |
| `memory_gb` | int | ✅ | 显存 GB，**必须 > 0** |
| `budget` | float / null | ⬜ | 预算（与 compute_estimate 同单位），**必须 ≥ 0**；`null` 表示不设算力门禁（TIER_FULL 恒成立） |
| `time_budget_days` | int | ✅ | 总时间预算（天），**必须 > 0** |

来源：用户 / 配置文件（不是模块一 Agent 产出）。

**强校验规则**（与 [Conception Schema §1.3](../../conception/references/conception-schemas.md) 对齐）：
1. `gpu_hours ≥ 0`——负数视为不合法（用户多填了一位或漏写单位）
2. `memory_gb > 0`——0 / 负数视为不合法（无显存无法运行）
3. `time_budget_days > 0`——0 / 负数视为不合法（0 工期无意义）
4. `budget ≥ 0` 或 `null`——负数视为不合法；`null` 时 `check_feasibility_and_downgrade.judge_tier` 走"无算力门禁"分支（TIER_FULL 恒成立，不触发降级）
5. `gpu_type` 为 `str` 或 `null`——非字符串视为不合法

### 1.6 ResearchFrontier（研究前沿）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `frontier_text` | str | ✅ | 前沿描述（一段话，**弱格式**）—— 给 Architect 提供"前沿在干什么"的画面 |

来源：模块一 LiteratureScout。
注：弱格式，load_inputs.py 只检查文件存在 / 字段非空，**不校验内容**。

> ⚠️ **字段待确认**：设计方案 §8 Pydantic 未显式列 ResearchFrontier 实体，本字段为推断，待用户确认。

### 1.7 Domain（领域标签）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `domain_name` | str | ✅ | 领域名（如 `nlp` / `cv` / `rl` / `agent`）—— 确定研究领域；原样透传自 [Conception Schema §1.2](../../conception/references/conception-schemas.md)，模块二不重写 |

来源：用户 / 配置文件（构思模块透传）。

**实际消费点**（截至当前代码实现）：
- 唯一硬消费：`method-designer.md:100` 的 LLM prompt 占位符 `domain: {DOMAIN}`——Architect 据此按领域挑方法范式（nlp → Transformer 类；cv → CNN 类；rl → policy gradient 类；agent → ReAct / Reflexion 类）。
- 透传链：Domain 字段进入 Module 1 产出包后，由 `load_inputs.py` 读出并随 `payloads["domain"]` 透传给 MethodArchitect 的 prompt 拼装；`iterate_method_design.py` 在 REPLAN 路径上再次透传。

**未实现（明确不假装已做）**：
- ❌ 按 `domain_name` 过滤 dataset / baseline / metric 候选集——目前**没有**任何代码按 domain 收窄候选；LLM 自己在 prompt 里自觉选用领域典型数据集/基线，不做代码层强制。
- ❌ 模块三 ExperimentExecutor 按领域选 dataset/baseline——正式 [Experiment Schema](../../experiment/references/experiment-schemas.md) 当前**未消费** `domain_name`，按领域切分由 prompt 自驱。

---

## 1.5 外部输入（模块三 → 模块二）

本节定义模块二除模块一输入外的外部输入：REPLAN 反馈（驱动 MethodDesign / ExperimentPlan / DataPlan 的迭代重写）。ExecutionConfig 不在此处——它是模块二的**输出**（见 §2.5），由模块二在规划阶段决定运行/结果目录、种子集合等，再交付给模块三执行。

### 1.5.1 PlanningFeedback（重规划反馈，模块三 → 模块二）

> 来源：模块三 ExperimentExecutor 在 `status = REPLAN` 时返回（见 [Experiment Schema §3.4](../../experiment/references/experiment-schemas.md)）。
> 用途：模块二在 REPLAN 路径上消费 PlanningFeedback，针对 `affected_experiment_ids` 重写 MethodDesign / ExperimentPlan / DataPlan，回到 stage 1 / stage 2 / stage 3 起点重新跑。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `reason` | str | ✅ | 为什么不能按原计划执行（"数据不可访问 / 显存不足 / 基线无可信实现 / 指标歧义未消解"等） |
| `affected_experiment_ids` | list[str] | ✅ | ★ 受影响的实验 ID 列表（与 `ExperimentPlan.primary_experiments` / `experiment_matrix` 首列对齐）；模块二仅需重写这些 ID 对应的部分 |
| `blockers` | list[str] | ✅ | 阻断项，每条形如 `[category] description`，`category ∈ {data, compute, baseline, method, metric, schema, experiment, budget}`；模块二据此决定是降档（算力类）还是回到 stage 1（方法/基线类）还是 REPLAN_DATA（数据类） |
| `suggested_changes` | list[str] | ✅ | 给模块二的修改建议（**指出问题，不替模块二改**）；模块二逐条评估后选择性采纳，写入 MethodReview.feedback 形成下一轮反思 |

**强校验规则**：
1. `affected_experiment_ids` 非空——`status = REPLAN` 必须有具体受影响的实验，否则视为 `FAILED`
2. `blockers` 至少 1 条——空 blockers 不得触发 REPLAN
3. `suggested_changes` 至少 1 条——模块三不得只报问题不给建议
4. `affected_experiment_ids` 中的每个 ID 必须能在上一轮 `ExperimentPlan.experiment_matrix` 首列找到

**消费方**：
- 模块二 `iterate_method_design.py`：若 `blockers` 含 `baseline` / `method` / `metric` / `experiment` 类，回到 stage 1 让 Architect 重写 MethodDesign
- 模块二 `plan_experiment_with_data.py`：若 `blockers` 含 `data` / `experiment` 类，触发 REPLAN_DATA 路径或 stage 2 重规划。
  ⚠️ **REPLAN_DATA 的语义（2026-09-16 修正）**：**不**复用上一轮 `experiment_plan`，而是**把本 feedback 喂回
  experiment-planner 重新生成**——payload 携带 `previous_feedback`（blockers / suggested_changes 逐条原文）、
  `previous_experiment_plan`（仅作参考）、`forbidden_source_urls`（从 blockers 抽出的实跑失败 URL 黑名单）、
  `resolved_download_hints`（可选，模块三已验证候选直链）。生成后照常走数据源自修复循环（≤3 次）与
  Layer A 硬门禁，critic 限 1 轮。
  历史 bug：该分支曾 `dict(prev_experiment_plan)` 逐字复用 + `skip_reflection=True`，跳过 planner 与全部门禁，
  导致数据源 URL 一字不变、同样的 blocker 必然复现，3 轮 REPLAN 全部白烧（终态 `waiting_for_experiment_replan`）。
  仅当调用方**未传** `planning_feedback` 时才保留逐字复用语义（兼容旧调用方）。
- 模块二 `check_feasibility_and_downgrade.py`：若 `blockers` 含 `compute` / `budget` 类，触发三档降级而非完全重规划

**重规划回路**（与 §3 评审门禁联动）：
```
stage 3 出口
  ↓
ExperimentExecutor.status = REPLAN
  ↓
PlanningFeedback（§1.5.2）
  ↓ 按 blockers 分类路由
  ├─ data / experiment → REPLAN_DATA → stage 2 起点（planner **带本 feedback 重新生成** DataPlan / ExperimentPlan，
  │                     禁止复用上一轮 source_url）
  ├─ compute / budget → 降档（TIER_TRIM / TIER_CORE）→ 仍在 stage 3 内
  ├─ baseline / method / metric → stage 1 起点（重出 MethodDesign）
  └─ schema → 报错退出（设计契约不一致，需人工介入）
  ↓
下一轮 MethodReview（§2.2）→ 若仍不通过 → 再来一轮
  ↓ 若通过
ExperimentPlan / DataPlan 重新下发
```

---

## 2. 输出侧（模块二 → 模块三 / 模块四）

### 2.1 MethodDesign（方法设计）

> ⚠️ **富字段 vs 模块三扁平形状（2026-09-09 澄清）**
> 本节记录的是**模块二自己的产物形状（富字段）**，模块四 Writer 消费这一版。
> 模块三 `contracts.py` 要的是扁平版（`framework` 是 str、`technical_route` 是 str、
> `algorithm_reference` 是 list[str]、`objectives` 是 list[str]），由 paper-gen 的
> **投影层**（`paper-gen/scripts/_experiment_bootstrap.py`）负责压平——**模块二不要自己压平**。
> 原因：富字段是资产而非漂移，§5 的确定性交叉校验有一半靠它们才做得成
> （如 `objectives[].experiment_ids` 压成 str 后，逐假设的实验绑定就无法校验）。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `research_goal` | str | ✅ | 一句话目标（与 research_question 呼应收敛） |
| `core_mechanism` | str | ✅ | 核心机制，一句话讲清（"用一个轻量化 X 实现 Y"） |
| `framework` | dict | ✅ | `{name, overview, stages}`；`name` 非空。投影层压平为 `"name: overview"` |
| `components` | list[Component] | ✅ | **顶层字段**（不在 `framework` 内），每块可独立评估（见 §2.1.1） |
| `technical_route` | list[dict] | ✅ | `[{step, duration_hours}]`；投影层压平为编号单串。⚠️ `duration_hours` 是**人工开发工时**，与 `ResourceConstraints.gpu_hours`（GPU 机时）不同量纲，不得相加 |
| `algorithm_reference` | list[dict] | ⬜ | `[{paper_id, algorithm, adaptation}]`；`paper_id` 必须 ∈ 上游 `key_papers[].id`（防重复造轮子）。投影层压平为 list[str] |
| `innovation_points` | list[InnovationPoint] | ✅ | ★ 创新点，每条连到实验证据（见 §2.1.2） |
| `hypothesis_coverage` | list[HypothesisCoverage] | ✅ | 每条假设↔方法机制↔实验核对（见 §2.1.3） |
| `limitations` | list[str] | ✅ | 适用边界与已知局限。⚠️ 不得与 `ExperimentPlan.success_criteria` 互相否定（自认做不到的事不能被列为必达项） |
| `implementable` | bool | ✅ | 可否落成代码（可行性门禁判据）。⚠️ 存在 `severity: high` 的可行性风险时不得为 `true` |

#### 2.1.1 Component（框架组件）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 组件名（如 `feature_aligner`） |
| `function` | str | ✅ | 一句话职责 |
| `input_schema` | str | ✅ | 入参 |
| `output_schema` | str | ✅ | 出参 |
| `novelty_degree` | enum | ✅ | `novel` / `adapted` / `standard` |

#### 2.1.2 InnovationPoint（创新点）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `claim` | str | ✅ | 创新点陈述 |
| `evidence_metric` | list[EvidenceMetric] | ✅ | ★ 结构化判据，**至少 1 条**（见 §2.1.4）。2026-09-09 由自由字符串改为结构化 |
| `experiment_ref` | str | ✅ | ★ 对应实验（与 ExperimentPlan.primary_experiments / experiment_matrix 对应） |

#### 2.1.3 HypothesisCoverage（假设覆盖）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `hypothesis_id` | str | ✅ | 对应上游 Hypothesis.id |
| `mechanism` | str | ✅ | 承接机制，**必须逐字符包含至少一个 `components[].name`**（否则消融方案无法与组件对齐，见 §5） |
| `experiment_ref` | str | ✅ | 对应实验 |
| `evidence_metric` | list[EvidenceMetric] | ⬜ | 结构化判据（见 §2.1.4）。模块三 `HypothesisCoverage` 无此字段，投影层会裁掉；保留供模块四 Writer 与 critic 使用 |

#### 2.1.4 EvidenceMetric（结构化指标判据，2026-09-09 新增）

**为什么结构化**：原本是自由字符串（`"temporal_qa_accuracy_delta >= -1% relative; compression_ratio >= 5x"`），
带来三个问题：① 一条串塞多个判据，投影层只能挑一个、静默丢掉其余；
② 派生后缀（`_delta` / `_improvement` / `_stability`）让"指标名是否在 `metrics[]` 里"无法机械判断；
③ 阈值与方向混在文本里，无法与 `success_criteria` 比对。
结构化后 ①②③ 全部由**确定性代码**校验，不再依赖语义判断。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `metric_name` | str | ✅ | ★ **逐字符 ∈ `ExperimentPlan.metrics[]`**。禁止派生后缀（`_delta` / `_improvement` / `_stability` / `_gain`）——方向与基准由下面字段表达 |
| `comparison_mode` | enum | ✅ | `absolute`（指标原值）/ `delta`（相对 `vs` 的变化量） |
| `comparator` | enum | ✅ | `>=` / `>` / `<=` / `<` |
| `threshold` | float | ✅ | 阈值数值；`delta` 模式下可为负（表示"容许下降不超过"） |
| `unit` | enum | ✅ | `ratio`（0-1 小数）/ `percent`（百分点）/ `points`（judge 分）/ `times`（倍数，如 5x）/ `count`（计数） |
| `vs` | str | 条件 | `comparison_mode=delta` 时**必填**；逐字符 ∈ `baselines[].name` ∪ `ablation_plan[].component` |

**写法示例**（左边是改造前的旧写法，右边是结构化后）：

```
"temporal_qa_accuracy_delta >= -1% relative"
  → {"metric_name": "temporal_qa_accuracy", "comparison_mode": "delta",
     "comparator": ">=", "threshold": -1, "unit": "percent",
     "vs": "Uncompressed baseline"}

"compression_ratio >= 5x"
  → {"metric_name": "compression_ratio", "comparison_mode": "absolute",
     "comparator": ">=", "threshold": 5, "unit": "times"}

"qa_accuracy_improvement >= 2% absolute vs single-level compression"
  → {"metric_name": "qa_accuracy", "comparison_mode": "delta",
     "comparator": ">=", "threshold": 2, "unit": "percent",
     "vs": "Gist-based compression (uniform)"}
```

⚠️ **注意 `compression_ratio_stability >= 0.9` 这类写不出来是刻意的**：
`compression_ratio_stability` 不在 `metrics[]` 里，说明该创新点声称的判据**没有任何实验会去测**。
遇到这种情况必须二选一：把该指标补进 `ExperimentPlan.metrics[]` 并安排实验，或改用已有指标。
**不允许保留一个无法被验证的创新点判据。**

**投影层降级规则**（模块三 `InnovationPoint.evidence_metric` 是单个 str 且必须 ∈ `metrics`）：
取 `evidence_metric[0].metric_name`；若有多条判据，其余条目**记 warn 列出**（模块三字段装不下，
但结构化原文保留在 stage2 产物里供模块四使用），不再静默丢弃。

**发给**：模块三 ExperimentExecutor（执行依据）+ 模块四 Writer（Method 章节素材）。

### 2.2 MethodReview（方法评审，反思循环判据）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `novelty_ok` | bool | ✅ | 整体新颖性是否通过 |
| `overlap_papers` | list[str] | ⬜ | 撞车论文清单（来源：`search_methods_gap`，可降级） |
| `feasibility_risks` | list[str] | ✅ | 可行性风险清单 |
| `missing_components` | list[str] | ⬜ | 缺失组件 |
| `adequacy_score` | float | ✅ | 0-10 总评 |
| `feedback` | str | ✅ | 给 Architect 的重写意见（**指出问题，不替 Architect 改**） |
| `passed` | bool | ✅ | 通过判据（`adequacy_score ≥ 7.0` 且无 blocker 风险） |

**用途**：模块二内部反思循环判据（**不进模块三/四**）。

### 2.3 ExperimentPlan（实验方案）

> ⚠️ 同 §2.1：本节是**模块二富字段形状**，模块三扁平版由投影层压平。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `objectives` | list[dict] | ✅ | `[{hypothesis_id, experiment_ids, description}]`；与 hypotheses 一一对应。⚠️ **`experiment_ids` 必须与 `method_design.hypothesis_coverage[].experiment_ref` 在同一 `hypothesis_id` 下一致**（见 §5）。投影层压平为 list[str]（取 `description`） |
| `datasets` | list[DatasetSpec] | ✅ | 数据集（见 §2.3.1） |
| `baselines` | list[BaselineSpec] | ✅ | 基线（见 §2.3.2） |
| `metrics` | list[str] | ✅ | ★ **指标名词表**；`method_design` 所有 `EvidenceMetric.metric_name` 必须逐字符命中本表（见 §2.1.4） |
| `primary_experiments` | list[str] | ✅ | 主实验 id 列表（**字符串数组**，不是对象数组） |
| `experiment_matrix` | list[list[str]] | ✅ | 实验矩阵（模块三标准行格式，见 §2.3.4） |
| `ablation_plan` | list[Ablation] | ✅ | 消融方案（见 §2.3.3） |
| `expected_results` | list[str] | ✅ | 反直觉点 / 预期（供 Reviewer 后验） |
| `success_criteria` | list[str] | ✅ | ★ 可量化（含阈值，如"比基线高 ≥ X"）。⚠️ 同一指标的阈值方向不得与 `EvidenceMetric` 矛盾，且不得偷换指标（如创新点写 `retrieval_precision@5`、这里写 `retrieval_recall@5`） |
| `compute_estimate` | float | ✅ | 算力估算（GPU 机时，与 `ResourceConstraints.gpu_hours` 比对；**不要把 `technical_route[].duration_hours` 的人工工时算进来**） |
| `feasibility_risks` | list[dict] | ⬜ | `[{risk, severity, mitigation}]`，`severity` ∈ `high`/`medium`/`low`。⚠️ 有 `high` 时 `method_design.implementable` 不得为 `true`。模块三不收此字段，投影层裁掉 |
| `notes` | str | ⬜ | 备注。⚠️ 这里声明的 seed 数量必须与 `ExecutionConfig.seeds` 一致（见 §5） |

#### 2.3.1 DatasetSpec（数据集）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 数据集名 |
| `source_url` | str | ✅ | ★ 真实可下载 URL（防虚构数据）。必须是**数据文件直链**（HF `datasets/<id>/resolve/<rev>/<file>`、`raw.githubusercontent.com/...`、GitHub `releases/download/...`、Zenodo 文件页）；论文页、`github.com/<org>/<repo>` 裸仓库主页、`/blob` `/tree` 浏览页、以 `/` 结尾的目录页一律不合格——模块三 `dataset_resolver._is_dataset_landing_page` 会判 NOT_FOUND。模块二 stage 2 门禁 1b 用同一套规则提前拦截 |
| `scale_estimate` | str | ✅ | 规模估算（条数 / 体积） |
| `license` | str | ⬜ | 许可（影响能否商用/复现） |
| `readiness` | enum | ✅ | `available` / `download` / `apply` |
| `preprocess_required` | list[str] | ⬜ | 预处理项 |
| `usage` | str | ⬜ | 2026-08-29 v2：单条数据用途（"训练集 / 测试集 / 检索语料"）；DataPlan.usage_plan 由所有 datasets[].usage 拼接 |

#### 2.3.2 BaselineSpec（基线）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `name` | str | ✅ | 基线方法名。**全局唯一**：同名基线不得登记两条（哪怕 `metric_name` 不同）——模块三 `ExperimentPlan.identifiers_are_unique` 会 fail-closed 拒收（`baselines names must be unique`），投影层 invalid → 顶层 `aborted_experiment_replan`，且该路径不产生 `planning_feedback`。确需分列时改名，**并同步 `experiment_matrix` 中引用该名的每一行** |
| `paper_id` | str | ✅ | 真实来源（arXiv id / 标题） |
| `metric_name` | str | ✅ | 该基线报的核心指标名；必须是 `ExperimentPlan.metrics[]` 的成员（模块三硬校验） |

#### 2.3.3 Ablation（消融）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `component` | str | ✅ | 消融哪个组件（对齐 `Component.name`） |
| `removed_by` | str | ✅ | ★ **实验矩阵里的实现名**（矩阵第3列起不含 `=` 的 token），逐字符一致。**不是**「怎么去掉」的自然语言描述——模块三 `implementation_builder` 对此 fail-closed 拒收（「未精确登记为实验矩阵实现」，类别 `[experiment]`），且它不会从自然语言猜消融代码。组件名也不算实现名（矩阵里是 `ablation=<component>` 参数，含 `=`，会被跳过）。自然语言描述请写进 `expected_impact`。契约源：模块三 `plan_execution.planned_method_names` |
| `expected_impact` | str | ⬜ | 去掉后期望看到什么；「怎么去掉」的自然语言描述也放这里 |

#### 2.3.4 experiment_matrix 行格式（模块三标准，2026-09-09 对齐）

每行格式：`[experiment_id, dataset_name, method_or_baseline, ..., key=value, ...]`

**硬规则**（模块三 `contracts.py` / [Experiment Schema §2.2.2](../../experiment/references/experiment-schemas.md) 强制校验，违反即 REPLAN）：

1. 每行 **≥ 4 列**，且前 4 列非空字符串。
2. **首列 `experiment_id`**：主实验行必须取 `primary_experiments` 已声明的 id（每个 primary id 至少在首列出现一次）；消融行用约定 id `EXP-ABL-{COMPONENT}`（COMPONENT 取 `ablation_plan[].component` 的大写形式，空格/连字符转下划线）。
3. **第二列 `dataset_name`**：必须与 `datasets[].name` **逐字符一致**（禁止 `DS1` 这类未声明别名）。
4. **第三列起为实现名**：`baselines[].name` 或本方法名（`method_design.framework` 的名称）的逐字符一致字符串；**每个 baseline 至少在矩阵中出现一次**。同一 `(experiment_id, dataset)` 下的多个对照实现推荐聚合成一行（共享行尾参数），也可每个实现单独一行。
5. **行尾可附 `key=value` 参数**（如 `seed=42` / `lr=1e-4` / `epochs=3`），同一行内 key 唯一且非空；不含 `=` 的 token 一律被模块三视为实现名，不得写自由描述（如 `none` / `full framework`）。
6. 消融行：第三列写**基础方法名**，组件差异用 `ablation=<component_name>` 参数表达（与 `ablation_plan[].component` 精确一致），不得把消融描述当实现名。
7. `method_design.innovation_points[].experiment_ref` 与 `hypothesis_coverage[].experiment_ref` 必须能在 `primary_experiments ∪ 矩阵首列` 中找到。

示例：

```json
"experiment_matrix": [
  ["EXP-H1-ENDTOEND", "LoCoMo", "Uncompressed baseline (full context)", "SHC-PER", "seed=42"],
  ["EXP-H1-ENDTOEND", "LongMemEval", "Gist-based compression (uniform)", "SHC-PER", "seed=42"],
  ["EXP-ABL-TEMPORAL", "LoCoMo", "SHC-PER", "ablation=temporal_preservation", "seed=42"]
]
```

**发给**：模块三 ExperimentExecutor（执行）+ 模块四 Writer（Experiments 章节素材）。

### 2.4 DataPlan（数据订单）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `datasets` | list[DatasetSpec] | ✅ | 数据集（与 ExperimentPlan.datasets 同集合） |
| `usage_plan` | str | ✅ | ★ **整体用法**（"拿到数据后怎么用" 1-3 句话；可由 `datasets[].usage` 拼接推导） |
| `split_strategy` | dict | ⬜ | 切分策略：`{method, train, val, test, seed, kwargs}`，`method` 取值：`ratio_8_1_1` / `by_file` / `by_domain` / `by_id` / `temporal`；**仅 train/val/test 类研究需要** |
| `preprocessing_pipeline` | list[str] | ⬜ | 预处理步骤：`"清洗"` / `"归一化"` / `"tokenize"` / `"去重"` / `"采样"` 等；**仅需要预处理时** |
| `expected_size` | dict | ⬜ | 规模：`{rows, disk_gb, gpu_estimate}`；**仅需要算力/磁盘估算时** |

**强校验规则**：
1. `datasets[].source_url` 必须非空（防虚构数据）
2. `scale_estimate` 必须合理（过大且算力不足 → 触发减档）
3. `readiness` 标注「直接可用/需下载/需申请」，实验模块据此决定动作
4. `split_strategy` 若存在，必须给出 `seed`（保证可复现）— 2026-08-29 改为「若存在」而非「必须」

**设计动机（2026-08-29 v2 重构）**：
- 旧版把 3 个 NLP 专属字段（`split_strategy` / `preprocessing_pipeline` / `expected_size`）标成必填，对非 NLP 方向（理论 CS / simulation / 调 API / 跨域）都是噪音
- 实测 3 scenario：`preprocessing_pipeline` 全空、`expected_size.rows: 0` 全错、`split_strategy` 是 hardcoded default（不是 LLM 推的）
- v2：3 字段降为可选；新增 `usage_plan` 必填承载「数据怎么用」语义

**发给**：模块三 DataPreparer（执行数据订单）。

### 2.5 ExecutionConfig（执行配置，模块二 → 模块三 / 编排层）

> 来源：模块二在 stage 3 末尾由 `check_feasibility_and_downgrade.py` 汇总产出；初值可由编排层 / 用户配置文件在 stage 0 注入（用户仅给"默认值"——run_dir 模板、seeds 上限、dry_run 开关等，模块二据此决定最终值）。
> 用途：模块二在规划阶段**自己决定** ExecutionConfig 的最终值（运行/结果目录、种子集合、超时、重试、dry_run），下发给模块三执行；模块三在执行时**只读消费**，不得修改。

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `run_dir` | str | ✅ | ★ **运行目录**：本次执行的代码、中间产物、日志、临时文件；模块三执行时 cwd 与所有写产物都应落在此目录或子目录 |
| `result_dir` | str | ✅ | ★ **结果目录**：本次执行的最终交付产物（指标 CSV、表格、图片、复现包），相对 `run_dir` 的路径或绝对路径均可；与 `run_dir` 解耦，便于「运行产物 vs 交付产物」分层管理 |
| `seeds` | list[int] | ✅ | 实验随机种子集合；至少 1 个，多 seed 决定 `expected_size.gpu_estimate` 是否需要 ×N |
| `max_retries` | int | ✅ | 单实验最大重试次数，必须 `>= 0`；模块二据此在 compute_estimate 上预留重试余量 |
| `timeout_seconds` | int / null | ⬜ | 单实验超时（秒）；`null` 表示由执行器决定；模块二在 ExperimentPlan 中可显式标注每个 `primary_experiments` 的预计时长 |
| `dry_run` | bool | ✅ | 为 `true` 时模块三只检查环境与命令、不生成科研结论；模块二此时仍正常出 MethodDesign / ExperimentPlan / DataPlan，但 `success_criteria` 可降级为"环境就绪" |

**与 [Experiment Schema §2.5](../../experiment/references/experiment-schemas.md) 的关系**：
- 模块三视角的 ExecutionConfig **不**含 `result_dir`（实验侧约定所有产物都在 `run_dir` 及其子目录下）。
- 模块二视角显式拆出 `result_dir` 字段，便于在规划阶段就明确"运行产物 vs 交付产物"分层。模块三在读取 ExecutionConfig 时应将 `result_dir` 视作 `run_dir` 下的子目录约定（如 `run_dir/results/`），不得视为任意位置。
- 其他字段（`seeds` / `max_retries` / `timeout_seconds` / `dry_run`）两边语义一致。
- 模块三侧可以**新增**额外字段（如 `entry_command` 模板），但 `run_dir` / `seeds` / `max_retries` / `timeout_seconds` / `dry_run` 的语义必须与本契约一致。

**强校验规则**：
1. `run_dir` 必须非空且模块二进程对其有写权限（preflight 检查）
2. `result_dir` 必须非空；如为 `run_dir` 的相对路径，模块三应解析为 `run_dir/result_dir`
3. `seeds` 至少包含 1 个整数
4. `max_retries >= 0`
5. `dry_run=true` 时 `success_criteria` 中不得含"提升 ≥ X%"等量化阈值（仅能含"环境就绪 / 命令可执行"等就绪类条件）
6. `result_dir` 解析后必须位于 `run_dir` 内或其子目录（防止模块三把交付产物写到 `run_dir` 之外）

**生产方**：
- 模块二 `check_feasibility_and_downgrade.py`（stage 3 末尾）：根据 ResourceConstraints.gpu_hours + 用户提供的 seeds 模板 + 默认超时策略产出最终 ExecutionConfig，写入 `<output-dir>/execution_config.json` + `execution_config.md`
- 模块二 `main.py`（preflight 阶段）：若用户未提供 run_dir，按 `<output-dir>/run` 兜底；若用户未提供 result_dir，按 `<output-dir>/results` 兜底

**消费方**：
- 模块二 `estimate_compute.py`：将 `seeds` 数量 × 单 seed GPU 小时 + 重试余量得到 `compute_estimate`
- 模块二 `plan_experiment_with_data.py`：根据 `result_dir` 决定 DataPlan 中 `expected_size` 的"交付体积"分项
- 模块三 `ExperimentExecutor`：执行时遵守全部字段

**发给**：模块三 ExperimentExecutor（执行依据）+ 编排层（产物登记）。

---

## 3. 评审门禁常量

| 常量 | 值 | 说明 |
|---|---|---|
| `ADEQUACY_PASS` | 7.0 | MethodReview.adequacy_score 通过阈值 |
| `BLOCKER_FORBIDDEN` | `blocker` | 存在该严重度风险即不可 passed |
| `MAX_METHOD_ROUNDS` | 3 | 方法反思循环最大轮数 |

---

## 4. 算力门禁（三档减方案）

| 档位 | 触发 | 动作 |
|---|---|---|
| 0 精算 | `total ≤ budget` | 全量实验 |
| 1 消减 | 超 ≤ 25% | 砍非核心消融，保留主实验 + 核心创新点验证 |
| 2 核心优先 | 超 > 25% | 只保留回答核心创新点的最小实验集，其余标为可选 |

---

## 5. 一致性约束

### 5.1 跨产物交叉引用（2026-09-09 新增，`validate_cross_refs()` 确定性强制）

> 这些约束的**共同特征**：`method_design` 与 `experiment_plan` / `data_plan` 各自独立声明同一件事，
> 结构上不共享，因此必然可能漂移。根因是流水线顺序——method-designer 跑在 experiment-planner **之前**，
> 写 `experiment_ref` 时那批实验 id 还不存在。
> 全部规则**照模块三 `experiment/scripts/contracts.py::validate_cross_references` 镜像**
> （契约知识零复制，同事契约演进自动跟随）。违反 = 模块三 fail-closed 拒收整个请求。

| # | 规则 | 一致性要求 |
|---|---|---|
| X1 | `method_design.innovation_points[].experiment_ref` ∈ `primary_experiments ∪ 矩阵首列` | 集合成员 |
| X2 | `method_design.hypothesis_coverage[].experiment_ref` 同上 | 集合成员 |
| X3 | 按 `hypothesis_id` join：`hypothesis_coverage[].experiment_ref` ∈ 对应 `objectives[].experiment_ids` | join + 成员 |
| X4 | `hypothesis_coverage[].hypothesis_id`、`objectives[].hypothesis_id`、上游 `hypotheses[].id` 三方集合相等 | 集合相等 |
| X5 | `framework.name` 逐字符出现在矩阵实现名 token 中 | 字符串相等 |
| X6 | `ablation_plan[].component` ⊆ `components[].name` | 集合包含 |
| X7 | 矩阵消融行用 `EXP-ABL-*` 首列 + `ablation=<component>` 参数，值 ∈ `components[].name` | 字符串相等 |
| X8 | `baselines[].paper_id` ⊆ 上游 `key_papers[].id`；`algorithm_reference[].paper_id` ⊆ `key_papers[].id ∪ baselines[].paper_id`（无上游 `key_papers` 时整条跳过——没白名单判"编造"必然假阳） | 集合包含 |
| X9 | 正文内联引文（`[\d{4}\.\d{4,5}]`）⊆ 已声明论文 id（防编造引文） | 正则 + 集合 |
| X10 | `experiment_plan.datasets` == `data_plan.datasets`；`expected_size.gpu_estimate` == `compute_estimate`；`expected_size.rows > 0` iff datasets 非空 | dict 相等 / 数值 |
| X11 | 矩阵与 `success_criteria` 中的数据集标识逐字符 ∈ `datasets[].name`（**禁止 `DS1` 这类别名**）；`notes` 声明的 seed 数量 == `len(ExecutionConfig.seeds)` | 字符串 / 计数 |
| X12 | 所有 `EvidenceMetric.metric_name` 逐字符 ∈ `experiment_plan.metrics`（见 §2.1.4） | 集合成员 |

**前置自检（重要）**：做 X5 / X7 / X11 这类**按列号**的校验前，必须先确认矩阵列语义正确
——首列能在 `primary_experiments ∪ EXP-ABL-*` 命中。否则整块按列号校验降级为 warning。
理由：列语义整体错位时（如首列是数据集别名而非 experiment_id），按列号校验会报出
**"错误正确但归因错误"**的信息，比不报错更难排查。

### 5.2 只能靠语义判断的约束（交 experiment-critic，代码写规则必然假阳/假阴）

| # | 规则 |
|---|---|
| S1 | 方法组件的 `input_schema` 要求的数据形态 vs 所选数据集**实际**提供的形态（需外部世界知识，如某基准有没有 timestamp） |
| S2 | 组件隐含的预处理需求 vs `datasets[].preprocess_required` / `DataPlan.preprocessing_pipeline` |
| S3 | `hypothesis_coverage[].mechanism`（散文）→ `components[].name`（标识符）的对齐——**这条是 X6/X7 的上游根因，优先检查** |
| S4 | `claim` 里"vs 某方法"这类自然语言对照物 → 具体 `baselines[].name` / 消融变体的映射 |
| S5 | `limitations` 与 `success_criteria` 的软矛盾（自认可能做不到的事被列为必达项） |
| S6 | 阈值量纲与基数自洽（如基线 0.55 judge accuracy + 0.3 points = 0.85 是否可信） |
| S7 | `technical_route[].duration_hours`（人工工时）与 `compute_estimate`（GPU 机时）的口径是否被混算 |
| S8 | **同量纲偷换指标**：创新点写 `retrieval_precision@5`、`success_criteria` 写 `retrieval_recall@5`——两者都在 `metrics` 里，任何词表校验都会放行，只有理解语义才抓得住 |

### 5.3 单产物内部约束

- **MethodDesign.hypothesis_coverage[].hypothesis_id** 必须覆盖上游 `hypotheses[].id` 全集（无未承接假设）
- **ExperimentPlan.datasets** 与 **DataPlan.datasets** 应为同一集合（DataPlan 由 ExperimentPlan 确定性抽取，天然一致）
- **ExperimentPlan 标识符唯一性（2026-09-17 新增，stage 2 门禁 1c 强制）**：`datasets[].name` / `baselines[].name` / `metrics[]` / `primary_experiments[]` 各自内部不得有重复项，且每个 `baselines[].metric_name` ∈ `metrics[]`——逐条镜像模块三 `ExperimentPlan.identifiers_are_unique`（pydantic `model_validator`）。违反时模块三**还没开跑**就在投影层 raise → `projection_report.status=invalid` → 顶层 `aborted_experiment_replan`，**不产生 `planning_feedback`**，planner 没有自愈机会，所以必须由门禁在 Layer A 拦住并退回 planner 重写
- **ExperimentPlan.success_criteria** 全部可量化（含阈值）
- **DataPlan.datasets[].source_url** 必须非空
- **DataPlan.split_strategy.seed** 必须存在（保证可复现）；⚠️ 若 `method_design` 依赖验证集（`technical_route` / `components[].input_schema` 提到 validation / development set），`split_strategy` **不得缺失**
- **ExecutionConfig.run_dir / result_dir** 必须非空，且 `result_dir` 解析后必须位于 `run_dir` 内或其子目录
- **ExecutionConfig 是模块二的强制输出**：每次 stage 3 完成（无论全量 / 降档 / REPLAN 触发）都必须写出 `<output-dir>/execution_config.json` + `execution_config.md`，不得省略；模块三必须能在固定路径读到该文件
- **ExperimentPlan.compute_estimate** 在数值上必须满足：`compute_estimate ≤ ExecutionConfig.max_retries × 单 seed 估算 + len(ExecutionConfig.seeds) × 单实验单 seed GPU 小时`，且 `compute_estimate ≤ ResourceConstraints.gpu_hours` 才允许走 stage 3 全量；否则触发 §4 算力门禁降档
- **PlanningFeedback.affected_experiment_ids** 中的每个 ID 必须在重规划上一轮 `ExperimentPlan.experiment_matrix` 首列或 `primary_experiments` 中能找到
- **PlanningFeedback.blockers** 的 `category` 必须是 `{data, compute, baseline, method, metric, schema, experiment, budget}` 之一（8 类别，见 `load_inputs._FEEDBACK_CATEGORIES`），且每条形如 `[category] description`
  - `experiment` 专指 **`experiment_plan` 自身字段**的问题（消融登记、矩阵覆盖、标识符契约等）：路由为跳过 stage 1、只让 planner 带反馈重写。模块三侧 `contracts.categorize_blocker` 对此有一条**锚定字段路径**（`experiment_plan.` / `ablation` / `消融`）的前置判定，**先于**关键词规则跑——否则含"实现/方法"字样的 experiment_plan 问题会被误判成 `[method]` 送去重跑 method-designer，而那条路由改不到 experiment_plan（2026-09-17 事故，两轮完全复现）
- **MethodReview → PlanningFeedback 闭环**：若一轮反思循环结束后仍触发 REPLAN 且 `blockers` 含 `method` / `baseline` 类，模块二必须把上一轮 `MethodReview.feedback` 与 `PlanningFeedback.suggested_changes` 拼接后作为下一轮 Architect 的输入，避免重复同样的错误
