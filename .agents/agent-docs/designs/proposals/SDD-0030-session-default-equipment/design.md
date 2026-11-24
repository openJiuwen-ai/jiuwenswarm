---
id: SDD-0030
title: 全局默认装备（Default Equipment）：新会话自动挂载默认扩展/连接器
slug: session-default-equipment
status: implemented
stage: proposals
reviewers: []
created: 2026-11-20
updated: 2026-11-24
version: 3
domain: core
modules: [server/runtime/agent_adapter, server/runtime/session, server/runtime/mcp, gateway, channels/web/frontend]
tags: [mcp, session-equipment, default-equipment, chat-send, ux]
related: []
author: 'StoneChan_'
author_email: 'chensitong2@huawei.com'
type: D
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
issue: 'GitCode #6383 / GitHub #11108'
---

# 全局默认装备：新会话自动挂载默认连接器

> 对应 Issue：GitCode #6383（GitHub #11108）。方案已实现并通过端到端验证，实施记录见 `features/F_20261120_1534_session-default-equipment.md`。

## 1. 问题

在「扩展 / 连接器」里装好并连接一个 MCP 后，新建会话还是空的——每个会话都要手动勾一遍，工具才可用。

原因：连接器和会话是两套状态。连接成功只把 `state.json` 里的状态置为 `connected`，不代表它进了任何会话；会话里挂载哪些 MCP，由每条 `chat.send` 消息的 `mcp` 字段决定。

（`config.yaml` `mcp.servers` 注册的 MCP 本来就对所有会话可见，不受此问题影响。）

## 2. 方案（已实现）

一句话：给每个连接器加一个「默认使用」标记（`state.json` `is_default` 字段）；新会话第一次发消息时，如果没带 `mcp` 信息，就自动按这份默认清单挂载。

### 2.1 存储

`state.json` 每条连接器记录新增 `is_default: bool`。读写由 `state_store.py` 的三个函数负责：

| 函数 | 作用 |
|---|---|
| `list_default_mcps` | 列出生效的默认（只算已连接的） |
| `list_default_flagged_mcps` | 列出所有被标记的（含断连，供界面灰显） |
| `replace_default_mcps` | 一次性整体替换（加锁防并发写坏） |

### 2.2 挂载规则（谁说了算）

一条 `chat.send` 消息的装备按三级取值，从高到低：

```
消息里带了 mcp（用户此刻的选择） > 会话快照（上次用过什么） > 全局默认
```

- 「快照」= 会话上一轮成功后存下的装备记录（`session_equipment`）；
- 默认只在**前两级都空**（新会话、且没碰过扩展面板）时补一次；补完即存快照，之后默认清单怎么改都不影响这个会话；
- 用户在会话里明确清空过（发送 `mcp: []`）的，默认**永远不会再灌回去**；
- 只处理 `mcp`，不动插件和专家模板。

### 2.3 读写接口

新增两个 RPC：`session_equipment.defaults.get / set`（前端复选框用它读写默认清单）。

- get 返回：生效名单 + 全部标记 + 断连部分；
- set 收一份完整清单整体替换；每个名字必须已连接，否则报错并说明原因（断连的设了默认也不会生效，不如当场告诉用户）；
- 数量不设上限，由用户自行控制；
- WS 和 HTTP 两条通道共用同一份实现（`handlers/session.py`），行为不会不一致；
- 读取失败一律返回空名单——默认装备绝不能把聊天弄挂。

### 2.4 前端

- 连接器详情页加「默认使用」复选框：勾选即设为默认，断连或读取失败时置灰；
- 新会话打开扩展面板时，默认清单已勾好（所见即所发）；手动改过的会话按快照显示，不被默认覆盖。

### 2.5 各场景表现

| 场景 | 行为 |
|---|---|
| 新会话第一条消息（没碰过面板） | 自动挂载默认 |
| 新会话（勾过/清空过） | 按用户操作，默认不介入 |
| 历史会话 | 按快照 |
| fork / copy 会话 | 按既有快照继承 |
| 定时任务（cron）会话 | 同样自动挂载默认 |
| 团队会话 | 不生效（团队装备另有机制） |
| 企业模式 | 企业模板优先，加载失败时默认兜底 |
| 默认连接器断连 | 新会话静默跳过；重连后仅**之后新建的会话**自动恢复生效，已创建的会话不回补（web 既有剪枝行为会把断连项移出会话勾选，需手动重勾） |
| 回合失败 | 不存快照，下一条消息默认重新生效 |
| 服务重启 | 标记持久化，历史会话不受影响 |

## 3. 防护

- set 拒绝断连的连接器（设了不生效，不如报错）；
- 读取失败返回空，不影响聊天；
- `is_default` 与 TUI 的 `enabled` 是两个独立开关，互不影响。

## 4. 网关依赖（发布前必须解决）

前端的 RPC 要经过 agent-gateway（外部依赖仓库）转发，那里有三处硬编码名单需要正式加入这两个方法：

1. `_FORWARD_REQ_METHODS`；2. `_FORWARD_NO_LOCAL_HANDLER_METHODS`；3. `common/protocol_methods.py`。

放行之前：复选框暂时不可用（置灰）；**自动挂载本身不经过网关，先行生效**（SDK、定时任务、没碰过面板的会话都正常）。本地验证时曾对这三处打临时补丁打通全链路。

## 5. 测试

- 新增 27 个单测：存储操作、挂载规则全组合（含逃生门）、RPC 校验、读取失败降级；
- 回归全绿：装备挂载、路由表、协议枚举、会话预热、定时任务；
- 端到端：WS 直连冒烟（读/写/非法拒绝）+ 浏览器实测（勾选实时生效、新会话预勾选、取消不挂载）。
