# 专家团成员工具渐进式加载

文件名：`tool_management/docs/features/F_01_team-progressive-tool-loading.md`。

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-13 |
| 范围 | `team_progressive.py`、`TeamInvokeAwarePermissionRail`、`RequestScopedMcpToolsRail`、registry/config_specs、单测 |
| 关联 spec | [[S_08]]、[[S_06]]、[[S_02]]、[[S_04]]、[[S_16]] |
| 关联设计叙事 | 外部方案 `专家团工具渐进式加载实现方案.md` |
| 测试基线 | `tests/unit_tests/agents/swarm/test_team_progressive.py` |

## 关联依据

- 成员此前无 `tools_search` / `invoke_tool`；宿主 ProgressiveToolRail 不裁成员工具表。
- 常驻名单按角色写死在代码，不复用单 agent yaml `eager_tools`。

## 团队 vs 单 agent 常驻名单取舍

| 维度 | 单 agent（yaml `eager_tools`） | 团队成员（代码常量） |
|---|---|---|
| 协作 | 无 | `send_message` / 任务板 / `build_team` 常驻 |
| Shell | 仅 `bash` | `bash` + `powershell` |
| 记忆 | 五件套 + external 常驻 | 仅 `memory_search` / `memory_get`；写入延迟 |
| 子代理 | `task_tool` + 六个 `subagent_*` 常驻 | `task_tool` 仅 GP 开启时追加；`subagent_*` 延迟 |
| 待办 | `todo_*` 常驻 | 延迟（团队走任务板） |
| 询问用户 | `ask_user` 常驻 | 仅 leader 常驻 |
| TTSE | `react.ttse.enabled` | **`react.ttse.team_enabled`**（与单 agent 开关独立） |

## 实际实现与开发决策

- 新增 `swarm.team_progressive_tool` provider：全员 + leader/teammate 追加 + 条件 `ttse_consult` / `task_tool`。
- `agent_card_id = {member_card}_s_{session}`，避免并发成员共享 meta-tool / OfficeClaw pin。
- `RequestScopedMcpToolsRail` 对本成员 `ProgressiveToolRail` 钉选；不调用宿主 sync。
- **故意不**在钉选后 `invalidate_deferred_tool_cache`：与宿主 sync 一致，id-only MCP 重绑由 stale 检查刷新实例缓存，导航指纹保持稳定。
- `ttse_consult` 常驻门控用 `get_team_ttse_enabled` + `inject_enabled`，避免 `team_enabled=true` 而单 agent `enabled=false` 时静默滤进 deferred。
- `TeamInvokeAwarePermissionRail`：对 `invoke_tool` 按目标工具做 TeamPermission 裁决，堵住 deferred 代理绕过 leader ASK。
- `code.team` / `team.plan` 额外常驻 `switch_mode` / `enter_plan_mode` / `exit_plan_mode`：与 `interface_code` 模式三件套对齐，避免 deferred 经 `invoke_tool` 绕过 `CodeConfirmInterruptRail` / `CodeAgentModeRail`。

## 与设计的偏差

| 原方案 | 实际 | 原因 |
|---|---|---|
| 钉选后同时 `invalidate_deferred_tool_cache` | 仅 `set_office_claw_active_tool_ids` | invalidate 会清导航指纹，每请求重写 TOOL_NAVIGATION，抵消 prefix cache；与 [[S_04]] / 宿主行为对齐 |

## 实际执行与证据

| 执行日期 / 验证范围 | 实现版本与环境 | 命令 | 实际结果 | 未覆盖项 |
| --- | --- | --- | --- | --- |
| 2026-11-13 单元 | 本分支 | `uv run pytest tests/unit_tests/agents/swarm/test_team_progressive.py -q` | 见当次运行 | 权限全链路 interrupt 集成未跑 |
| 2026-11-13 手工 | 备考团两场会话 | history `parts.tools` + 成员自报清单 | Leader 24 / teammate 21 直接可见；首轮 tools ~14.5–18.6k | 延迟工具 search→invoke 路径 |

## 验证结论

- 已证明：角色名单、TTSE team_enabled 门控、invoke_tool 目标 unwrap、card id、MCP 钉选、手工 token 下降。
- 未覆盖：开启 `enable_permissions` 时 `invoke_tool(mcp_exec_command)` 的端到端审批卡。

## 已知遗留

| 遗留项 | 性质 | 影响 | 后续 |
|---|---|---|---|
| 成员未挂 SkillActiveStateRail | 有意取舍 / 既有缺口 | `required_skill` 工具难靠 active_skill_provider 解锁；仍 fail-closed | 若产品要在成员上技能门控直接可见，另开特性挂 SkillActiveState |
| 权限 unwrap 端到端审批 | 未验证 | 单测只证 unwrap；需开权限团实测 | enable_permissions 团队调 ASK 工具 |
| WORKSPACE_QUOTA 对 `invoke_tool` 旁路 | 既有/配额默认关 | deferred 的 `mcp_exec_command` 等按外层名匹配不到配额门 | 通用 unwrap 若落地可一并覆盖；当前不扩 scope |
