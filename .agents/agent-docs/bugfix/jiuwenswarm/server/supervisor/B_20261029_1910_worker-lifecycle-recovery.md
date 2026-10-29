---
id: B_20261029_1910_worker-lifecycle-recovery
title: 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
  - jiuwenswarm/channels/desktop
files:
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/shutdown.py
  - jiuwenswarm/server/supervisor/identity.py
  - jiuwenswarm/debug_launcher.py
  - jiuwenswarm/channels/desktop/electron/main.cjs
  - scripts/jiuwenswarm.spec
designs: []
design_note: 修复已确认的常驻与最终退出契约及回退前暴露的兼容回归，未建立独立方案文档。
---

# 常驻 Worker 生命周期与故障恢复修复记录

## 现象与证据

本记录候选为 `develop` 分支修复，起始上游基线 `ffc539d74ba6fe034c60b6eac5ae3ea1da04b7dc`。参考 [#5909](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5909)、[#5954](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5954)、[#5964](https://gitcode.com/openJiuwen/jiuwenswarm/issues/5964) 的实际描述、回退前代码和日志，并分别运行最小复现。

- #5909：常驻 Worker 可持有旧模块与 PYZ TOC。真实 PyInstaller reader 的旧 TOC 在归档替换后可复现 `incorrect header check`；这是机制验证，没有将其宣称为用户 DMG 现场完整复现。当前分支还可复现依赖变化后连接旧 Worker，以及收到关闭应答却仍有线程保留进程。
- #5954：回退前旧 Python Worker 无部署身份时迁移直接阻断新启动；stop 失败前清除 debug 状态，下一次 stop 无操作却返回成功。两个分支回退前均运行真实历史协议进程复现。原日志的加载耗时与 IPC 重连失败支持该链路，旧 Worker 完整 stderr 缺失，不能据此断言全部内存来源。
- #5964：Windows 日志记录主线程停留在 `logging.emit` 和 Rail 构建，随后三次替换未建立 IPC，长期 session.create 失败。没有 OOM 事件或替换进程 stderr 证据。真实故障注入表明重试耗尽后 FAILED 不再自行恢复；仅显式 reload 可恢复。该状态机在生命周期修复前已存在。

## 根因与修复决策

当前契约见 `.agents/agent-docs/specs/jiuwenswarm/server/supervisor/S_01_worker-lifecycle.md`。

1. 保留 Front 重启时的 Worker 常驻；最终应用退出、升级或明确 stop 获得已验证重连进程的清理权。关闭验证共用期限，要求 ACK 与 PID 退出；强杀、负应答和应答缺失不伪装为持久化成功。
2. 默认端点按部署和数据作用域隔离。未知历史 Python Worker 只隔离并提供 PID 诊断，不因它阻断新端点启动，也不仅凭共享解释器自动强杀；操作者可以 `--legacy-worker-pid` 明确选择，仍经 OS 身份验证。这替代了回退前“无法证明归属就启动失败”的处理。
3. 代码身份观察实际安装依赖的 metadata、RECORD 和 editable 源码；WHL 与原生后端每次构建分配独立 backend_build_id。WHL 构建时写入身份，运行时读取成品身份并观察实际依赖，避免冷启动递归扫描整个包目录；源码 checkout 继续内容校验。不能只靠版本字符串、锁文件或解释器文件大小判断代码相同。
4. Debug 状态在完整关闭成功后才清除。桌面升级附带本次退出凭据，清理失败时禁止替换文件。Electron 的异步清理错误被消费并进入失败结果。
5. 启动/IPC 失败耗尽有限重试后发布 FAILED，按 30→60→120→240→300 秒冷却自动重试。显式关闭取消恢复；永久配置和身份错误保留人工修正入口，不重放失败请求。
6. develop 已有后台日志能力，本次保留并回归验证。

没有恢复回退前的构建根分离等无关打包调整，也没有改变 macOS 关闭窗口/正式 Quit 的既有交互。

## 验证与限制

实际环境为 Windows、各自锁定依赖的独立 Python 3.11 虚拟环境、隔离数据目录；测试没有接入真实模型。

- 定向 Python 回归覆盖 Worker 进程、IPC、重连关闭、旧协议、依赖变化、关闭失败状态保留、PID 复用、共享关闭期限、自动恢复取消、日志 sink 阻塞和桌面契约。
- 新增真实子进程注入：两次启动在 IPC 前退出 73；移除故障标记后无需 reload 自动恢复 AGENT_READY；收到 shutdown.done 后保留非 daemon 线程必须被识别为强制退出且返回失败。
- Electron 执行 `tests/electron/*.test.cjs`，覆盖退出凭据、安装助手与异步 Front 清理结果。
- 真实依赖验证：Worker 实际 import HTTPX 0.27.2，Front 切换到 0.28.1 后旧 PID 退出，新 Worker 实际 import 0.28.1。
- 各分支构建候选 WHL 并安装到独立 target，从安装目录验证 Worker 复用、重连最终退出和停止命令参数。候选沿用仓库默认构建版本 0.2.5b1，用于分支代码验证，不是正式 0.2.8 发布物。
- 本地未执行 macOS DMG、Windows 完整 EXE 安装/升级，也未人为耗尽物理内存；原生平台打包需重新验收。未把原始 #5964 的高内存第一触发点宣称为已证实或已消除。

具体验证结果与 PR 当前提交一并记录在 PR 描述；本记录不复制本机用户日志或私有模型配置。

## 防错模式与资料同步

身份、存活与完整关闭是不同证据：进程常驻属于正常设计；最后一次应用退出必须覆盖可验证归属的 Worker。未知旧进程的兼容策略应保留新部署可用性，并提供明确、可检查的清理入口。规约、中文及英文运维说明和回归同步维护。

## 遗留与重验条件

原生 DMG/EXE 的真实打包退出及升级，需要对应平台使用该 PR 当前提交构建后复验。#5964 原机器的物理内存/SDK 初始化触发原因仍需要同进程时间线与系统事件证据；本修复覆盖已复现的日志 sink 阻塞风险和不可自行恢复的状态机。
