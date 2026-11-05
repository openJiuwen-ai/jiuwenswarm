---
id: B_20261029_1910_worker-lifecycle-recovery
title: 修复常驻 Worker 退出、历史端点兼容和耗尽重试后的恢复
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
  - jiuwenswarm/server/ipc
  - jiuwenswarm/channels/desktop
  - tests/unit_tests/agentserver
files:
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/shutdown.py
  - jiuwenswarm/server/supervisor/identity.py
  - jiuwenswarm/server/worker/main.py
  - jiuwenswarm/server/worker/service.py
  - jiuwenswarm/server/ipc/stream.py
  - jiuwenswarm/server/agent_ws_server.py
  - tests/unit_tests/agentserver/test_worker_entrypoint.py
  - tests/unit_tests/agentserver/test_ipc_backpressure.py
  - tests/unit_tests/agentserver/test_worker_shutdown_deadline.py
  - tests/unit_tests/runtime/test_runtime_service.py
  - pyproject.toml
  - MANIFEST.in
  - jiuwenswarm/debug_launcher.py
  - jiuwenswarm/channels/desktop/electron/main.cjs
  - scripts/jiuwenswarm.spec
  - tests/unit_tests/agentserver/test_worker_legacy_compatibility.py
  - tests/unit_tests/scripts/test_electron_lifecycle_assembly.py
  - tests/unit_tests/test_electron_windows_packaging.py
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

旧云端 Linux UT 报告的四项历史 Worker 失败还暴露两类测试夹具缺陷：xdist 的长临时目录使 AF_UNIX 路径超过系统长度限制；真实退出后由测试持有的 Popen 子进程尚未回收，`pid_exists()` 仍能看到 zombie。历史端点用例现使用短、独立的临时 workspace，仍按实际旧端点规则生成路径；处置后先通过持有的进程句柄回收子进程，再检查 PID。没有把 zombie 当作存活 Runtime，也没有放宽进程归属或退出断言。该文件 9 项 Windows 回归通过，原生 Linux 结果仍需云端确认。

## 2026-11-04 Windows WHL 控制台中断清理

在固定 Swarm `a2abceccfba89ab25a518568916152865fa28acd`、core
`5a35c39f0163a4e6190800df53ad7e24848756eb`，模拟合入 PR !9259 并补齐打包资源的
`248874ee66a27c3bbf6c2b19f2a6465168b66170` 上，重新安装原 WHL 复现：
正常 `jiuwenswarm-start all` 就绪后 Ctrl+C，Worker 返回 `0xC000013A`，并留下
`KeyboardInterrupt` 和 SDK pending-task 销毁日志。启动期间中断还可出现 Windows
Overlapped I/O 未完成就销毁的错误。此退出码本身表示 Ctrl+C；缺陷依据是异步资源未完成清理。

Worker 入口原先忽略 Windows 不支持的 `loop.add_signal_handler()`，没有安装替代处理；
`KeyboardInterrupt` 穿过 `run_until_complete()` 后直接 `loop.close()`，跳过服务取消清理及
SDK 后台任务收尾。现在 Windows 使用 `signal.signal()` 将中断安全投递到事件循环，
只取消 Worker 任务一次，连续信号不打断正在进行的关闭。使用 `asyncio.Runner` 完成
后台任务、异步生成器和执行器收尾；信号处理覆盖日志收尾。未中断退出恢复原处理器；
Windows 已中断的进程在解释器退出期间继续忽略 SIGINT，避免第二次 Ctrl+C 在资源已清理、
进程尚未消失的窗口触发系统默认的 `0xC000013A` 终止。已有 `WorkerForceExit` 仍在
Runner 收尾前执行有诊断的强制退出，保留不可取消加载/调度的有界失败路径。

首次无字节码的隔离安装还观察到 SDK 冷导入超过旧的 10 秒加载收尾上限；Worker 会受控
退出为 1，未留下进程，但不能作为优雅关闭通过。默认加载收尾现使用总期限剩余预算，
保持整个服务关闭的 30 秒上限。测试实际等待超过 10 秒的加载完成并确认 Runtime 被关闭，
已有显式短期限与不可取消任务的失败路径继续回归。最终安装同时记录字节码编译条件。

命名管道的 `poll()` 本身也持有 Overlapped read；并发 `CloseHandle` 会与其完成收集竞争。
连接关闭先停止接收，并异步等待当前 native poll/read 退出后关闭原生句柄，不占用可能饱和的
默认执行器。并发关闭共享同一个清理任务，调用者取消后重试仍等待真正关闭完成。

源码真实进程检查中，就绪后中断的 Worker 退出码为 0；IPC 就绪、Runtime 尚未就绪时中断，
Worker 等待加载与清理后退出码为 0，未再出现上述清理异常。新增入口测试覆盖 Windows
信号回退、连续中断和 SDK 后台任务完成；管道测试覆盖在途 poll 与取消关闭后重试。
最终 WHL 仍须从本候选构建、隔离安装后，以真实控制台 Ctrl+C、PID/创建时间和进程句柄重验。

隔离安装复测进一步发现：外层 Runtime 加载完成不代表 `runtime-start` 已完成。原收尾先关闭
准备注册表，初始化任务随后调用 `mark_minimum_ready()`，产生 `preparation registry is closed`
和未读取任务异常。启动任务现先取消并等待结束，再关闭其依赖资源；即便任务已结束也读取
异常。定向 Runtime 服务、启动优先级与共享期限回归为 189 passed，覆盖冷启动关闭竞态、
已结束启动失败的消费及正常 stop/start。最终安装成品的进程验证仍单独记录。

真实就绪进程的连续信号复测保留了一次退出码 `0xC000013A` 的失败：第一次中断已关闭
Runtime，第二次到达解释器退出窗口，未出现异步清理异常。上述 Windows 退出信号处理
覆盖这一窗口，新增用例在 `main()` 返回后再送 SIGINT，并要求仍不抛异常。

最终 WHL 冻结构建为 `b9f5d8a58c07354e6d1786a57f059cb5147278e2`，上游仍为
`a2abceccfba89ab25a518568916152865fa28acd`、core 仍为上述 `5a35c39f`，不是原 PR 的
依赖升级。SHA256 为 `c2944c549b6aa424adf63ff344a4dbb33f6527287ff3e18f039eb589e02e9b63`。
独立安装环境的 AgentServer / 完整服务分别在启动中及就绪后接受真实 Ctrl+C，四项均确认
Worker 退出码为 0、无作用域内残留、无原有清理异常。另验证 Front 丢失后复用同一 Worker、
最终关闭应答成功且实际 PID 退出为 0，相关 TCP 监听消失。测试没有调用真实模型。

成品同时验证 2601 项 RECORD 哈希、2574 项源码与运行资源、255 个前端文件和 14 个入口。
测试生成的资源根 `.env` 与 `*.lock` 通过 manifest 和 package-data 双重排除，保留
`.env.template`；成品检查明确拒绝这类运行时文件。构建资源补齐和固定依赖等既有本地
装配项未因此进入原 PR，原 PR 只接入本轮中断修复、排除规则及对应测试和文档。

原 PR checkout 使用自身 Core `c8c621b22f1ad5da6acee0ceee33778007c06efb` 与验证环境中
foundation `5c5b09d468e2132a91aa091057bd8e74892ca363`，本轮八个文件定向回归为
331 passed、3 skipped，覆盖真实日志、背压、卡顿故障、入口、关闭期限、Runtime 归属及
启停、loopback 和附属 Worker 最终关闭。没有将这组结果扩展为全仓或云端 CodeCheck 通过。

普通 Front 断开与控制台直接中断 Worker 仍是两个场景；没有改动 resident 模式保留 Runtime 的
断开/重连契约，也没有把 Windows 结果当作 #6063 的 openEuler 原环境验收。

## 遗留与重验条件

原 PR 新增修复推送为 `7e01951bff54f0f82b4065cdb965c6e6b8d00ed5` 后，远端主干继续
推进至 `5111fbffbf8e83e5a7371689956de0e52d1e3209`，并已拆出 `agent_gateway` 独立依赖。
此次合入仅两个生成索引冲突，按当前同步工具重建，保留 Gateway 迁移、协议更新及 Team
显示身份修复。Worker 入口、服务收尾与 IPC 文件相对上述修复提交未变化，AgentServer
保留相同的启动任务收尾修复。固定 WHL 仍是前述 `b9f5d8a5`，未追随移动主干重打包。

合入后定向复验为 314 passed，Electron 为 99 passed、1 skipped；AgentServer 加载中和
就绪后的两次源码真实控制台中断均确认 Worker 退出 0、无残留和原有清理异常。没有重复
已经验证且合入未改变的长时间 IPC 卡顿故障计时用例，其 331 passed、3 skipped 结果仍
限定为合入前 `7e01951bf` 候选。

本轮还发现主干声明的 `agent-gateway @ develop` 未进入 `uv.lock`。正常 `uv sync --locked`
解析其当前提交 `a9e4d447033f0eadf5e8583c06cfb8993e01e539` 时，与根项目的 Core 引用
产生 `4dbaa84c` / `c8c621b` URL 冲突。独立未修改 `5111fbffb` checkout 执行相同安装
命令复现同一错误，PR 的依赖声明及 uv 配置与该基线一致。本次不改动这些依赖引用；合入
验证环境先消费主干冻结锁文件，再以 `--no-deps` 补装该 Gateway 明确提交。实际 Core 为
`c8c621b`、protocol 为 `cbbadde9`、foundation 为 `d4a8761c`。这是明示的验证补充条件，
不能把上述通过结果解释为原始 locked 安装已修复或原生平台、云端 CodeCheck 已验收。

原生 DMG/EXE 的真实打包退出及升级，需要对应平台使用该 PR 当前提交构建后复验。#5964 原机器的物理内存/SDK 初始化触发原因仍需要同进程时间线与系统事件证据；本修复覆盖已复现的日志 sink 阻塞风险和不可自行恢复的状态机。

## 2026-11-05 develop 提交整理与复验

本轮以 develop `260657025fa74f9ee8235c06678716f779bf1066` 合入既有 PR !9259，
通用资源与测试修正提交为 `a3bb602da`。验证期间主干推进到
`ccb7569f7096a6ec52b7f8a3c0d8f508a6e7a5f5`，仅修正英文 SUMMARY 的文档链接大小写，
已合入为 `8f1f4c81a`；没有改变已验证的生产逻辑或依赖。

与此前临时装配区分，本候选保留主干的依赖声明与锁文件：Core 为 `4dbaa84ca568af3f181da4f9cd26b713a5b73039`，
Gateway 为 `a9e4d447033f0eadf5e8583c06cfb8993e01e539`，foundation 为 `d4a8761c8a561b1ee1b8a4333def0077b308e709`。
Windows / Python 3.11.15 下使用缓存执行 `uv sync --locked --extra dev --extra cua` 成功，
解析 330 个包，安装环境的 306 个分发包通过依赖一致性核查。首次在线安装因 PyPI 网络超时
失败，离线重试未修改依赖声明或锁文件；实际导入位置为本候选源码及该候选独立环境。

此前本地装配中确认必要的资源补齐本次进入生产 PR：WHL/sdist 携带 browser network guard
和 code rules；冻结后端还明确携带 Process CLI schema、Designer skills、独立 Gateway 的
动态模块、YAML 与分发元数据。冻结资源排除根目录真实 `.env`、运行锁和各层 `__pycache__`，
保留 `.env.template`、嵌套技能目录及项目维护技能的既有排除规则。本地 Gateway 路径、临时
Gateway 版本和为验收包固定的新 Core 等依赖覆盖仍未进入 PR。

整套定向执行 766 项：753 passed、12 skipped、1 failed。唯一失败为升级助手旧夹具缺少
新的 `require_clean_shutdown` 依赖、关闭成功凭据及端口已释放返回值，和历史云端 UT 报告中
的同一消费方失败一致；该云端 HTML 的实际 GIT_COMMIT 为 `b0cb684c`，不能归给当前 HEAD。
夹具现使用真实凭据函数，分别检查 missing/pending/failed 时不启动安装器，以及成功时先
确认 Worker 关闭、等待端口释放再启动安装器。资源收集测试归并到已有的装配测试文件。
受影响用例复验 33 passed；期间没有修改生产代码。合并去重并移除被替换的旧夹具后，
现行验证用例集合为 757 passed、12 skipped；不是把一次失败运行改写成整套一次通过。
跳过项均为 Windows 下不适用的 Unix socket、POSIX 进程组或不同平台的心跳场景。
Electron 全部现有测试为 99 passed、1 skipped。本地没有运行格式化或静态检查。

另以真实 Runtime、真实控制台中断和操作系统进程句柄验证 3 项：加载中连续 Ctrl+C、
AGENT_READY 后连续 Ctrl+C、只终止 Front 后重连同一 PID/创建时间的 Worker 再显式最终关闭。
三项 Worker 退出码均为 0，作用域内无进程、监听或原有异步/I/O 清理异常；没有调用真实模型。
源码重连探针最初沿用 WHL 构建身份前置检查，在启动产品进程前即失败，随后改为严格核验
当前源码导入位置并重跑通过；没有为源码伪造安装包身份。

实际构建的资源核查用 WHL 与 sdist 均比对 29 个运行旁置文件；WHL 另通过 1,849 项 RECORD
哈希，两个分发物均保留模板并排除真实 `.env` 与运行锁。这组产物未构建前端，不作为完整
桌面交付包。真实 PyInstaller 收集 hook 在本候选锁定环境中得到 Gateway 509 个模块、
3 个 YAML、分发元数据及 Designer 26 个技能文件；这不替代当前候选的原生后端重编译验收。

此前交付的完整 Windows 成品为 `de90750a592049662881561e384fc23c1a8336ad` 装配，基于
develop `26acdaddae1fa0daf0f888f8012f0284a2548204` 和相同 PR `8383acc6a`，Core 为
`8ede5297c9e6dea59337b6ea9d2fc81c803a7bee`，配套 Gateway 为本地元数据版本。
其 EXE 3 项、已安装 WHL 5 项进程验收全部通过，Worker 均退出 0，无相关残留；EXE 原生
关闭窗口选择退出由用户点击后自动核验，另外两项由应用既有 IPC 触发正式退出。完整成品
的依赖覆盖和实际构建身份不同于本候选，只作为对应成品的独立证据。原生 macOS/DMG、
openEuler、真实模型和覆盖安装升级仍未验收，#6063 不随本 PR 关闭。当前 HEAD 的远端
CodeCheck/全仓 UT 以推送后的独立结果为准，不沿用历史云端结果。
