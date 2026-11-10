---
id: B_20261110_2305_resident-ipc-recovery-after-backlog
title: IPC 流式积压断连后 resident Worker 重挂接超时未恢复
type: B
author: GeirvaldrOden
author_email: qzqanlhy2012@163.com
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - tests/unit_tests/agentserver/test_stream_heartbeat.py
  - tests/unit_tests/agentserver/test_worker_automatic_recovery.py
designs: []
design_note: 针对既有 IPC 与 Worker 生命周期行为的局部缺陷修复，无独立设计。
---

# IPC 流式积压断连后 resident Worker 重挂接超时未恢复

关联 Issue：#6400。

## 现象与影响

高密度流式事件超过 IPC 读端的业务消息缓存上限时，`StreamConnection` 抛出 `IpcError` 并断开。Supervisor 随后重连 resident Worker；若 HELLO 超时，原实现会反复连接同一个进程，导致 Worker 不能重新接收 `chat.send`。

Issue #6400 日志记录了 731 条流式事件及随后约 10 分钟的 HELLO 超时重试。报告中的 worker PID 与构建版本来自用户现场，本地未持有该容器日志。

## 关联设计与当前契约

无独立设计。本修复沿用 IPC 帧协议、Supervisor restart budget 和已验证进程身份契约，不改变消息格式及 Gateway 行为。

## 根因与修复决策

- Stream 业务帧缓存此前最多 256 条，虽字节限额为 64 MiB，但高密度的小帧先触及条数上限并使读取任务抛错。本次将 stream 条数限制提高到 2048，字节限额维持 64 MiB；新回归覆盖 731 条有序帧。
- Resident 重连入口此前清空已验证的 Worker PID 与创建时间。HELLO 超时后 Supervisor 因而失去安全退役该 resident 进程的身份，重启预算耗尽后仍持续重挂接。现在保留上次成功验证的身份，并将 resident HELLO 超时作为可识别的失败；仅当已有已验证 PID 时退役该进程，再由原重启流程拉起新 Worker。
- 进程退役成功后清除保存的身份，避免后续启动沿用过期 PID。

未选择在 IPC 读循环达到缓存上限时直接阻塞：心跳也由该循环读取和快速响应，简单阻塞会改变心跳处理时序。未来若要引入真正的业务背压，应单独设计能够同时保障心跳及时处理的读调度。

## 回归证据与适用范围

- 新增自动化用例覆盖 TCP/Unix stream 缓存接收并按序消费 731 帧，以及 resident HELLO 超时后退役先前已验证的 Worker。
- Linux 本地环境，Python 3.11.16 / pytest 9.2.0；按仓库声明安装项目锁定的 `openjiuwen-protocol` 与 `agent-gateway` 后执行：
  `PYTHONPATH=.:$PYTHONPATH /home/Admin/miniconda3/envs/jiuwenswarm/bin/python -m pytest tests/unit_tests/agentserver/test_stream_heartbeat.py tests/unit_tests/agentserver/test_worker_automatic_recovery.py -o addopts='' -q`
  结果：26 passed，40.17 秒。覆盖两份完整测试文件，包含 TCP/Unix 缓冲边界、731 帧突发、resident HELLO 超时清理以及已有自动恢复路径。
- 无 CI 容器复现或真实模型/E2E 重测；修复后的部署验收仍需在 Linux 容器中复跑 Issue #6400 步骤，并确认恢复后新会话 `chat.send` 正常。

## 防错模式与资料同步

Resident 重连失败时，退役操作必须基于此前验证的进程身份，不得仅凭 IPC 地址杀进程。Supervisor 保留该身份直到新 Worker 验证成功，或旧 Worker 被确认退役。本次通过故障记录与回归用例记录此模式。

## 遗留与后续

2048 条仍是有界缓存而非无界背压。若未来事件负载或消费者停顿超过当前边界，仍可能断开；届时应在保持心跳响应能力的前提下评估业务帧背压。
