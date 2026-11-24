---
title: 全局默认装备（SDD-0030）：新会话自动挂载默认 MCP
author: '@StoneChan_'
id: F_20261120_1534_session-default-equipment
type: F
modules:
- jiuwenswarm
files:
- jiuwenswarm/server/runtime/mcp/state_store.py
- jiuwenswarm/common/config.py
- jiuwenswarm/common/schema/message.py
- jiuwenswarm/server/dispatch.py
- jiuwenswarm/server/handlers/session.py
- jiuwenswarm/server/runtime/agent_adapter/interface.py
- jiuwenswarm/channels/web/frontend/src/services/equipmentDefaultsApi.ts
- jiuwenswarm/channels/web/frontend/src/stores/sessionStore.ts
- jiuwenswarm/channels/web/frontend/src/components/ChatPanel/ExtensionPickerPanel.tsx
- jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/McpDetailPage.tsx
- jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
- jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
- tests/unit_tests/agentserver/mcp/test_mcp_state_store.py
- tests/unit_tests/server/extensions/test_session_equipment_defaults.py
- docs/zh/MCP配置.md
- docs/en/MCPConfiguration.md
designs:
- SDD-0030
author_email: chensitong2@huawei.com
---

# 全局默认装备（SDD-0030）：新会话自动挂载默认 MCP

对应 Issue：GitCode #6383（GitHub #11108）。设计见 `designs/proposals/SDD-0030-session-default-equipment/design.md`。

**做了什么**：给连接器加「默认使用」标记（`state.json` `is_default`）；新会话首条消息没带 `mcp` 时按默认清单自动挂载一次，之后按会话快照走。后端（存储/挂载规则/读写接口）+ 前端（详情页复选框 + 新会话面板预勾选）+ 中英文档。

**验证**：新增单测 27 个、相关回归全绿；WS 直连冒烟（读/写/非法拒绝）通过；浏览器实测（复选框实时生效、新会话预勾选、取消勾选后不挂载）。

## 关键实现

- **存储**：`state_store.py` 新增 `is_default` 字段和四个读写函数；`replace_default_mcps` 加锁整体替换，防止并发写坏。
- **挂载规则**：`interface.py` `restore_chat_send_equipment_params()` 末尾加一级：会话无快照且消息省略 `mcp` 时补默认。「没有快照」和「快照是空的（用户清空过）」用返回值区分，后者不再补默认（逃生门）。
- **读写接口**：`session_equipment.defaults.get/set` 两个 RPC；set 整体替换、只收已连接项；WS 与 HTTP 共用 `handlers/session.py` 的同一份实现。
- **前端**：McpDetailPage 复选框（读 `runtime` 集、写完整清单）；新会话面板预勾选（`sessionStore.prefillDefaultMcps`），手动改过的会话以快照为准不被覆盖。
- **取舍**：默认来源只有 `state.json`（2026-11-24 社区简化，config.yaml 预置层已删）；数量不设上限（评审决定）。

## 已知边界

- **网关依赖（发布前置）**：前端 RPC 需 agent-gateway 三处名单放行（`_FORWARD_REQ_METHODS` 等）才对外生效；自动挂载本身不经过网关，先行生效。见 SDD-0030 §4。
- 只覆盖 `mcp`，不含插件和专家模板。
- 回合失败不存快照（既有行为），下一条消息默认重新生效。
