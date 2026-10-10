# Plan Supervisor / 规划总管

CCF BDCI 2026 论文生成系统「规划模块（模块二）」**协调者 agent**。持长 session、接跨模块 context、做 4 检查点人审、聚合 REPLAN 反馈，把 planning-skill 当子进程调。

> 设计文档：`D:/jiuwenswarm/planning-agent-supervisor-todo.md`
> 上游 Skill：`skills/planning/SKILL.md`

---

## 为什么需要这个 agent

规划 skill 本身是一次性的（SwarmFlow 跑完一份产物就完），4 阶段 deterministic 流程不需要长 session。但协调层需要长生命周期（history / 跨模块 context / REPLAN 状态）——**这些不属于 skill 的职责**。所以把"协调者"层拆出 skill，做成 agent。

- **skill 退化为纯生成器**：subprocess 跑一次，输出 5.json + 5.md + status.json，**不审、不管历史、不管跨模块**
- **agent `plan-supervisor` 拥有协调者职责**：长 session、history、跨模块 context、4 个检查点人审、REPLAN 反馈聚合
- **agent 把 skill 当 tool 调**，agent 看不到 skill 内部的 4 sub-agent

---

## 能力构成

- **Persona**：定义规划总管角色、4 检查点流程、approve/modify/abort/details 路由、REPLAN 反馈聚合、跨模块 context
- **Tool**：`call_planning_skill` —— subprocess 调 planning-skill，包装 input_dir / output_dir / feedback_file 协议，读 status.json 返回
- **Rail**：`input_validation_rail` —— 注入输入契约 prompt（7 个模块一文件 + PlanningFeedback 格式），引导 LLM 调 tool 前自查
- **无 Skill / 无 MCP** —— 业务执行在 planning-skill 子进程里；plan-supervisor 本身不打包 skill（TODO §4.1 明确写"无 skills"）

---

## 工作流程

```
Step 0: 拉起 + 准备（检查 input_dir / output_dir）
Step 1: 调 call_planning_skill 跑产物（5.json + 5.md + status.json）
Step 2: 4 检查点（顺序固定）
  [1/4] method_design      → approve / modify / abort / details
  [2/4] experiment_plan    → approve / modify / abort / details
  [3/4] budget / feasibility（仅 tier>0 触发）→ downgrade / manual / abort / details
  [4/4] execution_config   → approve / modify / abort / details
Step 3: 最终总结（产物清单 + status 指标 + 跨模块交接提示）
```

### modify 路由

```
用户选 modify
  → 收 user feedback
  → 拼 PlanningFeedback JSON（user 反馈 + 阶段名 + 上一轮产物关键字段）
  → 写 feedback_file
  → 调 call_planning_skill（带 feedback_file）
  → 重回当前检查点
```

modify 最多 2 轮；2 轮后仍 modify → abort。

### 跨模块 context

其他模块（执行模块三、写作模块四）通过 `PlanningFeedback` JSON 文件注入反馈：

```json
{
  "blockers": [
    "[schema] data_plan.usage_plan 字段缺失",
    "[experiment] 真实跑下来发现消融 A 不显著，建议加大 seed 数"
  ],
  "hints": ["执行模块反馈：method.components[2] 实现复杂"],
  "previous_artifacts_dir": "<output_dir>"
}
```

`blockers` 数组每条以 `[category]` 开头：当前支持 8 类别 —— `data` / `compute` / `baseline` / `metric` / `schema` / `method` / `experiment` / `budget`。`--feedback-file` flag 把反馈灌进 skill。

**类别路由**（agent 决策树）：
- `[data]` / `[schema]` / `[method]` → stage-1 method_design REPLAN
- `[experiment]` → stage-2 experiment_plan REPLAN
- `[budget]` / `[compute]` → stage-3 tier_downgrade 检查
- `[baseline]` / `[metric]` → stage-2 experiment_plan REPLAN（涉及 baseline/eval 调整）

---

## 工具协议（Skill ↔ Agent）

### 输入（agent → skill）

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `--input-dir` | path | ✅ | 模块一产物（7 个 JSON） |
| `--output-dir` | path | ✅ | 产物落盘目录 |
| `--feedback-file` | path | ❌ | REPLAN 反馈 JSON（替代旧 `--replan-from`） |
| `--status-file` | path | ❌ | status.json 落点（agent 默认指向 `<output_dir>/status.json`） |
| `--no-human-review` | flag | ❌ | 兼容旧 flag；agent 模式下**不传**（人审归 agent） |
| `--strict` | flag | ❌ | 严格模式 |
| `--max-method-rounds` | int | ❌ | 方法反思最大轮数 |
| `--seeds` | int[] | ❌ | execution_config.seeds |
| `--max-retries` | int | ❌ | execution_config.max_retries |
| `--timeout-seconds` | int | ❌ | execution_config.timeout_seconds |
| `--dry-run` | flag | ❌ | execution_config.dry_run |

### 输出（skill → agent）

| 文件 | 说明 |
|---|---|
| `method_design.json` / `.md` | 方法设计产物 |
| `experiment_plan.json` / `.md` | 实验方案产物 |
| `data_plan.json` / `.md` | 数据方案产物 |
| `execution_config.json` / `.md` | 执行配置 |
| `budget_report.json`（可选） | 算力估算 |
| `check_feasibility_output.json`（可选） | 门禁校验结果 |
| **`status.json`** | `{status, artifacts, errors, warnings, check_feasibility, method_rounds_used, wall_time_seconds}` |

### 退出码

| 退出码 | 含义 |
|---|---|
| 0 | status=complete |
| 1 | status=error |
| ~~2~~ | ~~aborted（已删除——abort 决定权在 agent）~~ |

---

## 目录结构

```
plugins/agent_templates/plan-supervisor/
├── manifest.json                              # agent 注册（id=plan-supervisor）
├── README.md                                  # 本文件
├── persona/
│   └── plan-supervisor.md                     # 角色 + 4 checkpoint 流程 + REPLAN 路由（4 checkpoint 调框架 ask_user tool）
├── tools/
│   └── call_planning_skill.py                 # subprocess 调 planning-skill + status.json 协议
├── rails/
│   ├── input_validation_rail.py               # 输入契约 rail（DeepAgentRail，注入 7 文件清单 + PlanningFeedback 格式）
│   └── ask_user_rail.py                       # 框架 AskUserRail 的本地 wrapper（让 manifest 能 {file,class} 引用）
└── (无 skills/ / runner/ / __main__.py — 框架 invoke)
```

**为什么没有 `skills/` 子目录** —— planning-skill 是在子进程里当黑盒调，不复制成子 skill。agent 看不到 skill 内部的 4 sub-agent，agent 只看产物 + status.json。详见 TODO §1.3 架构分层图。

**为什么没有 `runner/` / `__main__.py`** —— `openjiuwen.harness.DeepAgent` 读 `manifest.json` 自动实例化 agent + 加载 persona/tools/rails。框架自己负责 LLM 循环、tool calling、session 管理、checkpoint context。**不要**自建 ReAct 循环。

---

## 框架集成点

| 概念 | plan-supervisor 用什么 | 框架机制 |
|---|---|---|
| Agent runtime | `openjiuwen.harness.DeepAgent` 通过 `load_agent_template_package(manifest.json)` 实例化 | `harness/resources/extension_loader.py:193` |
| Persona prompt | `persona/plan-supervisor.md` | `SystemPromptBuilder` 装配（Harness.md:136） |
| Tool calling | `tools/call_planning_skill.py`（subprocess wrapper） | 框架原生 tool calling，不需要解析 XML |
| 人审 / 4-checkpoint 决策 | `rails/ask_user_rail.py` → 框架 `AskUserRail` → `ask_user` tool | 框架自动渲染菜单 / 收 user 决策 / 注入到 `answers` |
| 输入契约注入 | `rails/input_validation_rail.py`（`before_model_call` 钩子） | `DeepAgentRail` 基类 |
| 长 session / 跨模块 context | `agent.invoke(query, session=...)` / `agent.steer(...)`（Harness.md:526, 542） | `openjiuwen.core.runner.Runner` |
| Subprocess tool 权限 | `call_planning_skill` 不是 ToolCard `severity` 字段（`ToolCard` 没有这个字段，`base.py:22-65`），走 `permissions.tools.call_planning_skill: "allow"` 让 PermissionEngine 直接 allow。`/permissions allow call_planning_skill`（TUI）或者写 `~/.jiuwenswarm/config/config.yaml` | `harness/security/tiered_policy.py:373-400`（_baseline_level 读 string level）|

### `ask_user` tool schema

人审的 4 个 checkpoint 都通过 `ask_user` tool 收用户决策。schema（来源 `openjiuwen/harness/prompts/tools/ask_user.py`）：

```json
{
  "questions": [
    {
      "question": "[1/4] method_design 是否通过？",
      "options": [
        {"label": "approve", "description": "进入检查点 2（experiment_plan）"},
        {"label": "modify",  "description": "收集反馈后 REPLAN 方法阶段"},
        {"label": "abort",   "description": "终止整个规划流程"},
        {"label": "details", "description": "重新渲染 method_design 摘要再问"}
      ],
      "multi_select": false
    }
  ]
}
```

约束：1-4 questions, 2-4 options per question。**不要**自己加 "Other" / "Custom" 选项（系统自动追加）。

### 加载验证

```python
from openjiuwen.harness.resources.extension_loader import load_agent_template_package
spec = load_agent_template_package(
    "jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/manifest.json"
)
# spec.agent_card.id == "plan-supervisor"
# spec.tools[0] → CallPlanningSkillTool (file-based)
# spec.rails[0] → InputValidationRail (file-based)
# spec.rails[1] → AskUserRail (file-based, wraps openjiuwen built-in)
```

---

## 复用自 planning skill

| 复用项 | 路径 | 用途 |
|---|---|---|
| skill 主入口 | `skills/planning/scripts/main.py` | subprocess 调 `-m scripts.main` |
| skill 产物 schema | `skills/planning/references/planning-schemas.md` | 产物字段定义（与 agent 摘要渲染对齐） |
| skill 摘要渲染 | `skills/planning/scripts/_human_ui.py` (`render_summary`) | 检查点摘要渲染（method / experiment）—— agent 可在 persona 里直接复用相同字段抽取逻辑 |
| skill 退出码 | `skills/planning/scripts/main.py` (main 函数) | 退出码 → status 映射（fallback 路径） |
| skill 7 个输入文件清单 | `skills/planning/scripts/load_inputs.py` (`_INPUT_FILES`) | rail 注入契约 prompt 的数据源——**与 skill 单一事实源** |
| skill PlanningFeedback category | `skills/planning/scripts/load_inputs.py` (`_FEEDBACK_CATEGORIES`) | rail 注入契约 prompt 的 category 集合——**与 skill 单一事实源**（**8 类别**：data/compute/baseline/metric/schema/method/experiment/budget） |

---

## Phase 进度

```
Phase A: 写 agent（不动 skill）                ✅ 完成
   A1. 设计 status.json / feedback-file schema    ✅
   A2. 写 plan-supervisor manifest.json + persona  ✅
   A3. 写 call_planning_skill tool (subprocess)    ✅
   A4. skill 加 --status-file 支持                 ✅
   A5. smoke test agent → skill → 读 status.json   ✅

Phase B: 改 skill（agent 跑通后）              ✅ 完成
   B1. 删 _human_ui.py                              ✅
   B2. 删 planning_flow.py 4 个 _prompt_human_menu  ✅
   B3. 删 main_sess 拉起 + aclose                   ✅
   B4. planning_main_session 接受 sess=None         ✅
   B5. main.py 加 --feedback-file / --status-file   ✅
   B6. skill 单独 smoke                             ✅

Phase C: 端到端联调                              ✅ 完成（接入框架原生机制）
   C1. manifest 走框架 load_agent_template_package  ✅
   C2. 4 checkpoint 调 ask_user tool (AskUserRail)   ✅
   C3. 8 类别 PlanningFeedback 扩展                  ✅
   C4. framework 集成验证 (manifest 加载 + spec 解析) ✅

注：原 Phase C 计划自己手搓 runner/ + AskUserTool + ReAct 循环——已撤销。
框架 DeepAgent + AskUserRail 已经提供这些能力，没必要重新发明。
```

---

## 使用方式

### 1. Web / TUI（产品形态）

在 JiuwenSwarm 专家中心打开 `plan-supervisor` 专家（agent id=`plan-supervisor`），按引导对话。首次对话提供 `input_dir`（模块一产物目录）+ `output_dir`（落产物的目录），随后 agent 自动跑 4 检查点流程，每到一个检查点由框架 `AskUserRail` 渲染摘要 + 4 选 1 菜单让你决策。

### 2. Python API（dev / smoke test）

```python
from openjiuwen.harness.resources.extension_loader import load_agent_template_package
from openjiuwen.harness import DeepAgent

spec = load_agent_template_package(
    "jiuwenswarm/resources/agent/workspace/plugins/agent_templates/plan-supervisor/manifest.json"
)
agent = DeepAgent(spec=spec)  # 框架自动注册 persona/tools/rails
# agent.invoke({"query": "..."}, session=session)
```

参考 `docs/en/Harness.md:478-537` 看完整 `create_deep_agent(...)` 用法。

### 3. 子进程底层命令（agent 内部调，调试时也可手跑）

```bash
cd D:/jiuwenswarm/jiuwenswarm/resources/agent/workspace
uv run python -m scripts.main \
    --input-dir <input_dir> \
    --output-dir <output_dir> \
    --status-file <output_dir>/status.json \
    [--feedback-file <feedback_file>]
```

---

## 注意事项

- **不要直接读 / 改产物 .json**——让 `call_planning_skill` 统一管理产物落盘
- **不要绕过 4 检查点顺序**——method → experiment → budget → execution 顺序固定
- **abort 决定权在用户**（人审 action 路由），agent 不擅自决定
- **REPLAN 反馈必走 feedback_file**——不要把 feedback 字符串直接塞进 skill 调用
- **status.json 必读**——`check_feasibility.passed` / `tier` / `method_rounds_used` 都从 status.json 取
- **失败回退**：`call_planning_skill` 失败（spawn 失败 / 超时 / 退出码非 0）时立刻把异常渲染给人审
