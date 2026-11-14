---
id: B_20261113_0119_web-proxy-personal-sse
title: 个人版 /api 代理整包缓冲 SSE 流式响应，HTTP 传输聊天被 15 秒超时截断
type: B
author: chendongcai
author_email: chendongcai95@qq.com
designs: []
design_note: 依据 #6411 现场报告与源码定位，无独立设计文档；修复对齐企业版路径既有 stream_sse 行为，仅一行参数补充并补回归测试。
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/app_web.py
  - tests/unit_tests/channels/web/test_web_enterprise_proxy.py
---

# 个人版 /api 代理整包缓冲 SSE 流式响应，HTTP 传输聊天被 15 秒超时截断

## 现象

前端走 HTTP 传输（企业版默认，或设置 `WEB_TRANSPORT=http`）并经 Web 静态服务
（默认 5173）的 `/api` 反向代理访问时，发消息后一直没有输出，约 15 秒后前端
中断显示「错误: 请求超时」，而 Agent 实际已开始执行（日志有工具调用）。
WebSocket 传输不受影响。

报告：GitCode [#6411](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6411)
（配对 GitHub #11168）。

## 根因

**一句话**：Web 静态服务的 `_dispatch_proxy`（个人版 `/api` 代理）调用
`_proxy_http()` 时未传 `stream_sse=True`，SSE 响应走了整包缓冲分支。

`jiuwenswarm/channels/web/app_web.py` 的 `_proxy_http` 自 2026-11-12 起
（`737041a07`）支持流式透传：`stream_sse=True` 且上游响应
`Content-Type: text/event-stream` 时先回写响应头，再用 `resp.read1(65536)`
分块 flush；否则 `resp.read()` 读完整包才回写。

`/api/v1/chat/completions`（`is_stream`）与 `/api/v1/events/stream` 在
Gateway 侧均为 `StreamingResponse(media_type="text/event-stream")`
（`jiuwenswarm/server/agent_http_routes.py`）。企业版路径
`_dispatch_enterprise` 已传 `stream_sse=True`，但个人版路径
`_dispatch_proxy` 遗漏——整包读取导致响应头要等整轮流结束才到，前端 15 秒
收不到响应头即发 interrupt。

## 修复决策

`_dispatch_proxy` 的 `_proxy_http()` 补传 `stream_sse=True`，与企业版路径
行为对齐。安全性：`streaming` 分支仅在上游响应为 `text/event-stream` 时生效，
非 SSE 响应（普通 JSON 等）行为与原先完全一致。

有意不改动：`/idp`、`/manager-api`、`_handle_file_download` 三处
`_proxy_http` 调用（无 SSE 场景，维持原样）；企业版路径不动。

## 验证

- 新增回归测试
  `test_personal_proxy_streams_sse_without_buffering_the_whole_response`：
  上游返回含完整事件 + 未完成 chunk 头的 SSE，个人版代理须返回 200 且内容为
  `b"data: ok\n\n"`。修复前该测试失败（502，`IncompleteRead`，整包读取等不到
  chunked 终止符）；修复后通过。
- `tests/unit_tests/channels/web/test_web_enterprise_proxy.py` 全量
  9 passed。
- `tests/unit_tests/channels/web/` 全目录 215 passed；另有 2 个失败
  （`test_send_head_cross_drive_path_returns_400`、
  `test_public_fallback_does_not_escape_static_root`）经 stash 基线对照确认为
  干净 develop 上已存在的 Windows 环境既有问题，与本次改动无关。

## 遗留

- `_HTTP_PROXY_TIMEOUT = 30`：SSE 流上相邻事件间隔超过 30 秒时代理读超时，
  企业版路径同样存在；长静默场景（深度思考）若复现可另行评估按响应类型放宽。
- Windows 本机 pytest 临时目录（带空格用户名）需 `--basetemp` 指到替代目录
  才能跑通，属本机环境问题，与仓库无关。
