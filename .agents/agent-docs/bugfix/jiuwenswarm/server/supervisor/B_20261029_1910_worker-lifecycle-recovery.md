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

## 2026-11-03 develop 适配

本次从上游 `b8c8206c4ede7122f9ee3f81770b8f86dd5659ed` 开始适配，验证期间继续接入 `3926849e408020eed4b8d80c9eee7e01ff67a718`，续写同一修复，不替代原有最终退出、升级凭据和历史进程处置决策。

- 保留主干 HarmonyOS `front_loopback` 的一次性鉴权、generation 校验、进程退出监听、连接断开清理及 PID 复用保护。resident 的选举、历史端点迁移和替换校验留在 resident 连接路径，附属模式不进入这些流程。
- 两种模式共用完整关闭判定和有限重试后的自动恢复；附属模式 `stop()` 始终关闭自己的 Worker，桌面 resident 的普通 Front 重启继续保留 Worker。
- 保留主干 `JIUWENSWARM_RUNTIME_INSTANCE` / `HOSTNAME` 的端点区分，并将实例纳入部署身份，避免共享数据目录中的其他 Pod 被误认为当前部署的历史进程。
- Electron 装配同时包含主干新增的 `cdp_port.cjs` 和本修复的 `update_shutdown.cjs`；文档索引使用当前同步工具重建。
- 故障注入夹具通过正式 `-m jiuwenswarm.server.worker.main` 入口装配诊断 Runtime，继续验证日志、心跳、背压、真实阻塞和恢复；不为独立测试脚本放宽进程归属校验。
- 损坏的构建身份通过 `WorkerIdentityError` 标明永久错误，停止自动重试；修正后显式 reload 可恢复。两种 IPC 均覆盖该路径，临时启动失败仍使用自动冷却恢复。

回归新增两个共享目录真实实例的独立启动/退出、附属 Worker 的 ACK 后线程残留，以及 resident / front_loopback 两条链路的启动故障自动恢复。原生 HarmonyOS、macOS DMG 与 Windows EXE 安装/升级仍需对应平台验收；Windows 源码下的 loopback 行为不代替原生验证。

Windows 扩展回归初轮为 9948 passed、23 skipped、64 failed、6 errors。适配正式故障注入入口、补齐上游声明但冻结锁文件缺失的 minio / runtime-foundation 验证依赖，并限制测试进程的 OpenBLAS 线程后，逐项重验原失败/错误及新增主干路径断言：同条件未修改主干与候选均为 54 failed、6 passed，未增加失败；11 项真实进程故障用例已在生产入口复测通过。残留涉及 Windows 路径、符号链接权限、POSIX 文件描述符等已有行为，不能把这一扩展范围报告为全部通过。独立核心回归为 325 passed、1 skipped；故障撤除后默认冷却实测约 31.2 秒自动恢复，不需要 reload。当前 PR 描述记录后续提交的补充验证与成品验证。

## 2026-11-03 CodeCheck 与问题覆盖复核

按未解决项导出逐条修正四项：`identity.py` 与 `resident_lifecycle.py` 拆分超长行；`shutdown.py` 提取历史端点判断，保留原有归属条件；`_reap_validated_worker()` 的三个提前返回统一为 `False`，表示没有执行强制退出。没有改变终端退出策略。

在 `e4441b4562ce36cbc2b992a5f155e61eb26e5f04` 加上述修正的 Windows 候选上执行相关回归，结果为 282 passed、8 skipped、2 failed。两项 `test_process_supervision.py` 的 POSIX 信号模拟失败，在同条件未修改主干 `dee257a66cba61c9d7b61236b2e6804aee9ee222` 均复现：Windows SIGABRT 编号不同及缺少 SIGKILL。首轮另一项历史 Worker HELLO 在 5 秒等待处超时，独立复测和后续整组均通过；首次 Worker stderr 未保留，不能确定该次超时根因。本地未运行静态检查工具，导出项修正与云端 CodeCheck 通过是不同结论。

#5954 的独立真实进程对照使用原始协议 1 Worker、停止入口、隔离数据与 debug 状态目录，Agent Runtime 使用 stub：

- 回退前基线 `3e97637a2ebc56d9bbcaff483d9a5193e3c911d4` 首次 stop 返回 1 却丢失状态，第二次返回 0 且 Worker 仍存活，新启动报 `legacy Worker migration failed`。
- 当前候选两次拒绝自动处置无部署身份旧 Worker，均返回 1 且保留状态；新部署在独立端点达到 AGENT_READY。错误 PID 不获得清理权，指定实际历史 PID 后确认旧进程退出。普通 stop 随后关闭当前 Worker 并清除状态，再启动产生新 PID 且达到 AGENT_READY。所有探针进程结束后均确认退出。

这验证了 #5954 已定位的迁移阻断和停止误报链路；没有将“新部署能启动”解释为“自动清除了无法确认归属的旧 Python 进程”。

[#6063](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6063) 声明环境为 openEuler / 0.2.8release，描述 Ctrl+C 后残留导致不能重启。附件只有 `runtime worker closed` UI 提示，缺少残留进程身份、启动命令及 Worker stderr；原现场首次触发原因尚未复现。按用户确认，先验证本机可执行的进程与退出链路：实际 AgentServer `_run()` 和 Supervisor/Worker 配合替代网络 Front，注入 Worker FAILED readiness，再调用应用注册的 SIGINT 回调。develop 当前候选及 0.2.8 修复候选 `eee69a64dd4ad7483583743462fc488549320edb` 均出现 Worker 保留、下次启动复用同一 PID 并保持 FAILED。最后经显式关闭入口清理，确认没有探针 Worker 残留。

因此，本修复不能声明解决 #6063：AgentServer 收尾仍使用默认 detach；`start_services`、`app` 的终端收尾也未覆盖常驻 Worker 的最终退出。Windows 回调验证不等于原生 SIGINT，更不能代替 openEuler 验收。普通 Front 重启保持常驻仍是有效契约；终端最终退出需要另行区分，并覆盖多实例归属和重启行为后再验收。

本次推送后 develop 继续推进到 `4512240b4242da3ff329c8b17fddc993f79cc9be`，重新合入时仅五份生成索引冲突；使用当前同步工具重建并保留双方源文档。新增的 Worker 启动补丁、TTSE 插件和会话清理路径纳入复验，合并候选为 424 passed、8 skipped、2 failed；失败仍为上述两项 Windows 下的 POSIX 信号模拟。四个 CodeCheck 修改文件没有被此次主干合并改变。

## 遗留与重验条件

原生 DMG/EXE 的真实打包退出及升级，需要对应平台使用该 PR 当前提交构建后复验。#5964 原机器的物理内存/SDK 初始化触发原因仍需要同进程时间线与系统事件证据；本修复覆盖已复现的日志 sink 阻塞风险和不可自行恢复的状态机。
