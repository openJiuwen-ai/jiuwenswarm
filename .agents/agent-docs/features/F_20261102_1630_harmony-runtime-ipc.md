---
id: F_20261102_1630_harmony-runtime-ipc
title: 鸿蒙 Runtime IPC 平台适配与双分支交付
type: F
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/ipc
  - jiuwenswarm/server/runtime/mcp
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
files:
  - jiuwenswarm/common/runtime_platform.py
  - jiuwenswarm/server/ipc/session.py
  - jiuwenswarm/server/ipc/loopback.py
  - jiuwenswarm/server/ipc/server.py
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/runtime/mcp/cli_driver.py
  - jiuwenswarm/server/supervisor/identity.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/worker_process.py
  - jiuwenswarm/server/worker/main.py
  - jiuwenswarm/server/worker/service.py
  - jiuwenswarm/server/worker/stub.py
  - jiuwenswarm/server/worker/gateway_bridge.py
  - tests/unit_tests/agentserver/test_front_loopback_ipc.py
  - tests/unit_tests/agentserver/fixtures/loopback_front.py
designs:
  - D-20261102-1502-harmony-runtime-ipc
design_note: 用户确认方案并要求分别基于主仓 develop 和 0.2.8 适配；Windows 保留原实现，仅鸿蒙默认采用新路径。
---

# 鸿蒙 Runtime IPC 平台适配与双分支交付

## 依据、范围与基线

设计：[D-20261102-1502-harmony-runtime-ipc](../designs/active/D-20261102-1502-harmony-runtime-ipc/design.md)。
当前契约：[S_01_runtime-ipc-transport](../specs/jiuwenswarm/server/ipc/S_01_runtime-ipc-transport.md)。
操作说明：[中文](../../../docs/zh/HarmonyOS_Runtime_IPC.md)、[English](../../../docs/en/HarmonyOS_Runtime_IPC.md)。

| 目标 | 独立拉取的主仓基线 | fork 源分支 |
| --- | --- | --- |
| develop | `44919104bfca0a81b573c78f063e6e0254684890` | `feature/harmony-runtime-ipc-develop-20261102` |
| 0.2.8 | `df3b123ffd887358749b7fe82f1d3992a41d5197` | `feature/harmony-runtime-ipc-028-20261102` |

两个实现分别从各自基线创建，未将 develop 整体合入 0.2.8。原工作区与鸿蒙专用测试分支保留。
develop 初始验证基线为 `08515d6e721edb9933b55ef389b07c6301f70f1f`；交付前重新 fetch 发现主仓新增四个提交，已快进至上述最新基线、同步其锁定依赖并重验。0.2.8 基线未变化。
测试分支曾按用户要求在 `c21143a90` 无条件默认 loopback；本次主仓 PR 按最新要求恢复平台选择，该测试分支的临时默认策略不进入主仓。

## 决策归属与实际实现

- 未设置、空值或纯空格的 `JIUWENSWARM_RUNTIME_IPC` 使用平台默认值。普通 Windows 继续 Named Pipe，普通 Linux/macOS 继续 Unix socket 和 resident Worker。
- 识别为鸿蒙后默认 `front_loopback`。识别沿用既有 MCP CLI 规则：`JIUWEN_HARMONY_RUNTIME=1`、`sys.platform == "ohos"`，或 PATH 中存在同时包含 `/data/app/` 与 `/hnp/` 的目录。判断提取到纯工具模块，避免 IPC 反向依赖 Runtime/MCP。
- 显式 `resident`、`front_loopback` 保留诊断覆盖；未知值报错，运行失败不自动降级。Windows 真实默认路径独立验证，不把跨平台显式 loopback 自测写成 Windows 默认变更。
- Front 在 Worker 启动前监听 IPv4 `127.0.0.1:0`。随机 256-bit token、独立 generation、代码身份和数据范围通过 stdin 引导；token 不进入 argv、环境变量或日志。
- 认证候选并发处理，认证帧最多 4096 bytes、单个候选最多 2 秒，受总建连截止时间约束。非法或慢候选不消费合法 Worker 的一次连接机会；首个合法连接关闭 listener。
- Worker 先 connect/auth，收到 transport 确认后才发送 Hello 并加载 Runtime。业务连接复用 `StreamConnection`，不使用 AF_UNIX、socketpair 或 pass_fds，不获取 resident endpoint 锁。
- attached Worker EOF 后 drain dispatcher、关闭 Runtime 并退出。重启先回收旧进程，再分配新端口、token 和 generation。启动失败、取消和 Hello 后通知失败均有对称清理；清理本身失败时发布 FAILED，避免启动等待悬挂。
- Windows/POSIX resident 分支不执行 listener/auth/attached shutdown；原有常驻、重连和最终退出语义保留。

## 不同版本的必要适配

develop 复用现有 stdin 部署身份验证、身份缓存、IPC v2 和 shutdown.done。既有业务消息、32 MB 业务帧上限、Gateway/Runtime 业务路径不变。

0.2.8 的原始基线没有 stdin 身份载荷、身份缓存或 shutdown 回执，IPC 为 v1。因此仅为 attached 路径增加私有启动身份、代码内容摘要和数据范围验证；resident 启动仍沿用原实现，不读取该载荷。
0.2.8 的 Hello 身份字段与 shutdown.done 仅由新 transport 的 Worker 使用；resident 流量与 `PROTOCOL_VERSION = 1` 保持。没有移植 v2 的请求 journal、重放或 resident deployment election。
attached 的严格 loader/shutdown drain 也仅在新 source 下启用；普通 Windows 的原有回收逻辑保留。

## 实际执行与证据

2026-11-02，Windows、CPython 3.11.15，两个 worktree 各用独立 `.venv` 和自己的锁定依赖。
环境使用 `uv sync --frozen --python <3.11 interpreter> --no-default-groups --group test`；未改依赖或锁文件。执行本地 WebSocket 用例时，测试进程设置 `NO_PROXY=127.0.0.1,localhost`，不修改全局代理。

| 范围 | 实际入口 | 结果与适用范围 |
| --- | --- | --- |
| 首轮 develop | 新 IPC、Worker entrypoint、原 phase2、MCP CLI 四个文件 | 161 passed、1 skipped |
| 最新 develop 基线重验 | 同上四文件，基线 `44919104b`，包含最终 75 项新 IPC 用例 | 164 passed、1 skipped |
| 首轮 0.2.8 | 同类四文件，按 v1 readiness 接口适配 | 151 passed、1 skipped |
| 最终新 IPC 验收 | 各分支 `python -m pytest tests/unit_tests/agentserver/test_front_loopback_ipc.py --no-cov -q -o log_cli=false` | develop 75 passed；0.2.8 75 passed，包含启动清理失败发布 FAILED 的补充用例 |
| develop 扩大回归 | `08515d6e7` 基线上的 `test_worker_*`、`test_resident_worker_*`、`test_supervisor_*`、`test_ipc_*`、`test_runtime_worker_*`、`test_code_identity_cache.py` | 首轮 297 passed、5 skipped、1 failed；slow-shell 的时间相位断言定向重跑通过，未修改的同一基线也通过。最新基线上重验四文件定向集，未重跑扩大集；保留首轮失败，不声称一次全绿 |
| 0.2.8 扩大回归 | 原有同类文件，共 94 项 | 82 passed、5 skipped、4 failed、3 fixture errors。四个失败在未修改 `df3b123ff` 上逐项复现；原入口 fixture 在 `asyncio.run` 后无当前 loop 的条件也复现相同错误，未改这些既有测试或产品功能 |
| wheel 安装验证 | 两版本各自 `uv build --wheel`，`uv pip install --target <isolated> --no-deps <wheel>`，从源码目录外启动生产 Supervisor/Worker stub | 最终源码重新构建、安装后均通过。普通 Windows 默认 PipeConnection；模拟鸿蒙默认 StreamConnection；四个实际进程均完成请求并以 0 退出，原生 PID 已消失 |

扩大回归中，0.2.8 的四个既有失败为 slow-shell 心跳时间断言、持续 stall 替换断言以及两个 50ms LoopBeat 计时断言；不是本次 transport 的正向验收证据。
3 个入口 fixture 错误源于原 `restore_process_state()` 直接读取不存在的当前 event loop；单独入口回归已通过。上述失败与错误均保留在 PR 描述。

实际日志、wheel、隔离安装目录和基线复现位于本地任务证据目录 `D:\work_space\.codex-temp\harmony-ipc-main-prs-20261102`，不提交日志、凭据或用户运行数据。
wheel 探针验证导入来自隔离安装目录而非 editable 源码；测试过程中核对原生 Worker PID，不能仅依靠 launcher return code 或 ACK。

## 覆盖、检视与限制

已覆盖平台选择、stdin 双端、token/generation/identity/scope 校验、并发非法候选、超时、取消、late Hello 失败、请求/推送/取消、关闭顺序、崩溃恢复、真实 Front 强退和 Windows resident 重连。
自检基于各自固定基线与候选差异；Windows 行为通过真实 Named Pipe 与进程验证，未以普通 Linux 的平台字符串模拟替代原生 Unix socket 验收。
复用原平台路径，未改变 Gateway、模型、Tool、Memory 或 session owner/fencing。未执行格式化或静态检查。

尚未验证 HarmonyOS 真机、Linux/macOS 原生运行、真实模型或完整服务；未构建/签名 HNP/HAP。鸿蒙打包仓仍须同步代码、HNP 与 pyc 覆盖层，避免旧代码覆盖新 transport。
源码、依赖、模式选择、打包内容或运行平台变化时，重验相应证据；远程 CI、Maintainer review 与用户真机验收分别记录，不能由本地结果替代。
