---
id: B_20261106_1015_macos-worker-executable
title: macOS Framework Python Worker 启动时被误判为无关进程
type: B
author: "“ZhangRan-Zora"
author_email: "“hidadamao@gmail.com”"
designs: []
design_note: 重启服务时复现现有进程身份校验的 macOS 兼容问题，恢复既有启动契约，无独立设计。
modules:
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
---

## 现象与根因

0.2.9（HEAD d1dfe0011 加当前工作区）在 macOS Framework Python 3.11 下启动：前端和 Gateway 监听正常，但 Worker bring-up 报 `Worker lifecycle refused an unrelated process`，无法执行任务。

`sys.executable` 与 `_base_executable` 解析为 Framework 的 `bin/python3.11` 启动器，而 psutil 从 OS 读取的实际进程为 `Resources/Python.app/Contents/MacOS/Python`。合法 Worker 因路径不一致被拒绝。既有 `test_request_shutdown_sends_shutdown_and_returns_pid` 在修复前复现相同错误（该文件 1 failed、2 passed）。这是独立的新兼容问题，不替代旧的 supervisor 入口修复记录。

## 修复与边界

仅在非 frozen 的 Darwin 环境、既有可执行文件匹配未命中时，追加当前 supervisor 自身的 OS 可执行文件作为允许项。仍检查用户、deployment/data scope、PID 与创建时间、Worker 模块和 socket 参数；未跳过归属校验。Linux/Windows 和 frozen 分支保持原行为。

该约束位于 `resident_lifecycle.worker_process`；调用方包括 Worker 启动及退休流程。未修改组织消息路由、任务卡片、专家团执行或模型参数。

## 验证证据

临时数据目录、当前锁定依赖、macOS Python 3.11 下运行：

```text
.venv/bin/python -m pytest tests/unit_tests/agentserver/test_resident_worker_shutdown.py tests/unit_tests/agentserver/test_worker_shutdown_deadline.py tests/unit_tests/agentserver/test_front_loopback_ipc.py --basetemp=<独立短临时路径> -q -o addopts='' -o log_cli=false -o asyncio_mode=auto
```

最终 130 passed（27.69s）；覆盖真实 Worker IPC 启停、拒绝无关进程、PID 复用及 Front 生命周期。日志 `/private/tmp/swarm-029-worker-regression-final.log`。初次扩展测试因 macOS 默认临时路径过长无法绑定 Unix socket；改用独立短路径验证。开发中发现已有身份匹配测试无需读取当前进程 exe，故收窄为仅旧匹配未命中时调用，最终全部通过。

实际后端启动已观察到 `runtime worker agent_ready`。此次没有发起真实业务任务验证；启动期间模型自动探测曾收到模型不支持关闭思考的 400，未修改该模型配置。Windows/Linux 未执行实机测试。

## 检视与后续

按根 AGENTS、jw-dev/jw-review 及现有 Worker 身份契约检查，差异仅增加四行 Darwin 兼容处理；无新外部接口和持久化格式变化。保留用户原有工作区修改，未提交代码。后续调整 Python 安装方式或进程校验规则时重跑上述回归。
