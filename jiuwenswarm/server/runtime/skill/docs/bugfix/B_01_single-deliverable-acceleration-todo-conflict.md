# 单交付物任务走加速通道时仍强制建外层 todo：前端任务列表显示异常

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_01 / single-deliverable-acceleration-todo-conflict |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/skill_protocol_prompt_rail.py`（`_CN_PROTOCOL` / `_EN_PROTOCOL` 规则②） |
| 归档日期 | 2026-10-21 |
| 关联 spec | S_05_agent-skill-rails.md（SkillProtocolPromptRail 段：9 条执行规范 ② 描述） |
| 发现渠道 | 线上会话定界（BUG20261021388603：单 PPT 任务前端右侧任务列表显示外层 todo 而非 skillturbo Stage 步骤；思考过程同时出现外层 todo 与 Stage 步骤） |

## 复现场景

单份交付物（一次 PPT）任务，外层 LLM 在 `skill_acceleration_exec` 加速通道可用时，
仍先调 `todo_create` 建外层待办清单（或与 `skill_acceleration_exec` 同轮并行发出），
再进入加速通道。结果是：

1. 外层 `todo_create` 建立了外层 todo（`_ACTIVE_TASK_ID` 被设置）→ 入口
   `skill_turbo_tools` 的 `has_outer_todo` 检测命中 → 进入「跳过内部 task.* 事件」
   分支（`skill_turbo_tools.py:1307-1323`）。
2. 加速通道内部的 13 个 Stage 步骤事件被抑制（`task.start/complete/update` 全跳过），
   前端右侧任务列表不再显示 Stage 步骤，只剩一个外层 todo 槽位。
3. 对单交付物任务，这个外层 todo 是**多余的**——加速通道本应用自己的 Stage 步骤
   事件独立展示进度，外层 todo 反而顶掉了它，造成前端任务列表异常。

复现会话：`officeclaw_eb9365c636e1516f35673f23`（同轮 `todo_create` +
`skill_acceleration_exec` 并行调用，reasoning 原话可证）。

## 根因

两段 system prompt 指令冲突：

- `SkillProtocolPromptRail`（`skill_protocol` 段）规则② **无条件**要求
  「执行 skill 步骤前必须先创建 todo 列表」，且「`todo_create` 必须和第一个工作
  工具同轮」——它把 todo 当成一切 skill 执行的强制前置。
- `SkillTurboPromptRail`（`skill_turbo_guide` 段）要求「第一个工具调用必须是
  `skill_acceleration_exec`」，且 todo 只在**多交付物**（需多次调用
  `skill_acceleration_exec`）时才强制建外层清单。

单交付物任务下两条指令互相矛盾：protocol 段让模型建 todo，turbo 段让模型第一个
工具直接进加速通道。模型在两者间摇摆，落到「建外层 todo」一侧时即触发上述前端
异常。protocol 段 docstring 声明「加速通道由 SkillTurboPromptRail 注入，本段不
重复该内容」，但其规则②缺少对加速通道的例外豁免，这正是冲突来源。

## 修复口径

在 `_CN_PROTOCOL` / `_EN_PROTOCOL` 规则② 增加**加速通道例外**：单份交付物直接走
`skill_acceleration_exec` 时**不建 todo**（加速通道内部用 Stage 步骤事件自行展示
进度）；仅单轮需产出多份交付物时才先建外层待办清单（规则已在 `skill_turbo_guide`
段「多交付物待办清单」中给出，本处只引用不重复）。

要点：
- 豁免条件精确到「单份交付物」——**不**放宽多交付物场景（那仍必须外层 todo，否则
  各交付物进度/总结互相顶替，见 skill_turbo B_03 / F_03）。
- 只增一行例外子弹，不新增规则编号，9 条执行规范计数不变。
- CN / EN 两版同步改，措辞对齐 `skill_turbo_guide` 段「多交付物待办清单」命名。

### 拒绝的方案

- **删掉 protocol 段规则②**：todo 搭便车纪律（`todo_create` / `todo_modify` 与工作
  工具同轮、最终回复前一轮例外、用户打断全标 cancelled）对 `skill_tool` 标准流程仍
  是有效契约，整体删除会伤及非加速路径。
- **在 `skill_turbo_guide` 段反向压制 protocol 段**：两段同 priority（45）都注入，
  靠「后注入覆盖」不可靠（分段顺序非契约），且把矛盾留给模型二次裁决；在源头段
  加例外才是唯一真相源。
- **豁免扩大到所有 `skill_acceleration_exec`**：会漏掉多交付物场景——那正是外层
  todo 必需且必须串行推进的形态（B_03 / F_03），不能一并豁免。

## 测试

- `tests/unit_tests/agentserver/rails/test_task_execution_todo_close_nudge.py`
  （新增 1 用例，先红后绿）：
  - `test_skill_protocol_exempts_single_deliverable_acceleration_channel`——
    断言 CN / EN protocol 文本均含「加速通道例外 / Acceleration-channel
    exception」「不要创建 todo / do NOT create a todo」与
    `skill_acceleration_exec`（复现本 bug：修复前文本无该例外）。
- 既有回归：`test_skill_protocol_allows_standalone_todo_modify_before_final`
  不受影响（规则②其余子弹未动）。
