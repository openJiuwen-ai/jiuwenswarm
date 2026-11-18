---
id: B_20261118_1056_issue6785-snapshot-owner-cancellation
title: Issue 6785 快照发送回收取消传播与模型列表自动恢复
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models
files:
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelsSettings.tsx
  - tests/unit_tests/agentserver/test_snapshot_send_recovery.py
  - tests/unit_tests/agentserver/test_worker_recovery.py
  - tests/unit_tests/agentserver/test_worker_automatic_recovery.py
  - jiuwenswarm/channels/web/frontend/tests/modelRefreshUi.test.mjs
designs: []
design_note: 对既有快照查询取消边界和模型列表自动恢复的小范围修复，无独立设计。
---

# Issue 6785 快照发送回收取消传播与模型列表自动恢复

关联 [Issue #6785](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6785)、[develop PR !10467](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10467) 和 [0.2.9 PR !10468](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10468)。本记录补充这两个 PR 的评审修复；检视前候选分别为 `7312f3d8762cdba3a9dfdf1ae510fdfec3e5959a`、`721458da99b71248e98d38e13ef4f565dedfac11`。

## 故障与证据边界

原问题中短暂 Worker IPC 不可用会使模型列表请求失败，Web 仍连接但页面未自动恢复。原 PR 把快照发送交由连接持有，避免观察者等待超时取消 Windows 管道写；前端则持续重试临时不可用的列表请求，恢复列表及按钮，无手动重新加载入口。受控管道实验验证了超时取消写的断链路径，但现场缺少首次关闭调用栈，不能据此认定原现场首次关闭的唯一来源。

两条 PR 的评审进一步指出：`wait_for(shield(sending))` 只阻止外层取消影响发送；连接回收通过 `_cancel_side_tasks()` 取消发送时，`CancelledError` 仍会向外传播。实际受影响入口为 `session.get_metadata`：`MethodRouter._dispatch_control` 等待快照时被取消，无法发送元数据响应。新增直接调用与 Router 两条用例在未修复候选上均以 `CancelledError` 失败。

## 修复决定与不变量

- 使用 `asyncio.wait({sending}, timeout=...)` 观察连接持有的发送任务。等待超时返回 `snapshot_transport_timeout`，保留尚未完成的发送；同一时刻最多保留一个发送任务。
- 发送因连接回收而取消时，返回 `recovery_pending=true`、`recovery_reason=snapshot_transport_cancelled`。调用方主动取消查询仍传播 `CancelledError`，不取消共享写。普通发送异常仍由原异常路径处理，发送任务始终由连接统一观察和回收。
- Router 在恢复中保留持久化 `is_processing`、`status`，避免把无法取得快照误报为空闲。E2A 信封、IPC 身份校验、协议版本和原有重试间隔保持一致。
- 采纳前端断言建议：检查实际错误区域内没有按钮，替换对从未存在的 test id 的空值断言；错误文案分支改为明确的 `if/else`。
- 不采纳仅因 `RUNTIME_FAILED` 就停止自动重试的建议。`_mark_retryable_failure()` 在启动资源暂不可用时也会进入 FAILED，并由 `_automatic_recover()` 等待后继续恢复；该错误码不能区分所有永久故障和可恢复冷却态。沿用原重试集合，补充等待超过 30 秒后恢复的前端行为覆盖，以及后端既有资源故障自动恢复用例。

## 验证

Windows Python 3.13.9，分别使用两个目标分支已安装的冻结依赖环境；测试直接加载对应工作树。前端使用 Node 与 npm 10.9.2 的既有 `test:model-refresh` 行为测试。

- 修复前对照：新增 owner-cancellation 两项均失败，错误为 `CancelledError`，其中一项经过真实 `MethodRouter.dispatch` 消费入口。
- develop 定向回归：`test_snapshot_send_recovery.py`、`test_worker_recovery.py` 与 `test_bootstrap_recovers_automatically_after_resource_failure`，共 19 passed、2 deselected。包含主动取消仍生效、连接回收仍响应、处理态保留、Windows 原生管道背压，以及 resident/front_loopback 两种后台自动恢复路径。
- develop 相关回归增加 `test_ipc_receive_recovery.py`、`test_supervisor_forwarding.py`、`test_worker_residency.py`、`test_resident_worker_shutdown.py`，共 114 passed、3 skipped、2 deselected；跳过项为 Windows 平台条件，包含真实 Worker 与 WebSocket 在转发阻塞后继续工作的既有验收。
- 前端 `npm run test:model-refresh`：31 passed，覆盖临时错误自动恢复、RUNTIME_FAILED 长时间等待、恢复后按钮可用、无手动恢复按钮、断开重连、离开页面和过期响应。
- 两个已知持久 Worker 历史回放用例在原 develop 基线同样发生 20 秒超时；使用 `-k 'not persistent_subprocess_recovers'` 明确排除，不计入通过数。

按当前仓库约束不运行格式化或静态检查。未完成真实浏览器视觉验收、现场完整会话或真实模型调用；React 组件行为测试与受控 IPC 实验不替代这些证据。远端 CI 结果需在 PR 上单独核验，不能由本地测试推定。
