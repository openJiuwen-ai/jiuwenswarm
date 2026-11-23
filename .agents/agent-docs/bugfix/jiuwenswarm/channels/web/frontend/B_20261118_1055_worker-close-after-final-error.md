---
id: B_20261118_1055_worker-close-after-final-error
title: 任务正常收尾后 worker 关闭被渲染为对话错误
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/features/free-models/chatError.ts
  - jiuwenswarm/channels/web/frontend/tests/chatError.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/webSocketStreamBoundary.test.mjs
designs: []
design_note: 只在 Web 前端区分「本轮已收尾」与「处理中失败」，不改 supervisor/gateway 的错误码与生命周期协议；无独立设计。
---

# 任务正常收尾后 worker 关闭被渲染为对话错误

## 现象与根因

#6373：专家团任务正常完成后，会话末尾多出一条灰色错误提示
`错误: runtime worker closed`，用户误以为任务失败。

常驻 worker 在任务结束后被回收，supervisor 会把尚未收到完成确认的请求
以 `ok=false`、`code=WORKER_CLOSED/WORKER_UNSTABLE` 结束；gateway 将其
转成 `chat.error`。前端 `chat.error` 处理器此前无条件调用
`setExecutionError`、`setSessionError`、`onError` 并追加
`错误: <message>` 气泡，没有区分“这一轮已经正常交过 `chat.final`”与
“处理中真正失败”，于是把 INFO 级的进程回收当成了任务错误。

## 修复决策

- 前端记录**覆盖整轮输出**的 `chat.final` 的 `request_id`（有界集合，最多 256 条，
  超出后丢弃最旧一条）。跨会话请求、`session_task_summary` 子任务总结和
  `final_mode=append` 分段 final 不参与记录：它们只覆盖局部输出，之后同一
  `request_id` 仍可能继续执行。
- `chat.error` 到达时，仅当同时满足以下条件才忽略该错误：
  - `code` 属于 `WORKER_CLOSED` / `WORKER_UNSTABLE`；
  - 该事件的 `request_id` 已经交过整轮 `chat.final`；
  - 该请求已不是会话当前正在处理的一轮（`isProcessing` 为 false，或正在处理的
    `request_id` 不是它）。
- 忽略范围仅限错误提示本身；`chat.error` 到达前已有的思考态、历史加载态、
  流式增量清理保持不变。
- 中途崩溃（没有 `chat.final`）、缺少 `request_id`、以及其它错误码仍按原逻辑
  报错；中间段 final 之后整轮仍在处理时（Goal 续跑、团队多轮），worker 关闭
  照常报错，避免把半截回答静默成完整结果。
- 不修改 supervisor / gateway 的错误码、日志级别或推送时机。

## 验证

先红后绿：关闭抑制分支后，新增的边界用例失败（错误气泡被追加）；
恢复后全部通过。

- `node --test tests/webSocketStreamBoundary.test.mjs`：15 passed。
  - `worker close after the turn final does not append an error bubble`：
    `chat.final` 后同一 `request_id` 的 `WORKER_CLOSED` 不追加气泡、
    不写 `executionError`；没有 final 的请求收到同一错误仍然追加错误气泡。
  - `worker close during a continuing turn after an intermediate final still reports the crash`：
    团队多轮中中间段 final 后整轮仍在处理，同一请求的 `WORKER_CLOSED`
    必须写入 `executionError` 并追加错误气泡。
  - `cross-session finals do not mark a request as a finished turn`：
    跨会话 final 不能作为整轮收尾判据。
- `npm run test:chat-error`：12 passed。
  - `worker lifecycle close after a delivered final is not a task error`：
    `WORKER_CLOSED`/`WORKER_UNSTABLE` 且已有 final → 忽略；
    同码但无 final、缺少 `request_id`、其它错误码 → 仍报错。
  - `worker lifecycle close while the same request is still running stays a task error`：
    同一请求仍在处理 → 报错；新一轮已处理别的请求 → 忽略；恢复出的处理态
    无 `request_id` → 不视为该请求仍在运行。
