# Team Plan 模式设计文档

## 1. 目标

新增两个 Team Plan 模式：

- `team.plan.normal`：Leader 和 Teammate 使用普通 DeepAgent 能力。
- `team.plan.code`：保留当前 `team.plan` 的 Code Profile 能力。

同时约定：

- `team.plan` 是 `team.plan.normal` 的正式别名，不废弃。
- 两种模式都由 Leader 先生成计划，用户批准后再由 Team 执行。
- `teammate_mode` 不负责选择 normal/code Profile。

## 2. 模式定义

| 输入模式 | 规范模式 | Adapter | Team Profile | 批准后模式 |
| --- | --- | --- | --- | --- |
| `team` | `team` | `DeepAdapter` | normal | - |
| `team.plan` | `team.plan.normal` | `DeepAdapter` | normal | `team` |
| `team.plan.normal` | `team.plan.normal` | `DeepAdapter` | normal | `team` |
| `code.team` | `code.team` | `CodeAdapter` | code | - |
| `team.plan.code` | `team.plan.code` | `CodeAdapter` | code | `code.team` |

模式入口先处理别名：

```python
MODE_ALIASES = {
    "team.plan": "team.plan.normal",
}
```

解析结果应显式携带 `profile`，后续不再通过模式字符串猜测成员能力：

```python
ResolvedMode(
    canonical_mode="team.plan.normal",
    manager_mode="team",
    is_team=True,
    is_plan=True,
    profile="normal",  # normal | code
    normal_mode="team",
)
```

Plan 退出关系统一维护：

```python
PLAN_EXIT_MODES = {
    "team.plan.normal": "team",
    "team.plan.code": "code.team",
}
```

## 3. TeamAgentSpec 组装

Team 继续通过 `TeamAgentSpec` 和成员 `DeepAgentSpec` 声明式构建。建议在 `SwarmBuildContext` 中增加：

```python
profile: Literal["normal", "code"]
is_plan: bool
```

成员能力按以下维度组装：

```text
profile 决定：normal 能力或 code 能力
role 决定：Leader 能力或 Teammate 能力
is_plan 决定：是否给 Leader 增加计划审批能力
```

避免继续使用 `mode in ("code.team", "team.plan")` 一类判断，否则 `team.plan.normal` 容易误装 Code 能力。

## 4. Leader 与审批

### 4.1 Normal Leader

`team.plan.normal` 的 Leader 走 DeepAgent normal Profile：

- 使用普通 `team` Leader 的提示词、工具、MCP、Skill 和 Rail。
- 不挂 Code 专属提示词、工具、子智能体或约束。
- 额外挂载 Team Plan 审批 Rail。

计划阶段只允许分析和拆分任务。计划应包含任务分工和依赖关系，审批前不能开始执行。

### 4.2 Code Leader

`team.plan.code` 保留当前 Code Team Plan 行为：

- 使用 Code Profile 提示词和工具。
- 保留 `CodeAgentModeRail` 等 Code 能力。
- 审批通过后进入 `code.team`。

### 4.3 审批机制

两种模式共用审批协议和状态机，但使用不同的计划提示词与能力：

| 项目 | `team.plan.normal` | `team.plan.code` |
| --- | --- | --- |
| 计划生成 | DeepAgent / Work Rail | Code Rail |
| 计划侧重点 | 通用任务分解和协作 | 代码、文件和验证步骤 |
| 审批协议 | `plan_approval` | `plan_approval` |
| 批准后模式 | `team` | `code.team` |

公共审批逻辑负责：

1. 发出计划审批 interrupt。
2. 接收批准、修改意见或拒绝。
3. 批准后切换到对应普通模式。
4. 通知 Leader 使用 Team 工具分派执行，不能由 Leader 独自完成全部任务。

用户要求修改计划时，保持原 Plan 模式，更新计划后再次审批。

## 5. Teammate 行为

### 5.1 Normal Teammate

`team.plan.normal` 的 Teammate 必须与普通 `team` 完全一致：

```text
member_spec(team.plan.normal, teammate)
    == member_spec(team, teammate)
```

它使用普通 DeepAgent 和 Team Teammate 的提示词与能力，不挂载：

- Code Profile 提示词和工具。
- Code 专属 Rail 或子智能体。
- Leader 专属计划审批 Rail。

### 5.2 Code Teammate

`team.plan.code` 的 Teammate 与 `code.team` 完全一致：

```text
member_spec(team.plan.code, teammate)
    == member_spec(code.team, teammate)
```

### 5.3 `teammate_mode`

`teammate_mode` 只控制原有 Teammate 行为，不改变 Profile，也不决定是否挂审批能力：

```text
profile = resolved_mode.profile
teammate behavior = teammate_mode
```

因此，`team.plan.normal` 不会因 `teammate_mode` 变成 Code Teammate，`team.plan.code` 也始终保持 Code Profile。

## 6. 代码改动

### 模式解析

修改 `jiuwenswarm/common/mode_matrix.py`：

- 注册两个新模式和 `team.plan` 别名。
- 增加显式 `profile`。
- 更新 Team、Plan 模式集合和 Plan 退出映射。

同时更新 CLI、TUI、Server 和 Session 中硬编码的 Team 模式集合。

### Team 构建

主要修改：

- `jiuwenswarm/agents/swarm/context.py`
- `jiuwenswarm/agents/swarm/config_specs.py`
- `jiuwenswarm/agents/harness/team/team_manager.py`

将当前针对 `team.plan` 的 Code 特判改为按 `profile` 构建；计划审批 Rail 只挂到 Plan Leader。

### 审批 Rail

保留现有审批消息结构，抽取公共审批状态机。Normal 与 Code Leader 分别使用自己的计划提示词和能力。

## 7. 测试要点

1. `team.plan` 与 `team.plan.normal` 解析结果一致。
2. Normal/Code 模式分别选择 `DeepAdapter`/`CodeAdapter`。
3. Normal Teammate 与 `team` 的组装结果一致。
4. Code Teammate 与 `code.team` 的组装结果一致。
5. 只有 Plan Leader 挂审批 Rail。
6. Normal Leader 不包含 Code 专属能力。
7. 两种模式批准后分别切换到 `team` 和 `code.team`。
8. 修改计划后保持原模式并再次审批。
9. `teammate_mode` 不改变 Profile。

## 8. 核心约束

```text
team.plan == team.plan.normal

team.plan.normal teammate == team teammate

team.plan.code teammate == code.team teammate
```

两种模式只共用审批协议，不共用 Profile。Normal 全链路保持 DeepAgent 语义，Code 全链路保持 Code Profile 语义。
