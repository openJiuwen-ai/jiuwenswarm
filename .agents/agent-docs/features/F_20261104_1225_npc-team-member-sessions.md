---
id: F_20261104_1225_npc-team-member-sessions
title: NPC 主从会话与成员咨询
type: F
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/server/runtime
  - jiuwenswarm/agents/harness/common
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/server
  - docs
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin
files:
  - gateway/channel_manager/web/web_connect.py
  - tests/unit_tests/channel/test_web_channel_personal_context_payload.py
  - jiuwenswarm/runtime/app_role.py
  - jiuwenswarm/runtime/session_provisioner.py
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_app_role.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_manager.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/agents/swarm/assembly.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - tests/unit_tests/agentserver/test_app_role_npc_team.py
  - tests/unit_tests/agentserver/test_app_role_adapter.py
  - tests/unit_tests/agentserver/test_team_helpers.py
  - tests/unit_tests/runtime/test_session_create_provisioner.py
  - tests/unit_tests/test_session_ops_fork.py
  - docs/zh/AppRoleSessions.md
  - docs/en/AppRoleSessions.md
  - jiuwenswarm/resources/agent/workspace/skills/workswarm-builtin/references/capabilities/workswarm-app-builder/SKILL.md
designs:
  - D-20261104-1141-npc-team-member-sessions
---

# NPC 主从会话与按需搜索工具

## 范围与决定

基于 `feat/app-market` HEAD `96fe7b777` 创建 `codex/npc-team-members`。按用户确认的三项决策实施：member 必须是真实会话且在 session 页面与普通 chat-session 同样可查看；member session 复用现有 app_role 实现；宿主未配置搜索能力时创建请求直接报错。当前契约见 `S_02_app-role-npc-team`；设计确认与阶段见关联 D。

## 2026-11-06 用户修订：团队 NPC 独立发言

应用生成阶段可选择单角色或团队 NPC。团队模式改用原生 Team 会话与固定 MemberSession 名册。玩家输入仅进入 leader，各成员通过 Team 调度运行自己的发言轮。leader 和成员的最终文本分别带角色身份，生成 skill 要求应用逐条展示最终发言与必要状态，并在回合结束时检查缺席角色。成员发言由模型通过 Team 任务调度，应用将缺席视为回合不完整。

此前独立轻量咨询的调用契约继续用于 `mode:"agent"` 携名册的旧调用。新应用按更新后的 `workswarm-app-builder` skill 传 `mode:"team"`。同时修正旧咨询工具的 OpenAI function 声明和 Web 创建入口吞掉错误名册类型的问题。

验证使用 Windows 项目 `.venv`。定向用例覆盖创建及构建原生 Team、角色独立署名输出、应用事件过滤、普通 Team 输出和装配回归、旧轻量咨询，共 7 项通过。真实供应商与生成应用界面仍需联调。

## 2026-11-04 用户修订：撤销宿主工具注入

用户在检视后要求撤销工具注入。本次移除 `web_search` 的声明、执行分支、全局缓存、搜索配置预检、提示词变体和 `role_tools` 的持久化/幂等/fork 传播；创建输入保留 `role_tools` 仅用于显式拒绝 API 的非空 `tools`。保留固定 NPC 名册、真实成员会话及 `ask_<name>` 咨询。已有 metadata 的搜索配置不再生效，fork 不带回旧装备。此修订替代下文初版记录中的工具白名单与搜索实现，原始验证结果仅描述当时版本。

该次撤销未处理成员删除回退普通装配和 `npc.member` Gateway 丢字段；后续修复见下节。当前契约见 `S_02_app-role-npc-team`。

本次验证：2026-11-04，基线 `96fe7b777` 上的当前工作区，Windows，项目 `.venv` Python 3.12.13。执行 `python -m pytest` 定向选择以下节点，附 `-q --no-cov`：

- `tests/unit_tests/agentserver/test_app_role_npc_team.py::test_default_role_keeps_tools_none`
- `tests/unit_tests/agentserver/test_app_role_npc_team.py::test_leader_asks_member_and_receives_tool_result`
- `tests/unit_tests/agentserver/test_app_role_npc_team.py::test_member_session_exposes_no_ask_tools`
- `tests/unit_tests/agentserver/test_app_role_npc_team.py::test_validate_app_role_request_rejects_equipment_and_pins_roster`
- `tests/unit_tests/agentserver/test_app_role_npc_team.py::test_tool_budget_exceeded_raises`
- `tests/unit_tests/agentserver/test_app_role_npc_team.py::test_leader_rejects_unadvertised_search_tool`
- `tests/unit_tests/runtime/test_session_create_provisioner.py::test_app_role_rejects_tool_equipment_before_claim`
- `tests/unit_tests/runtime/test_session_create_provisioner.py::test_npc_team_create_cascades_member_sessions`
- `tests/unit_tests/test_session_ops_fork.py::TestForkSessionNpcTeam`（2 个用例）

结果：`10 passed in 9.64s`。覆盖创建/聊天装备拒绝、模型实际收到的工具集合、模型返回意外搜索调用时拒绝、成员无工具、leader 咨询与预算、级联创建和旧工具配置不随 fork 传播。未调用真实模型或搜索 API；未运行全量 UT、格式化或静态检查。

## 2026-11-04 检视问题修复与提交

用户明确授权修复已发现问题并新建中文 commit。本次提交包含尚未提交的 NPC 主从特性、此前按用户要求撤销搜索工具的修改，以及本轮修复；不推送远端。

- 根因：executor 未验证成员仍为所属 leader 的 app_role，会话不存在时通用 selector 会走普通 Agent 装配。现在 executor 先校验 profile 与 `npc_member` 的 leader/name，selector 在 session adapter 锁内强制刷新元数据、再次校验并调用 lifecycle guard；删除、归档、改绑或 profile 丢失都不能回退通用装配。缺失/改绑与选择时删除回归同时断言没有普通 factory 调用、没有咨询历史写入。
- 根因：Gateway 的结构化事件保留规则未登记 `npc.member`，事件降为 session_id/空 content。现在完整保留成员、phase、摘要、用量及会话 ID，并补齐外层 request_id，客户端可关联到主会话请求。保留既有 personal_context 事件的回归证据。
- 之前合并提交检视发现的两个技能路径问题同时修复，范围与独立磁盘证据见 `B_20261104_1509_catalog-script-paths`。

验证：Windows、项目 `.venv` Python 3.12.13、基线 `96fe7b777` 的当前候选。`python -m pytest -q --no-cov` 定向选择 `test_app_role_npc_team.py` 中新增的缺失/改绑、删除竞态用例及原有持久化/空回答/leader 咨询/成员失败用例，`test_app_role_adapter.py::test_root_restores_role_adapter_without_building_general_agent`，`tests/unit_tests/channel/test_web_channel_personal_context_payload.py` 和 `tests/unit_tests/resources/test_catalog_script_paths.py`。结果 `17 passed in 11.48s`；后续只重验 PPT manifest 去重的单个用例，结果见关联 B。没有调用真实供应商或搜索接口，没有运行全量 UT、格式化或静态检查。此前撤销搜索的 10 项证据仅在对应代码未变的范围继续有效。

仍未进行真实 Gateway WebSocket/UI 和模型联调；并发咨询顺序、计量聚合等不在本轮已确认问题的验证范围，不能据此宣称所有主从场景均已验收。

## 初版实施与取舍（搜索部分已撤销）

- `app_role.py` 新增 `normalize_npc_team`/`normalize_role_tools`（不静默降级风格）与三档基座提示（默认/带搜索/leader 名册、member 基座）。`validate_app_role_request` 的 `tools` 由无条件拒绝改为"必须等于创建时白名单"，`npc_team` 不可变按定义比较。
- provisioner 在 leader 创建事务内级联：先取成员 session id，leader metadata 一次写全（含宿主内部绑定 `npc_member_sessions`），再逐个 init 成员 metadata；任一失败删除已写会话目录后整体失败。`web_search` 在创建时预检 `is_paid_search_enabled() or is_free_search_enabled()`，无能力报 `BAD_REQUEST`。default profile 携 `npc_team`/`tools` 显式拒绝，不静默忽略。
- `interface_app_role.py` 改造为受限 ReAct 循环：`AssistantMessageChunk` 累计分片 tool_calls，单条 assistant 消息同时携带轮内文本与 tool_calls，`ToolMessage` 回填继续生成；工具执行统一 `tool.invoke()`，不经 AbilityManager。leader 工具 = 搜索 + 每成员 `ask_<name>` 动态定义；成员无 ask 工具，天然不递归。迭代上限 leader 5 / 成员 3；工具中间消息不落持久化历史。
- `AppRoleMemberExecutor`（facade 构造 leader adapter 时注入）：经 `_get_or_create_session_adapter` 取成员自己的 `AppRoleAdapter` 跑内部子请求，user 问题与 `chat.final` 用主路径同一 `append_history_record` 形状写成员历史（completion_state 走 extra 平铺，已核实 `build_role_messages` 可读回）。成员失败/被单独删除时返回错误文本，leader 容错，不自愈重建。
- fork：leader 透传名册与白名单、绑定不复制并级联全新成员会话；单 fork 成员剥离 `npc_member` 成独立 NPC。删除 leader 成功后级联删除成员（best-effort，不回滚 leader 删除）。
- 元数据四个新字段全部纳入首绑定保护块与 create_token 幂等签名。

取舍：不走产品 team 链路（每成员完整 DeepAgent + team.db 物化会摧毁最小输入与隔离边界，调研见 D 第 2 节）；member 不用"无状态工具"形态（用户已决策要求真实可查看会话，且历史/删除/查看全部复用现有实现）。运行时绑定单独存 `npc_member_sessions` 而非并入 `npc_team`，保持创建参数形状与 `normalize_npc_team` 的纯净。

设计偏差：无（按 D 第 5 节实施）。补充实现细节：leader 轮内文本与 tool_calls 合并为单条 assistant 消息（AssistantMessage 的 content 不接受 None，纯工具轮用空串）。

## 验证证据

Windows，项目 `.venv` Python 3.12.13，2026-11-04，分支 `codex/npc-team-members` 工作区候选：

```text
python -m pytest tests/unit_tests/agentserver/test_app_role_npc_team.py tests/unit_tests/agentserver/test_app_role_adapter.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/test_session_ops_fork.py tests/unit_tests/agentserver/test_session_create_runtime_boundary.py tests/unit_tests/test_work_mode.py tests/unit_tests/test_app_market_runtime.py tests/unit_tests/agentserver/test_agent_warm_pool.py tests/unit_tests/agentserver/test_agentserver_acp.py -q
221 passed in 22.52s
```

新增用例覆盖：名册/白名单校验（参数化非法集）、默认会话 `tools=None` 回归基线、leader 咨询全链路（工具定义集合、`npc.member` started/finished、ToolMessage 回填、final）、成员无 ask 工具与 member 基座、预算超限报错、成员失败降级文本、executor 落盘契约（user/assistant 记录形状与 completion_state）、空回答拒绝、provisioner 级联（leader/成员 metadata、绑定、claim 透传）、无搜索能力创建拒绝（claim 前）、default profile 携新参数拒绝（claim 前）、token 重试名册冲突（存量不被改写）、fork 级联（新绑定、成员 metadata、title）与成员 fork 剥离。

既有 `test_app_role_adapter.py` 两处按签名变更适配：`build_role_messages` 首参改为 system；"意外 tool_calls 拒绝"语义保留（无工具会话收到 tool_calls 仍报 `app_role does not execute tool calls`，不让它变成执行路径）。回归范围含 fork/边界/预热/ACP/工作模式/应用市场运行时，均通过。

定位过程记录：chunk 构造对 SimpleNamespace 拒绝（pydantic）改用真 `ToolCall`；`asyncio.coroutine` 在 3.12 已移除改 async def；`_search_tool_definition` 对无 card 的工具实例加 getattr 防御。

未覆盖：真实供应商 function calling（当前证据来自脚本化 mock 模型）、真实 Gateway/UI 下 session 页面展示、并发同成员咨询排队行为、真实搜索 API 调用。未运行全量 UT、格式化或静态检查（本仓约束禁止）。

## 交接与限制

- 需部署重启宿主后经应用新会话流程启用；现存 NPC 不迁移。
- member 会话可被用户从 UI 直接对话（人工引导），该消息会进入成员后续咨询上下文——文档已写明，属预期特性。
- 供应商不支持 function calling 时模型自然不调用工具，功能退化但无害。
- Gateway `npc.member` 字段转换已通过真实转换方法验证；真实 WebSocket/UI 联调仍待后续完成。提交结果以 Git 实际 SHA 为准。
