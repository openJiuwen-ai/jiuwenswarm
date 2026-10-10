# 规划模块 C 方案改造工作记录

> 记录日期：2026-08-25
> 范围：把规划模块（模块二）从「CLI + 手撸 stdin 人审」改造成「CLI 薄壳 + SwarmFlow 子工作流 + 框架 `await human()` + Pydantic schema」

---

## 0. 背景与决策

**起点状态**（改造前）：
- `scripts/main.py`：367 行的 CLI + 内嵌编排（preflight → load_inputs → 3 阶段 → 写产物），所有阶段全在主进程顺序跑。
- `scripts/human_review.py`：手撸的 HITL，循环读 stdin / 解析 `a/m/x` 字符 / 类型校验全部自己写。
- 人审产物是不带类型的 `dict`，下游拿不准 key 是什么。

**痛点**：
1. **重复造轮子**：JiuwenSwarm 框架本身就有 `openjiuwen.agent_teams.workflow.engine.facade` 的 `human()` operator 算子，带 TUI `h` 键集成和 `StructuredAskUserRail`，自己手撸 stdin 没必要。
2. **后续要升级 team 模式**（leader + 组员 agent 分工）：手撸编排升级成本高，框架的 `phase()` / `agent()` / `team_session` 才是正解。
3. **类型不安全**：人审答案是 untyped dict，写检查点 2/3/4 时每个字段都要 `isinstance` 验。

**决策**：走 **C 方案**——把规划模块包成 SwarmFlow 子工作流（`scripts/planning_flow.py`），CLI 改成薄壳调 `run_workflow()`。`await human()` 留好接口，将来切 team 模式只动 `iterate_method_design()` 内部。

---

## 1. 改动清单（文件级 diff）

### 1.1 新增

| 文件 | 行数 | 用途 |
|---|---|---|
| `scripts/planning_flow.py` | 391 | SwarmFlow 子工作流：5 phase + 4 `await human()` 检查点 + 4 Pydantic schema |

### 1.2 重写

| 文件 | 改动 | 用途 |
|---|---|---|
| `scripts/main.py` | 367 → 176（-52%） | CLI 薄壳：只解析参数 + `run_workflow(planning_flow.py)` + 退出码映射 |

### 1.3 删除

| 文件 | 原因 |
|---|---|
| `scripts/human_review.py` | 被框架 `await human(schema=...)` 完全替代 |

### 1.4 修改

| 文件 | 改动 |
|---|---|
| `scripts/iterate_method_design.py` | 新增 `previous_feedback: str = ""` 参数；非空时拼进 architect initial_payload |
| `scripts/plan_experiment_with_data.py` | 新增 `previous_feedback: str = ""` 参数；非空时拼进 planner_payload |
| `workflows/planning.md` | 重写 §Human Review 段：从「手撸 stdin」改成「`await human()` + Pydantic schema」；新增「升级到 team 模式时」小节 |
| `SKILL.md` | **未改**——仍描述旧入口（见 §4 TODO） |

### 1.5 未动（保持兼容）

- `scripts/load_inputs.py` / `check_feasibility_and_downgrade.py` / `validate_plan.py` / `estimate_compute.py` / `write_outputs.py` / `run_subagent.py`：零改动。
- `references/{method-designer,method-critic,experiment-planner}.md`：3 个 persona 的 Self-Reflection Checklist 已在之前一轮加好，本次不动。
- `tests/regression_check.py`：6 类 47 个结构性校验点保持通过（`uv run python -m tests.regression_check`）。

---

## 2. 当前结构与设计思路

### 2.1 顶层结构（C 方案）

```
用户 CLI
  │  uv run python -m scripts.main --input-dir ... --output-dir ...
  ▼
scripts/main.py（薄壳）
  │  1. argparse 解析 12 个 flag
  │  2. 把 Namespace 转 dict
  │  3. asyncio.run(run_workflow(planning_flow.py, args=dict))
  │  4. result["status"] → 退出码 0/1/2
  ▼
openjiuwen SwarmFlow runtime
  │  load → check META.phases vs phase() 标题
  │  async def run(args) → return dict
  ▼
scripts/planning_flow.py（子工作流）
  │  preflight → load_inputs → 5 phase + 4 human()
  │  内部 import scripts.* 各阶段脚本
  ▼
scripts/{iterate_method_design, plan_experiment_with_data, check_feasibility_and_downgrade, write_outputs}.py
  │  仍走 LLMConfig.from_default_model() 调 LLM（run_subagent 路径）
  ▼
LLMConfig（jiuwenswarm.symphony.llm）→ 模型 API
```

### 2.2 planning_flow.py 内部流

```python
async def run(args) -> dict:
    # 1. preflight（环境/agent md/依赖）
    phase("方法设计")                      # 标题须与 META.phases 严格一致
    inputs, errors = load_inputs(...)
    # REPLAN 路由（按 blockers 类别）

    # 2. 阶段 1：方法设计 + 反思循环
    method_design, method_review, _ = iterate_method_design(inputs, ...)
    # ── 检查点 1：方法设计+评审
    for hr_round in range(1, MAX_HUMAN_REVIEW_ROUNDS + 1):     # MAX=2
        decision = await human(prompt, schema=MethodReviewDecision)
        if no_hr or decision.action == "approve": break
        if decision.action == "abort": return {"status": "aborted", ...}
        # modify → feedback 拼回 iterate_method_design(previous_feedback=...)

    # 3. 阶段 2：实验规划 + 抽 data_order
    phase("实验规划")
    experiment_plan, data_plan = plan_experiment_with_data(...)
    # ── 检查点 2：实验+数据

    # 4. 阶段 3：门禁校验（算力档位）
    phase("门禁校验")
    experiment_plan, data_plan, tier, _ = check_feasibility_and_downgrade(...)
    # ── 检查点 3：仅 tier>0 触发；三选一 d/m/x
    if tier > TIER_FULL:
        decision = await human(prompt, schema=TierDowngradeDecision)
        # downgrade → LLM 裁剪；manual → 改 budget；abort → 中止

    # 5. 阶段 4：执行配置
    phase("执行配置")
    execution_config = produce_execution_config(...)
    # ── 检查点 4：ec modify 仅记日志

    # 6. 阶段 5：写产物
    phase("写产物")
    paths = write_outputs(...)
    return {"status": "complete", "output_dir": ..., "tier": ..., "paths": ...}
```

### 2.3 4 个检查点 + 4 个 Pydantic schema

| # | 触发条件 | Schema | 三选一 | feedback 流转 |
|---|---|---|---|---|
| 1 | stage 1 完成后 | `MethodReviewDecision` | approve / modify / abort | `iterate_method_design(previous_feedback=...)` |
| 2 | stage 2 完成后 | `ExperimentPlanDecision` | approve / modify / abort | `plan_experiment_with_data(previous_feedback=...)` |
| 3 | `tier > TIER_FULL` 时 | `TierDowngradeDecision` | **downgrade / manual / abort**（d/m/x） | downgrade → `downgrade_with_llm()`；manual → 改 `inputs["resource_constraints"]["budget"]` |
| 4 | ExecutionConfig 后 | `ExecutionConfigDecision` | approve / modify / abort | modify 仅记日志（ec 实际是 CLI 参数，refactor 时只改 `--seeds` 等重跑） |

**关键设计**：
- `action` 用 `Literal[...]` 强约束，框架自动按 Pydantic 校验，下游不用 `isinstance` 验。
- `feedback: str = ""` 默认值——`no_human_review` 模式下所有检查点走默认值（AUTO-APPROVE / AUTO-DOWNGRADE），CI/批处理**不可中断**。
- `MAX_HUMAN_REVIEW_ROUNDS = 2` 避免人审 feedback 引发的死循环（每检查点最多 2 轮 modify + 1 轮 accept）。
- 检查点 3 是 d/m/x（与 1/2/4 的 a/m/x 不同）：`m=manual` 是改 budget，`d=downgrade` 是 LLM 自动裁剪——是「策略性判断」而非「重写产物」。

### 2.4 与结构性校验的分工

| 类别 | 工具 | 触发 |
|---|---|---|
| **结构性**（类型/格式/范围/存在/唯一性） | `load_inputs` / `validate_plan` / `check_feasibility` | 自动；不通过即报错或 warn |
| **内容性**（语义/策略/可证伪） | LLM persona 的 **Self-Reflection Checklist** | 自动在生成时自查 |
| **策略性**（人判断走哪条路） | **4 个 `await human()` + Pydantic schema** | 人审 4 个检查点 |

三段式分工是这次改完后才真正落地的——以前是「LLM 反思 + 手撸人审」混在一起，现在结构校验纯代码、Self-Reflection 跑在 LLM 里、人审走框架 `human()`，边界清晰。

---

## 3. 已完成什么 + 还需做什么才能投入用

### 3.1 ✅ 已完成

| 项 | 状态 | 证据 |
|---|---|---|
| 5 phase + 4 human() 检查点 + 4 Pydantic schema 编排 | ✅ | `planning_flow.py` 391 行；5 phase 标题与 META.phases 严格一致 |
| CLI 薄壳化 | ✅ | `main.py` 367→176 行（-52%）；所有 flag 保留；退出码语义清晰（0/1/2） |
| 删手撸 HITL | ✅ | `human_review.py` 已删；所有检查点改用 `await human(schema=...)` |
| Pydantic schema 强约束 | ✅ | `MethodReviewDecision` / `ExperimentPlanDecision` / `TierDowngradeDecision` / `ExecutionConfigDecision` |
| modify 反馈回流 LLM | ✅ | `iterate_method_design(previous_feedback=...)` / `plan_experiment_with_data(previous_feedback=...)` |
| `no_human_review` 关闭路径 | ✅ | 所有检查点走 schema 默认值，CI/批处理不可中断 |
| 算力超预算 d/m/x 三选一 | ✅ | 检查点 3 + `downgrade_with_llm` 路径 |
| 人审迭代上限防死循环 | ✅ | `MAX_HUMAN_REVIEW_ROUNDS = 2` |
| 6 类 47 个结构性校验 | ✅ | `tests/regression_check.py` 47/47 通过 |
| 3 个 persona Self-Reflection Checklist | ✅ | `references/method-designer.md` / `method-critic.md` / `experiment-planner.md` |
| Lint clean | ✅ | `planning_flow.py` 0 warning |

### 3.2 ⏳ 还需做什么才能投入生产

按优先级从高到低：

1. **【文档同步】更新 `SKILL.md` 的 Files 段**
   - 现在 SKILL.md 仍把 `scripts/main.py` 描述为「CLI 编排入口（3 阶段串联）」——已过时。
   - 需把 Files 段加 `scripts/planning_flow.py`（SwarmFlow 子工作流），并把 main.py 描述改为「CLI 薄壳，调 run_workflow()」。
   - 顺手在 §Agents 表加一句「teammate 派出走 framework 的 `agent()` / `team_session`（team 模式升级后）」。

2. **【LLM 凭据】配 `jiuwenswarm/resources/.env`**
   - `jiuwenswarm.symphony.llm.LLMConfig.from_default_model()` 从 `.env` 读 API key。
   - 现状是 preflight 阶段只 warn 不阻塞；不配就调不通 LLM，会卡在 `iterate_method_design` 的 `run_subagent`。

3. **【端到端冒烟】用 `mock/` 跑一次完整 CLI**
   - `mock/` 目录应该有模拟的 7 个输入 JSON（research_question / hypotheses / gap_report / key_papers / resource_constraints / research_frontier / domain）。
   - 需要跑：`uv run python -m scripts.main --input-dir mock/ --output-dir out/`，确认能产出 5 对 `.json + .md` 产物，且 status=complete。
   - 之前 regression_check 只测了 6 类结构性校验，**没跑过端到端**——C 方案 wiring 之后这是必须做的一次。

4. **【TUI 集成】在 TUI 里挂上 human() 算子**
   - 框架的 `StructuredAskUserRail` 已支持把 Pydantic schema 转可点击 options，TUI `h` 键触发。
   - 当前 `planning_flow.py` 已用 `await human(label="checkpoint-method-design", phase="方法设计", schema=...)` 标好，框架会自动路由。
   - 需要实际在 TUI 跑一次，确认按 `h` 键能看到 4 个检查点的结构化提问（不是普通 prompt）。

5. **【team 模式升级】替换 sub-agent 拉起方式**
   - 现状：`iterate_method_design()` / `plan_experiment_with_data()` 内部用 `run_subagent(REFERENCES_DIR / "method-designer.md", payload)` 调 LLM。
   - 目标：替换为框架的 `await agent()` / `team_session`，让方法设计/评审/规划走 leader + 组员 agent 协作。
   - 升级时 `await human()` 接口不动；只改 `scripts/iterate_method_design.py` 等内部即可。
   - 这一点用户已明示「之后会升级」，不是 C 方案的硬阻塞。

6. **【可选】把 cli 的 `--no-human-review` 改名 `--no-hitl`**
   - 框架里 `human()` 算子默认走交互；目前用 schema 默认值模拟 AUTO-APPROVE，行为 OK 但命名不一致。
   - 短期不动；等框架有显式 `auto_approve=True` 参数再切。

### 3.3 不在本次范围

- 模块一（conception）/ 模块三（experiment）/ 模块四（writing）：按用户明示「只动 planning」，其他模块不碰。
- 4 模块整体编排（`research-paper-generator`）：4 个模块齐了再写。
- `dependencies.yaml` 的外部依赖列表：本次未动。

---

## 4. 预计使用方法

### 4.1 最简单跑法

```bash
cd D:/jiuwenswarm

# 准备 7 个输入 JSON（模块一产物；或用 mock/ 试跑）
ls plan_input/
# research_question.json  hypotheses.json  gap_report.json  key_papers.json
# resource_constraints.json  research_frontier.json  domain.json

# 跑规划模块
uv run python -m jiuwenswarm.resources.agent.workspace.skills.planning.scripts.main \
    --input-dir plan_input/ \
    --output-dir plan_output/
```

跑完 `plan_output/` 下有 5 对文件（10 个）：
- `method_design.json` / `.md`
- `method_review.json` / `.md`
- `experiment_plan.json` / `.md`
- `data_plan.json` / `.md`
- `execution_config.json` / `.md`

退出码：0=complete，1=环境/输入/REPLAN 错误，2=用户在某 human() 检查点中止。

### 4.2 常用参数

| 参数 | 默认 | 用途 |
|---|---|---|
| `--max-method-rounds N` | 3 | 方法反思循环最大轮数；超过按「超轮未定稿」处理 |
| `--strict` | false | 严格模式：任一错误即退出码 1（CI 用） |
| `--replan-from <path>` | None | REPLAN 入口：模块三 `PlanningFeedback` 路径 |
| `--no-human-review` | false | 关闭 4 个检查点（CI/批处理用，schema 默认值 AUTO-APPROVE） |
| `--seeds N [N ...]` | `[42]` | ExecutionConfig.seeds（多随机种子跑实验） |
| `--max-retries N` | 2 | 单次实验最大重试次数 |
| `--timeout-seconds N` | null | 单次实验超时（秒）；null=不设 |
| `--dry-run` | false | 试跑：模块三读到 `dry_run=true` 时跳过实际训练 |

### 4.3 三种典型场景

**场景 A：首次跑（无 REPLAN）**
```bash
uv run python -m jiuwenswarm.resources.agent.workspace.skills.planning.scripts.main \
    --input-dir plan_input/ --output-dir runs/v1/
```

**场景 B：模块三返回 REPLAN 后重跑**（按 `blockers[].category` 路由）
```bash
uv run python -m jiuwenswarm.resources.agent.workspace.skills.planning.scripts.main \
    --input-dir plan_input/ --output-dir runs/v2/ \
    --replan-from runs/v1/planning_feedback.json
```

**场景 C：CI / 批处理**（关人审 + 严格）
```bash
uv run python -m jiuwenswarm.resources.agent.workspace.skills.planning.scripts.main \
    --input-dir plan_input/ --output-dir runs/ci/ \
    --no-human-review --strict
```

### 4.4 TUI 跑法（推荐）

如果用 JiuwenSwarm 提供的 TUI（其使用指南随宿主 JiuwenSwarm 安装提供），
进入 TUI 后按 `h` 键可在 4 个 human() 检查点看到结构化提问（Pydantic schema 转可点击 options），
不用手敲 `a/m/x`。

### 4.5 单独跑子步骤（调试用）

| 子脚本 | 用途 | 用法 |
|---|---|---|
| `load_inputs.py` | 仅输入解析 | `python -m scripts.load_inputs --input-dir <dir> [--strict]` |
| `validate_plan.py` | 仅产物契约校验 | `python -m scripts.validate_plan --method-design <p> --method-review <p> --experiment-plan <p> --data-plan <p> [--execution-config <p>]` |
| `write_outputs.py` | 仅写产物 | `python -m scripts.write_outputs --method-design <p> ... --output-dir <dir>` |
| `estimate_compute.py` | 仅算力估算+三档判定 | `python -m scripts.estimate_compute --estimate <p> --budget <num\|null>` |

### 4.6 跑回归测试

```bash
# 6 类 47 个结构性校验点
uv run python -m jiuwenswarm.resources.agent.workspace.skills.planning.tests.regression_check
```

预期输出：`47/47 pass`。

---

## 5. 升级路径（备忘）

等用户做 team 模式升级时：

1. `planning_flow.py` 的 `await human()` 调用**完全不动**——team 模式下 `human()` 行为一致。
2. 把 `iterate_method_design()` / `plan_experiment_with_data()` 内部从 `run_subagent` 切到 `await agent()` / `team_session`。
3. `main.py` 薄壳完全不动；只需检查 `run_workflow()` 签名兼容。
4. Pydantic schema 直接复用（框架的 `StructuredAskUserRail` 已支持 team 模式下的 human()）。

---

## 6. 关键文件指针

| 想看什么 | 看哪里 |
|---|---|
| 整体编排逻辑 | `scripts/planning_flow.py`（391 行） |
| CLI 入口 | `scripts/main.py`（176 行） |
| 4 个 Pydantic schema | `scripts/planning_flow.py` §"4 个 Pydantic schema" 段 |
| 人审改写回流 LLM 逻辑 | `scripts/iterate_method_design.py:79-81` 和 `scripts/plan_experiment_with_data.py:72-73` |
| 算力档位常量 | `scripts/planning_flow.py:104-106`（TIER_FULL/TRIM/CORE） |
| REPLAN 路由 | `scripts/planning_flow.py:163-181` |
| 3 persona 定义 | `references/{method-designer,method-critic,experiment-planner}.md` |
| 6 类 47 个回归测试 | `tests/regression_check.py` |
| 工作流文档 | `workflows/planning.md`（含 4 检查点表 + TUI 集成说明） |

---

**完。下次回来先做：**
1. 配 LLM `.env`
2. 用 `mock/` 跑一次端到端 CLI
3. 更新 `SKILL.md` Files 段
4. 升级到 team 模式（拆 `iterate_method_design` 内部）
