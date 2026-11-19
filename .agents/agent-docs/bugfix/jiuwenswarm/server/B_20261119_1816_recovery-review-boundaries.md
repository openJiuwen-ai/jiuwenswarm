---
id: B_20261119_1816_recovery-review-boundaries
title: Worker 恢复的收包新鲜度、前置锁、取消登记和历史终态边界
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/harness/common
files:
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/runtime/session/session_archive.py
  - jiuwenswarm/server/runtime/session/history_stream.py
  - jiuwenswarm/agents/harness/common/session_ops_service.py
designs: []
design_note: 对既有恢复契约的缺陷修复，未建立独立设计。
---

# Worker 恢复边界复查

## 现象与影响

对 develop PR !10589 和 releases/0.2.9 PR !10626 复查，确认四类问题：

1. Front 自身停顿后，两个旧 pong 分别在 watchdog 检查前后出队，会把健康 Worker 直接判为失联。新请求完成帧若早于探测观察器登记，也可能在写队列堵塞时被遗漏。
2. 冷恢复的前置检查在进入恢复配额前等待会话文件锁，20 个会话可占满共享 I/O 执行器，无关读取也无法执行。
3. 解归档登记已落盘、等待结果时取消，失败记录和恢复唤醒被跳过，状态保持 running，直到普通恢复扫描。
4. 重复终态回执被当成后续模型活动，导致已取消的孤立调用把 1,100 个独立完成回合连成一个超限组，错误返回 HISTORY_RESTORE_LIMIT。

## 关联设计与当前契约

保持 IPC 线协议、recv 返回值、会话及持久化格式不变。遵循现有恢复配额、生命周期执行所有权与历史工具调用成组约束。当前排障边界同步在 `docs/zh/session-restart-recovery.md` 和对应英文文档。

## 根因与修复决策

- IPC 队列额外携带本地解码时间，Front 使用帧的实际到达时间。业务 beat 按观察时间排序，旧排队帧不能覆盖更新的探测证据；更新的高 stall-age 心跳仍能暴露真实业务停顿。
- 每条物理连接保留至多 256 个已匹配完成请求的 issuance/receipt 时间，仅保留关联元数据。探测先登记观察器再查近期结果，匹配 generation 和新请求时间下界，接受后消费，关闭连接不提供有效证据。
- 两处恢复配额前的锁检查改为非阻塞；锁忙分别返回“需要受配额保护的恢复”或“不复用旧 LIMIT 判定”。真实历史恢复仍在原有配额下执行。
- 将解归档登记纳入取消收尾，并在同步登记线程中保存准确 operation_id。只收尾该操作，保留已有身份校验；已接纳文件 I/O 完成后才允许释放执行所有权，重复取消也会唤醒恢复。
- 前瞻索引忽略纯终态回执及非手动回退的压缩审计，与模型迁移 reader 的语义一致。真实后续消息和工具结果仍阻止提前切断调用组。

拒绝延长普通心跳超时、扩大线程池或取消强制杀线程：这些做法不能修正新鲜度与配额顺序，还会掩盖实际失联或破坏持久化顺序。未删除迟到工具结果保护，也未修改原始历史内容。

## 回归证据与适用范围

Windows / Python 3.13，分别使用两个发布线锁定的 agent-core 环境。新增用例使用真实 loopback TCP、Windows resident named pipe、临时目录和文件锁，以及原生 ContextEngine/Session（受控压缩替身）。

- 旧代码在两条线复现上述四类故障，旧 pong 场景记录 `evidence=no incoming frames`，关闭前没有发送新探测；同一对端能立即响应探测。
- 前置预检用 20 个真实会话锁占满 20 个共享 I/O 线程，并检查锁释放前能否完成无关元数据读取；包含普通预检和缓存 LIMIT 预检两条入口。
- 解归档分别在 claim 和 update 阶段重复取消，检查已接纳操作的失败状态、三秒内恢复、执行锁释放和原始历史字节一致。
- 重复终态回执、自动压缩审计均以 1,100 个完成回合验证原生上下文恢复，当前请求排除，源历史不变。既有迟到工具结果、跨请求同名调用和超大真实调用组测试继续保留。
- 两条线各执行 17 个相关测试模块，均为 673 passed、7 skipped；跳过项为 Windows 不支持的 Unix-domain-socket 用例。覆盖历史迁移/快照、恢复队列、解归档、IPC 心跳/停滞、Worker 换代、并行会话准备和运行服务。真实四会话/双 DB 验收结果另随 PR 验证报告记录，不以单测或 agent_ready 代替会话恢复证据。

## 防错模式与资料同步

本地排队不改变事件实际发生时间；配额前的推测检查不得同步等待资源锁；取消边界必须包括登记持久化阶段；历史组的前瞻判断只统计实际模型语义。对应中英文运行恢复说明已同步，回归用例固化这些边界。

## 遗留与后续

本次修复不提供崩溃后自动重放外部副作用，也不把历史任务状态当作实时执行证明。四会话验收需同时核对原 DB 不变、Worker 实例替换、历史进入新推理、新回合落盘、团队成员/任务保留及退出清理。
