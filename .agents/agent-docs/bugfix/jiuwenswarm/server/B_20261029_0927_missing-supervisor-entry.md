---
id: B_20261029_0927_missing-supervisor-entry
title: 重启 AgentServer 时缺少 supervisor 启动函数
type: B
author: xinyang_bali
author_email: 835397263@qq.com
designs: []
design_note: 用户请求重启时发现当前工作区入口不完整，恢复既有 Front/Worker 启动契约。
modules:
  - jiuwenswarm/server
files:
  - jiuwenswarm/server/app_agentserver.py
---

# 现象与原因

2026-10-29 重启 default 实例时，Front 监听后因 `_start_supervisor` 未定义退出。当前工作区保留了 `_run()` 对该函数的调用，但只存在旧 `_start_runtime_backend()`，其残留实现不能代替 Front/Worker supervisor 入口。

# 修复

补回 `_start_supervisor()`：延迟导入 RuntimeSupervisor，传入 Front readiness/forwarder，绑定 runtime backend，等待 Worker IPC 就绪后返回 supervisor。保留用户已暂存的其他修改，不切换到在 Front 内初始化完整 Runtime 的旧路径。

# 验证

现有 `.venv`、PYTHONUTF8=1、PYTHONPATH=本仓;../agent-core 下运行 `python -m pytest -o addopts='' -o log_cli=false -q --tb=short tests/unit_tests/test_app_agentserver.py`：1 passed。

实际 default 实例重启后，5173/18092/19000/19001 均监听；Worker PID 46988 报告 agent_ready；Web 首页返回 HTTP 200。本次不验证具体聊天模型或 TTSE 插件升级。
