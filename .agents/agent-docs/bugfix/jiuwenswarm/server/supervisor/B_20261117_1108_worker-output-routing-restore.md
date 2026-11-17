---
id: B_20261117_1108_worker-output-routing-restore
title: Issue 6596 恢复 Worker 原始输出路由
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/supervisor/worker_process.py
  - tests/unit_tests/agentserver/test_ipc_backpressure.py
  - docs/zh/runtime-worker-lifecycle.md
  - docs/en/runtime-worker-lifecycle.md
designs: []
design_note: 用户明确要求还原日志采集改动之前的逻辑，属于既有 Worker 输出行为的定向恢复。
---

# Issue 6596 恢复 Worker 原始输出路由

## 现象与根因

Issue #6596 的启动截图出现外层 `ERROR [Worker stderr ...]` 包裹内层 `INFO:common:Registered parser ...`，启动随后正常完成。

0.2.9 的 !9883（`3e8d9e672`）新增 stdout/stderr 管道、读取线程和逐行转发，将 stderr 全部标成 ERROR。develop 的 !9920（`bb527b51`）只为 front_loopback 转发，将 stderr 标成 WARNING。两者均把输出通道当成日志级别；原工单未提供 WHL 或构建 SHA，不能仅凭分支字段确定原包来源。

## 修复决策与契约

按用户要求恢复两项 PR 各自父提交中的 `WorkerProcess` 实现：stdout 继承启动进程，stderr 直接追加到原日志文件；删除输出读取线程和逐行重标，覆盖 resident 与 front_loopback。保留 stdout/stderr 之外的现有部署身份、IPC、健康检测及鸿蒙 `/proc` 身份兼容修复。

原始 INFO、异常堆栈和 stderr 字节仍保存在 stderr 文件；stdout 按继承关系进入原启动链路。运行诊断保留 stderr 路径，兼容的 `worker_stdout` 字段为 null。此次没有增加等级解析、调整告警阈值或新建日志协议。

没有独立的 Supervisor 日志 spec；本次以用户确认的历史行为、实际 `WorkerProcess.spawn/wait/stop` 及现有进程/IPC 测试为依据。中英文生命周期文档同步说明输出位置和诊断方式。

## 验证与边界

2026-11-17，在 Windows / Python 3.11.15 上验证当前工作区；0.2.9 基线为 `745377ed18490cabfd7008313d598f6e0331ebc5`，使用该分支锁定的 Core `45d53dd19`、Gateway `1119ea00f` 和 openjiuwen-protocol `267c077f` 源码。

- 当前 `WorkerProcess` 类全文与 !9883 父提交 `d5e3505dc71a954a0c365e9b90f721bf4694911e` 逐字相同；两分支恢复后的类 SHA-256 均为 `aedc885bcc414222058de906ccf8daeb1989624344f7c845db41554e496546a7`。
- 两分支合计执行 24 个真实子进程，组成 8 组历史版、修复版、修复前版对照，覆盖 resident/front_loopback 和正常/异常退出。修复版与历史版的继承 stdout、stderr 全文、退出码及无逐行转发行为完全相同；8 个修复前负向对照均不同。该对照只替换身份提供函数和子进程命令。
- 运行 `test_ipc_backpressure.py`、`test_front_loopback_ipc.py`、`test_resident_worker_shutdown.py`、`test_worker_entrypoint.py`、`test_worker_shutdown_deadline.py` 和 `test_runtime_owner_identity.py`，选择 `-m "not slow"`：166 passed、6 skipped、19 deselected。覆盖真实异常退出堆栈、IPC 重连、Front 退出、Worker 回收及现有进程身份兼容测试。
- 构建 WHL 后无依赖安装到隔离目录，确认包内及安装后 `worker_process.py` 与候选源码逐字节一致。通过安装后的真实 Worker 模块和内置 stub Runtime，完成 resident/front_loopback 两次 IPC 就绪、stdout 继承、stderr 保存及关闭；退出码均为 0，PID 均消失，无逐行重标。

初轮补充回归的本地失败来自测试覆盖 `PYTHONPATH` 后子进程缺少协议依赖，以及独立环境重复路径使父子依赖指纹不同。使用锁定依赖的独立环境并去重测试启动器路径后，上述组合回归通过；未修改产品的身份校验逻辑。Windows 日志句柄由仓库外测试启动器关闭，未改动通用测试基础设施。

详细日志、JUnit、类全文比较和子进程对照保存在任务证据目录 `artifacts/issue6596-restore-20261117/`，不提交运行产物。安装包验证使用内置 stub Runtime，没有执行完整模型业务；未执行 slow 用例、Linux/macOS 或鸿蒙真机验收。候选代码、锁定依赖或启动模式改变时，需重新验证受影响范围。
