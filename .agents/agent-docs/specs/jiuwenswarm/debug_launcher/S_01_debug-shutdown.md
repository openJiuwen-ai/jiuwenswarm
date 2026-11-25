---
id: S_01_debug-shutdown
title: Debug 服务最终停止的预算与旧 Worker 选择
type: S
author: alan_cheng
author_email: chengshuo5@huawei.com
modules:
  - jiuwenswarm/debug_launcher.py
files:
  - jiuwenswarm/debug_launcher.py
  - Makefile
---

# Debug 服务最终停止规约

最近修订：2026-11-20。

## 范围与责任

`jiuwenswarm-stop` 管理源码 checkout 的 debug 状态，停止 Front 进程树后调用既有 resident Worker 生命周期入口。停止、升级及身份验证的通用行为见[运行说明](../../../../../docs/zh/runtime-worker-lifecycle.md)。本规约不修改桌面、命名实例、普通 Front 重启或 Worker 内部清理协议。

## 预算与参数

- 未指定预算时，Front 使用 15 秒，Worker 使用 35 秒。Worker 的默认预算覆盖内部 30 秒清理期限及 IPC、进程退出确认的余量；不在 Front 导入阶段加载 Worker 或 Runtime 以取得该值。
- `--timeout N` 为每个阶段各指定 N 秒，保持旧调用的显式预算语义。`--worker-timeout N` 优先覆盖 Worker 阶段。两个阶段串行，不承诺全命令仅耗时 N 秒。
- `stop_debug_service(timeout=None, *, worker_timeout=None, legacy_worker_pid=None)` 遵循相同规则。CLI 拒绝零、负数、NaN 和无穷值，且拒绝非正的 legacy PID。
- `make stop`、`make restart-debug`、`make restart-debug-fast` 将 `STOP_ARGS` 传给停止命令；停止非零退出时不执行后续启动。

## 身份与失败语义

- 缺少 deployment identity 的旧 Python Worker 仍需操作者核实安装来源后显式指定 `--legacy-worker-pid`。参数传递不代替既有用户、解释器、模块、socket、进程创建时间及部署验证。
- 关闭成功须通过既有协议确认清理成功及实际进程退出；强杀、负应答、缺失应答或无法确认归属均不能改写为成功。
- 失败保留 debug 状态供重试，成功删除状态。没有状态且未显式选旧 PID 时维持无操作；显式旧 PID 在没有 debug 状态时仍进入定向关闭。

## 验证入口

- `tests/unit_tests/test_debug_launcher.py`：默认、显式及独立预算，CLI 参数传递与错误输入。
- `tests/unit_tests/test_debug_makefile.py`：实际执行 Makefile recipe，验证参数透传和停止失败中断重启。
- `tests/unit_tests/agentserver/test_debug_worker_shutdown_budget.py`：真实 CLI、Worker、IPC 和进程退出，业务 Runtime 使用可控耗时保存替身。
- `tests/unit_tests/agentserver/test_worker_legacy_compatibility.py`：旧 Worker 与不同实例的身份隔离。

故障证据及取舍见 [B_20261120_1958_debug-worker-shutdown-budget](../../../bugfix/jiuwenswarm/B_20261120_1958_debug-worker-shutdown-budget.md)。
