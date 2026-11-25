---
id: B_20261119_1150_chat-terminal-reasoning-close
title: 消息出错后思考段不收口，「思考中…」永久转圈直到下次发消息
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/tests/chatTerminalReasoningClose.test.mjs
designs: []
design_note: 交互层缺陷修复，复用同文件 chat.processing_status(false) 分支已有的「终态兜底收口」口径（幂等空操作），无独立设计文档。
---

# 消息出错后思考段不收口，「思考中…」永久转圈直到下次发消息

## 现象与影响

WS 传输下发送消息，模型先吐思考流，随后收到 `chat.error`（如
GATEWAY_FORWARD_FAILED、模型调用失败等终态错误，且无后续
`chat.processing_status(false)`）：错误气泡正常出现，但思考块标题仍是
「思考中…」并持续转圈，直到用户下次发消息。MessageList 以
`running = !segment.closed` 判定思考块进行中，而 `chat.error` 处理器只
`setThinking(false)` 不封思考段——思考段的 `closed` 标记没人收。

## 关联设计与当前契约

`chat.processing_status(false)` 分支已有「轮次终态兜底收口」处理：思考段若因
「思考后无文本增量/无工具调用」未关闭，在该终态统一 `closeReasoning` 收口
（已关闭为幂等空操作）。本修复把同一口径补到三个终态处理器。

## 根因与修复决策

根因：终态事件的思考段收口依赖 `chat.processing_status(false)` 单一信号；
`chat.error`、`chat.final` 兜底分支、`harness.session_finished` 三个终态处理器
都清了 `thinking`/`processing` 状态，唯独不封思考段。错误路径常常没有后续
processing_status(false)（注释自证 GATEWAY_FORWARD_FAILED "may never deliver
processing=false"），思考段就永远悬在未闭合态。

修复决策（三处，均为 `closeReasoning(sessionId, { atMs: eventTimestampMs(payload) })`）：

- `chat.error`：紧跟 `setThinking(false)` 之后无条件收口（覆盖 heartbeat 等提前
  return 分支之前的所有错误路径）；
- `chat.final`：agent 模式 `!goalStillActive` 兜底分支补收口（goal 仍活跃时不动，
  遵守该分支既有的「不能清 thinking」注释语义）；
- `harness.session_finished`：终态兜底补收口。

不新增状态、不改事件协议；已闭合段上 closeReasoning 是幂等空操作，与既有兜底
分支同一口径。

## 回归证据与适用范围

2026-11-19 验证（结论级）：

- 新增 UT 3 用例（`npm run test:chat-terminal-reasoning-close`，沿用
  contextUsage.test.mjs 的源码锚定模式——useWebSocket 挂载依赖面太大无法在单测
  实例化）：钉住三个终态处理器必须含 `closeReasoning` 及其位置约定。红绿验证：
  未修复源码 3 红，修复后 3 绿。
- `tsc --noEmit` 无新增错误。
- 客户端复现（mock 后端 + vite dev + CDP 真实 UI：发送消息 → 思考流 →
  chat.error 终态）：develop 基线错误落地后思考块 `data-variant="thinking"`
  （转圈持续）；修复分支同操作 `data-variant="thought"`（已完成思考）。
  证据截图随 PR 描述。
