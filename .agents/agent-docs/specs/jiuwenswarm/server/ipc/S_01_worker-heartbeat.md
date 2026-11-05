---
id: S_01_worker-heartbeat
title: Worker IPC 心跳隔离与业务卡顿诊断
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/worker
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/ipc/io_loop.py
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/worker/stall_context.py
  - jiuwenswarm/server/worker/diagnostics.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
---

# Worker IPC 心跳隔离与业务卡顿诊断

最近修订：2026-11-05。

## 范围与归属

本规约覆盖 Supervisor 与 Runtime Worker 的 IPC 存活检测，不涉及业务 Heartbeat 调度器。命名管道由持续读取线程处理；Unix domain socket 和 loopback TCP 的连接建立、鉴权、收发与关闭由每进程一个专用 asyncio I/O 线程处理。该线程不执行 Runtime 业务，随进程结束退出；连接关闭必须取消其读取任务并释放 transport。

所有生产连接由 `ipc.client.connect`、`IpcServer` 或 loopback 的 listener/session source 创建，保证 asyncio reader/writer 始终由创建它们的 I/O loop 操作。调用方通过线程安全的 future 桥接发送、接收和关闭；取消传递至所属 loop。`StreamConnection` 的直接构造适用于已在正确所属 loop 上建立的 stream，不迁移既有 transport。

## 不变量

- 业务循环进行普通同步阻塞等待时，I/O 线程持续读取；心跳回复不等待业务循环消费上一条消息。
- 普通业务消息按顺序进入有界缓冲区：256 条、64 MiB，任一上限超出即报告 `ipc business receive backlog exceeded`，并单独记录积压条数、字节数、当前帧大小和上限。消息不静默丢弃；已入队消息先交付，随后报告连接故障。同一连接复用心跳与业务帧，不能暂停整条连接的读取来等待业务消费，否则心跳也会被暂停。
- socket 的普通写入和心跳整帧写入均在同一个 I/O loop 串行执行，不跨线程操作 reader/writer，不交错帧字节。
- 独立心跳仅读取业务循环的 `LoopBeat` 时间戳，不能刷新它。`_beat_loop` 在业务循环内每秒刷新一次。
- 通信超时保持 30 秒，以最后一次收到任意 Worker IPC 消息计算；业务循环卡顿超过 30 秒告警，持续超过 180 秒清退并按重启预算恢复。实际处置时刻还受 5 秒 watchdog 检查周期影响。
- 清退进程共用既有退出期限，首次等待必须预留部分预算供强制终止和退出观察。PID 和创建时间仍须核验，不能以已发送终止信号代替已确认旧进程退出；未确认退出不得开始下一代 Worker。
- 心跳不能证明 Agent 已就绪；就绪、投递确认、取消和业务请求继续使用各自的独立消息。

## 心跳与归因载荷

保留 `kind: heartbeat`、`pid` 和可选 `loop_beat_age`。业务循环停顿至少 5 秒时，回复可增加 `loop_stall`；提前采样使首条 30 秒告警即使先于同周期 pong 被消费，仍有此前采集的业务上下文。旧 Worker 不提供该字段时保留兼容行为，旧 Supervisor 可以忽略它，不改变 IPC 协议版本。

`loop_stall` 的可保留字段为 `operation`、`task`、`request_id`、`session_id`、`channel_id`、`method`、`dispatch_id` 和 `stack`。标识最多 128 字符，最多 16 个栈位置，每项仅包含最多 256 字符的文件路径、行号和最多 128 字符的函数名。Supervisor 再次按白名单约束内容，日志和诊断快照不保留其他字段。

请求执行时绑定不可变的诊断标识，子任务通过现有 task factory 链继承；显式自定义 context 按该 context 获取标识。Worker 采样业务线程中实际运行的 Task 及原生 Python 栈，不使用所有在途请求的列表推测原因。不复制请求参数、提示词、工具结果、局部变量、源码文本或异常内容。无法归因时标记 `unattributed_task`、`event_loop_callback` 或 `unavailable`。

30 秒业务告警和 180 秒重启日志均输出 `business`，Supervisor 诊断快照保留 `worker_stall_context`。循环恢复、旧版无上下文心跳或新 generation 会清除旧上下文，避免跨业务误归因。约 25 秒的 Worker 原生线程栈转储保持有效，后台 pong 不推迟它。

IPC 写锁等待者的增删与诊断快照使用同一把线程锁，锁内不等待 I/O。Supervisor 收集 IPC/Gateway 诊断指标失败时仅记录异常类型；快照序列化或输出失败也不能中断 watchdog 的健康判定、180 秒恢复或转发失败处理。

## 限制与验证入口

归因是尽力而为的诊断信息，不参与健康判定。线程隔离无法保证 C 扩展长期持有 GIL、I/O 线程本身阻塞、同一连接出现传输背压或进程完全无法获调度时继续回复。资源耗尽和协议错误继续按连接故障处理。

- `tests/unit_tests/agentserver/test_stream_heartbeat.py`：TCP/Unix socket 阻塞隔离、子任务归因、有界队列、取消和并发写入期间的诊断快照。
- `test_worker_loop_liveness.py`：30/180 秒阈值、告警/重启日志、恢复后的上下文清除、诊断失败不影响恢复。
- `test_ipc_backpressure.py`：真实心跳丢失的 30 秒传输超时、Worker 60 秒阻塞与排队请求、真实 180 秒阈值下的重启和后续请求。
- `test_front_loopback_ipc.py`：鉴权、单连接消费、关闭、Front 丢失和进程回收。

修复决策与实际证据见 [B_20261105_1510_stream-heartbeat](../../../../bugfix/jiuwenswarm/server/B_20261105_1510_stream-heartbeat.md)。
