---
id: B_20261121_1817_parked-team-busy-snapshot-reseed
title: 暂停后页面持续转圈：挂起团队忙闲快照读到内存名册残留
type: B
author: zhanglongmin
author_email: zhanglongmin@huawei.com
designs: []
design_note: 依据运行日志与逐跳代码核实定位，无独立设计文档；修复走 Core 既有公开契约，不修改 agent-core。
modules:
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/runtime
files:
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/runtime/team_session_lifecycle.py
  - tests/unit_tests/agentserver/test_team_manager_registry.py
  - tests/unit_tests/runtime/test_team_session_lifecycle.py
---

# 暂停后页面持续转圈：挂起团队忙闲快照读到内存名册残留

## 现象

团队会话暂停完成后（Gateway 已派发 `chat.processing_status=false`、数据库无 busy
成员），页面打开/刷新/切换时仍显示处理中转圈且不再熄灭。已排除暂停终态未推送
与收尾竞态两类前因。

## 根因

**一句话**：Core 暂停只把成员状态写进数据库，挂起的 Leader 内存名册保留上一轮
的 busy；worker 的 runtime 快照在 work-record 全部判闲后，仍会把「团队侧忙闲
判定」OR 进 `is_processing`，读到残留 busy 即恒真。

完整链路（逐跳核实）：

1. 前端打开/刷新/切换会话 → `session.get_metadata`；`front/router.py:190-201`
   用 supervisor IPC 订阅查询（worker `_subscribe_locked`）富化响应，并把快照
   的 `is_processing` 盖到顶层 payload、`status` 置 `processing`。
2. `_subscribe_locked` 先按 work-record 聚合：暂停后业务记录已被协调器执行态
   reconcile 清掉、passive 观察者被排除，均为 False。
3. **`dispatcher.py:762-771` 把 `team_lifecycle.is_running(...)` OR 进
   `is_processing`**——该边专为「仅剩 passive 观察者、但队友还在执行」设计；
   走 Port `is_running` → `is_generation_running`（暂停不换代、generation 匹
   配通过）→ `is_team_session_running`（team_manager.py:4185）。
4. `is_team_session_running` 读 `team_agent._state.member_registry`，任一 peer
   "busy" 即判忙。Core 暂停（`agent/coordination/kernel.py`
   `_mark_live_teammates`）只写 DB（PAUSED），注释明言持久层与内核易失态
   decoupled；`MemberActivityRegistry` 仅 `record()`（核心转移驱动）与
   `seed()`（DB 基线）两条写入口，暂停路径都不走 → 残留 busy → 判忙 → 快照
   恒真。
5. 前端「快照只能拉起、回落只认终态事件」规则（B_20261112_1405 链）点亮转圈；
   interruptSettleGuard 收尾窗口有时限；暂停终态 false 在快照查询之前已推过，
   之后无新终态 → 转圈不灭。以真实 TeamManager 的最小验证与判定代码直测均
   确认：DB=paused、内存=busy → true；同步后 → false。

排查教训：追这条边时按 `is_session_running` 名字 grep 会漏——快照路径经 Port
方法名 `is_running` 间接进入忙闲判定。

## 修复

不改 agent-core（不新增 Core 代码、不动 pin）：Core 的 `TeamAgent
.seed_member_registry()` 本就是公开契约，语义即「以数据库为准重建 Leader 内存
名册」，内核每个 run cycle 开始时自调（`kernel.py:250-254`，注释明说内存会残
留上一轮状态）。缺口只是暂停把 runtime 挂起后、下一次 run cycle 之前无人调它。

- `TeamManager.reseed_member_registry(session_id) -> bool`（新增公开包装）：经
  `get_team_agent()` 公开句柄调用 `seed_member_registry()`；无 agent 或缺契约返
  回 False。全程公开 API，不触碰 `_state` 私有成员；键空间（成员名）由 Core
  自身保证。
- `publish_pause_completed`（team_session_lifecycle.py）在推成员面板与终态之
  前 reseed，自带 try/except（失败不阻塞发布）；时机在暂停屏障内、Core 已写完
  DB PAUSED 之后，下一次前端快照查询必然读到正确状态。主路径与 Adapter 兜底
  汇于此处，一处覆盖。

## 验证

- `pytest tests/unit_tests/agentserver/test_team_manager_registry.py`：新增用例
  复刻完整判定链——真实 TeamManager + fake parked agent（真实
  `MemberActivityRegistry` 预置 busy）→ `is_team_session_running` True →
  reseed → False；并覆盖无 agent / 缺契约返回 False。全套通过。
- `pytest tests/unit_tests/runtime/test_team_session_lifecycle.py`：新增用例断
  言 `publish_pause_completed` 驱动 reseed；既有 publish 用例（含真实
  TeamManager 路径，无 agent 时 reseed 为无害 no-op）全部通过。
- 受影响定向套件（含 runtime_service / team_helpers / interface_interrupts /
  web_cron / expert_team）合计 629 用例通过。
- 用户本地复测待执行：暂停后刷新页面/重开会话，转圈不再点亮。

## 遗留风险

- `seed_member_registry` 依赖被挂起 agent 的 `team_backend` 存活；若 Core 未来
  在挂起时剥离 infra，reseed 会静默 no-op（方法内部 warning、返回仍 True），
  届时忙闲判定回到本缺陷形态——依赖本地复测闭环确认。
- 补发停灯事件不足以修复本缺陷：后续任何一次错误快照仍会重新点亮（快照只能
  拉起规则），状态源同步才是根修。
- 无终态事件的静默流退出窗口（见 B_20261121_1545 遗留）与本缺陷独立，本次未
  处理。
