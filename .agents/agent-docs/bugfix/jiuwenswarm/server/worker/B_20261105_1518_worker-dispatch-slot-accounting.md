---
id: B_20261105_1518_worker-dispatch-slot-accounting
title: Worker 派发容量排除已返回的 handler
type: B
author: kdgmpw
author_email: lishuqi4@huawei.com
modules:
  - jiuwenswarm/server/worker
files:
  - jiuwenswarm/server/worker/event_store.py
designs: []
design_note: 按会话中确认的最小修复方案调整 Worker 内部计数。
---

# Worker 派发容量排除已返回的 handler

## 现象与根因

现场日志最早在 16:05:21 记录服务端推送被 `worker dispatch limit reached` 拒绝，cron 随后也出现同类错误。日志能确认派发饱和，槽位的实际构成无法从日志还原。

`WorkerEventStore` 原先用 `_active` 去重索引的大小计算派发容量。handler 返回时会将 `DispatchRecord.active` 设为 `False`，但有待答交互的记录仍留在去重索引中。256 条这类记录即可复现全局拒绝。

## 修复决策

`register()` 按 `DispatchRecord.active` 计算 256 个派发槽的占用，去重和待答交互仍沿用各自的生命周期。直接删除去重索引会允许旧请求 ID 重复登记，清除交互则会影响重连恢复。派发和保留记录分别受 256 和 2048 的上限约束。

## 验证与边界

在 Windows 的项目 `.venv` 中对分支基线与工作区实现运行定向探针，直接调用 `WorkerEventStore` 的登记、结束、事件追加、快照和回放方法。当前环境缺少拆分后的 `agent_gateway`，探针仅补齐其历史同值常量并跳过旧 E2A 包初始化。

- 256 条已返回的待答记录使基线拒绝新请求。修复后新请求与新目标推送可登记，原交互仍可快照和回放，同调用方换会话重用请求 ID 仍会被拒绝。
- 256 条仍活动的派发使第 257 条请求被拒绝。结束一条后，新请求可登记。
- 保留 2048 条记录后仍触发保留记录上限。

探针覆盖事件存储的计数行为。完整 Worker IPC 和真实 Runtime 的挂起、恢复链路需在依赖齐备的环境验证。
