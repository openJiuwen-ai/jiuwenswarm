# 核心数据模型（schema）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / schema-data-models |
| 关联模块 | jiuwenswarm/common/schema/（7 文件） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **贯穿全仓的核心数据模型**：`ReqMethod` / `EventType` / `Mode` / `Message` /
`AgentRequest` / `AgentResponse` / `AgentResponseChunk` 及 ask_user / chat_send /
swarmflow_reply 的参数契约。模型之间的协议转换（E2A ↔ AgentRequest）归 [[S_02]]；
各业务域对字段的消费语义归各业务层。

`schema/__init__.py` 只导出四件：`Message` / `AgentRequest` / `AgentResponse` /
`AgentResponseChunk`——这四件是跨层稳定契约；其余符号走子模块导入。

## 不变量

- **N1 ReqMethod 是方法域的唯一枚举**（约 260 成员）：`chat.*` / `command.*` /
  `mcp.server.*` / `sandbox.*` / `session.*` / `team.*` / `agents.*` / `skills.*` /
  `personal_context.*` / `permissions.*` / `channel.*` / `updater.*` / `workspace.*` /
  `im.hosting.*` 等域。新增方法 = 枚举扩成员 + 消费方 handler；**禁止**用裸字符串当
  method 传递。
- **N2 EventType 固定 43 个事件**：事件名变更属破坏性变更，须全仓 grep 消费点。
- **N3 Mode 归一**：外部 `agent` / `agent.plan` / `agent.fast` 归一为 `agent` /
  `code.plan` / `code.normal` / `code.team` / `team`（`from_raw` / `to_runtime_mode`）。
  组合解析与 canonical 化归 [[S_08]] 的 mode_matrix，本枚举只承载终值。
- **N4 thread_id 与 session_id 分离**：`session_id = sha256(thread_id)`；`workspace_key`
  为 MD5 hex。会话身份派生公式变更属破坏性变更（历史数据定位失效）。
- **N5 bot_id 已弃用**：`Message.bot_id` 保留只为兼容读取；新代码一律 `app_id` +
  `agent_ref`（V2 透传，从 E2A 信封直达 [[S_02]]）。
- **N6 PermissionContext 派生链**：`principal_user_id` / `triggering_user_id` /
  `scene` 派生 / `owner_scope_key`——权限判定的输入在此定型，下游不得重新推导用户身份。
- **N7 ask_user 冻结模型**：`AskUserAnswer` / `AskUserResponse` 为 frozen dataclass；
  `normalize_ask_user_response` 强制 **skipped 不得携带输入**；`decode_user_input` 兼容
  双重 JSON 编码与 pydantic 风格。ask_user 的 JSON Schema 由
  `ask_user_response_schema()` 单点生成。
- **N8 ChatSendParams 契约**：`content` 是主字段，`query` **DEPRECATED**（读兼容、写禁
  新增）；`skills` / `mode` / `team` / `attachments` / `answers`（plan 审批）/ `evolution`
  / `auto_harness` 等为可选槽位。TypedDict 是纯契约，无运行时校验——校验在消费方。
- **N9 HookEventBase 自动前缀**：`__init_subclass__` 自动给事件名加 scope 前缀，对齐
  openjiuwen 0.1.9+ EventBase；下游 `GatewayHookEvents` / `AgentServerHookEvents` 定义在
  `extensions/hook_event.py`（不在本包）。
- **N10 SwarmflowReplyParams 无消费方**：纯前向契约（TypedDict），当前仓库内无 import
  者——为 swarmflow 回复路径预留，勿假设其已接线。

## 接口契约

| 模块 | 关键符号 | 语义要点 |
|---|---|---|
| `message.py` | `ReqMethod`（~260）/ `EventType`（43）/ `Mode` + `from_raw` / `to_runtime_mode` / `Message`（dataclass） | N1–N3、N5 |
| `agent.py` | `PermissionContext` / `AgentRequest` / `AgentResponse` / `AgentResponseChunk` | N4、N6；AgentRequest 携带 workspace_key / agent_ref |
| `ask_user.py` | `AskUserResponseError` / `AskUserAnswer` / `AskUserResponse` / `ask_user_response_schema()` / `normalize_ask_user_response` / `decode_user_input` | N7 |
| `chat_send.py` | `ChatSendParams`（TypedDict） | N8 |
| `event_base.py` | `HookEventBase` | N9 |
| `swarmflow_reply.py` | `SwarmflowReplyParams`（TypedDict） | N10 |

## 数据结构

- `AgentRequest` 核心身份字段：`thread_id`（外部线程）/ `session_id`（内部会话，派生）/
  `workspace_key`（工作区定位，派生）/ `agent_ref`（V2 agent 引用透传）。
- `Message`：对话消息载体；`metadata` 是扩展字典（routing / ext / channel 等子键的落点
  约定见 [[S_13]]）。
- `AgentResponse`（一次性完整响应）与 `AgentResponseChunk`（流式分片）是响应的两个形态；
  流式 → E2AResponse 的分派规则见 [[S_02]]。

## 消费者

全仓 30+ 文件：`server/`（handlers、runtime、agent_ws_server）、`gateway/`
（message_handler、routing、cron、web/tui handlers）、`channels/`、`extensions/`、
`agents/harness`。`Message.metadata` 的内部键消费面最广，改键名前必须全仓 grep。

## 修改检查单

- 加 ReqMethod 成员 → 消费方 handler 注册 + gateway 路由表 + E2A method 白名单（[[S_02]]）。
- 改 Message 字段 → E2A 双向适配（[[S_02]] adapters）+ 序列化兼容（旧消息回放）。
- 改 Mode 归一 → `tests`（mode_matrix 回归）+ [[S_08]] 组合表同步 + 前端模式展示。
- 改 ask_user 契约 → `tests/unit_tests/common/test_ask_user_response.py` + 前端卡片
  schema 同步（`ask_user_response_schema` 是对前端的契约）。
- 弃用字段 → 保留读取兼容一个版本周期，标注 DEPRECATED 注释，勿物理删除。

## 与其它 spec 的关系

- [[S_02]]：E2A 信封与 AgentRequest 的转换边界。
- [[S_08]]：Mode 的组合解析与 canonical 化（mode_matrix / work_mode）。
- [[S_13]]：`Message.metadata` 内 routing / ext 子键的落点约定。
