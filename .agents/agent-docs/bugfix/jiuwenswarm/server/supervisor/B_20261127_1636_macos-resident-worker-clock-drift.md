---
id: B_20261127_1636_macos-resident-worker-clock-drift
title: macOS 升级后旧 Worker 因创建时间漂移无法退出
type: B
author: XINCHEN ZHANG
author_email: 279219073@qq.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/common/process_identity.py
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - tests/unit_tests/agentserver/test_worker_process_identity.py
designs: []
design_note: 在现有 Worker HELLO、部署归属和关闭应答契约内修复旧版兼容路径，无独立设计文档。
---

# macOS 升级后旧 Worker 因创建时间漂移无法退出

## 现象与根因

[Issue #6925](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6925) 报告 WorkSwarm 0.2.9 DMG 升级后出现 `AgentServer runtime failed`，无法创建会话。本机报告版本为 `f462c188f`。在其 Git 历史中，旧 Worker 的严格创建时间比较由 [!9259](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/9259) 恢复。旧 Worker 只上报墙钟创建时间。新进程读取同一 PID 时若观测到不同时间，就会报 `Worker PID identity changed`，拒绝通过 IPC 关闭旧 Worker。残留 Worker 继续持有 IPC，启动替换可能报 `resident Worker replacement failed`，升级前关闭也可能失败。

macOS 实机上用真实旧 Worker 在测试侧注入 0.750 秒 HELLO 创建时间偏差，复现了此故障链。公开 `develop` 基线 `8b3b92a71` 启动替换和升级前关闭均失败。公开 `releases/0.2.10` 基线 `73cc686b3` 启动时已有强制回收兜底，升级前关闭仍因身份校验失败而中止。实际安装的 WHL `6f8b8895` 和 `46cc394b` 也复现了失败。对后者发起真实 WebSocket `session.create`，返回 `RUNTIME_FAILED`。

## 修复决策与归属约束

1. 将只有墙钟身份的旧 HELLO 时间戳不符与其他身份校验失败区分开。已上报内核启动计数器或稳定启动标记的 HELLO 保留分支原有的身份校验规则，关闭时也不会按仅墙钟身份处理。
2. macOS DMG 和 WHL 的关闭路径在旧时间戳不符时，读取现有 Unix socket 的内核对端 PID（`LOCAL_PEERPID`）。内核 PID 必须与 HELLO 声明的 PID 一致，部署、用户、可执行文件、Worker 命令和 socket 参数仍须匹配。缺少部署身份的非冻结旧 Worker 继续遵循显式 PID 选择规则。
3. 使用本端观测到的进程身份监测退出，沿同一连接请求正常关闭。对于支持关闭应答的 Worker，成功还需收到成功应答、确认进程退出和 IPC 所有权锁释放。启动替换、历史端点迁移和升级前关闭使用同一退役逻辑。
4. macOS 对端 PID 不可读取或不匹配时，启动替换失败。发布分支的旧强制回收依赖目标进程的用户、可执行文件和命令行特征，无法核实该进程是否拥有当前已连接的 IPC，因此此处不回退到旧强制回收。其他平台保留原有行为。

## 回归证据与适用范围

| 分支 | 候选 | 定向 Python 回归 |
| --- | --- | --- |
| develop | [!11093](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/11093)，实现提交 `6bdfae292` | 118 项通过 |
| releases/0.2.10 | [!11094](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/11094)，实现提交 `9230940be`，测试提交 `e80277070` | 133 项通过 |

- WHL 在 macOS arm64 的隔离环境中原地切换。保留真实 0.2.9 WHL Worker，对其 HELLO 注入 0.750 秒偏差。两条候选分支的启动替换和停止入口均收到正常关闭应答，旧 Worker 以状态 0 退出并释放 IPC。新 AgentServer 返回 `AGENT_READY`，WebSocket `session.create` 返回 `succeeded`。
- 两条分支的本地未签名 DMG 均经校验并只读挂载。包内冻结后端复验了同样的启动替换与升级前关闭场景，两者均确认旧 Worker 正常退出，替代 Runtime 返回 `AGENT_READY`。
- 定向回归包含对端 PID 正常、不匹配和缺失，非冻结旧 Worker 缺少部署身份，以及发布分支的 macOS 回收拒绝路径。当前证据为 macOS arm64 上的受控偏差、真实进程与命令行验证。自然发生的时间偏差、完整桌面界面升级及 Windows/Linux 实机行为仍待验证。

运行契约见[中文生命周期说明](../../../../../../docs/zh/runtime-worker-lifecycle.md)和[英文生命周期说明](../../../../../../docs/en/runtime-worker-lifecycle.md)。设备验证经过及已知限制也记录在两条 PR 中。

进程身份不变量见[进程身份规约](../../../../specs/jiuwenswarm/common/S_01_process-identity.md)。
