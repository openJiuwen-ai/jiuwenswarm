---
id: SDD-0030
title: 全局默认装备（Default Equipment）：新会话自动挂载默认扩展/连接器
slug: session-default-equipment
status: draft
stage: proposals
reviewers: []
created: 2026-11-20
updated: 2026-11-20
version: 1
domain: core
modules: [server/runtime/agent_adapter, server/runtime/session, server/runtime/mcp, gateway, channels/web/frontend]
tags: [mcp, session-equipment, default-equipment, chat-send, ux]
related: []
author: 'StoneChan_'
author_email: 'chensitong2@huawei.com'
type: D
files: []
issue: 'GitCode #6383 / GitHub #11108'
---

# 全局默认装备（Default Equipment）：新会话自动挂载默认扩展/连接器

> 对应 Issue：GitCode #6383（GitHub #11108）。设计草案，供维护者评审；阶段一（后端）与阶段二（前端 UI，Cursor 模式）已按本文实现并通过本地端到端验证，实施记录见 `features/F_20261120_1530_session-default-equipment.md`，跨仓依赖（agent-gateway 转发门）见 §5。

## 1. 背景与问题

在「扩展 / 连接器」（MCP）里把某个服务全局启用（安装并连接成功）后，新建的每个会话仍然是空装备，必须在该会话里手动勾选一次，工具才会挂载。日常高频使用 2~3 个 MCP（日志检索、代码托管、知识库）时，每个新会话重复勾选成本高，且漏勾会导致任务中途才发现工具不可用。

根因（已在 develop 核实）：

- 「全局安装/连接」与「会话挂载」是两套状态，会话侧选择才是生效开关。`registry.py` 连接完成只把 state 置为 `connected`，注释明确"不加载进任何 agent 会话，会话级加载由 `chat.send` 的 `mcp` 字段在后续驱动"（`server/runtime/mcp/registry.py:829-831`、`:965-967`）。
- 会话 MCP 集合由 `reconcile_session_mcp()`（`server/runtime/agent_adapter/interface_deep.py:6591`）按 `chat.send` 的 `mcp` 字段驱动，`None` 与 `[]` 都清空会话自加载集合 `_session_selected_mcp`；init 阶段由 config.yaml 注册的 MCP 不在该集合内，因而对所有会话天然可见。
- `restore_chat_send_equipment_params()`（`server/runtime/agent_adapter/interface.py:231`）只在同一会话内用已存快照回填被省略的字段，不跨会话继承，也没有全局默认来源。
- 新会话创建（`session.create`）不初始化 `session_equipment`，`get_session_equipment()` 返回 `{}`（`server/runtime/session/session_metadata.py:1843`）。
- 因此只有被打包进专家模板/插件的 connectors 会自动带上（`compute_chat_send_mcp_needed()`，`interface.py:183`），单独全局启用的 MCP 不会。

## 2. 关键现状核查：三态契约已经存在（对 AI 评估结论的修正）

AI 自动修复评估（issue 评论区 openjiuwen-AIReview）认为"前端把『新会话未勾选』与『用户显式清空』都编码为 `mcp: []`，后端无法区分"，并据此判定不适合自动修复。经对前后端链路的完整核查，这一判断**不完全准确**：

**前端 `buildExtensionSendPayload()`（`channels/web/frontend/src/utils/enabledExtensions.ts:81`）的完整语义：**

- 会话 runtime 未 hydrated（`extensionsHydrated === false`）→ 返回 `{}`，即**省略** `mcp` / `plugin_names` 字段。新会话 `'new'` 占位由 `createEmptyRuntime` 初始化为 `enabledMcps: []` + `extensionsHydrated: false`（`sessionStore.ts:767`），用户未操作面板时首条消息**省略装备字段**。
- 已 hydrated → 发送完整快照 `{ plugin_names, mcp }`，其中 `mcp: []` 表示**用户显式清空**。用户任何勾选/取消/清空操作都会把 `extensionsHydrated` 置 `true`（`sessionStore.ts:1818/1838/1852`）。

**后端 `restore_chat_send_equipment_params()` 的语义（`interface.py:235-254`）：** key 缺省 = 用快照回填（保持）；key 存在且为 `[]` = 显式清空，**不回填**。

也就是说，「省略 = 未表达意图」「`[]` = 显式清空」的 key-presence 三态契约**在 wire 协议上已经存在**，新会话未勾选（省略）与显式清空（`[]`）天然可区分。本设计不需要重新定义编码契约，只需在后端回填链路上**新增第三级来源（全局默认）**即可，改动面比 AI 评估的小。

> 需要承认的边界：AI 评估指出的"resume / activate_confirm 同族请求"确实都会带装备字段（`useWebSocket.ts:3035/3064/3120`），但这些请求发生在已有会话上，会话快照此时已存在，走"快照优先"分支，全局默认不介入，无冲突。

## 3. 目标与非目标

**目标**

- 引入「全局默认装备」：用户可将已连接的 MCP 标记为"全局默认"，新建会话自动挂载，无需逐会话重复勾选。
- 保留逃生门：会话内显式清空装备（`mcp: []`）后，后续消息不被默认值回灌。
- 优先级明确：同一会话内 **显式传参 > 会话已存快照 > 全局默认**。
- 默认清单变更仅影响"无快照的新会话"，不回溯影响历史会话。
- `config.yaml` init 注册的 MCP、企业模式 routed Agent 模板装备、团队会话装备路径行为保持不变（或按既定优先级覆盖默认）。
- 提供运行时读写接口与前端"设为全局默认"开关（分阶段）。

**非目标**

- 不改变 `chat.send` 参数语义（`mcp` / `plugin_names` / `agent_template_name`），向后兼容。
- 第一版默认装备**仅覆盖 `mcp`**；`plugin_names`、`agent_template_name` 不纳入（专家绑定涉及 `_loaded_agent_template` 等会话状态，风险高；插件挂载上下文占用大，留作后续阶段，见 §8 开放问题）。
- 不改变团队会话（team_runtime_mode）的装备机制；团队 MCP 仍走 team spec 级配置（见 §6 场景矩阵）。
- 不引入"装备预设/场景模板"（issue 方案 C），可作为默认装备之上的后续增强。

## 4. 总体设计

### 4.1 三级优先级与初始化语义

```
chat.send 到达
  └─ restore_chat_send_equipment_params(session_id, params)   （interface.py:231，改造点）
       for key in (agent_template_name, plugin_names, mcp):
         if key in params:                → 显式传参（含 []），直接使用，不回填
         elif key in 会话快照:             → 快照回填（现有行为）
         elif 会话无任何快照 且 key 在全局默认中: → 全局默认回填（新增第三级）
  └─ reconcile_session_mcp(session_id, compute_chat_send_mcp_needed(params))
       → 按 needed 幂等 diff 挂载/卸载（企业模式分支整体替换 needed_set，天然优先）
  └─ _ensure_chat_extensions → 装备应用成功后 save_session_equipment(params)
       → 新会话首轮即把"默认装备"物化为会话快照，之后该会话按快照走，不再受默认清单变更影响
```

要点：

- **默认只在"会话无任何快照"时介入一次**。首轮 chat.send 落盘快照后，该会话与全局默认脱钩——满足"默认清单变更不影响历史会话"。
- **逃生门**：用户在新会话首条消息前打开面板勾选再全部取消（前端置 hydrated=true），首条消息发 `mcp: []` → key 存在 → 不回填 → 挂载空集 → 快照落盘为空数组 → 后续省略字段时从快照回填 `[]`，永远不被默认回灌。
- **失效过滤**：回填时对默认清单做活性过滤（仅保留当前 `connected` 的 MCP），已卸载/断连项静默跳过；设置页对失效项给出提示。`reconcile_session_mcp` 对注册失败已有 warn + `failed_names` 隔离，不阻塞对话。

### 4.2 存储设计

默认装备是**运行时状态**（用户在 UI 上随时开关），同时支持**部署期预置**（管理员/企业下发）。采用双层来源，与现有 MCP 配置"yaml 为 legacy stock、state.json 为活跃源、同名 state 优先"（`interface_deep.py:6776-6787`）的先例一致：

| 层 | 位置 | 角色 |
|---|---|---|
| 部署预置层 | `config.yaml` 新增 `session.equipment.defaults.mcp: [...]`、`session.equipment.inherit: true` | 可选；管理员/企业预置；`inherit: false` 整体关闭本特性，恢复逐会话手工勾选 |
| 运行时活跃层 | MCP state store（`state.json`）每条记录新增 `is_default: bool` | UI 开关的落点；运行时增删默认项 |

合并规则：启动时以 yaml 预置清单为初始值写入 state（仅当 state 中该记录尚无 `is_default` 字段时），运行时以 state.json 为准。`inherit: false` 时回填逻辑整体短路（等价于特性关闭），但 state 中的 `is_default` 数据保留，便于重新开启后恢复。

**默认数量上限**：建议 10 个（写入接口校验），防止 tool schema 过度占用上下文（见 §7 安全与上下文占用）。

### 4.3 对外 API

沿用现有 gateway/handler 注册风格（参考 `server/handlers/bootstrap.py` 的方法分发模式），新增两个方法：

- `session_equipment.defaults.get` → `{ mcp: string[], inherit: bool, max: number }`
  - 返回合并后的生效默认清单（仅 `connected` 项）与原始标记（含失效项标记 `unavailable: string[]`），供设置页展示。
- `session_equipment.defaults.set` 参数 `{ mcp: string[] }` → 全量替换默认清单
  - 校验：每项必须为已安装且 `connected` 的 MCP；数量 ≤ 上限（10）；非法项返回 `BAD_REQUEST` 并指明原因。
  - 写入 state.json（逐记录置/清 `is_default`），同步返回生效清单。
- `mcp.list`（或前端连接器数据源）响应合并 `is_default` 标记，连接器卡片据此渲染开关状态。

兼容性：纯新增方法，`chat.send` 语义不变；旧前端不调用新接口时行为与本版本一致（后端默认清单为空 → 不回填）。

### 4.4 前端设计（分两阶段）

**阶段一（MVP，后端能力为主，前端零改动可用）：**

后端回填默认并挂载后，首轮 `save_session_equipment` 落盘快照；`App.tsx:2108` 现有逻辑在下次拉取 metadata 时 `restoreSessionEquipment()` 恢复勾选态（`extensionsHydrated=true`），面板随即显示默认项为已勾选——UI 最终一致，用户可在会话内单独取消（取消后走显式传参，优先于一切）。此阶段无新会话预显，面板在首条消息前不显示默认勾选，可接受。

**阶段二（完整 UI）：**

1. 连接器卡片增加"设为全局默认"开关（与"已连接"状态分离），调用 `defaults.set`。
2. 设置页展示当前默认清单、失效项提示、数量上限与上下文占用提示文案。
3. 新会话预显：`createEmptyRuntime` 初始化时（或 `'new'` 提升为真实会话时）调 `defaults.get` 预填 `enabledMcps`，使面板在首条消息前即显示默认勾选。此改动会让首条消息带上显式默认数组（hydrated=true），语义与后端回填等价（结果一致），但 UI 更即时。注意预显不等于锁定：用户可在发送前取消任意项。

**阶段二细化方案（2026-11-20 与提出人确认，采用 Cursor 模式）：**

业界对照（已核实官方文档）：

| 模式 | 代表产品 | 机制 | 结论 |
|---|---|---|---|
| ① 全局开关 + 新会话自动可用 | Cursor | 管理页每个 server 一个 enable 开关；开启后所有新会话自动可用，会话内可按工具粒度临时关 | **本方案采用**（双层结构：全局粗粒度默认 + 会话细粒度覆盖） |
| ② 每会话手动激活 | ChatGPT | 连接器不在新会话自动启用，每会话 "+" 菜单手动勾选，不跨会话记忆 | 等于本仓现状痛点（issue #6383 要解决的问题），否 |
| ③ 配置即全局常挂 | Claude Desktop/Code | 配置文件里的 server 永远挂载，无会话级选择 | 上下文成本失控（每个 server 的工具目录常驻所有会话），否 |

交互设计（对应阶段二实施）：

1. **默认开关位置**：MCP 管理详情页（`McpDetailPage.tsx`，"会话使用"按钮旁）加"新会话默认启用" Switch；读 `defaults.get` 的 `runtime` 集，写 `defaults.set`（前端基于当前清单组装完整新清单，PUT 语义）。断连时置灰 + "连接后可设为默认"提示；达上限（10）时置灰 + 计数提示。实现说明：不需要按 §4.3 备选方案合并 `mcp.list` 响应——`defaults.get` 已返回完整视图（effective/preset/runtime/unavailable/inherit/max）。
2. **新会话面板预勾选**：面板初始状态 = 会话快照（若有）∥ 全局默认（无快照时取 `defaults.get` 的 `mcp` 活集）。无快照新会话打开面板即显示默认项勾选（unavailable 项显示灰色断连态），预勾选即视为 hydrate → 首条消息带显式默认数组，所见即所发。
3. **会话内覆盖**：现状保留——显式取消 → `mcp: []` 逃生门；回合成功后快照落盘，全局默认变更不再影响该会话。
4. **后端第三级回填保留为兜底**：SDK/API/旧前端（省略 `mcp` 字段）仍经后端回填获得默认；新前端预勾选后首条消息带显式数组，回填不触发。两者读同一 `is_default` 源，无冲突（`mcp` 在参数中即跳过回填）。

边界与风险：

1. 预勾选只对"无快照"会话生效；有快照的会话必须用快照，否则用户此前的显式清空会被默认"复活"（语义破坏）。
2. 失败回合后刷新：无快照会话刷新 → 面板重新预勾选默认 → 回合中手动取消的选择丢失（回合失败未落快照）。边缘场景，接受；根治需"面板操作即落盘"，代价大不做。
3. `is_default`（web 新会话默认）与 TUI `enabled`（TUI 全局加载）是两个独立字段，UI 文案统一用"新会话默认启用"，避免用户误以为是 TUI 开关。
4. 上下文成本：默认集越大每个新会话开场消耗越大；上限 10 已防护；演进方向可参考 Cursor 的动态工具加载（只发工具名、按需加载 schema）。

## 5. 后端改造点详述

| # | 位置 | 改动 |
|---|---|---|
| 1 | `interface.py` `restore_chat_send_equipment_params()` | 新增第三级回填：`saved` 为空（`get_session_equipment` 返回 `{}`）时，从全局默认（`mcp` 键）回填缺省字段。需判定"会话无快照"与"快照为空数组"的区别——前者走默认，后者尊重显式清空。当前 `get_session_equipment` 对无快照与空快照均返回 `{}`，需要补充区分手段（如返回/metadata 直读 `session_equipment` 键是否存在，或新增 `has_session_equipment()` 判定函数）。**这是本设计唯一触及语义的地方，需评审确认。** |
| 2 | `server/runtime/mcp/` state store | 记录新增 `is_default` 字段；提供 `get_default_mcps()` / `set_default_mcp(name, flag)`；读取时过滤 `connected` 并做活性校验。 |
| 3 | `server/runtime/config`（config.yaml 加载处） | 解析 `session.equipment.defaults` / `inherit`；启动时向 state 层播种预置值。 |
| 4 | gateway + handler | 注册 `session_equipment.defaults.get/set` 两个方法，参数校验与上限校验。 |
| 5 | `interface_deep.py` `reconcile_session_mcp()` | **无需改动**。默认装备经 restore 进入 `params.mcp` 后走现有 reconcile 通道；企业分支（`:6650`）整体替换 `needed_set`，天然优先于默认。 |
| 6 | `interface.py` `process_message` + `handlers/session.py`（2026-11-20 实测补充） | **WS 聊天线必须在 agent 路由层显式接线**：`dispatch.py` HANDLERS 只服务 HTTP REST 路径（`agent_http_routes.py` / `pod_rest_map.py` 消费），WS 请求的最终路由在 `JiuWenSwarm.process_message`（`_SESSION_ASSET_ROUTES` 同款模式）。不接线时合法 ReqMethod 会落入聊天路径、按空 query 触发一次 LLM 调用（与 `_SKILL_SYNC_WS_REJECTED` 注释描述的坑一致）。实现：`handlers/session.py` 抽出共享核心（`read/apply_session_equipment_defaults_payload`，供 WS 与 HTTP 双传输消费）+ `interface.py` 新增 `_handle_session_equipment_defaults_request` 在 adapter 创建前应答。 |

**跨仓依赖（agent-gateway，2026-11-20 实测发现，发布前必须解决）**：web WS 前端 → WebChannel(19000) → AgentServer 的转发链上，外部依赖 agent-gateway 有三处硬编码门（皆在 pin 的 435d3323 中）：

1. `channel_manager/web/app_web_handlers.py` `_FORWARD_REQ_METHODS`（方法不在集合内 → 网关直接回 `METHOD_NOT_FOUND`）；
2. 同文件 `_FORWARD_NO_LOCAL_HANDLER_METHODS`（纯转发 RPC 必须同时在此集合，否则消息虽已投递仍会被 web_connect 补发 `METHOD_NOT_FOUND`）；
3. `common/protocol_methods.py` 的 ReqMethod 枚举补丁清单（`_parse_req_method` 解析不到 → 信封 method 为空串，连第 1 关都过不去）。

正式路径：agent-gateway 仓库把两个方法加入上述三处（或 protocol 仓直接发枚举成员）+ jiuwenswarm 升 pin。**本地验证**：当前在 `.venv` 内对上述三处打了 LOCAL-ONLY 补丁（uv sync 会覆盖，不进 PR）；阶段二前端 UI 的端到端联调依赖此补丁。

不改动 `save_session_equipment`（物化快照沿用现有逻辑）、不改动 cron 继承逻辑、不改动团队/企业路径——这是本设计与"方案 B（连接即全局挂载）"的本质区别：默认装备只在"会话无快照 + 字段缺省"的窄窗口介入一次，随后完全物化为会话快照，所有既有治理（快照回填、显式清空、fork 继承、cron 继承、企业覆盖）自然延续。

## 6. 场景矩阵

| 场景 | 路径 | 默认装备是否生效 | 依据 |
|---|---|---|---|
| 新会话首条消息（用户未碰面板） | 前端 hydrated=false → 省略 `mcp` → 无快照 → **默认回填** | ✅ 生效 | §4.1 |
| 新会话首条消息（用户已勾选） | hydrated=true → 显式数组 | ❌ 显式优先 | key-presence |
| 新会话首条消息（用户勾选后全部取消） | hydrated=true → `mcp: []` | ❌ 逃生门，永不回灌 | key-presence + 快照落盘 `[]` |
| 历史会话（已有快照） | 省略 → 快照回填 | ❌ 快照优先 | 现有行为 |
| `session.fork` | 继承源快照 ± override（`session_ops_service.py:536`） | ❌ 继承结果即快照 | 现有行为 |
| `session.copy`（fork 的另一入口） | 同上 | ❌ | 同上 |
| cron 创建 | `_inherit_session_mcp`（`cron_runtime.py:226`）继承创建会话快照 | 继承创建会话快照；若创建会话无 MCP 选择则 payload 不带 `mcp`，cron 执行会话首轮经 restore 走默认回填 | ✅ 间接继承（与 issue 建议一致：cron 继承默认装备） |
| cron 执行会话 | 新会话 + 显式/缺省 `mcp` | 同上 | 同上 |
| 团队会话（team_runtime_mode） | `restore_chat_send_equipment_params` 直接 return（`interface.py:243`）；MCP 走 team spec 级（`agents/swarm/assembly.py:521`） | ❌ 不生效（保持现状） | 团队装备机制独立，是否支持团队级默认列入 §8 开放问题 |
| 企业模式（ENTERPRISE） | reconcile 阶段 routed Agent 模板**整体替换** `needed_set`（`interface_deep.py:6650`）；策略加载失败（返回 None）时保留消息级选择（此时含已回填的默认） | 企业模板优先；策略失败时默认作为兜底 | ✅ 优先级满足 issue 要求 |
| TUI 通道 | TUI 有 init 注册全局集（config.yaml `mcp.servers` enabled）；默认装备进 `_session_selected_mcp` 与之并存 | ✅ 生效（通道无关） | TUI 用户已有 config.yaml 全局手段，两机制不冲突 |
| 服务重启 | state.json 持久化 `is_default`；既有会话快照不受影响 | ✅ 默认清单保留，历史会话不变 | 存储设计 §4.2 |

## 7. 安全与上下文占用

- **默认攻击面**：默认挂载等于把工具的默认暴露从"逐会话显式授权"放宽为"管理员/用户预设授权"。缓解：① 默认清单仅用户本人显式设置（UI 开关）或管理员显式预置（yaml），不与"连接成功"自动联动；② 高危工具（写文件、执行命令、外部账号操作）的审批机制不受影响——默认挂载只解决"工具可见"，工具**执行**仍走既有 rails/permission 审批链，本设计不触碰审批路径。
- **上下文占用**：每个默认 MCP 的 tool schema 常驻所有新会话上下文。缓解：① 数量上限 10（写入接口强制）；② 设置页展示占用提示；③ `context.usage` 已有观测，可回归对比。
- **权限校验**：`defaults.set` 仅允许选择已安装且 `connected` 的 MCP，无法借此挂载无权限的包；`reconcile_session_mcp` 的注册失败隔离（`failed_names`）保证失效项不阻塞会话创建。

## 8. 开放问题（需维护者拍板）

1. **默认范围**：第一版仅 `mcp` 是否可接受？`plugin_names` 是否纳入第二阶段？`agent_template_name` 是否永久排除？
2. **数量上限**：10 是否合适？是否需要按 channel 区分？
3. **失效策略**：默认清单含已卸载/断连 MCP 时，回填静默过滤（本设计）vs 回填并提示，哪种？设置页提示的文案与级别？
4. **团队会话**：是否需要团队级默认装备（team spec 层），还是明确不支持？
5. **企业模式细节**：策略加载失败兜底到默认装备是否可接受，还是企业模式下应整体禁用全局默认？
6. **存储合并优先级**：yaml 预置与 state 运行时标记冲突时以 state 为准（本设计，沿用 MCP 先例）是否认可？
7. **历史会话**：默认清单变更仅影响无快照新会话（本设计），是否需要提供"对存量会话批量应用"的管理操作？（建议否）
8. **TUI**：是否需要 TUI 的 `/mcp default` 命令与 web 开关同等能力？

## 9. 测试计划（对应 issue 8 类）

- **功能**：设置默认 → 新会话自动挂载；`/mcp list` 与面板可见；取消默认后新会话不再挂载。覆盖 restore 三级优先级的单测（显式/快照/默认/空快照四组合）。
- **兼容**：config.yaml init 注册 MCP 在默认为空/非空时均正常；`inherit: false` 时行为与本版本一致。
- **边界**：默认清单含已卸载/连接失败 MCP 时新会话正常创建（跳过 + 设置页提示）；默认清单为空；达到上限的写入被拒绝。
- **逃生**：会话内显式 `mcp: []` 后，后续省略字段的消息不被回灌（快照 `[]` 优先）。
- **场景**：普通会话、fork/copy、cron 创建与执行、团队会话（不生效）、企业模式（模板优先）各一条集成用例。
- **可靠性**：多会话并发创建（`save_session_equipment` 的幂等短路与 `flush_pending_writes` 防竞态）；重启后默认清单与既有快照均正确；默认清单变更不影响历史会话。
- **性能**：上限 10 个默认 MCP 时会话创建耗时与上下文体积回归（对比 `context.usage`）。
- **安全**：默认挂载 MCP 的高危工具仍需审批；`defaults.set` 不能绕过安装/连接校验。

## 10. 分阶段实施建议

1. **阶段一（后端 MVP）**：存储层（state `is_default` + yaml 预置解析）+ restore 第三级回填 + `defaults.get/set` 接口 + 单测。前端零改动即可获得能力（§4.4 阶段一）。
2. **阶段二（前端 UI）**：连接器卡片开关 + 新会话预显（细化方案见 §4.4"阶段二细化方案"，2026-11-20 确认采用 Cursor 模式；设置页清单展示列为可选项）。
3. **阶段三（增强，可选）**：cron/team 语义确认后的补齐、`plugin_names` 纳入、TUI 命令、装备预设。

阶段一预计触及文件：restore 判定（`interface.py` + `session_metadata.py` 补判定函数）、state store、config 解析、gateway/handler 注册、单测，共约 6~8 个文件，不涉及前端与 reconcile 核心语义，风险可控。
