---
id: B_20261127_1515_restore-chat-error-indicator
title: 恢复失败轮次的会话侧栏错误标记
type: B
author: lizhiyuan
author_email: lizhiyuan65@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/historyRestore.ts
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/ConversationSidebar.tsx
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/sidebarModel.ts
  - jiuwenswarm/channels/web/frontend/src/types/message.ts
  - jiuwenswarm/channels/web/frontend/tests/historyRestorePagination.test.mjs
designs: []
design_note: 复用现有 chat.error 事件和侧栏失败状态，修复历史恢复遗漏。
---

# 恢复失败轮次的会话侧栏错误标记

OpenAI 模型触及输出限制时，网关正常转发 `chat.error`，会话历史也记录了该事件。Web 恢复历史时能显示报错正文，但侧栏原先只读取实时运行态中的错误字段。恢复出的错误气泡没有携带结构化错误状态，侧栏因此显示时间。

实时处理和历史解析现都在前台 `chat.error` 气泡保留错误字段。侧栏优先使用实时状态，空闲时从最新一轮消息补读错误。后续轮次开始或已有成功回复时，旧错误仍可在聊天历史中查看。

回归用例通过 `history.message` 帧恢复输出超限错误并检查侧栏状态，同时覆盖运行中、等待输入以及后续成功轮次。自动化验证使用模拟历史帧，实际事件形态已与本地复现记录核对。
