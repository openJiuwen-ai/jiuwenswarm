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
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/shutdown.py
  - jiuwenswarm/server/runtime/session/lifecycle.py
  - jiuwenswarm/agents/swarm/group_route.py
  - jiuwenswarm/start_services.py
  - jiuwenswarm/instance_manager/status.py
  - jiuwenswarm/extensions/app_market/resources.py
  - tests/unit_tests/agentserver/test_worker_process_identity.py
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
未直接复制参考分支的依赖与日志体系修改：参考基线是独立 0.2.9 构建，本次目标是当前 develop 的身份读取问题。
旧 HELLO 按主干既有兼容行为处理；新 Worker 两种身份都不可读时明确失败，不能发送无身份 HELLO。

## 当前契约与防错约束

见 [S_01_process-identity](../../../specs/jiuwenswarm/common/S_01_process-identity.md)。
`jiuwenswarm/AGENTS.md` 明确禁止新增无异常处理的 `create_time()`，并要求权限拒绝及回退失败的行为回归。

## 回归证据与适用范围

Windows、Python 3.11.15、psutil 7.2.2；core 为锁定的 `0b1aa2102104a2eeadd960d92e332fb9eeb861b1`，
gateway 为 `dda0e957bd5ae501a326d3ea61a4d336c91ebfc7`，从本地只读 editable 环境加载。
本任务使用独立 `.venv` 和临时数据；补齐该环境缺少的 pywin32 DLL，不修改源依赖声明。

初步定向结果：新增身份行为 34 passed，原 runtime owner 18 passed。
调用方回归为 227 passed、1 skipped、8 failed；8 项均在相同 develop 基线重现，是 Windows 不具备 POSIX API/端口语义的既有失败。
首次较大回归还遇到环境缺少 pywintypes；补齐独立环境后群 owner 原有回归通过。
对照参考方案前的 Worker 生命周期回归为 255 passed、4 skipped。
对照后补充 ticks 优先比较、HELLO 失败日志、未知 booting Worker 和实例 PID 文件等场景，身份专项为 43 passed。
最终 14 个相关测试文件合并回归为 **528 passed、5 skipped、8 deselected**，耗时 231.53 秒；包含新增身份、
原 owner、loopback、常驻退出、重连、inflight stream 恢复、实例管理、服务树监督、应用资源及群 owner 消费方。
8 个 deselected 是前述已在未修改基线复现的平台失败，不解释为通过；未执行全仓测试或远端 CI。
完整命令和 JUnit 分别位于本地任务目录的 `final-command.txt`、`final-regression.xml`。

真实 loopback 子进程的受控实验：两版均注入 `create_time()` 权限拒绝；Windows 的 start counter 是用真实进程创建时间生成的模拟计数。
原 develop 在 IPC 已连接后报 `ipc connection closed`；候选可以完成 HELLO、AGENT_READY 和实际 stub 请求响应。
最终候选的 loopback 和 resident 均启动成功并响应 stub 请求，记录了 venv launcher 与实际 Worker PID，关闭后两者均退出。
loopback 监听端口与 resident IPC 端点也已关闭，没有遗留锁。两种身份都拒绝时启动失败，Worker 私有日志包含 HELLO 阶段、PID 和 traceback，进程及端口仍完成清理。
原始记录保存在本地任务目录
`D:/work_space/.codex-temp/harmony-process-identity-20261123/`，不提交运行产物。

## 遗留与重验条件

未连接 HarmonyOS 真机、未构建 HAP；真实模型服务和其他系统 API 的权限仍需用户设备验收。
用户本地已调通属于补充证据，不替代本分支真机验证。身份读取、HELLO、退出校验或依赖变化时重验对应路径。
