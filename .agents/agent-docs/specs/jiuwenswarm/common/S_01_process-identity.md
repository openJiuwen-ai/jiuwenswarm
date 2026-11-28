---
id: S_01_process-identity
title: 受限系统进程身份与 PID 重用校验
type: S
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/supervisor
  - jiuwenswarm/server/worker
  - jiuwenswarm/instance_manager
  - jiuwenswarm/agents/swarm
  - jiuwenswarm/server/jiuwenmate
files:
  - jiuwenswarm/common/process_identity.py
  - jiuwenswarm/server/supervisor/identity.py
  - jiuwenswarm/server/supervisor/resident_lifecycle.py
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/worker/service.py
  - jiuwenswarm/server/worker/stub.py
  - jiuwenswarm/debug_launcher.py
  - jiuwenswarm/server/jiuwenmate/dingtalk_cli_local_provider.py
---

# 受限系统进程身份与 PID 重用校验

最近修订日期：2026-11-27。

## 范围与事实源

`jiuwenswarm/common/process_identity.py` 提供 Worker、Supervisor、实例管理与群消息 owner 共用的进程身份读取。
进程身份来自操作系统；HELLO 和持久记录携带待验证的声明，不能单独授予终止权限。
HarmonyOS 的应用沙箱可能拒绝全局 `/proc/stat`，同时允许读取 `/proc/<pid>/stat`。

## 后续开发与检视约束

- 读取进程创建身份必须处理 HarmonyOS 沙箱拒绝访问 `/proc/stat` 的情况；不得新增无异常处理的
  `psutil.Process(...).create_time()` 调用。
- 归属、存活、重连和终止前的 PID 重用校验统一复用 `jiuwenswarm/common/process_identity.py`，
  使用创建时间或 `/proc/<pid>/stat` 的 start ticks。两者单位必须分开，不得互相比大小，
  不得用当前时间或 PID 单独代替身份。
- 身份不可验证时保留未知状态或受控失败，不能静默放行。
- 确实需要 wall-clock 时间的调用须显式处理 `psutil.Error`/`OSError`；修改相关路径时增加
  `AccessDenied` 及回退不可用的行为回归。

## 数据与接口

`read_process_identity(pid)` 优先读取 psutil 创建时间；失败时读取进程 stat 的第 22 字段。
正常返回浮点时间戳，回退返回显式带单位的 `proc_start_ticks:<整数>`；PID 由调用者另外保存。
`process_identity_matches` 按记录的来源重读身份。时间戳与 ticks 不互相换算；不能比较的旧时间戳返回不可验证错误。

HELLO 保留两个不同单位的字段：`process_created` 是 wall-clock 时间，`process_start_ticks` 是内核启动计数。
Worker 始终提供两个字段，不可读者为 null；psutil 被拒绝时，前者为空，后者仍可参与校验。
Supervisor 优先使用双方可读取的 ticks，再兼容时间戳；校验得到的身份用于后续存活、重连与终止前检查。
protocol 2 HELLO 必须至少提供一个有效身份字段；两个字段都缺失或为空时拒绝握手。
仅 protocol 1 允许省略创建字段，并继续执行旧部署、用户、可执行文件等校验、保存本地 OS 身份。
创建时间必须为有限正数，ticks 必须为正整数，bool 均不是有效身份；本次不升级 IPC 版本。

develop 的可选 `process_started` 保存平台原生的稳定启动标记，不与时间戳或 ticks 换算、混放。
其读取受限时发送 null，不得因此中断仍有有效 ticks 的 HELLO；它也不能替代 protocol 2 必须声明的
`process_created` / `process_start_ticks`。双方有可比较 ticks 时仍以 ticks 为准；否则可用同字段的精确
标记比较兼容观察方 psutil 时钟修正差异，并保留本地 OS 出生身份供后续检查。
未声明稳定标记的旧时间戳仍按原有身份比较规则校验。

macOS 常驻 Worker 退出时，如果旧 HELLO 只上报墙钟创建时间且时间不符，
退役流程可以用已连接 Unix socket 的内核对端 PID 复核归属。对端 PID 必须与 HELLO 匹配，
部署、用户、可执行文件和端点仍须通过既有校验。复核后以本端观察到的进程身份确认退出。
内核对端 PID 不可读取或不匹配时拒绝退役。

实例 PID 文件的 `started_at` 保持数值展示时间；新增的 `process_identity` 字段保存受限系统上的身份凭据。
群 owner 兼容旧数值与稳定 psutil hash；回退记录使用带类型的内核计数。
runtime owner 保留已有 `started_at`、`start_ticks` 与旧记录的读取行为，复用公共 stat 解析器。
runtime owner 的时间读取同时处理 psutil 异常与 OSError。

debug 服务状态与钉钉 OAuth PID 文件的新记录保存 `process_identity`，受限系统可使用带类型的启动计数。
debug 兼容旧时间戳记录；OAuth 兼容旧纯 PID 文件的时间校验。无法比较时保留未知状态，不能凭 PID 放行。
终止和强制结束前重验相同身份；未知状态下 debug 保留状态并拒绝停止，OAuth 保留 pending 数据及凭据、报告清理失败。

## 不变量与失败语义

1. **S-PID-01**：读取创建时间失败时，调用者必须处理异常，不能让它意外中断 HELLO 或错误地证明进程已退出。
2. **S-PID-02**：选定的可比较身份必须匹配；双方具备可读 ticks 时以 ticks 为准。PID 相同不能代替身份匹配，终止前必须重验。
3. **S-PID-03**：当前时间和零值不是创建身份，ticks 不能放入时间戳字段。
4. **S-PID-04**：新身份读取的两个来源都失败时抛出带 PID 和失败来源的 `psutil.AccessDenied`；Worker HELLO 与 Supervisor 加入阶段上下文。
5. **S-PID-05**：权限不足是未知状态。已有 owner 状态检查保守保留未知，终止路径拒绝未验证目标。
6. **S-PID-06**：解析进程名时使用最后一个右括号定位字段起点，不能直接按空格切整行。
7. **S-PID-07**：仅 protocol 1 可省略 HELLO 身份；protocol 2 缺少身份字段必须拒绝，不能用本地身份补齐对端声明。

真实系统仍可能拒绝其他 psutil API；该回退仅替代创建身份读取，不证明整个 HarmonyOS 应用已经验收。

## 验证入口与依据

- `tests/unit_tests/agentserver/test_worker_process_identity.py`：权限拒绝、不同端权限、PID 重用、HELLO 和清理。
- `tests/unit_tests/server/test_runtime_owner_identity.py`：旧 owner 记录及保守失败。
- `tests/unit_tests/common/test_process_identity_contracts.py`：debug/OAuth 未知状态、信号前重验及异常类型兼容。
- [修复与验证记录](https://gitcode.com/openJiuwen/jiuwenswarm/merge_requests/10796)。
- [macOS Worker 时间戳漂移的修复与验证](../../../bugfix/jiuwenswarm/server/supervisor/B_20261127_1636_macos-resident-worker-clock-drift.md)。
