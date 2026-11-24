---
id: SDD-0030
title: 全局默认装备（Default Equipment）：新会话自动挂载默认扩展/连接器
slug: session-default-equipment
status: implemented
stage: proposals
reviewers: []
created: 2026-11-20
updated: 2026-11-24
version: 2
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

# 全局默认装备（Default Equipment）：新会话自动挂载默认扩展/连接器

> 对应 Issue：GitCode #6383（GitHub #11108）。方案已全部实现并通过本地端到端验证；实施记录见 `features/F_20261120_1534_session-default-equipment.md`。

## 1. 背景与根因

在「扩展 / 连接器」（MCP）里安装并连接一个服务后，新建会话仍是空装备，必须逐会话手动勾选，工具才可用。根因（develop 已核实）：

- 「安装/连接」与「会话挂载」是两套状态：`registry.py` 连接完成只把 state 置为 `connected`，不加载进任何 agent 会话；
- 会话 MCP 集合由每条 `chat.send` 的 `mcp` 字段驱动（`reconcile_session_mcp`）；
- 新会话无装备快照（`get_session_equipment` 返回 `{}`）。

`config.yaml` `mcp.servers` init 注册的 MCP 本就对所有会话可见，无需本机制；本方案解决的是 web 面板安装连接的 MCP（state.json 记录型）的默认挂载。

## 2. 已实现方案

### 2.1 存储：state.json `is_default`（单一来源）

MCP state store（`server/runtime/mcp/state_store.py`）每条记录新增 `is_default: bool`，配四个操作函数：

| 函数 | 语义 |
|---|---|
| `set_mcp_default(name, flag)` | 单项翻转，不动连接状态 |
| `list_default_mcps()` | 生效默认名单（仅 `connected`/`connecting`） |
| `list_default_flagged_mcps()` | 全部被标记项（含断连，供 UI 灰显"默认但不可用"） |
| `replace_default_mcps(names)` | 单锁内原子全量替换 `is_default` 集（PUT 语义基础，防并发互踩） |

### 2.2 回填：三级优先级 + 逃生门

`interface.py` `restore_chat_send_equipment_params()` 新增第三级回填：

```
显式传参 > 会话快照 > 全局默认
```

- 仅当会话**无快照**（`get_session_equipment` 返回 `{}`，区别于快照 `{"mcp": []}` 的显式清空）且消息**省略 `mcp` 字段**时，用默认播种一次；
- 首轮回合成功后 `save_session_equipment` 落快照，默认清单后续变更**不追溯**已有会话；
- 显式 `mcp: []` 永不被回灌——key-presence 三态契约（缺省=保留 / `[]`=清空 / 值=替换）原样保留，前端 hydrate 机制天然保证"碰过面板=显式"；
- 只播种 `mcp`，不碰 `plugin_names` / `agent_template_name`；
- 读取入口 `config.py` `get_default_equipment_mcp_names()`：state 单一来源、去重排序，provider 异常降级为空——默认装备绝不阻塞 `chat.send`。

### 2.3 RPC：`session_equipment.defaults.get/set`（双通道）

- **get** → `{mcp: 生效名单, runtime: 全部标记, unavailable: 标记但断连, max: 10}`；
- **set** 参数 `{mcp: [...]}`：PUT 语义原子替换。校验链：必须 list → 条目 string → strip/去重 → ≤10 → 每名必须有活记录（否则 `BAD_REQUEST`，错误带具体名字与状态）；KeyError → `MCP_NOT_FOUND`；
- **双通道共享核心**（`handlers/session.py` `read/apply_session_equipment_defaults_payload`，WS 与 HTTP 不可能漂移）：
  - WS 聊天线：`JiuWenSwarm.process_message` 的 `_handle_session_equipment_defaults_request`，在 adapter 创建前应答（`_SESSION_ASSET_ROUTES` 同款模式；不接线的合法 ReqMethod 会落入聊天路径按空 query 触发 LLM 调用）；
  - HTTP：`dispatch.py` HANDLERS 注册（`agent_http_routes` 消费）；
- 枚举注册：`message.py` `_ensure_req_method`（协议包演进机制）；
- 所有磁盘 IO 走 `asyncio.to_thread`（带锁 IO 不阻塞事件循环）。

### 2.4 前端

- MCP 详情页「默认使用」复选框（社区对齐形态：原生 checkbox；读 `runtime` 集，写 PUT 全量清单；断连/达上限/读取失败置灰带 title 提示；set 校验错误红字透出）；
- 无快照新会话打开扩展面板即预勾选默认集（`sessionStore.prefillDefaultMcps` 整体初始化 + 标记 hydrated，所见即所发）；已 hydrate 会话（快照恢复/手动改过）以快照为准**不被覆盖**；team 模式跳过；空默认集不预填（保持省略字段，后端回填活读取）。

### 2.5 场景行为

| 场景 | 行为 |
|---|---|
| 新会话首条消息（未碰面板） | 默认回填播种一次 → 快照接管 |
| 新会话（已勾选/已清空） | 显式数组 / `[]` 优先，默认不介入 |
| 历史会话 | 快照优先 |
| `session.fork` / `session.copy` | 既有快照继承，默认不介入 |
| cron 创建/执行会话 | 裸消息无快照 → 同样回填默认 |
| 团队会话（team 模式） | 不生效（装备走 team spec 级装配） |
| 企业模式 | routed Agent 模板整体替换优先；策略加载失败时默认兜底 |
| 断连默认项 | 读取源头过滤 + reconcile 失败隔离双保险；重连自动恢复生效 |
| 失败回合 | 不落快照（既有行为），下条消息默认重新播种 |
| 服务重启 | `is_default` 持久化；历史会话快照不受影响 |

## 3. 防护

- **数量上限 10**（`_EQUIPMENT_DEFAULTS_MAX`）：防默认连接器的 tool schema 挤占新会话上下文；
- **set 拒绝断连名**：断连项在回填时本就静默失效，写入时报错让用户当场修正（先重连再设）；
- **读取异常降级为空**：默认装备不能成为 `chat.send` 的故障源；
- **`is_default` 与 TUI `enabled` 独立**：前者是 web 新会话默认，后者是 TUI 全局加载开关，互不污染。

## 4. 跨仓依赖（agent-gateway）

web WS 转发链上 agent-gateway（外部 pin 依赖）有三处硬编码门：

1. `channel_manager/web/app_web_handlers.py` `_FORWARD_REQ_METHODS`（不在集合 → 网关回 `METHOD_NOT_FOUND`）；
2. 同文件 `_FORWARD_NO_LOCAL_HANDLER_METHODS`（纯转发 RPC 必须同时在列）；
3. `common/protocol_methods.py` 的 ReqMethod 枚举补丁清单（解析不到 → 信封 method 为空）。

正式发布需 agent-gateway 加入两个方法并升 pin。在此之前：前端 RPC 置灰降级（读取失败提示），**后端回填（SDK / cron / 未碰面板会话）不依赖网关、先行生效**。本地验证曾在 `.venv` 内对三处打临时补丁打通端到端。

## 5. 测试与验证

- **新增 UT**：state 四件套（翻转/缺记录/活性过滤/原子替换）；回填语义矩阵（无快照+缺省→播种、显式 `[]`→不回灌、显式名单→不动、空快照→快照优先、无默认→不动、team 跳过、只播种 `mcp`）；RPC 校验（非 list/超上限/未连接拒绝、PUT 替换）；读取降级；
- **回归全绿**：extension mount、dispatch 注册表、ReqMethod、session warmup、cron 调度；
- **端到端**：WS 直连冒烟（get 全量视图 / PUT 替换 / 非法拒绝）；浏览器实测（复选框实时翻转 state.json、新会话面板预勾选、取消勾选后不挂载、快照接管）。
