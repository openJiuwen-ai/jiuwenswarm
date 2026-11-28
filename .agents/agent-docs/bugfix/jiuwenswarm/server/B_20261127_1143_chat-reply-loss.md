---
id: B_20261127_1143_chat-reply-loss
title: 修复会话无回复与集群审批后未执行
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/server/worker/execution_requests.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/server/runtime/session/session_history.py
  - jiuwenswarm/agents/harness/code/rails/code_agent_mode_rail.py
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
designs: []
design_note: "修复现有对话派发和计划审批流程，依据 Issue #7119 的原始日志与复现。"
---

# 修复会话无回复与集群审批后未执行

## 现象与根因

[GitCode Issue #7119](https://gitcode.com/openJiuwen/jiuwenswarm/issues/7119)

- 单 Agent 的语音处理器缺席时，语音合成请求透传到 AgentServer，误占同一会话的输出租约。后续普通请求已写入用户消息历史，却在派发前返回成功受理和结束帧。模型正文归属于误入执行的语音请求。
- Team 计划审批等待期间，Web 把上一轮的空闲事件当作审批轮结束，提前补发执行指令。指令进入即将结束的审批轮 steering 队列。受控复现中，它直到后续普通消息启动新轮时才进入模型上下文。

## 修复决策

- AgentServer 拒绝被透传的语音合成请求。语音合成继续由网关处理。
- 普通对话没有输出租约时返回 SESSION_BUSY，保留已有租约的所有权以及现有的中断恢复和 Goal 注入逻辑。派发给其他请求的输出流会让回复归属变得不明确，因此这里明确报错。
- Web Team 复用现有批准语义，在审批恢复轮内继续执行。Leader 成功退出计划模式后同步会话模式并通知客户端。该路径消除了依靠会话空闲事件启动实施的时序依赖。旧 Plan 流的输出保留历史记录上的模式，写历史时不再回退会话模式。

## 回归证据

基线来自 releases/0.2.10。使用 Windows、项目 Python 环境、真实 Gateway 和 AgentServer、本地可控模型及 WebSocket 入口复现。

- 默认语音处理器和处理器缺席两种配置均验证了后续普通聊天能得到模型正文。处理器缺席时返回 METHOD_NOT_FOUND，模型没有收到 TTS 请求。
- 用真实 SDK 租约固定冲突窗口。流式和非流式请求均返回 SESSION_BUSY，租约释放后同一会话正常回复。
- Team Work 和 Code 均在批准恢复轮发起模型调用并返回正文，无需第二条执行消息。session.plan_status 返回 in_plan=false，磁盘元数据保留对应的普通模式。
- Chromium 中运行 Web 审批逻辑并记录请求。Team Work 和 Code 各发一次批准，上一轮空闲事件未追加请求。跳过、修订和单 Agent 的审批选项继续使用原取值。
- 两条定向历史单测通过，覆盖 Team Plan 输出与子 Agent 输出对父会话路由模式的隔离。
- CI 首轮 UT 暴露出 3 个断言旧「假装受理」契约的用例：另一 reader 持有租约时的普通请求、同一会话两个并发 chat.send、MoA 已有输出所有者。这些请求实际从未派发，用例却只观察到 runtime.accepted。现改为断言 SESSION_BUSY，并保留原有「不提交中断状态」「模板只加载一次」「保留既有所有者邮箱」断言。
- 后续 CI 报告还有两条 UT 失败。群任务用例通过事件验证代理受理后仍可调整同一执行，并保留整条用例的超时保护。个人知识用例使用停用的 RSS 源，保留配置默认值与真实 Host/Core/Rail 的检查。Windows 项目环境中，给首次加锁注入 1.25 秒延迟并将个人知识用例的临时目录设为 Core 敏感路径，两条用例在修复前复现报告中的异常，修复后均通过。Linux CI 待重跑。
- Linux CI 复跑 `17f6496f7` 时群任务用例通过，个人知识用例仍报配置校验失败，另有两条启动超时。rebase 到 `upstream/develop` 的 `720475742` 后沿用上游对 AgentServer 停止顺序用例的 Memory HTTP 隔离。个人知识用例固定空默认模型列表，Worker 保活用例为首次 IPC 握手留出 Supervisor 的启动时限，并在启动失败时回收子进程。Windows 项目环境定向运行报告中的三条用例及群任务用例，注入不完整的默认模型环境变量与 5.25 秒 Worker 启动延迟，四条通过。新提交尚待 Linux CI 验证。
