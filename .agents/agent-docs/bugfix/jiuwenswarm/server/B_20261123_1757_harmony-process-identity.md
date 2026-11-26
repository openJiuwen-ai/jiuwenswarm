---
id: B_20261123_1757_harmony-process-identity
title: HarmonyOS 拒绝读取全局 proc stat 时 Worker HELLO 失败
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/worker
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/instance_manager
  - jiuwenswarm/server/jiuwenmate
files:
  - jiuwenswarm/common/process_identity.py
  - jiuwenswarm/common/process_supervision.py
  - jiuwenswarm/server/worker/service.py
  - jiuwenswarm/server/worker/stub.py
  - jiuwenswarm/server/worker/main.py
  - jiuwenswarm/server/worker/diagnostics.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/worker_process.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/shutdown.py
  - jiuwenswarm/server/runtime/session/lifecycle.py
  - jiuwenswarm/debug_launcher.py
  - jiuwenswarm/server/jiuwenmate/dingtalk_cli_local_provider.py
  - pyproject.toml
  - uv.lock
  - jiuwenswarm/agents/swarm/group_route.py
  - jiuwenswarm/start_services.py
  - jiuwenswarm/instance_manager/status.py
  - jiuwenswarm/extensions/app_market/resources.py
  - tests/unit_tests/agentserver/test_worker_process_identity.py
  - tests/unit_tests/agentserver/test_worker_failure_diagnostics.py
  - tests/unit_tests/agentserver/test_worker_entrypoint.py
  - tests/unit_tests/agentserver/test_ipc_backpressure.py
designs: []
design_note: 按用户明确修复任务和已有受限系统回退补齐调用方，无独立设计。
---

# HarmonyOS 拒绝读取全局 proc stat 时 Worker HELLO 失败

## 交付范围与来源

本记录的 `files` 是故障涉及的实现和回归入口，不是待实施清单。首个 release 候选 `8ed9f7f00` 相对基线 `f554e7717`，
[PR !10933](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10933) 的首个提交
`8ed9f7f00f43262a4e654706369388b0a6ce6a26` 共修改 30 个文件：15 个生产文件、6 个测试文件和 9 个文档/索引。
Worker、Supervisor、`start_services.py` 及本记录列出的四个测试文件均包含在该提交的净差异中。
完整来源为 develop [PR !10796](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10796) 的 `488612236`，
两项 UT 的首次提交和 release 移植落点分别见各自故障记录。下文历史证据不自动等同于后续候选验证。

## 现象与影响

用户提供的 `worker-startup-proc-stat-permission.md` 记录 HarmonyOS 6.1.1 的 Worker 在 loopback 连接后、HELLO 发出前，
因 psutil 读取 `/proc/stat` 抛出 `AccessDenied`，前台停留在模型服务恢复状态。
补充的 `worker-process-identity-changes.md` 描述用户本地已调通的双字段方案；该文档中的验证结果属于其本地版本。

比较基线为 develop `fe3da09837caf79c48b17cdb673f925547646e44`。
此前 `3e8d9e6725` 的 runtime-owner 回退仍在；Worker 与 Supervisor 直接调用不经过该入口，因此覆盖不完整。
本次延续原回退决策，扩展到启动、重连和退出身份检查。

## 根因与修复决策

公共工具放在 `common`，供 Worker、Supervisor、启动器和群 owner 复用，避免底层组件依赖 session lifecycle。
复用原 `/proc/<pid>/stat` 字段解析；时间戳与 ticks 分开传输，校验和终止前使用同类身份。
HELLO、stub、常驻/attached 校验、退出回收和 pending Worker 观察成套更新。
群 owner、实例启动器、服务树清理和应用资源采样中的同类直接读取也使用公共回退。

未采用只删除 `process_created`、只比较 PID 或用当前时间冒充创建时间：这些做法削弱 PID 重用保护。
参考基线是独立 0.2.9 构建，不复制其依赖。用户随后明确要求补入 `worker-log-path-routing.md` 中的日志诊断链路，
本次按 develop 现有的后台日志与原始输出路由补齐实现。
旧 HELLO 按主干既有兼容行为处理；新 Worker 两种身份都不可读时明确失败，不能发送无身份 HELLO。

初版由 `JIUWEN_WORKER_LOG_DIR` 指定 Worker 日志目录，未设置或为空时使用 socket 父目录下的 `worker-logs`；
后续主干路径迁移后的默认位置见下文“同步 develop 路径契约”。
每次启动的 stderr 使用 UUID 文件名；Supervisor 清理固定诊断文件并注入 `JIUWEN_WORKER_DIAGNOSTIC_PATH`。
Worker 在日志初始化之前的顶层异常、强制退出、ERROR 日志及 HELLO 失败均保留 PID 和 traceback；
ERROR handler 在现有后台日志启用前装配，继续由后台写入。诊断写入失败不替换原异常。
HELLO 失败通过共享诊断工具在关闭 IPC 前落盘，避免 EOF 触发父进程回收时后台队列尚未写入。
Supervisor 对自己启动失败的 Worker 在清理时读取诊断文件末尾最多 16 KiB，报告路径或不可读原因，不转发原始 stdout/stderr。

CodeCheck 的重复父子异常捕获改为仅捕获 `ValueError`（已包含 `UnicodeError`）；HELLO 创建时间校验拆分类型检查，
pending Worker 检查用提前跳过拆分条件并消除超长行，保持原有校验与保守存活语义。
后续 G.ERR.11 告警来自启动逻辑提取后，原 `main()` 内的 `SystemExit` 留在普通函数 `_run_worker()` 中。
内部运行函数改为返回退出码，由入口 `main()` 统一退出；stub 失败仍先写诊断并清理后台日志，再以状态 1 退出。

## 当前契约与防错约束

见 [S_01_process-identity](../../../specs/jiuwenswarm/common/S_01_process-identity.md)。
后续 `create_time()` 调用的异常处理、公共工具复用、身份比较和行为回归要求，统一维护在上述规约的“后续开发与检视约束”章节。

## 回归证据与适用范围

Windows、Python 3.11.15、psutil 7.2.2；core 为锁定的 `0b1aa2102104a2eeadd960d92e332fb9eeb861b1`，
gateway 为 `dda0e957bd5ae501a326d3ea61a4d336c91ebfc7`，从本地只读 editable 环境加载。
本任务使用独立 `.venv` 和临时数据；补齐该环境缺少的 pywin32 DLL，不修改源依赖声明。

身份专项为 **50 passed**，包含权限拒绝、双字段校验、PID 重用、非法创建时间、未知 booting Worker 和实例 PID 文件。
最终 18 个相关测试文件的合并回归为 **611 passed、8 skipped、31 deselected**，覆盖日志、入口、导入隔离、
原 owner、两种 IPC、常驻退出、重连、inflight stream 恢复、实例管理、服务树监督、应用资源及群 owner 消费方。
主批次排除 8 个已在未修改 develop 基线复现的 Windows 平台失败和 23 个标记 slow 的用例。
其中日志 sink 阻塞用例单独执行为 **1 passed**；其余未执行项不计为通过。
一次前置回归曾在认证前等待启动 PID 时超过 10 秒；候选与原基线的单项复跑通过，随后完整回归通过。
首次环境缺少 pywintypes 的问题在独立环境补齐，不改变项目依赖声明。
完整命令和 JUnit 位于本地任务目录的 `logging-final-command.txt`、`logging-final.xml` 和 `logging-final-blocked.xml`；
未执行全仓测试，远端 CI 状态以 PR 对应提交的报告为准。

真实 loopback 子进程的受控实验：两版均注入 `create_time()` 权限拒绝；Windows 的 start counter 是用真实进程创建时间生成的模拟计数。
原 develop 在 IPC 已连接后报 `ipc connection closed`；候选可以完成 HELLO、AGENT_READY 和实际 stub 请求响应。
最终候选的 loopback 和 resident 均启动成功并响应 stub 请求，记录了 venv launcher 与实际 Worker PID，关闭后两者均退出。
loopback 监听端口与 resident IPC 端点也已关闭，没有遗留锁。两种身份都拒绝时启动失败，Worker 私有日志包含 HELLO 阶段、PID 和 traceback，进程及端口仍完成清理。
日志链路的生产启动入口重测覆盖 loopback、resident 与双来源均拒绝三种情况；前两者完成真实 stub 请求，
后者在诊断文件中保留 AccessDenied、HELLO、PID 和 traceback，Supervisor 读取并输出该诊断。三组均独立确认进程、端口、端点与锁完成清理。
真实日志 sink 阻塞的已有回归通过，Worker 请求与心跳继续响应，最终退出没有等待该 sink 恢复。
上述合并回归对应 `30f24e4aa`；G.ERR.11 收尾调整的定向验证为 **24 passed**，覆盖入口、诊断、真实 stub 请求触发退出
和 Front 导入隔离。新增回归确认诊断已写入、后台日志已清理且最终退出码保持 1；该轮未重跑完整合并回归。
原始记录保存在本地任务目录
`D:/work_space/.codex-temp/harmony-process-identity-20261123/`，不提交运行产物。

## 2026-11-24 Windows 与 HarmonyOS 规约复核

此次运行代码为 `62b5127e6`；工作区只调整认证 UT 的模拟响应顺序，生产代码没有再次改动。
新发现的基线认证测试竞态另记于
[登录回调 UT 竞态](../../tests/unit_tests/common/B_20261124_1812_auth-callback-ut-race.md)。

19 文件 Windows 相关回归为 **629 passed、8 skipped、31 deselected**。
8 个平台失败在候选和未修改 CI develop `3f2ae160b` 上分别重现，包括 POSIX 进程组 API 和端口复用语义；
另 23 个 slow 用例未在本批执行，均未计为通过。原始报告为本地任务目录的 `windows-final.xml`。

原生 Windows 实进程以原 develop `fe3da0983` 与候选对照，保留实际 `-m jiuwenswarm.server.worker.main` 启动，
两侧使用各自 cwd 和源码，未注入创建时间失败。默认 resident 均保持浮点创建时间，启动并响应请求后 Front 断开，
新 Front 复用同一 Worker 再次响应；最终启动器和实际 Worker 均退出且状态为 0，IPC 端点关闭，无残留 owner/startup 锁文件。
候选显式 front_loopback 也完成请求并关闭监听、退出进程。该范围未发现新增 Windows 功能回归。

逐项核对 [S_01_process-identity](../../../specs/jiuwenswarm/common/S_01_process-identity.md)：

| 规约 | 本次核验 |
| --- | --- |
| S-PID-01 | `AccessDenied` 和 `PermissionError` 使用带类型 ticks 回退；权限未知不被当成进程已退出。 |
| S-PID-02 | HELLO 优先匹配双方可读 ticks，保留已验证身份；重连、服务树与终止前重读，PID 重用拒绝操作。 |
| S-PID-03 | 时间戳与 ticks 分字段，实例展示时间与身份分开；Windows 保留数值时间戳与历史记录读取。 |
| S-PID-04 | 两个来源均不可用时启动明确失败，诊断保留 PID、HELLO 阶段和 traceback；Supervisor 读取诊断。 |
| S-PID-05 | 无法验证的 owner 保留未知状态；未验证的 Worker 不被当作已退出，也不凭 PID 强制结束。 |
| S-PID-06 | 使用最后一个右括号定位进程名结束，再读取第 22 字段；空、短、非法 UTF-8 与非正计数均保守失败。 |

对应身份专项 50 项在此次 Windows 回归和用户提供的 Linux CI 中均通过。
另重跑真实子进程的 resident、front_loopback 回退及双来源均拒绝三种实验；前两者完成请求，
后者受控失败并由 Supervisor 输出诊断，三组均完成进程和 IPC 清理。
该实验在 Windows 注入权限拒绝，并使用由真实创建时间生成的模拟 start counter，不是 HarmonyOS 原生内核验证。
Linux CI 对相关 Worker/身份/生命周期调用方的 363 项记录没有失败；它对应旧提交 `62b5127e6`。

本次原始记录和独立副作用验证位于
`D:/work_space/.codex-temp/harmony-process-identity-ut-20261124/`，包含
`native-windows-verified.json`、`harmony-controlled/verified.json` 和 `ci-summary.json`。

## 2026-11-25 诊断重复写入评审修复

评审候选为 `725c2b004`。CR-10796-01 指出普通 Worker 和 stub 的异常先在 `_run_worker()` 记录，
再传播到 `main()` 记录，导致同一异常、相同阶段写入两份 traceback，挤占 Supervisor 读取的最后 16 KiB。

共享诊断工具改为在异常对象上保存已成功写入的目标路径和阶段；同一异常向同一目标、相同阶段只记录一次。
目标使用绝对路径和平台大小写规范化；不同异常、不同目标或不同阶段仍各自保留记录，写入失败后可以重试。
不使用全局对象 ID 缓存，避免异常释放后 ID 重用或缓存增长。异常元数据不可用时仍尽力落盘。
保留内层的即时记录点：后台日志停止失败时会调用 `os._exit()`，无法依赖异常继续传播到 `main()`；
仅删除内层记录会丢失这一退出路径上的原始故障。

CR-10796-02 和 CR-10796-04 的说明已同步中英文用户文档：任何共享日志目录的 Worker 都会共享固定诊断文件，
并发写入或启动前清理可能相互影响，需要隔离时应使用独立目录；相对目录按 Supervisor 当前工作目录解析，建议使用绝对路径。
CR-10796-03 的一次性 HELLO 读取优化本轮未实施。首次身份可能为时间戳，而双方可比较的身份可能为 ticks，
不能直接用首次结果代替对应来源的校验；保留公共比较入口及已验证的权限不对称、PID 重用语义。

新增 9 项行为用例在修复前为 **4 failed、5 passed**，确认普通 Worker、stub 和共享写入工具中的重复记录，
同时覆盖硬退出前诊断、不同异常/阶段/目标及写入重试。修复后的 Windows 定向回归为 **85 passed**：
入口、失败诊断和身份测试共 83 项；真实异常退出 traceback 与 Front/Supervisor 导入隔离共 2 项。
原异常传播、stub 状态 1、HELLO 关闭前落盘、16 KiB 上限和进程身份规约回归均通过。
本轮未执行全仓 UT、远端 CodeCheck 或 HarmonyOS 真机验证；此前验证继续限定于各自版本。
原始报告保存在 `D:/work_space/.codex-temp/harmony-process-identity-review-20261125/` 的
`dedup-before.xml`、`review-regression.xml` 和 `process-regression.xml`，不提交运行产物。

## 2026-11-25 同步 develop 路径契约

将 develop `fba1df034` 合入候选 `9d87a7c6d`，唯一冲突位于 `server/supervisor/worker_process.py` 的日志目录初始化。
develop 将默认 Worker 日志迁到数据根的 `logs/worker-logs`，统一经 `common.paths.get_worker_logs_dir()` 获取；
本 PR 的显式 `JIUWEN_WORKER_LOG_DIR` 和固定诊断文件继续保留，两种日志始终使用同一目录。
未设置或空配置走主干默认入口，非空配置仍优先；路径属于系统态日志，未迁入 `profile/`。
同步中英文生命周期说明，路径用例隔离数据根，避免收集阶段已经初始化的路径缓存干扰断言。

合并工作区在 Windows / Python 3.11.15 上验证 **135 passed**，包括默认/空值/显式日志目录、
诊断去重、HELLO、身份规约、Worker 入口、旧 owner 兼容、后台日志、Front 导入隔离、真实异常退出和 Web 文件响应。
此前附件 CI 的两个 CSP 失败在 develop `fba1df034` 中已修复，本轮对应原用例均通过，未额外修改 Web 实现或测试。

原生 Windows 实进程覆盖默认 resident（未设置日志目录）和 front_loopback（空日志目录）两种情况，
确认实际日志使用数据根 `logs/worker-logs`；resident 的 Front 断开保留 Worker，新 Front 复用同一 PID 并完成请求。
两组最终进程正常退出，IPC、端口及 owner/startup 锁完成清理。
另重跑 resident/loopback 的创建时间拒绝回退及双来源拒绝三组受控实验，启动/拒绝语义、诊断和清理均通过。
受控实验仍使用 Windows 注入权限错误与模拟 start counter，不代表鸿蒙真机验收。

本轮未执行全仓 UT 或本地静态检查。原始报告和独立进程记录保存在
`D:/work_space/.codex-temp/harmony-process-identity-conflicts-20261125/` 的
`merge-verified.xml`、`native-verified.json` 和 `harmony-controlled/verified.json`。

## 2026-11-25 适配 releases/0.2.10

以远端 `releases/0.2.10` 的 `f554e7717` 为基线，移植 develop PR !10796 在 `488612236` 相对
develop `fba1df034` 的完整修复净差异。目标分支已具备统一路径布局，日志继续使用
`common.paths.get_worker_logs_dir()` 默认目录及显式环境变量覆盖；15 个生产文件的实现与该来源一致。
保留目标分支既有打包和 WebUI 修复，未修改依赖锁文件、构建配置或包级 `AGENTS.md`。

本分支 Windows / Python 3.11.15 专项为 **153 passed**，覆盖进程身份、HELLO、入口、诊断、旧 owner、
后台日志、Web 文件响应、认证回调的两种调度顺序、socket 写锁诊断、导入隔离与真实异常退出。
这些是当前 0.2.10 候选上的重新执行结果，未将 develop 的历史通过数直接归给该分支。

0.2.10 原生 Windows 基线/候选各验证 resident 与 front_loopback，共 **4 组通过**。
默认与空日志目录均落到数据根 `logs/worker-logs`；创建身份保持浮点时间戳，resident 的新 Front 复用同一 Worker 完成请求。
四组均独立确认实际 Worker 和启动器退出、监听关闭、owner/startup 锁释放。
候选另有 **3 组**创建时间拒绝/模拟 ticks 回退及双来源拒绝实验通过，包含原始诊断与回收检查。
它们仍是 Windows 上的受控实验，未构建 HAP、Windows 安装包或完成鸿蒙真机验收；全仓 Linux UT 和 CodeCheck 以该 PR 的 CI 为准。

本轮报告、执行脚本与实进程证据位于 `D:/work_space/.codex-temp/harmony-process-identity-0210-20261125/`：
`related.xml`、`native-verified.json`、`harmony-controlled/verified.json` 和 `production-comparison.json`。

## 2026-11-25 release PR 评审收尾

本轮基线为 `8ed9f7f00`。行内意见 203761483、203761485 与评论区汇总重复质疑测试和 Worker 改动未交付。
以该 head 的 GitCode 全量文件接口和本地 `f554e7717..8ed9f7f00` 净差异核对，均为 30 个文件：
15 个生产文件、6 个测试文件和 9 个文档/索引文件；所质疑的 9 个 Worker/Supervisor/启动器实现与测试均在其中。
这是评审使用了不完整清单造成的误判。补充本记录的范围说明及两项 UT 的原始提交、release 移植提交和 PR，保留实际交付的文件清单。

对意见 203761484，采纳显式存在性检查：已记录的 ticks 无法读取时，通过 `psutil.pid_exists()` 区分
已退出与存活但不可读，分别抛 `NoSuchProcess` 和 `AccessDenied`。存在性不代替身份匹配，未验证的目标仍拒绝发信号。
同时补齐服务树在进程查询后、身份检查期间退出的竞态，`NoSuchProcess` 按已无进程返回，权限未知仍报告失败。
评审称 Linux 的高层 `psutil.Process(pid)` 完全不探测存在性，这一前提与锁定的 7.2.2 源码不符：
高层 `_init()` 会调用 `_get_ident()`，不能只依据底层 `_pslinux.Process._init()` 下结论。
本轮显式检查减少了对构造器副作用的依赖，未将受控模型当作 Linux/HarmonyOS 原生缺陷的实测证明。

新增 7 项回归在修改前为 **3 failed、4 passed**，分别覆盖无身份可读时的退出/未知、owner 状态、服务树竞态，
以及真实子进程从存活到退出的状态转换。修复后相关回归为 **259 passed、1 skipped**；
跳过项为 macOS 专用的 psutil 时钟调整回归，当前 Windows 未计为通过。
Windows resident 与 front_loopback 两组实进程启动、resident 同 Worker 重连及退出清理通过；
创建时间拒绝回退和双来源拒绝三组受控实验通过。未执行全仓 Linux UT、鸿蒙真机或安装包构建。

原始回归、文件范围快照、psutil 源码核对和进程证据位于
`D:/work_space/.codex-temp/harmony-process-identity-0210-review-20261125/`：
`before.xml`、`related.xml`、`scope-before.json`、`psutil-constructor.json`、`native-verified.json` 和 `harmony-controlled/verified.json`。

## 2026-11-25 同步 release 路径回退与旧 Worker 回收

本轮以候选 `f21e0db16` 合并目标 `releases/0.2.10` 的 `682b1ad7f`，随后同步到 `58c17ce40`。
目标回退了 `fba1df034` 的路径迁移，并引入 `0565dc011` 的旧常驻 Worker 身份漂移回收；
冲突位于 `worker_process.py` 的日志目录和 `resident_lifecycle.py` 的身份检查。
日志默认恢复目标分支的 socket 父目录 `worker-logs`，继续支持 `JIUWEN_WORKER_LOG_DIR`，诊断与 stderr 同目录；
删除已回退的 `common.paths` 依赖并同步路径测试、中英文说明，不重新带入 profile 布局迁移。

身份检查组合共享 `validate_process_identity()` 与目标新增的 `_peer_matches()`，保留用户、解释器、模块与端点校验。
另修正语义合并边界：新强制回收只用于未声明 ticks 的旧时间戳漂移；HELLO 已声明内核 ticks 时，不允许把身份变化或不可验证当作墙钟漂移绕过。
两个协议回归分别验证旧时间戳漂移仍可回收并接入新 Worker，以及 ticks 改变时拒绝强制回收。
机械合并后的原回收条件为 **1 passed、1 failed**，补齐限制后两个分支均通过。

首轮 Windows / Python 3.11.15 相关回归为 **244 passed、1 skipped**，包含目标新加入的 4 项旧 Worker 回收测试、
身份规约、owner、服务树、诊断、入口、后台日志、认证和 socket 同步；跳过项为 macOS 专用时钟验证。
原生 resident/front_loopback 两组完成请求并确认进程、端点、端口和锁清理；resident Front 重连复用同一 Worker。
创建时间拒绝回退及双来源拒绝的三组受控实验均通过，仍属于 Windows 故障注入，未代表鸿蒙真机验收。
`58c17ce40` 又更新了内置 skill 目录投影；同步后连同这两份目录测试重跑，最终为 **266 passed、1 skipped**。

目标此次回退也撤销了 Web 文件接口绕开宿主全局 CSP 的调整。此前两个 CSP UT 在未修改的 `682b1ad7f`
和当前候选上均复现相同断言失败；这两项单列为目标基线问题，未计为通过。
`58c17ce40` 的对应 Web 实现和测试文件与该控制基线一致，本 PR 保持这两个文件与目标一致。
本轮没有执行全仓 CI 或静态检查，远端结果按新提交核对。
原始证据位于 `D:/work_space/.codex-temp/harmony-process-identity-0210-conflicts-20261125/` 的
`ticks-before.xml`、`related.xml`、`latest-related.xml`、`csp-baseline.xml`、`csp-candidate.xml`、
`native-verified.json` 和 `harmony-controlled/verified.json`。

## 2026-11-26 附件契约对齐到 0.2.10

原 PR !10933 已合入。本轮从 releases/0.2.10 的 c33b158dc 建立后续分支，移植 develop 对齐提交 2e8d4a2ca，
依据用户明确采用的 worker-process-identity-changes.md 收紧 protocol 2：HELLO 至少提供一种有效出生身份；
仅 protocol 1 保留省略身份字段的兼容。两种字段始终分开传输，不可读者为 null，非法类型和非正数拒绝。
本节替代此前不区分协议版本的缺字段兼容；内部继续使用公共模块的浮点时间/带类型字符串表示，保持已有调用方和持久记录兼容。

补齐 debug、钉钉 OAuth 和 runtime owner 的权限拒绝兜底。debug 与新 OAuth PID 记录保存 OS 出生身份；
无法验证时保留未知状态，拒绝终止或取消清理，保留状态及凭据；终止与强制结束前重验身份。
runtime owner 同时处理 psutil.Error 与 OSError，保留既有 owner 格式、可读 ticks 回退及受控生命周期错误。

保留 0.2.10 的 pid_exists 检查、已退出服务树处理和旧 Worker 身份漂移回收。
新增回归明确验证：旧时间戳漂移可以回收，ticks 变化和 protocol 2 缺少身份均不能触发强制回收。
日志默认仍位于 socket 父目录的 worker-logs，保留显式目录、诊断与 stderr 同目录、追加句柄及目标已有的轮转；
不引入 develop 的 common.paths/profile 迁移。AGENTS.md 无改动，长期约束继续维护在 S_01_process-identity。

Harmony extra 补入 a4p、openjiuwen-e2a、agent-gateway，声明与本分支主依赖保持同一版本或 Git 提交；
附件旧包名 openjiuwen-agent-gateway 按当前分支分发名适配。锁文件重新生成后，全部既有包版本与来源不变。
独立 Python 3.11.15 环境安装并核对了 release 的 openjiuwen 31171fb02、agent-gateway befc32e15、
openjiuwen-e2a 10ad4a2ee，实际导入来自本任务环境；没有复用 develop 的私有源码依赖。
uv lock --locked --offline 通过，实际依赖记录保存在 dependencies-verified.json。

未修改的 c33b158dc 对原始 12 项契约复现得到 5 failed、7 passed；移植后这些复现与新增契约共 35 项通过。
完整定向回归最终为 **632 passed、10 skipped**，覆盖协议、身份漂移回收、旧 owner、debug、钉钉、编码、
IPC、停机 deadline、Worker 入口、诊断、日志轮转、进程监督、导入隔离和真实异常退出；跳过均为平台专用路径。
首轮两项失败来自新测试未声明模拟 PID 存在；fixture 补齐 pid_exists 后通过，保留并重验 release 的已退出 PID 处理。

原生 Windows resident 与 front_loopback 两组均完成请求，resident Front 重连复用同一 Worker；
最终进程、IPC、端口及 owner/startup 锁完成清理。另验证真实 Windows debug 子进程的身份持久化、停止返回 0、
进程退出和状态删除；resident Worker 阶段由前两组独立覆盖。
resident/loopback 创建时间拒绝回退及双来源拒绝三组受控实验全部通过，使用 Windows 注入权限错误与模拟 start ticks。
本轮未执行全仓 UT、静态检查或 HarmonyOS 真机验收，远端 CI 结果按新 PR 提交另行核对。

证据位于 D:/work_space/.codex-temp/harmony-attachment-0210-20261126/：release-verified.xml、baseline-contract.xml、
contract-verified.xml、native-verified.json、harmony-controlled/verified.json、native-debug-verified.json。

## 遗留与重验条件

未连接 HarmonyOS 真机、未构建 HAP；真实模型服务和其他系统 API 的权限仍需用户设备验收。
用户本地已调通属于补充证据，不替代本分支真机验证。身份读取、HELLO、退出校验或依赖变化时重验对应路径。
