# 空闲回收把执行中的长任务当闲置沙箱删除，任务循环失败

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-25 |
| 严重程度 | 中（开启空闲回收后，沙箱内执行时长超过 TTL 的任务被误杀并循环失败；PR #8764 起回收默认开启） |
| 范围 | `jiuwenbox/src/jiuwenbox/server/sandbox_manager.py`、`jiuwenbox/src/jiuwenbox/server/runtime/base.py`、`jiuwenbox/src/jiuwenbox/server/runtime/process.py`、`jiuwenbox/tests/unit/test_idle_reaper_busy_guard.py` |
| 关联 spec | 无（见"关联契约"） |
| 测试基线 | `jiuwenbox/tests/unit` 38/39 通过；唯一失败 `test_permissions_and_independent_objects` 在未打补丁的 dev-stable 基线上同样失败（Windows 文件权限语义，与本修复无关） |

## 现象与影响

触发条件：空闲回收开启（`idle_timeout` > 0）+ 沙箱内任务执行时长超过 TTL + 执行期间没有其他 API 调用。典型场景是沙箱里跑长解析脚本：同步 exec 只在请求开始时打一次活跃标记，执行期间不再刷新。

实际行为：回收器把"最近一次 API 交互时间超过 TTL"当作闲置，执行中的沙箱被整体删除，正在跑的进程随沙箱被杀，exec 请求返回 `daemon IPC transport failure: socket closed`。沙箱删除后下次调用 404 触发自动重建，任务从头再来——解析时长稳定超过 TTL 时形成"失败-重建-再失败"循环。

复现证据（113 验证服务器，0.0.50s 沙箱镜像，TTL 60s/扫描 30s）：同步 exec `sleep 120`（timeout=180）在第 81 秒被回收（`idle reaper: deleting 1 sandbox(es) idle > 60.0s` + `Stopping sandbox`），exec 请求 84.5 秒后返回 `{"exit_code":1,"stderr":"daemon IPC transport failure: socket closed after 0/4 bytes"}`。

预期行为：有任务在执行的沙箱不是闲置沙箱，不应回收。

## 关联契约

无独立 `S_NN`。依据现行根 policy timeout 契约（PUT /api/v1/timeout，回收只处置"闲置"沙箱）。本次把"闲置"的定义从"最近无 API 交互"细化为：**无执行中命令 ∧ 无确认运行中的后台任务 ∧ 最近无 API 交互**。

## 根因与修复决策

根因：闲置判定只看 `last_active_at`（API 交互时间），与沙箱内部执行状态脱钩；同步 exec 只在开始时打一次活跃标记，后台任务轮询接口（get/list_background_job）不刷新活跃时间；回收循环无任何"忙判定"。

修复决策与取舍：

1. **回收前忙判定**：到期候选先查"执行中命令"计数，再向运行时核实后台任务。核实用 `has_running_background_jobs() -> bool | None` 三态：True=逐条向守护进程核实过且仍在跑；False=确认没有（含无任务记录）；None=守护进程不可达、本地记录可能是陈旧的。
2. **None 按到期回收而非跳过**：守护进程不可达的沙箱对外已不可用（任何命令都会失败），且只有客户端请求能触发自动重启——没有流量时永远不会自愈，永久跳过等于把"误杀"换成"永不回收"（内存堆积回潮）。任务进程若真还在跑，其命运在守护进程死亡时已注定：既有恢复路径（exec 失败→restart）同样会清掉它们。回收时日志打 `background jobs unconfirmed (daemon unreachable), deleting` 便于现场甄别。
3. **查询异常跳过本轮**：`has_running_background_jobs` 抛异常（非"守护进程不可达"）时本轮跳过——异常意味着状态未知且基础设施有毛病，不做破坏性动作。
4. **删除原子收口**：后台任务核实发生在锁外（阻塞 IPC），核实期间可能有新命令开始。`_delete_if_idle` 把"复核仍是闲置候选"与"置 DELETING"放在同一把锁内完成：先到的命令会让复核失败、本轮作废；后到的命令因状态非 READY 被拒绝，不会出现"命令刚开跑就被回收"。
5. **执行中计数**：exec 进入时在锁内计数 +1，try 从计数后立即开始全程覆盖（env 准备/请求构造/执行），`finally` 中**同步**收尾（无 await，取消传播时也必然执行完）解除计数并刷新活跃时间——长命令刚结束不会被紧跟的扫描收走。
6. **轮询即活跃**：查询后台任务的接口补活跃标记，"跑后台任务、定期看进度"的沙箱不算闲置。
7. **错误匹配收紧**：`daemon IPC transport failure` 纳入执行失败自动重启匹配，用整条前缀匹配（运行时拼的固定前缀），用户命令输出恰好包含同文字不会误触发重启重跑。

拒绝的方案：

- **None→跳过**（保守派）：守护进程死透且无流量时沙箱永不回收，回到内存堆积；
- **宽泛 except→直接删除**（初版）：任何异常都当"没有任务"做删除，把未知状态当确认结论；
- **给 BackgroundJobSummary 加 unknown 状态**：改公共 API 形状，收益与三态返回等价；
- **复用 list_background_jobs 判忙**：它在守护进程不可达时静默返回陈旧 running 记录（`_sync_background_job_blocking` 静默 return），"忙"信号不可信。

## 回归证据与适用范围

候选版本：`fix/sandbox-idle-reaper-busyguard`（dev-stable 基线）；50s 基线同名分支。

113 验证服务器（沙箱镜像 0.0.50s 烧入三文件补丁，box API 直驱）：

| 场景 | 结果 |
|---|---|
| TTL 60/30，同步 exec sleep 120 | 完整跑完 `exit_code=0`，期间两轮扫描 `skip (exec in progress)` |
| 命令结束后不再交互 | 命令结束 +89s 被回收（TTL+扫描窗口，回收起点=命令结束时刻） |
| TTL 60/30，后台 sleep 100 零轮询 | 任务运行期间 `skip (background job(s) running)`，任务结束后下一轮回收 |
| 核实期间并发开始命令（单测模拟） | 沙箱存活不被回收（原子收口回归用例） |

单元测试：`test_idle_reaper_busy_guard.py`，覆盖回收四分支（确认运行中/状态无法确认/查询异常/确认无任务）、计数在成功/运行时异常/取消/请求构造失败下的释放、删除清计数、轮询刷新活跃、错误匹配边界、锁外核实窗口并发用例、RuntimeAdapter 忙判定契约（ProcessRuntime 必须显式覆写）。

未验证范围与重验条件：守护进程死亡场景（None 分支）未在真实 bwrap 沙箱上复现，依据 `process.py` 静态核对（`_daemon_ipc_available` 判定与 `_sync_background_job_blocking` 静默失败路径）；若调整守护进程通道或任务表实现，重跑上述单测并在 113 重验三场景。

## 防错模式与资料同步

- **三态语义的维护要点**：新增 `RuntimeAdapter` 实现必须显式声明忙判定行为（基类默认 None=无法确认→回收器按到期回收）；契约由单测锁定（`ProcessRuntime.has_running_background_jobs is not RuntimeAdapter.has_running_background_jobs`）。
- **忙判定的可信信号必须区分"确认"与"猜"**：任何"拿不到状态就当没任务"的实现都会在守护进程死亡时给出假阴性。
- 本次未同步 `jiuwenbox_server_api.md`（对外接口无变更，新增两条回收器日志行不影响 API 文档）。

## 遗留与后续

`_is_daemon_ipc_exec_failure` 既有模式 `daemon IPC channel unavailable` 仍为子串匹配；其运行时产出为 `sandbox {id!r} daemon IPC channel unavailable; ...`（开头带沙箱 id），前缀匹配不适用，用户输出误命中概率极低，暂不改。若后续引入专用 sentinel exit code（类似 timeout 的 124），可一并收敛两类匹配。
