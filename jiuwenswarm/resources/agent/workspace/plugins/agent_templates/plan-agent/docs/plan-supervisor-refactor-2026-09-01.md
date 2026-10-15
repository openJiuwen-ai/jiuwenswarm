# plan-supervisor + planning-skill 改造设计（2026-09-01）

> 适用版本：plan-supervisor manifest `1.0.1` + planning-skill（scripts/ 截至 2026-08-31 15:32）
> 维护人：用户
> 状态：草案 v1，等用户确认后实施
> 关联 memory：[[plan-supervisor-ask-user-rail-fix]] / [[plan-supervisor-rollback-handrolled-runner]] / [[skill-vs-agent-decision]]

---

## 1. 背景：当前症状

2026-09-01 15:20–15:39 实测一次完整 run 拿到的事实：

| 现象 | 数据 |
|---|---|
| 主 agent 调 tool 前空转 LLM call | 6-7 次 / 60-90s（agent log: 15:27:23→15:28:27） |
| skill 子进程跑 4 阶段 | 7m54s（agent log: 15:28:27→15:36:21） |
| 主 agent 拿 status.json 后渲检查点 1 摘要 | 3m15s / 5 次 LLM call（15:36:21→15:39:36） |
| 总耗时 | **~15min**（用户报 10min 是省了视觉预热段） |
| 单独 skill 跑（命令行） | ~3min |
| **主 agent 4 个 checkpoint 只有第 1 个有摘要** | UI 显示空，无法审核 |
| planning-skill 在 manifest 不可见 | `plan-supervisor/manifest.json` 没 `skills[]` / `subagents[]` |
| REPLAN 触发频率 | 任何 modify 走整链重跑（4 阶段 × 反思循环） |

---

## 2. 根因

### 2.1 三层 context（已用子 agent 探查确认）

```
┌─ ① 主 agent history（TUI session 内）──────────────────┐
│   user_input + 自己的 LLM 思考 + ask_user 决策结果         │
│   默认全量到上 round（`context_window.get_messages()`）   │
└────────────────┬───────────────────────────────────────┘
                 │ 调 call_planning_skill
                 ↓
┌─ ② subprocess 黑盒（uv run python -m scripts.main）──┐
│   4 阶段脚本，硬隔离边界——子进程退出所有 session 销毁    │
└────────────────┬───────────────────────────────────────┘
                 │ 调 call_session_async
                 ↓
┌─ ③ LLM session（method-designer / method-critic / …）┐
│   session_holder 模式：跨反思轮复用同一 session          │
│   跨轮累积（设计意图：LLM 看到上轮自己的 draft / review） │
└──────────────────────────────────────────────────────┘
```

**关键事实**：
- ① ↔ ②：subprocess 硬隔离 ✅ 不会污染主 agent
- ② ↔ ③：每个 sub-agent 独立 session_holder ✅ 不互相污染
- ③ 内部：designer / critic session 跨轮累积 ⚠️ 这是设计意图

### 2.2 三个症状各自的根因

| 症状 | 根因 | 证据 |
|---|---|---|
| **10min 慢** | ① LLM 调 tool 前 6-7 次空转（persona Step 0 让 LLM 自己 ls/glob/mkdir，但 tool 已 hard-check）；② 4 阶段每阶段最多 8 个 LLM call（designer+critic 反思循环） | agent log / `iterate_method_design.py:108-225` |
| **3/4 摘要空** | persona 让 LLM 自由生成 4 个 checkpoint 摘要塞 `ask_user.questions[0].question`；history 涨后 LLM 偷懒，question 字段被压缩/填空 | `persona/plan-supervisor.md:81-117`（无模板）+ 实际观察 |
| **REPLAN 雪崩** | `planning_flow.py:147-185` 路由：`data` 跳 stage 1 其余重跑；其他 6 类（compute/baseline/metric/method/experiment/budget）→ stage 1 完整重跑 + stage 2/3/4 全跑 | `planning_flow.py:147-185` |

---

## 3. 改造目标

| 指标 | 当前 | 目标 |
|---|---|---|
| 单次 run 总 LLM call（skill 内） | 4-16 | 4-9 |
| 单次 run 总耗时 | 10-15min | 4-6min |
| 4 checkpoint 摘要显示 | 1/4 | 4/4 |
| 1 次 modify 触发 LLM call | 16-17（整链） | 2-8（局部） |
| planning-skill 在 manifest 可见 | ❌ | ✅（通过 `skills/` 目录） |

**非目标**（明确不做）：
- 不把 planning-skill 改成 framework native subagent（代价见 §6 风险）
- 不动 `_FEEDBACK_CATEGORIES` 集合
- 不动 `subprocess.run` 硬边界（隔离好处保留）

---

## 4. 改造方案

### Track A：4 个 checkpoint 摘要模板（persona 改 1 文件）

**目标**：消除 LLM 自由生成带来的截断/漂移；摘要长度恒定。

**改动文件**：`jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/persona/plan-supervisor.md`

**Before**（persona 当前 Step 2，line 81-117）：

```markdown
#### 检查点 1：method_design（必走）
读 `output_dir/method_design.json`，渲染关键摘要（创新点 / 组件 / 假设覆盖 / adequacy_score）。  
调 `ask_user` tool，options = `[approve, modify, abort, details]`：
...
```

**After**：

```markdown
## 4 个检查点摘要模板（强制使用，不要自由生成）

每个 checkpoint 调 `ask_user` 时，`questions[0].question` 字段**必须**严格按下面模板填，
LLM 只负责把 `{...}` 占位符替换为 status.json / 产物的实际值，**不要**自由发挥、**不要**
添加模板外的字段、**不要**省略模板内的字段。模板总长约 250-400 字符，远低于 max_tokens
上限，杜绝截断。

### 模板 1：method_design

```
[1/4] method_design
- 创新点 ({n_innov}): {innovation_points[0].claim}
- 组件 ({n_comp}): 核心={components[0].name}
- 假设覆盖: {covered}/{total_hypotheses}（{missing_ids}）
- adequacy: {adequacy_score}（reflection 轮数 {rounds}）
- 产物: method_design.json
请决策: approve / modify / abort / details
```

### 模板 2：experiment_plan + data_plan

```
[2/4] experiment + data
- 主实验: {n_primary}（{primary_experiments[0].id}: {primary_experiments[0].objective}）
- 基线: {n_baselines}（{baselines[0].name} @ paper_id={baselines[0].paper_id}）
- 数据集: {n_datasets}（{datasets[0].name}）
- 算力: {compute_estimate} GPUh（reflection 轮数 {reviewer_rounds}）
- 产物: experiment_plan.json, data_plan.json
请决策: approve / modify / abort / details
```

### 模板 3：budget / feasibility（仅当 status.json.check_feasibility.passed=false 时走）

```
[3/4] budget gate
- 当前 tier: {tier}（0=无降级 / 1=TRIM / 2=CORE）
- 估算: {total_gpu_hours} GPUh / 预算: {budget_gpu_hours} GPUh（超 {over_ratio}x）
- 主要超限项: {top_issue}
- 选项: downgrade（LLM 自动裁剪）/ manual（用户改 budget）/ abort
```

### 模板 4：execution_config

```
[4/4] execution_config
- seeds: {seeds} / max_retries: {max_retries} / timeout: {timeout_seconds}s / dry_run: {dry_run}
- run_dir: {run_dir}
- 产物: execution_config.json
请决策: approve / modify / abort / details
```

## 工作流程（改写 Step 0-2）

### Step 0：直接调 call_planning_skill

**不要**先 ls / glob / mkdir / 检查 input_dir / 检查 output_dir。Tool 内部已 hard-check。
直接用用户给（或 defaultInitInput）的 input_dir / output_dir 调 tool。

### Step 1：调 planning-skill 跑产物

```json
{
  "action": "run",
  "input_dir": "<input_dir>",
  "output_dir": "<output_dir>",
  "feedback_file": null,
  "passthrough": {"max_method_rounds": 2, "max_retries": 1}
}
```

注：把 `max_method_rounds` 从 3 降到 2（与 persona 第 189 行"modify 最多 2 轮"对齐
——当前代码实际是 3，这是 doc/code 不一致，顺手对齐）。

### Step 2：4 个检查点（顺序固定）

按顺序处理 method_design → experiment+data → budget → execution。
每个 checkpoint 调 `ask_user` 前**必须**按上面"模板 N"填 `question` 字段——
LLM 只做占位符替换，**不**自由生成文本。模板是定长（250-400 字符），单次
LLM 调用的 prompt 中模板部分不变，杜绝截断。
```

**预期效果**：
- 4 个 checkpoint 摘要长度恒定 ~300 字符，不再 LLM 自由发挥
- `ask_user.questions[0].question` 不再为空
- 模板约束 LLM 输出 → 单次 LLM call 输出长度可控

---

### Track B：反思循环合并——3 层门禁（skill 改 2 文件）

**目标**：Stage 1 / Stage 2 各自把 LLM call 从 2-8 砍到 2-4，且语义质量不降。

**改动文件**：
- `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/_hard_gates.py`（新建）
- `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/iterate_method_design.py`
- `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/plan_experiment_with_data.py`

**当前反思结构**（每个 stage 内部）：

```
designer 写初稿
  ↓
[反思循环 最多 3 轮]
  ↓ 每轮
  critic 评（同时算 4 件事：adequacy / feedback_addressed / missing / hypothesis）
  ↓
  passed 且 adequacy≥7 且 unaddressed=0 → break
  ↓ 否则
  拼 prev_feedback 给 designer 重写
  ↓
[假设兜底] 缺假设 → designer 再 1 轮
```

**每轮 critic 4 件事**：
| 任务 | LLM 必要？ | 备注 |
|---|---|---|
| ① adequacy_score 0-10 | ✅ 必要 | semantic 判断，LLM 不可替代 |
| ② feedback_addressed | ❌ 可确定性 | 已有 `script_diff_unchanged_count` |
| ③ missing_components | ✅ 必要 | 结构是否完整，LLM 判断 |
| ④ hypothesis_coverage | ❌ 可确定性 | `hypothesis.id` 是否在 `method.hypothesis_coverage[].hypothesis_id` 里 |

**After：3 层门禁结构**：

```
designer 写初稿                          ← 1 LLM
  ↓
[Layer A] 确定性 schema gate (0 LLM)      ← 新建 _hard_gates.py
  - validate_plan.py 必填字段 + 长度
  - hypothesis_coverage 100%（从 critic 移出来）
  - baseline.paper_id ∈ key_papers.id
  - metric ∈ success_criteria 至少 70%
  ↓ 有 gap
designer 重写（gap 列表塞 prev_feedback）  ← 1 LLM（条件触发）
  ↓
[Layer C] critic adequacy 评              ← 1 LLM
  - 只评 ① adequacy_score + ③ missing_components
  - 不再评 ②④（已确定性）
  - adequacy ≥ 7 → break
  ↓ 否则
designer 再重写                           ← 1 LLM（条件触发）
  ↓
[Layer C'] critic 再评                    ← 1 LLM（条件触发）
  - adequacy ≥ 7 → break
```

**LLM call 计数对比**：

| 路径 | Before | After |
|---|---|---|
| 一次跑通 | 2 | 2 |
| 重写 1 次 | 4 | 3 |
| 重写 2 次 | 6 | 4 |
| 假设兜底触发 | 8 | 4 |
| **上限** | **8** | **4** |

**Stage 1 + Stage 2 合计**：before 4-16 → after 4-8

**关键改动 1：新建 `_hard_gates.py`**：

```python
"""确定性门禁——0 LLM 调用，3 个 stage 复用。"""
from __future__ import annotations
from typing import Any


def hard_gate_method_design(
    method_design: dict,
    inputs: dict,
) -> list[str]:
    """method_design 阶段 Layer A：返回 gap 列表（空列表 = 通过）。

    检查项：
      1. 必填字段非空（research_goal / core_mechanism / framework.name /
         framework.components[] / innovation_points[] / hypothesis_coverage[]）
      2. hypothesis_coverage 100% 覆盖 inputs.hypotheses[].id
      3. components 数量 ≥ 1，每个有 name/function 双字段
    """
    gaps: list[str] = []
    for field in ("research_goal", "core_mechanism"):
        if not method_design.get(field, "").strip():
            gaps.append(f"缺少顶层字段或为空: {field}")
    framework = method_design.get("framework") or {}
    if not framework.get("name", "").strip():
        gaps.append("framework.name 为空")
    comps = framework.get("components") or []
    if not comps:
        gaps.append("framework.components[] 为空")
    else:
        for i, c in enumerate(comps):
            if not (isinstance(c, dict) and c.get("name") and c.get("function")):
                gaps.append(f"components[{i}] 缺 name 或 function")
    # 假设覆盖
    covered = {
        hc.get("hypothesis_id")
        for hc in method_design.get("hypothesis_coverage") or []
        if isinstance(hc, dict)
    }
    all_ids = {h.get("id") for h in inputs.get("hypotheses") or []}
    missing = sorted(all_ids - covered)
    if missing:
        gaps.append(f"假设未覆盖: {missing}（请在 hypothesis_coverage[] 补全）")
    return gaps


def hard_gate_experiment_plan(
    experiment_plan: dict,
    inputs: dict,
) -> list[str]:
    """experiment_plan 阶段 Layer A：返回 gap 列表。"""
    gaps: list[str] = []
    # 必填顶层
    for field in ("primary_experiments", "baselines", "metrics", "datasets"):
        if not experiment_plan.get(field):
            gaps.append(f"缺少顶层字段或为空: {field}")
    # baseline paper_id 真实性
    key_paper_ids = {p.get("id") for p in inputs.get("key_papers") or []}
    for i, b in enumerate(experiment_plan.get("baselines") or []):
        if not isinstance(b, dict):
            continue
        if b.get("paper_id") not in key_paper_ids:
            gaps.append(
                f"baselines[{i}].paper_id={b.get('paper_id')!r} 不在 key_papers.id 内"
            )
    # metric × success_criteria 覆盖
    sc_names = {s.get("name") for s in inputs.get("research_question", {}).get("success_criteria") or []}
    metric_names = {m.get("name") for m in experiment_plan.get("metrics") or []}
    if sc_names and metric_names:
        covered_metrics = sc_names & metric_names
        if len(covered_metrics) / max(len(sc_names), 1) < 0.7:
            gaps.append(
                f"metrics 与 success_criteria 覆盖率 < 70%："
                f"missing={sorted(sc_names - metric_names)[:5]}"
            )
    # compute_estimate 是 number
    ce = experiment_plan.get("compute_estimate")
    if not isinstance(ce, (int, float)):
        gaps.append(f"compute_estimate 应为 number，实际 {type(ce).__name__}")
    return gaps


def format_gaps_as_feedback(gaps: list[str], phase: str) -> str:
    """把 gap 列表拼成 LLM 可读的 prev_feedback 文本。"""
    if not gaps:
        return ""
    lines = "\n".join(f"- {g}" for g in gaps)
    return f"[{phase} 确定性门禁 gap 列表]\n{lines}\n请针对每条 gap 修复，不要改其他字段。"
```

**关键改动 2：`iterate_method_design.py` 改造**：

主循环替换为 3 层门禁（line 108-191 那段），**删掉**：
- `passed and total_unaddressed > 0` 的强制再 1 轮分支（line 154-159）→ 多余
- 末尾的 `unaddressed_block` 拼接（line 172-176）→ 不再让 LLM 评 feedback_addressed

**新增**：
- 主循环开头调 `_hard_gates.hard_gate_method_design(method_draft, inputs)`
- gap 非空 → 拼进 prev_feedback 让 designer 重写（1 次）
- gap 为空 → 调 critic adequacy

**`method_rounds_used` 计数**改为：
- designer 次数 + critic 次数

**`max_rounds` 从 3 改为 2**（与 persona 第 189 行对齐）。

**关键改动 3：`plan_experiment_with_data.py` 改造**：

同 method_design，对称改：
- 主循环开头调 `_hard_gates.hard_gate_experiment_plan(experiment_plan, inputs)`
- 删 `passed and total_unaddressed > 0` 分支
- 删 `unaddressed_block` 拼接

---

### Track C：REPLAN 阶段局部路由（skill 改 1 文件）

**目标**：modify 触发的 REPLAN 只重跑对应 stage，不全链重跑。

**改动文件**：`jiuwenswarm/resources/agent/workspace/skills/planning/scripts/planning_flow.py`

**当前路由**（`planning_flow.py:147-185`）：

| blocker category | 当前行为 | 实际效果 |
|---|---|---|
| `schema` | 直接返回 `replan_schema_blocked` | ✓ 不重跑 |
| `data` | 跳 stage 1，其余 stage 2/3/4 重跑 | ✗ 跑多了 |
| 其他 6 类（compute/baseline/metric/method/experiment/budget） | stage 1 完整重跑 + stage 2/3/4 全跑 | ✗✗ 跑得最多 |

**After：stage 局部路由**：

| category | stage 1 | stage 2 | stage 3 | stage 4 |
|---|---|---|---|---|
| `schema` | — | — | — | — | (直接返回 replan_schema_blocked) |
| `data` | 跳过 | **重抽** | 复用 | 复用 |
| `method` | **重跑** | 复用 | 复用 | 复用 |
| `experiment` | 复用 | **重跑** | 重算 | 复用 |
| `data` + `experiment` 混合 | 跳过 | **重跑** | 重算 | 复用 |
| `budget` | 复用 | 复用 | **重算** | 复用 |
| `metric` / `baseline` / `compute` | 复用 | **重跑**（覆盖到 experiment_plan 字段） | 重算 | 复用 |

**实现**（替换 `planning_flow.py:147-185`）：

```python
# ── REPLAN 路由（按 blocker category 局部重跑） ──
planning_feedback: dict | None = None
replan_data_only = False
replan_skip_stage1 = False
replan_only_stage2 = False
replan_only_stage3 = False
if feedback_file:
    planning_feedback, fb_errs = load_planning_feedback(feedback_file)
    if fb_errs:
        for e in fb_errs:
            log(f"[load_planning_feedback] {e}")
    cats: set[str] = set()
    for b in (planning_feedback or {}).get("blockers") or []:
        if isinstance(b, str) and b.startswith("[") and "]" in b:
            cats.add(b[1:b.index("]")].strip())
    if "schema" in cats:
        return {
            "status": "replan_schema_blocked",
            "errors": [f"categories={sorted(cats)}"],
            "categories": sorted(cats),
            "checkpoint_status": checkpoint_status,
        }
    # 局部路由表
    if "method" in cats:
        replan_skip_stage1 = False  # 重跑 stage 1
        replan_only_stage2 = False
        replan_only_stage3 = False
    elif "data" in cats and not (cats - {"data"}):
        # 纯 data 类别
        replan_data_only = True
        replan_skip_stage1 = True
    elif cats & {"experiment", "metric", "baseline", "compute"}:
        # 这些都改 experiment_plan 字段
        replan_skip_stage1 = True
        replan_only_stage2 = True
    elif "budget" in cats:
        replan_skip_stage1 = True
        replan_only_stage3 = True
    else:
        # 默认整链重跑（保守）
        pass

# ── 复用上一轮产物（局部 REPLAN 关键） ──
prev_method_design: dict | None = None
prev_method_review: dict | None = None
prev_experiment_plan: dict | None = None
prev_data_plan: dict | None = None
if feedback_file and (replan_skip_stage1 or replan_only_stage3):
    prev_path = Path(feedback_file)
    prev_dir = prev_path if prev_path.is_dir() else prev_path.parent
    if replan_skip_stage1:
        prev_md = prev_dir / "method_design.json"
        if prev_md.is_file():
            prev_method_design = json.loads(prev_md.read_text(encoding="utf-8"))
            log(f"REPLAN 复用 method_design: {prev_md}")
    if replan_only_stage3:
        # 复用 stage 1 + stage 2，只重算 budget
        for fname, target in (("method_design.json", "prev_method_design"),
                              ("experiment_plan.json", "prev_experiment_plan"),
                              ("data_plan.json", "prev_data_plan")):
            p = prev_dir / fname
            if p.is_file():
                locals()[target] = json.loads(p.read_text(encoding="utf-8"))
                log(f"REPLAN 复用 {fname}: {p}")

# ────────────── Stage 1: 方法设计 ──────────────
if replan_skip_stage1:
    assert prev_method_design is not None, "replan_skip_stage1 必须复用上一轮"
    method_design = prev_method_design
    method_review = {}  # 留空（agent 不依赖 review）
    method_rounds_used = 0
else:
    # 正常跑 stage 1（用 Track B 改后的 3 层门禁）
    method_design, method_review, method_rounds_used = await iterate_method_design_async(
        inputs, max_rounds=2, planning_feedback=planning_feedback,
        method_designer_holder=method_designer_holder,
        method_critic_holder=method_critic_holder,
    )
checkpoint_status["method_design"] = "passed"

# ────────────── Stage 2: 实验规划 ──────────────
if replan_only_stage2 or replan_only_stage3:
    assert prev_experiment_plan is not None, "replan 复用 stage 2 必传 prev"
    prev_experiment_plan_to_pass = prev_experiment_plan
else:
    prev_experiment_plan_to_pass = prev_experiment_plan  # REPLAN_DATA 用
experiment_plan, data_plan, experiment_review = await plan_experiment_with_data_async(
    inputs, method_design, method_review,
    replan_data_only=replan_data_only,
    replan_only_stage2=replan_only_stage2,  # 新增 flag
    planning_feedback=planning_feedback,
    prev_experiment_plan=prev_experiment_plan_to_pass,
    experiment_planner_holder=experiment_planner_holder,
    experiment_critic_holder=experiment_critic_holder,
)
checkpoint_status["experiment_plan"] = "passed"

# ────────────── Stage 3: 门禁校验 ──────────────
# 始终跑（即使是局部 REPLAN，budget gate 都得重算）
experiment_plan, data_plan, tier, _results = check_feasibility_and_downgrade(
    experiment_plan, data_plan, inputs["resource_constraints"],
)
# ... (tier 逻辑保留)

# ────────────── Stage 4: 执行配置 ──────────────
execution_config = produce_execution_config(...)
```

**REPLAN 触发 LLM call 计数对比**：

| 场景 | Before | After |
|---|---|---|
| method modify | 16-17（整链） | 2-4（只 stage 1） |
| experiment modify | 16-17 | 2-4（只 stage 2） |
| data modify | 8-10（跳 stage 1 但跑其余） | 0-2（纯抽 data_order） |
| budget modify | 16-17 | 0-1（只重算 gate） |

**同时新增 `scripts/plan_experiment_with_data.py` 参数** `replan_only_stage2: bool = False`：
- True 时：跳过 LLM planner，直接复用 prev_experiment_plan，但**仍然调 critic adequacy 评 1 次**（保证 review 质量）
- 复用 + 1 critic = 1 LLM call（vs 之前 8 LLM）

---

### Track D（轻量）：planning-skill 在 manifest 可见（agent 改 1 文件）

**目标**：把 planning-skill 移入 plan-supervisor 的 `skills/`，让 framework 识别为 first-class skill。

**改动文件**：
- 新建 `jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/skills/planning/SKILL.md`
- 新建 `jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/skills/planning/scripts/` → **符号链接**到 `../../../../skills/planning/scripts/`
- 改 `plan-supervisor/manifest.json`：加 `"skills": [{"dir": "./skills/planning", "mode": "all"}]`

**SKILL.md 草案**（60 行内）：

```markdown
---
name: planning
description: 规划模块（CCF BDCI 2026 模块二）。基于模块一产物（research_question / hypotheses / gap_report / key_papers / resource_constraints / domain / research_frontier）生成 method_design / experiment_plan / data_plan / budget_report / execution_config。输入 input_dir + 可选 feedback_file，输出 output_dir 下的 5 .json + 5 .md + status.json。
---

# Planning Skill

This skill is the body of the planning module. It is invoked as a framework Skill
by the plan-supervisor agent, but is also runnable standalone via:

```bash
uv run python -m scripts.main --input-dir <input_dir> --output-dir <output_dir> \
    --status-file <output_dir>/status.json
```

## Inputs (must exist in input_dir)

- `research_question.json` — topic, scope, success_criteria[]
- `hypotheses.json` — list of {id, claim, verifiable}
- `gap_report.json` — {research_question, existing_state, missing_capability}
- `key_papers.json` — list of {id, title, method_key}
- `resource_constraints.json` — {gpu_hours, memory_gb, time_budget_days, budget}
- `research_frontier.json` — {frontier_text}
- `domain.json` — {domain_name}

## Outputs (written to output_dir)

- `method_design.json` / `method_design.md`
- `experiment_plan.json` / `experiment_plan.md`
- `data_plan.json` / `data_plan.md`
- `execution_config.json` / `execution_config.md`
- `budget_report.json` / `budget_report.md`
- `status.json` — agent protocol (status / artifacts / errors / check_feasibility / method_rounds_used / wall_time_seconds / tier / checkpoint_status)

## REPLAN

Pass `--feedback-file <path>` with a `PlanningFeedback` JSON:
```json
{
  "reason": "string",
  "affected_experiment_ids": ["EXP-H1-..."],
  "blockers": ["[method] ...", "[experiment] ..."],
  "suggested_changes": ["..."]
}
```

The skill routes REPLAN by `blockers[].category`:
- `schema` → blocked, no rerun
- `data` → REPLAN_DATA (re-extract data_order)
- `method` → rerun stage 1
- `experiment` / `metric` / `baseline` / `compute` → rerun stage 2
- `budget` → rerun only feasibility gate

## Don't

- Don't modify `scripts/` directly inside this symlink; it's shared with
  `workspace/skills/planning/scripts/`. Edit there and the link follows.
```

**manifest.json diff**：

```diff
   "tools": [
     { "file": "tools/call_planning_skill.py", ... }
   ],
+  "skills": [
+    { "dir": "./skills/planning", "mode": "all" }
+  ],
   "rails": [...]
```

**预期效果**：
- framework `_load_agent_template` 把 planning 当 first-class skill 注册
- LLM 看到 "Skill: planning" 在可用 skill 列表里，**不需要"发现"**
- persona Step 0 的"先 ls/glob"空转消失
- call_planning_skill tool 保留为 subprocess 调用方式（plan-supervisor 仍走它）

**关于 call_planning_skill tool 是否还要保留**：
- 保留：subprocess 边界 = 硬隔离，**主 agent history 不被 skill 内部 LLM 调用污染**（这是核心价值）
- 不去掉：tool 仍然是 skill 调用的唯一路径

---

## 5. 改造前后 LLM call 总数对比

| 路径 | 当前 | 改造后 |
|---|---|---|
| 一次性跑通（无 modify） | 4-16（skill）+ 16-19（agent）= **20-35** | 4-8（skill）+ 12-15（agent）= **16-23** |
| 1 次 method modify | 16-17（skill 重跑整链） | 2-4（只 stage 1） |
| 1 次 experiment modify | 16-17 | 2-4（只 stage 2） |
| 1 次 data modify | 8-10 | 0-2（纯抽 data_order） |

**总耗时估算**（deepseek-v4-flash 23-52s/call，平均 30s）：
| 路径 | 当前 | 改造后 |
|---|---|---|
| 一次性跑通 | 10-17min | 8-12min |
| 1 次 method modify | +16-17 LLM × 30s = 8-9min | +2-4 × 30s = 1-2min |
| 1 次 experiment modify | +8-9min | +1-2min |

---

## 6. 风险与回退

| 风险 | 缓解 |
|---|---|
| 3 层门禁里 `hard_gate` 太严，把本来可用的方案打回 | 保留 `_hard_gates.format_gaps_as_feedback` 给 LLM 一次性修补；gate 不是 hard fail，是 soft gap |
| REPLAN 局部路由漏 case（混合 category） | 路由表设计成"取最严的子集"（data + method 混合时重跑 stage 1+2），不要试图优化所有组合 |
| 把 planning-skill symlink 进 agent `skills/` 后，路径解析出错 | 用 `Path(__file__).resolve()` 找绝对路径，不要 `cwd`-relative；先在 `call_planning_skill.py` 加日志打 cwd 看实际解析 |
| Track D 改了 manifest 后 server 不重新加载 | bump manifest version 1.0.1 → 1.0.2（沿用 [[plan-supervisor-ask-user-rail-fix]] 的强制 reload 模式） |

**回退方案**：
- Track A：persona 单文件改动，git revert 即可
- Track B：把 `iterate_method_design.py` 主循环的 `hard_gate_*` 调用注释掉就回退到旧路径
- Track C：`planning_flow.py` 局部路由是新代码块，删掉就回退到旧的"data 跳 stage 1 其余重跑"
- Track D：删 `manifest.json` 的 `skills[]` + 删 `skills/` 目录，秒回退

---

## 7. 实施步骤（4 个 phase，每个独立可验证）

### Phase 1（1-2h）：Track A 摘要模板

1. 改 `persona/plan-supervisor.md`（4 个模板 + Step 0 砍掉）
2. bump persona 不需要（persona 不是 manifest）
3. **验证**：跑一次 TUI 激活 plan-supervisor，4 个 checkpoint 摘要都稳定显示（即使内容是占位符），不再出现 "看不到要审核什么"

### Phase 2（2-3h）：Track B 反思合并

1. 新建 `_hard_gates.py`（含 `hard_gate_method_design` / `hard_gate_experiment_plan` / `format_gaps_as_feedback`）
2. 改 `iterate_method_design.py`：主循环加 `hard_gate_method_design` 调 + 删 unaddressed 强制再 1 轮 + 改 `max_rounds` 默认 2
3. 改 `plan_experiment_with_data.py`：对称
4. 跑 `tests/regression_check.py`（17+6 个回归测试）+ 加新测试：硬门禁 gap 列表准确性
5. **验证**：跑 skill 命令行，stage 1 + stage 2 实际 LLM call 数（看 logs/logs/llm.log 计数 call_start）

### Phase 3（2-3h）：Track C REPLAN 局部路由

1. 改 `planning_flow.py:147-185`：加 `replan_only_stage2` / `replan_only_stage3` / 复用 prev 产物
2. 改 `plan_experiment_with_data.py`：加 `replan_only_stage2` 参数（跳过 LLM planner 但仍调 critic 1 次）
3. 加测试：构造 4 类 REPLAN feedback，验证只触发对应 stage
4. **验证**：在 TUI 选 4 次 modify（method/experiment/data/budget 各 1 次），看 4 次的实际耗时

### Phase 4（1h）：Track D skill 可见

1. 改 `plan-supervisor/manifest.json`：加 `"skills": [{"dir": "./skills/planning", "mode": "all"}]`
2. bump `version: 1.0.1` → `1.0.2`（逼 server reload）
3. 新建 `plan-supervisor/skills/planning/SKILL.md`
4. 建 symlink `plan-supervisor/skills/planning/scripts/` → 实际 skills 目录
5. **验证**：TUI `/use-expert list` 看到 plan-supervisor 自带 planning skill；agent 启动后 6-7 次空转 LLM call 减少到 2-3 次

---

## 8. 验证清单

| 验证项 | 怎么验 | 目标 |
|---|---|---|
| 4 个 checkpoint 摘要都有内容 | TUI 激活 plan-supervisor，4 次人审都看到文本 | ✓ 4/4 |
| 1 次 run 总耗时 | agent log + skill log 时间差 | ≤ 6min |
| skill 内 LLM call 数 | grep `llm_call_start` in skill log | ≤ 8（method + experiment 合计） |
| REPLAN modify 不全跑 | 选 1 次 method modify，看 stage 1 后的 stage 2 是否复用产物 | stage 2 输入是 prev path |
| Track D skill 可见 | TUI `/use-expert plan-supervisor` 后查 skill list | "planning" 出现 |
| 反思合并后回归测试 | `uv run pytest tests/regression_check.py` | 全过 |

---

## 9. 关联资源

- 实测 log（2026-09-01 15:20-15:39）：`D:\jiuwenswarm\logs\logs\llm.log` + `D:\jiuwenswarm\jiuwenswarm\resources\agent\workspace\skills\planning\logs\logs\llm.log`
- 相关 memory：
  - [[plan-supervisor-ask-user-rail-fix]] —— manifest 不能重复声明 StructuredAskUserRail
  - [[plan-supervisor-rollback-handrolled-runner]] —— 改 framework DeepAgent + AskUserRail，自己写 LLM 循环前先查 docs/en/Harness.md
  - [[skill-vs-agent-decision]] —— 5 问决策表 + planning-skill 历史包袱
  - [[deepseek-v4-flash-planner-drift]] —— schema 漂移点（method_review.feedback dict / compute_estimate 嵌套 / feasibility_risks list[dict]）
  - [[feedback-addressed-empty-index-syntax]] —— `feedback_addressed.field_path` 偶现 `baselines[]` 空索引
- 关键文件：
  - `jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/persona/plan-supervisor.md`
  - `jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/tools/call_planning_skill.py`
  - `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/iterate_method_design.py`
  - `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/plan_experiment_with_data.py`
  - `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/planning_flow.py`
  - `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/_subagent.py`
  - `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/_status.py`
  - `jiuwenswarm/resources/agent/workspace/skills/planning/scripts/validate_plan.py`

---

## 10. 待用户确认的点

实施前需要用户拍板 3 个选择：

1. **Track B 的 `max_rounds` 降 3 → 2**：与 persona 第 189 行"modify 最多 2 轮"对齐。代价：极端情况（method 完全跑偏）可能需要 user modify 1-2 次后放弃。**建议：降到 2**
2. **Track C REPLAN 路由粒度**：当前设计是 4 类（method / data+experiment / budget / 其他整链）。要不要更细？例如把 `metric` / `baseline` 拆开？**建议：保持 4 类，简单优先**
3. **Track D 是否现在做**：Track D 是"治本"（让 skill 可发现），但改动 manifest + 建 symlink 有路径风险。建议先做 A+B+C 验证效果，**D 留到下次迭代**

如果用户都同意"建议"项，下一步就是按 Phase 1→4 顺序实施，每个 Phase 完成后跑一次端到端验证。
