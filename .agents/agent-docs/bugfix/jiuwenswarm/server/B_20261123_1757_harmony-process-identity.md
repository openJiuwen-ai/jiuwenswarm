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

`JIUWEN_WORKER_LOG_DIR` 指定 Worker 日志目录，未设置或为空时保留 socket 父目录下的 `worker-logs`。
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

## 遗留与重验条件

未连接 HarmonyOS 真机、未构建 HAP；真实模型服务和其他系统 API 的权限仍需用户设备验收。
用户本地已调通属于补充证据，不替代本分支真机验证。身份读取、HELLO、退出校验或依赖变化时重验对应路径。
