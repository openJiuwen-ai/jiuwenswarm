---
id: B_20261110_1042_issue6191-ipc-recovery
title: IPC 突发积压与降级就绪重连导致 Runtime 失败
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - tests/unit_tests/agentserver/test_ipc_receive_recovery.py
designs: []
design_note: 对现有有界 IPC 和 Runtime 恢复状态机的小范围修复，无独立设计。
---

# IPC 突发积压与降级就绪重连导致 Runtime 失败

关联 issue：#6191。目标为 develop 与 releases/0.2.9；两个目标各自从最新远端基线创建隔离工作树，分别验证锁定依赖。

## 现场证据与边界

2026-11-05 的现场日志记录：15:22、21:39 和 21:43:40 的 IPC 断连后均重连到驻留 Worker PID 17880，恢复计数仍累积为 1/3、2/3、3/3。21:43:43 明确出现 `ipc business receive backlog exceeded`，随后 Front 进入 FAILED。21:44:12 的 30 秒心跳告警晚于接收失败；21:56 的新对话在 Front 收到后立即返回错误。Worker 的业务日志在故障后继续产生。

启动日志同时记录可选 image capability 探测失败。源码会保留带 `agent_ready=true` 的 DEGRADED readiness；旧 Supervisor 仅在 AGENT_READY 分支清零恢复次数，成功恢复的 DEGRADED 分支不清零。现场未记录原始 readiness 帧，故该放大链由现场能力失败记录、源码与对照实验共同支持，不能声称已逐帧回放现场。

接收积压日志没有溢出瞬间的队列深度、字节数和消息类型，不能唯一归因于某个 history.get 或模型请求。这里的 IPC 接收队列与 [#6226 转发队列修复](../supervisor/B_20261107_1505_6226-forwarding-replay.md) 分属不同阶段。

## 修复决定与兼容性

- 接收队列仍限制为 256 条、64 MiB。命名管道用条件变量、socket 流用事件，在已读入帧无法入队时最多等待 0.5 秒供消费者排空；每次消费和关闭均唤醒等待者。队列、字节上限和消息顺序保持不变。
- 持续无法释放容量时仍返回同一个 IpcError；记录 transport、pending_messages、pending_bytes、incoming_bytes、incoming_kind 与限制，避免只看到稍后的心跳超时。等待仅发生在 I/O 线程或 I/O 事件循环，不阻塞业务事件循环；满载时后续同连接心跳也会被有界等待延迟，持续超载仍会失败。
- 带执行就绪的 DEGRADED 与 AGENT_READY 一样结束恢复失败计数；显式 agent_ready=false 的 DEGRADED 不解除 FAILED。
- 每个故障 IPC 会话退出时取消其心跳、看门狗、进程观察任务并关闭旧连接，覆盖驻留模式及恢复预算耗尽分支。关闭连接不替代 Worker 生命周期管理，健康驻留 Worker 仍可重连。
- 不改变 E2A、IPC 消息格式、协议版本、依赖、持久化格式或超时配置。不通过增加心跳超时、扩大队列或取消有界保护处理突发。

English summary: receive queues retain their 256-message / 64-MiB limits and allow a bounded 0.5-second drain window before reporting overload. Execution-ready DEGRADED reconnects reset the recovery streak, and failed IPC sessions release their transports and watchers.

## 验证

基线：develop `57546af0128573fa008e3fb1794eb9538bbcc280`；releases/0.2.9 `25db4d5c9f63ca7671e42f9cbe96d5fd6c2d366a`。候选为本记录所附产品与测试差异。

- Windows develop 使用 Python 3.14.6，锁定 Core `f3f8f576164cc405c046e43079c8ad00ee17f0c4`、Gateway `435d3323425e1051c6fe2d1feadcba68b4be020d`。
- Windows 0.2.9 使用 Python 3.11.15，锁定 Core `2f4bedb9c5d85ab70d7c79d36183e6a98799e94e`、Gateway `43a1d502d0bd6ea4b6665644be50d674167003e9`。
- 两个未修复基线运行同一组负向对照，均为 10 failed、1 passed、7 deselected；失败包括原生 pipe/socket 突发、DEGRADED 重连计数、故障清理及真实 Worker 重连。失败原因均位于预期断言。
- 两条候选分支新增定向用例各 18 passed，覆盖消息/字节边界、默认限制下 600 帧突发、持续超载、心跳恢复、等待中关闭、非就绪降级、故障退出及真实驻留 Worker 连续四次重连。真实 Worker 每次重连保持 PID/创建时间，业务请求完成，最终进程退出。
- 检视发现非执行就绪 DEGRADED 仍须保留能力进度；修订后两条最终候选重新运行新增定向与相邻回归，均为 214 passed、3 skipped、15 deselected。3 项跳过为平台条件，15 项未选择为 slow 标记。
- 定向命令：`.venv/Scripts/python.exe -m pytest tests/unit_tests/agentserver/test_ipc_receive_recovery.py -o addopts="" -o asyncio_mode=auto -o log_cli=false -q`。
- 相邻回归覆盖 `test_worker_loop_liveness.py`、`test_ipc_backpressure.py`、`test_supervisor_forwarding.py`、`test_runtime_worker_phase2.py`、`test_worker_automatic_recovery.py`、`test_front_loopback_ipc.py`，使用 `-m "not slow" -o addopts="" -o asyncio_mode=auto -o log_cli=false -q`。
- 本地测试通过独立 pytest 收尾插件在根 conftest 删除隔离 home 前关闭日志句柄，规避 develop 基线已有的 Windows WinError 32；不修改被测实现或断言。第一次使用旧依赖环境的 17 项初检虽通过，进程在日志清理阶段退出 1，不计入锁定依赖验证。

真实 Worker 验证使用生产子进程与 IPC 入口、stub 业务回声，只在测试进程中将就绪帧改为 DEGRADED；没有加载真实 AgentRuntime 或调用模型。

本地自检按 jw-review 核对接收上限、帧顺序、心跳/业务循环职责、就绪进度、跨代会话及资源所有权，未发现本次变更的阻塞问题；维护者评审和远端 CI 由 PR 流程继续完成。

## 限制与重验条件

未运行真实模型、现场完整会话、安装包、Linux/macOS 或远端 CI；本地真实管道和真实 Worker 进程证据不替代这些验收。未运行格式化或静态检查。持续慢消费和超过等待窗口的业务停顿仍允许触发有界过载错误；不承诺任意流量永不溢出。

后续修改接收队列、消费者调度、心跳、readiness 或 Supervisor 资源所有权时，应重跑本次对照和对应生命周期回归。队列上限、等待策略及错误语义的当前实现以 stream.py 和相关行为测试为依据。

