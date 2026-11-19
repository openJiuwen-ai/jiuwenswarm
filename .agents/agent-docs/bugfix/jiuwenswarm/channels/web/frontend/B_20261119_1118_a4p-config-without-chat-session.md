---
id: B_20261119_1118_a4p-config-without-chat-session
title: 空白新建会话无法读取 A4P 开关
type: B
author: yukuanj
author_email: jiayukuan@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/agents/harness/common/a4p
  - jiuwenswarm/server
files:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/experimental/A4PSettings.tsx
  - jiuwenswarm/channels/web/frontend/src/features/A4PAuthorizationModal.tsx
  - jiuwenswarm/agents/harness/common/a4p/rpc.py
  - jiuwenswarm/server/agent_ws_server.py
designs: []
design_note: 依据本次会话确认的跨仓修复方案实施，无独立设计文档。
---

# 空白新建会话无法读取 A4P 开关

2026-11-19 本地 WebSocket 日志记录了实验设置打开后发送 `a4p.config.get`，参数为 `{"session_id":"new"}`，Gateway 返回 `A4P_SESSION_IDENTITY_UNAVAILABLE`。`new` 是未创建会话的前端占位符，Host 无法为它解析会话身份。

配置 RPC 读写目标应用实例的 `a4p.enabled`，AgentServer 接受无会话的 Web 管理请求，更新仍校验布尔值、写配置并触发运行时热更新。旧客户端传来的 `session_id` 仅用于兼容，配置请求不参与会话生命周期校验。企业版要求 Gateway 转发的用户身份，目标应用访问授权和实例路由由 Gateway 执行。待授权查询和审批使用会话路由，前端在新建会话占位页跳过待授权查询。

本地 macOS、Python 3.13 定向 pytest 154 项通过，覆盖配置校验、兼容旧参数、非 Web 和定时任务拒绝、企业版身份，以及 AgentServer HTTP 无会话读写临时配置和热更新调用。前端 `test:a4p-settings`、`test:a4p-authorization`、`test:settings-panel` 共 96 项通过。已同步中英文 A4P 使用说明。

Gateway 的无会话配置转发、企业权限策略和依赖升级由单独任务完成。待两侧接入后，从空白页验证 WS 和 HTTP 的读写、刷新回显及企业租户隔离。
