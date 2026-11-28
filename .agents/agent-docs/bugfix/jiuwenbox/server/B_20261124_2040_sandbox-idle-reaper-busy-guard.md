---
id: B_20261124_2040_sandbox-idle-reaper-busy-guard
title: 沙箱空闲回收补齐忙状态判定与在途操作保护
type: B
author: Xiangju Zhu
author_email: 68637701+zxj0302@users.noreply.github.com
modules:
  - jiuwenbox/src/jiuwenbox/server
files:
  - jiuwenbox/src/jiuwenbox/server/sandbox_manager.py
  - tests/unit_tests/test_jiuwenbox_idle_reaper.py
designs: []
design_note: 针对 jiuwenbox SandboxManager 空闲回收判定与活跃时间戳更新的最小修复，不修改对外 HTTP 协议与默认 TTL 配置，无独立设计文档。
---

# 沙箱空闲回收补齐忙状态判定与在途操作保护

关联 issue：GitHub #12720。

## 现象与根因

沙箱内运行超过 `idle_timeout`（企业版部署默认配置 180s，代码层默认 `None` 禁用）的同步长任务或后台轮询任务时，`_idle_reaper_loop` 误判沙箱闲置并调用删除，直接中断执行中的进程并触发“失败 → 重建 → 再失败”循环。

根因集中在 `jiuwenbox/src/jiuwenbox/server/sandbox_manager.py`：
1. `_idle_reaper_loop` 仅检查 `phase == READY` 且 `last_active_at < cutoff`，完全未判断是否存在在途操作或运行中后台任务；
2. `exec_in_sandbox` / `exec_background_in_sandbox` / 文件操作仅在进入时刷新一次 `last_active_at`，无在途引用计数且返回时不刷新活跃时间；
3. `get_background_job_in_sandbox` 与 `list_background_jobs_in_sandbox` 未刷新 `last_active_at`；
4. 回收扫描释放锁后直接调用 `delete_sandbox`，存在 TOCTOU 竞态窗口，且 `_is_daemon_ipc_exec_failure` 漏匹配执行前 `daemon IPC unavailable`、`restart_sandbox` 缺少 `DELETING` 状态防护。

## 修复决策

1. **在途操作引用计数与完成刷新**：新增 `_inflight_ops: dict[str, int]` 与 `_track_active_op` 上下文管理器，覆盖同步/后台执行、后台任务查询/列表/终止与文件读写列举搜索；进入与退出（沙箱仍 `READY`）均刷新 `last_active_at`，`finally` 中同步扣减计数避免 `CancelledError` 泄漏；后台任务查询/列表保持 `require_ready=False` 兼容非 `READY` 状态查询。
2. **三条件完整判闲 + 后台任务结束宽限**：`_reap_idle_sandboxes_once` 仅当“无在途操作 ∧ 无运行中后台任务 ∧ 最近活动（含刚结束后台任务的 `finished_at`）超过 `idle_timeout`”时才回收，并对每次跳过记录可观测日志（`reason=in_flight_ops` / `running_background_jobs` / `recent_background_job_finished` / `became_active`）；若存在运行中后台任务记录，会通过公共接口 `runtime.is_running(sandbox_id)` 确认沙箱仍存活，避免已退出的 `ProcessRuntime` / `ConchRuntime` 沙箱因陈旧任务记录永久阻塞回收。
3. **删除前原子二次复核**：`_try_claim_idle_sandbox_for_delete` 在 `self._lock` 内重新校验阶段、在途计数与最新活跃时间后再原子置为 `DELETING`，彻底消除 TOCTOU 误杀；同时保留用户显式 `delete_sandbox` 立即终止语义。
4. **执行前 IPC 失败重试与防复活**：`_is_daemon_ipc_exec_failure` 补齐执行前 `daemon IPC unavailable`（刻意不对已发送后中断的 `daemon IPC transport failure` 与 cgroup OOM `exit_code=137` 自动重放，避免非幂等命令重复执行）；在 `restart_sandbox` 持锁块内显式拒绝 `DELETING` 沙箱，并在 `exec_in_sandbox` / `download_file_from_sandbox` 触发 `restart_sandbox` 前通过 `_is_sandbox_ready_for_restart` 守门。

## 验证与边界

- 新增 `tests/unit_tests/test_jiuwenbox_idle_reaper.py`（12 项回归用例，纳入主仓 `tests/unit_tests/` CI UT 门禁并在 Windows 下模块级跳过），覆盖同步长任务跨多轮扫描不被回收与结束后宽限、后台长任务与结束后宽限、后台任务查询/列表刷新活跃时间及 `STOPPED` 查询兼容、纯闲置沙箱正常回收、后台探测期新请求到达的 TOCTOU 保护、死沙箱陈旧任务仍可回收、探测异常跳过不阻断其他沙箱、显式删除不触发误重启、`DELETING` 沙箱拒绝重启、执行前 IPC 失败重试且不重放 `transport failure`/OOM、异常/取消不泄漏在途计数及文件操作嵌套回退引用计数。
- 本地验证：`tests/unit_tests/test_jiuwenbox_idle_reaper.py` + `tests/unit_tests/test_agentos_policy.py` 全量 18 passed，`jiuwenbox/tests/unit/` 全量 105 passed。
- 本次不修改默认 `idle_timeout` / `idle_check_interval` 与对外 HTTP API 契约。
