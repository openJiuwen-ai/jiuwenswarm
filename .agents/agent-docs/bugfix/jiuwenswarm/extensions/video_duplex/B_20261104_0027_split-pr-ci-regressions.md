---
id: B_20261104_0027_split-pr-ci-regressions
title: 全双工拆分 PR 的构建与测试回归
type: B
author: yzdnh123
author_email: yzdnh123@gmail.com
modules:
  - jiuwenswarm/extensions/video_duplex
  - jiuwenswarm/server/runtime/video_task
  - gateway/extensions/video_duplex
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/extensions/video_duplex/backend/session_receipts.py
  - jiuwenswarm/server/runtime/video_task/bridge.py
  - gateway/extensions/video_duplex/backend/tasks/bridge.py
  - jiuwenswarm/channels/web/frontend/src/App.tsx
designs: []
design_note: 根据拆分 PR 的实际流水线失败修复，无独立设计。
---

# 故障与根因

GitHub #10477（GitCode !9504）的模型身份测试在 attachment-only Agent 上失败：
RuntimePromptRail 调用全双工 summary policy 时 system_prompt_builder 为 None。
保留 attachment-only 路径，summary policy 在没有 builder 时直接返回。
原有 19 个失败可在本地复现；修复后整个模型身份测试文件 22 项通过。

Gateway checkpoint bridge 中误保留了一份 AgentServer 端 RemoteTaskEndpoint，
导致 gateway 反向引用 jiuwenswarm.runtime，违反分层契约。删除该重复实现，
将其中的诊断事件移到已有 server/runtime/video_task/bridge.py；调用方及 ACK
处理仍使用 server 端实现。没有放宽分层白名单。

桥接测试的 get_agent_root_dir 替身未接收现有 tenant_key 参数，单独运行时触发
首次导入路径失败。替身现在保留该可选参数，与生产签名一致。

GitHub #10484（GitCode !9509）的构建引用不存在的 newConversationProjectRef。
该字段已被 store 中的项目选择状态替代，删除设计会话创建路径中的过时赋值。
该修复单独提交到前端 runtime 分支。

# 验证

- Windows / Python 3.13.14：模型身份、checkpoint bridge、session receipts 共 72 项通过。
  后端组合验证临时提供了 #10476、#10478 的前置文件，验证后原样恢复，不将前置差异带入本 PR。
- 前端 designer-entry 20 项、task-full-duplex 62 项通过。Vite 构建通过；其后只删除
  一条未定义变量赋值。不运行本地格式化或静态检查；TypeScript 官方门禁待复跑。
- 两份官方 UT 日志都因 4 小时作业超时终止，最后停留在 test_session_messaging.py，
  未留下超时点堆栈。Python 3.13 下相关 12 个参数化用例通过；使用与 CI 相同的
  Python 3.11.5 后，相关 12 项及整个会话消息文件 152 项均通过。
  本地使用 Windows 和标准 SQLite，未覆盖 Linux/pysqlite3/xdist 组合。
  当前不能证明这次修复已消除官方偶发挂起，也没有通过跳过用例或放宽超时规避。

# 后续

推送后复跑官方流水线。若相同作业再次卡住，应取得运行中 worker 堆栈，区分
线程/SQLite 等待、协程清理与 xdist 调度，再修复实际阻塞点。
