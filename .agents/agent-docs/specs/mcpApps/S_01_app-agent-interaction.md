---
id: S_01_app-agent-interaction
title: MCP App 到 Agent 交互规约
type: S
author: LI Jinyang
author_email: lijinyang9@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/channels/web/frontend/src/features/mcpApps/mcpAppContext.ts
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/McpAppView.tsx
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/server/runtime/agent_adapter/user_turn.py
---

# MCP App 到 Agent 交互规约

## 范围与职责

Web 宿主在已有 MCP App 渲染、工具批准及沙箱能力之上，接收 `ui/message` 和 `ui/update-model-context`。
消息复用 `App.tsx` 注册的正常 `sendMessage` 入口；不直接调用模型或创建第二套 Agent 执行路径。
后端沿用 Gateway / AgentServer 的 metadata 传递，由 `UserTurn` 生成模型输入。

## 消息契约

- `ui/message` 仅接受 user 角色、1–8 个文本块，合并后的非空文本最多 8 KiB。
- 用户看到 App 标识和完整正文，明确确认后才发送；拒绝或卸载不发送。
- 同一实例最多一个待确认消息，每分钟最多五次请求；宿主未注册发送器、正在处理任务或队列暂停时返回失败。
- 成功响应表示正常聊天发送入口接受了消息，不代表 Agent 执行完毕或外部副作用成功。
- `tools/call` 继续走独立工具批准流程，`readOnlyHint` 不构成授权。

## 上下文契约与生命周期

- 身份为 `sessionId + appInstanceId`；UI HTML 的 `server + resourceUri` 缓存键不作为实例身份。
- `ui/update-model-context` 替换该实例完整快照，不追加历史；空文本数组且无非空 structuredContent 时清空快照。
- 文本最多 32 块；structuredContent 为 JSON 对象，深度最多 16。序列化单实例最多 32 KiB，同会话合计最多 64 KiB。
- 更新节流为每秒补充 20 个令牌、突发上限 40；拒绝的更新不替换上次合法快照。
- 当前普通 `chat.send` 携带该会话已挂载实例的快照到 `metadata.mcp_app_contexts`，保留已有 metadata；不跨会话读取，不主动触发聊天。
- 卸载清理快照、待确认消息及限流记录；不承诺刷新后的上下文恢复，也不将上下文自动发送到 MCP Server。
- `UserTurn` 再次验证标识、文本类型、深度及大小，过滤非法项。合法状态与用户正文分开，并附 `mcp_app_context_policy` 声明其不可信、不是指令或授权。

## 验证入口

- 前端：`test:mcp-app-context`、`test:mcp-app-view`、`test:chat-attachment-transport`。
- 后端：`tests/unit_tests/server/test_user_turn.py`。
- 基础能力回归：`test:mcp-app-document`、`tests/unit_tests/test_mcp_apps.py` 和 `tests/unit_tests/test_mcp_app_sandbox_http.py`。
