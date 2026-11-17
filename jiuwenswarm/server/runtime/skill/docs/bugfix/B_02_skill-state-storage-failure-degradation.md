# 技能状态存储故障被一刀切 fail-closed：整轮中止上报为「模型服务不稳定」（OA.05000050）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_02 / skill-state-storage-failure-degradation |
| 关联模块 | `jiuwenswarm/agents/harness/common/rails/skill_active_state.py`（`_update_state` / `clear_session_skill_state` / `_DEGRADED_SESSIONS`）+ `rails/permissions/skill_authorization/file_store.py`（诊断日志） |
| 归档日期 | 2026-11-17 |
| 关联 spec | S_05_agent-skill-rails.md（失败语义 / N2 / 降级注册表 / 修改检查单）、S_01_overview-and-boundaries.md（N5）、模块 AGENTS.md 铁律 4 |
| 发现渠道 | 线上会话定界（E-1114-1620-3c99：session `officeclaw_609dd2ca276c5fc8cbdb513e` 连续 6 轮失败，错误码 OA.05000050「模型服务不稳定」，全部发生在 LLM 调用前 tokens=0） |

## 复现场景

Windows 环境下会话目录的 `.skill_authorization/state.lock` 被拒绝访问
（`[WinError 5]`，杀软 / 文件索引器等外部进程短暂占用锁文件）时：

1. `SkillActiveStateRail.before_invoke` 的过期计数写入（纯记账，不含授权
   决策）抛 `AbortError`，整轮回调链中止。
2. agent-core 把该轮记为 `round_execution_error`；relay-claw 将其映射为
   OA.05000050「模型服务不稳定」推给前端。
3. 用户看到连续 6 轮「模型服务不稳定」，但模型从未被调用（tokens=0），
   与「模型服务」无关——真实故障是本地状态文件的一次瞬时写失败。

## 根因

`_update_state` 对**所有**写入路径一刀切 fail-closed：任何存储异常都抛
`AbortError` 中止本轮。这把两类语义不同的写入混在一起：

- **授权关键路径**（skill_tool 激活 / 切换 / skill_complete / 复活事务）：
  fail-closed 是正确语义——失败的切换 / 完成不能假装已生效。
- **纯记账路径**（before_invoke 过期计数、`clear_session_skill_state`
  显式清理）：写入失败不影响任何授权裁决结果，中止整轮是把可降级的
  瞬时故障升级为用户可见的「服务不可用」。

## 修复口径

`_update_state(..., strict=)` 双路径失败语义：

- **strict=True（授权关键路径）**：存储失败抛 `SkillStateUnavailable`
  （`AbortError` 子类，`error_code="skill_state_unavailable"`，不带 cause
  以穿透回调框架的 cause 改抛），中止本轮——fail-closed 语义不变。
- **strict=False（纯记账路径）**：存储失败 mark degraded（进程级
  `_DEGRADED_SESSIONS`）后放行本轮。**安全等价前提**：
  `get_session_active_skill` 对 degraded 会话短路返回 None（先于读盘），
  凭据注入等全部消费方读不到 active_skill，凭据窗口关闭——「不使用
  未清理的旧授权」由读侧短路达成（fail-safe：少授权），而非由中止
  本轮达成；磁盘文件本身继续承担跨进程的旧 state 保护。

degraded 标记的解除出口（防长期运行进程无界积累，检视 P3）：写成功
自动 clear、显式清理成功 clear、适配器淘汰 / teardown
（`clear_session_skill_state(local_only=True)`）**条件 clear**——仅当
确认磁盘 state 干净（会话目录已删，或读成功且无 active_skill）才
清除；盘上仍留有旧 active_skill 或读失败时保留护栏。无条件清除会在
「写坏读好」失效面下复活旧授权：重激活首轮若为 HITL resume 或
`stale_invoke_limit <= 0` 则无记账写、不会重新 mark，凭据注入 rail
将读到磁盘上的旧 active_skill（检视复审意见）。

`file_store._locked_directory_fd` 对锁文件打开失败补 WARNING 诊断
（errno / winerror / 只读属性 / 目标路径），便于线上定界。

规范同步（检视 P2）：S_05 失败语义段 / N2 / 降级注册表 / 修改检查单、
S_01 N5、模块 AGENTS.md 铁律 4 统一改为「双路径失败语义」表述——安全
目标「任何情况下不得使用未清理的旧授权注入凭据」不变，达成手段按
写入路径分类；绕过 degraded 短路直接读盘取旧 active_skill 的兜底
仍是违规。

### 拒绝的方案

- **授权路径也降级放行**：失败的 skill_tool 激活若被放行，模型会以
  为激活成功而继续执行未授权技能——破坏 fail-closed，不可接受。
- **全部 strict（维持原状）**：瞬时锁竞争继续被上报为「模型服务
  不稳定」，本 bug 未修复。
- **degraded 标记持久化到磁盘**：跨进程旧 state 保护已由会话文件本身
  承担（读侧 fail-safe），进程本地标记持久化只会引入新的写故障面。
- **驱逐时无条件清除 degraded 标记**：「写坏读好」失效面下磁盘读出的
  就是旧 active_skill，且重激活首轮（HITL resume / limit<=0）无记账写
  不会重新 mark——无条件清除会让凭据注入 rail 注入旧授权（检视复审
  拒绝，改为条件清除：仅确认盘上 state 干净才清）。
- **degraded 标记不设清理出口**：生产调用点全部 `local_only=True`，
  死会话条目在长期运行进程里无界积累（检视 P3 拒绝）。

## 测试

- `tests/unit_tests/agentserver/test_skill_active_state.py`（新增 10 用例，
  先红后绿）：
  - `test_before_invoke_storage_failure_degrades_instead_of_abort`
    （permission / timeout / corruption 三参数）——记账写失败降级放行，
    不抛 AbortError，degraded 阻断旧凭据注入。
  - `test_degraded_blocks_credential_injection_even_when_reads_work`——
    「写坏读好」场景下 degraded 短路挡住旧凭据注入（注入 rail 实测）。
  - `test_degraded_cleared_after_storage_recovers`——存储恢复后下一次
    成功写自动解除降级。
  - `test_strict_paths_storage_failure_raises_skill_state_unavailable`
    （skill_tool / skill_complete 两参数）——授权路径保持 fail-closed 且
    异常携带专用 error_code。
  - `test_local_only_clear_keeps_degraded_when_disk_state_stale`——盘上
    留有旧 active_skill 时驱逐不得清除 degraded（护栏保留）。
  - `test_local_only_clear_removes_degraded_when_disk_state_clean`——盘上
    state 确认干净时驱逐清除标记（清理出口）。
  - `test_local_only_clear_removes_degraded_when_session_dir_gone`——
    会话目录已删时驱逐清除标记。
  - `test_stale_degraded_marker_survives_eviction_and_blocks_credentials`
    （limit_disabled / hitl_resume 两参数）——去激活清理失败 → 驱逐 →
    重激活（无记账写，读成功写失败）→ 凭据注入不注入旧凭据的完整
    失效链回归（检视复审要求的用例）。
  - `test_clear_session_state_storage_failure_degrades`——显式清理失败
    降级标记而非抛异常。
  - `test_clear_session_state_success_clears_degraded`——显式清理成功
    解除 degraded。
- `tests/unit_tests/agentserver/test_file_skill_grant_store.py`：
  `test_lock_open_failure_logs_diagnostics`——锁打开失败输出诊断日志。
- 回归基线：`test_skill_active_state.py` 56 passed；相关组（active_state +
  grant store + credential injection + rails/）525 passed（另有 2 例
  Windows symlink 权限 WinError 1314 为环境预存失败、3 skipped，与本
  修复无关）。

## 已知遗留

- agent-core `deep_agent.py` 对 `error_code` 的透传与 relay-claw
  `agent-error-transform.ts` 的 `skill_state_unavailable` 专用分支为
  跨仓后续项（当前向后兼容：未透传时按通用 `round_execution_error`
  映射，行为与修复前一致）。
