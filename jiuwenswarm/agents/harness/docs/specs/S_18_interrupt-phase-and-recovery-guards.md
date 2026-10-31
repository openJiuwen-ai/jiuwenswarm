# S_18 中断相位与恢复守卫

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_18 / interrupt-phase-and-recovery-guards |
| 关联模块（agent-core 仓） | 无本体文件——`INTERRUPTION_KEY`（[[S_15]] 五状态键之一）是本域的清理对象；`RESUME_SIGNAL` 流 chunk 是相位 `paused → resumed` 的信号源；`InteractiveInput` 是 Team landed 判定的输入类型 |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/server/runtime/agent_adapter/interrupt_state_machine.py`（五相位状态机，153 行）、`server/runtime/session/permission_response_ledger.py`（权限响应台账 + Team landed ContextVar，170 行）、`server/runtime/agent_adapter/interrupt_resume_helpers.py`（续跑注入，171 行）、`server/runtime/agent_adapter/session_flag_proxy.py`（SessionFlagProxy 双写，110 行）、`agent_adapter/plan_pause_helpers.py`（恢复注入哨兵节 `INTERRUPT_RECOVERY_INJECTED_KEY`、`merge_supplementary_into_request_params`、`clear_session_interrupt_state`）；宿主适配调用方：`agent_adapter/interface.py`（unary 与流式两路径的守卫→reserve→start→settle→release 全序）与 `agent_adapter/interface_deep.py`（发卡相位标记 / RESUME_SIGNAL 转换 / 轮次收尾 / cancel·supplement 终态落盘 / `guard_stale_interrupt_response` / HITL 活性卡注册表） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **HITL 中断的相位生命周期、应答准入守卫与权限响应幂等台账**——中断"发出之后、应答进入 runtime 之前"的服务侧守卫层：

- **五相位状态机**（`interrupt_state_machine.py`）：`INTERRUPT_PHASE_SESSION_KEY` 相位记录、五相位常量、`TERMINAL_PHASES`、合法转换表、fail-open 语义、五个 `mark_*` 标记函数
- **适配层相位包装**（`interface_deep.py`）：`_mark_interrupt_paused_inmemory` / `_mark_interrupt_resumed_inmemory`（幂等条件转换）/ `_mark_interrupt_idle_inmemory` / `_mark_round_end_inmemory`——只标内存态的 best-effort 约定
- **终态守卫与活性卡守卫**：`guard_stale_interrupt_response`（相位终态 + 死卡活性双判，fail-open 放行）、`_stale_interrupt_response` 拒绝响应体、HITL 活性卡注册表四表结构（`_hitl_card_instances` / `_hitl_base_live_instance` / `_hitl_base_live_questions` / `_hitl_dead_card_ids`）及登记 / 失效 / 消费 / 陈旧判定四函数
- **权限响应台账**（`permission_response_ledger.py`）：`PermissionResponseLedger`（reserve / complete / abandon / release 全生命周期、FIFO recent 上限）、`PermissionResponseReservation`（start 的 CAS 语义）、Team landed ContextVar 三函数、`settle_permission_reservation` 结算规则
- **应答受理全序**（`interface.py` unary 与流式两路径）：终态守卫 → reserve → 排队 → start → reset landed → 执行 → settle → release_if_unstarted
- **续跑注入**（`interrupt_resume_helpers.py`）：`prepare_interrupt_resume_for_request` 早退链（mode 白名单 / `is_resume_user_query` / 活跃 todo）、恢复注入哨兵防重、决策提示词 merge、`runtime_session` 形参契约
- **SessionFlagProxy 双写铁律**（`session_flag_proxy.py`）：中断恢复域一切 session 标志的统一读写入口
- **cancel / supplement 落终态**（`_clear_session_persisted_interrupt_state`）：清 `INTERRUPTION_KEY`、清哨兵、（可选）清 snapshot-pending、落 cancelled / supplemented、全部活卡转死卡、清 skill_turbo resume ctx

不管：

- rail 本体与三态决策、五状态键注入/弹出 → [[S_15]]；权限判定序与授权记忆 → [[S_16]]；宿主装配、三源分类、发卡路径中 `questions_key` 判重与挂起表复用分支的**协议语义**（宿主侧去重决策为何存在）→ [[S_17]]
- 取消结算的其余部分——`settle_cancelled_confirm_interrupt` 写 cancelled ToolMessage、`{id}#{n}` 序号代际本体（`OuterCallIDDeduplicator` / `derive_outer_call_id`）、cancelled_tool_result 文案 → [[S_19]]（发卡路径调用 `next_final_id` 的调用点归本 spec P1，序号算法归 S_19）
- plan 暂停决策提示词本体、`PLAN_PAUSED_*` 标志生命周期、recovery 磁盘文件 → harness 域 [[S_04]]（本 spec 只管它与续跑注入共享的哨兵与互斥序）
- SkillTurbo 的 resume context 与 `TODO_RESUME_SNAPSHOT_PENDING_KEY` 在 todo 循环里的消费语义 → skill_turbo 域 [[S_06]] / [[S_09]]（本 spec 只管注入侧设置与清理侧复位）
- SessionManager 队列调度 / DeepAgentRuntimeController 路由 → server 运行时其它域

## 不变量

- **N1（五相位封闭与转换表固定）**（interrupt_state_machine.py @42-64）：相位集合 `idle / paused / resumed / cancelled / supplemented` 封闭；`TERMINAL_PHASES = {cancelled, supplemented}`；合法转换表固定为 idle→{paused}；paused→{resumed, cancelled, supplemented}；resumed→{paused（重放中再次中断）, idle（轮次正常完成）, cancelled, supplemented}；**终态→{paused（新卡重开）, idle}**。修改转换表即修改产品语义，必须走本 spec 修订。
- **N2（终态可被新卡重开）**：终态只表示"上一代中断已死"，不阻断新一代——新卡片发出即把相位重开为 `paused`。终态语义是"拒绝旧代应答"而非"锁死会话"。
- **N3（旧卡应答终态拒绝）**：进入终态后，同 session 旧卡应答一律被 `guard_stale_interrupt_response` 拒绝并回 `stale_interrupt_response`（`chat.interrupt_result` 事件、`invalidated: True`），**不触发 runtime 重放**——切断"点一张旧卡 → resume → 再弹新卡"的反馈循环（历史 bug：cancel 只终止 round 不清 `INTERRUPTION_KEY`，下一条纯文本被当 resume 输入 → re-interrupt 死循环）。
- **N4（状态机 fail-open）**：状态机是守卫不是运行时依赖——非法转换只记 warning 仍写入（`_transition_phase` @100-108）；`get` 失败返回 None、`update` 失败吞异常记日志；`guard_stale_interrupt_response` 自身异常 fail-open 放行。守卫故障不得阻断发卡/应答主流程。
- **N5（相位记录结构）**：记录恒为 dict `{phase, ts, prev, card_id?, source?, reason?}`；无记录视为 `idle`。`prev` 保留一代回溯线索；`card_id` 仅 paused 记录携带。
- **N6（内存态标记 best-effort + 幂等条件转换）**（interface_deep.py @22104-22190）：paused / resumed / idle 三个 inmemory 标记只写内存态（`build_flag_proxy(_interaction_session, loop_session)`，同步方法不做异步盘写——checkpointer 随 loop session 状态自然落盘，跨重启由 N2 新卡重开语义兜底）；标记失败只记 debug 不抛。`resumed` **仅在当前相位为 paused 时转换**（重复 resume 信号 / 其他相位 no-op，避免状态机警告变信号噪音）；`idle` **仅在当前相位为 resumed 时转换**（paused 在飞 / 终态守卫语义需跨请求存活，均保持原状）。
- **N7（cancel / supplement 终态双写落盘）**（interface_deep.py @14606-14675）：`_clear_session_persisted_interrupt_state` 经临时 session（`create_agent_session` + `pre_run`，checkpointer 磁盘态）与 runtime `_interaction_session` 的 flag proxy **双写**：清 `INTERRUPTION_KEY`、清 `INTERRUPT_RECOVERY_INJECTED_KEY`、按需清 `TODO_RESUME_SNAPSHOT_PENDING_KEY`、落 `cancelled` / `supplemented` 终态；随后**全部活卡转死卡**、清 skill_turbo resume ctx（显式传 card，防隔离键 fallback 清不到）。只清临时 session 会在运行时内存留下 stale 值。
- **N8（SessionFlagProxy 双写铁律）**（session_flag_proxy.py）：中断恢复域一切 session 标志的读写必须经 `SessionFlagProxy`——`update_state` 扇出所有注册实例（单实例失败不阻断其余）、`get_state` 依次查询返回**首个非 None 值**（None 是本域通用"已清除"标记，False/空串等真实值原样返回）、同一对象去重、嵌套 proxy 展开合并。prepare hook 的哨兵与标志**必须落 `runtime_session`**（`_interaction_session`，before_invoke 实际读取者）——标在临时 session 上等价于没标。鸭子类型兼容单 session 标志函数，调用方把 proxy 当 session 传即可。
- **N9（权限响应 reserve 幂等）**（ledger @118-129）：key = `(session_id, response_id)` 二元组；key 已在 `_active` 或 `_recent` 中则 `reserve` 返回 None → 回 `duplicate_permission_response`（`code: "duplicate_permission_response"`，`deduplicated: True`）。台账为**进程本地**——重启后清零，同卡应答可重试（跨请求幂等由 agent-core resume 语义兜底）。
- **N10（start 的 CAS 语义）**（ledger @86-91）：`start()` 仅当该 reservation 仍是 `_active` 表中**同一对象**（identity 比较 `is`）且未 started 时置 `started=True` 并授予 runtime 入场权；已被替换 / 已 start 返回 False → 按重复应答拒绝。排队期间被同 key 新请求抢占的旧 reservation 由此出局。
- **N11（recent FIFO 上限 1024）**（ledger @138-150）：仅 `complete` 且 `started` 的 key 进 `_recent`（OrderedDict FIFO，超 `_MAX_RECENT_KEYS=1024` 淘汰最旧）；`max_recent_keys=0` 禁用 recent 记忆。`abandon` / `release` 弹出 active **不进 recent**——失败可重试。
- **N12（release_if_unstarted 只释放未启动）**（ledger @161-170 + interface.py 两路径外层 finally）：排队任务因前序同 key 请求占用等原因**未 start** 时，`finally` 中 `release_if_unstarted` 弹出 active 条目，允许重试 reserve 替换；已 start 的不释放（结算权交给 settle）。
- **N13（Team landed ContextVar 与结算规则）**（ledger @18-65）：`_team_permission_resume_landed` ContextVar 三函数——`reset`（run_agent_task / run_stream_task 进入时清零，防上一任务残留）、`mark`（resume_interrupt 将 Team InteractiveInput 落地或入队时置 True）、`consume`（读后清，一次性）。结算 `settle_permission_reservation`：**单代理权限点击无条件 complete**；**Team InteractiveInput 点击仅当 `consume_team_permission_resume_landed()` 为 True 才 complete，否则 abandon**（key 不进 recent，保持可重试）——Team 的 resume 递交可能因 sidecar 去重未真正落地，落了台账却没落 runtime 会永久吞掉这次授权。
- **N14（续跑注入 mode 白名单）**（interrupt_resume_helpers.py @92-101）：仅 `mode ∈ ("agent", "agent.plan")` 注入续跑提示——dev-stable 实际 params.mode 统一为 `"agent"`，`"agent.plan"` 为历史遗留；两者都带 todo 循环。其余 mode（command / skill 直跑等）一律早退。
- **N15（续跑注入判定链与哨兵防重）**（@103-155）：注入须同时满足——`is_resume_user_query(query)`（≤32 字符短文本 + 续跑模式正则；**不匹配显式 replan 短语**（如「从头开始」）——那是全量重启）+ `has_active_todo_items(todos)`；且经 proxy 检查：已注入哨兵（`INTERRUPT_RECOVERY_INJECTED_KEY`，同请求周期内 plan_pause 先跑标到 runtime_session 也能读到）则跳过；`plan_paused` 为真则跳过（plan pause 机制优先，两机制互斥不叠加）。新增任何恢复机制必须走同一哨兵。
- **N16（注入内容与双写）**（@146-157 + plan_pause_helpers @423-460）：决策提示词经 `merge_supplementary_into_request_params` 以 `supplementary_info` 追加进 `params`（已有则换行拼接，不改写原值）；`set_todo_resume_snapshot_pending(True)` 与 `mark_interrupt_recovery_injected` 均经 proxy 双写**临时 session**（checkpointer 落盘，跨请求兜底）与 **runtime session**（before_invoke 本轮可见）——两者是不同对象，proxy 保证不漏写其一。
- **N17（作答消费销毁挂起表条目）**（interface_deep.py @22059-22078）：guard 放行（含 fail-open 放行）即 `_consume_hitl_live_card_for_answer` 摘除该 base 的挂起表活条目（base 取自应答 `request_id` 剥序号后缀）。挂起表语义是"还挂着没有"而非"发过没有"；销毁**不进死卡集**——同代卡重复应答由 N9 台账按原始 id 幂等去重；销毁后 resume 重放若重走到同 base 发卡点，按新代兜底而非误判复用（语义锚点：`test_stale_answer_rejects_non_live_generation_of_known_base`）。
- **N18（活性注册表有界）**（@22024-22037）：`_hitl_card_instances` 超 1024 淘汰最旧 512 条（被淘汰条目同步移出死卡集；若恰为该 base 当前活卡，live / questions 两表一并摘除防悬空）；`_hitl_dead_card_ids` 超 2048 全清。注册表为内存态，进程重启失效（fail-open，由磁盘态相位守卫兜底）。
- **N19（守卫判定序：活性卡快判在前、相位终态在后）**（`guard_stale_interrupt_response` @22192-22249）：先 `_is_stale_hitl_card_answer`（死卡集 / 代次取代，O(1) 内存判）再开临时 session 读磁盘+内存态判 `is_interrupt_terminal`；两判任一命中即 stale 拒绝。ask_user continuation（`params.source == "ask_user_interrupt"`）**绕过权限台账（by design：工作流输入非权限重放）但不绕终态守卫**——两类中断应答进 runtime 前守卫同权。`_is_stale_hitl_card_answer` 的序号后缀正判：未登记的 `{base}#{n}` 应答，若 base 当前代登记在案且非该 id，同样判陈旧（BUG20260928373903 复读风暴）；base 未登记 fail-open 交给相位守卫裁决。

## 接口契约

### 中断状态机（`agent_adapter/interrupt_state_machine.py`）

| 符号 | 签名 / 值 | 语义 |
|---|---|---|
| `INTERRUPT_PHASE_SESSION_KEY` | `"jiuwenclaw_interrupt_phase"` | 相位记录在 session state 的键名，固定 |
| `PHASE_IDLE / PAUSED / RESUMED / CANCELLED / SUPPLEMENTED` | `"idle" / "paused" / "resumed" / "cancelled" / "supplemented"` | 五相位字面值，固定 |
| `TERMINAL_PHASES` | `frozenset({cancelled, supplemented})` | 终态集合 |
| `get_interrupt_phase(session) -> dict \| None` | 读相位记录；非 dict 或无 `phase` 键返回 None；读失败 None | 只读不写 |
| `is_interrupt_terminal(session) -> bool` | 相位 ∈ TERMINAL_PHASES | 终态判 |
| `mark_interrupt_paused(session, *, card_id="", source="")` | 相位 → paused | 发卡 / 重放中再中断 |
| `mark_interrupt_resumed(session, *, card_id="", source="")` | 相位 → resumed | 应答受理进入重放 |
| `mark_interrupt_idle(session)` | 相位 → idle | 轮次正常完成 |
| `mark_interrupt_cancelled(session, *, reason="")` | 相位 → cancelled | 用户取消（终态） |
| `mark_interrupt_supplemented(session, *, reason="")` | 相位 → supplemented | 用户补充/切任务（终态） |

全部 mark 经 `_transition_phase`：先判合法转换（非法记 warning 仍写入），再 `update_state` 整条记录覆盖。session 形参接受 `SessionFlagProxy`（鸭子类型）。

### 权限响应台账（`session/permission_response_ledger.py`）

| 符号 | 签名 | 语义 |
|---|---|---|
| `PermissionResponseLedger()` | `max_recent_keys=1024`（负值抛 ValueError） | 进程本地台账 |
| `.reserve(session_id, response_id) -> Reservation \| None` | key 重复返回 None | 一次性占位 |
| `.is_current_reservation(r) -> bool` | `_active.get(r.key) is r` | identity 判 |
| `.complete_reservation(r)` | 弹 active；started 才进 recent（FIFO） | 记住已答 |
| `.abandon_reservation(r)` | 弹 active，不进 recent | 可重试 |
| `.release_if_unstarted(r)` | 未 started 才弹 active | 排队让位 |
| `PermissionResponseReservation.start() -> bool` | CAS：current 且未 started 才置 started | runtime 入场权 |
| `reset_team_permission_resume_landed()` / `mark_team_permission_resume_landed(bool)` / `consume_team_permission_resume_landed() -> bool \| None` | ContextVar 三函数 | Task 内单值，consume 读后清 |
| `settle_permission_reservation(r, *, settle_by_team_landing) -> None` | 见 N13 | 任务 finally 结算 |

### 适配层相位与守卫（`interface_deep.py`，宿主适配实例方法）

| 方法 | 语义 |
|---|---|
| `_mark_interrupt_paused_inmemory(*, card_id, source)` | 发卡时相位标记；`_instance` 为 None（`__new__` 单测构造）时 no-op，AttributeError 不得逃逸 |
| `_mark_interrupt_resumed_inmemory(*, source)` | RESUME_SIGNAL 到达时标记；仅 paused 转换（幂等） |
| `_mark_interrupt_idle_inmemory()` | 轮次完成标记；仅 resumed 转换 |
| `_mark_round_end_inmemory()` | idle 标记 + **无条件**全活卡转死卡（走到本调用点的轮次已无 pending 卡，残留活卡必为历史代） |
| `guard_stale_interrupt_response(request) -> bool` | True = 过期应拒绝；先活性卡判后相位终态判；只读不写；异常 fail-open 放行 |
| `_register_hitl_card_instance(*, base_id, final_id, questions_key="")` | 登记新卡取代同 base 旧卡（旧卡进死卡集，不广播失效事件——旧卡应答由守卫拒，屏幕旧卡由新卡覆盖）；questions_key 空串永不命中复用分支（保守安全） |
| `_invalidate_all_hitl_card_instances()` | 终态 / 轮次结束：全部活卡转死卡 |
| `_consume_hitl_live_card_for_answer(request)` | 作答被接受：销毁该 base 挂起表条目（N17） |
| `_is_stale_hitl_card_answer(final_id) -> bool` | 死卡集命中 / base 当前代非该 id（含 `{base}#{n}` 正判） |
| `_clear_session_persisted_interrupt_state(session_id, *, reason, clear_todo_resume_snapshot_pending=False, terminal_phase=None)` | N7 全清理序；`terminal_phase` 非空时同时落终态 + 活卡全失效 |

### 续跑注入与标志代理

| 符号 | 签名 / 语义 |
|---|---|
| `prepare_interrupt_resume_for_request(adapter, request, *, runtime_session=None)`（interrupt_resume_helpers.py） | agent.plan 跑前注入续跑指引；`runtime_session` 是 before_invoke 实际读取的 `_interaction_session`——哨兵与 snapshot-pending 必须落它上面 |
| `SessionFlagProxy` / `build_flag_proxy(*sessions)`（session_flag_proxy.py） | N8 双写入口；proxy 传入会被展开合并 |
| `is_interrupt_recovery_injected / mark / clear`（plan_pause_helpers.py @468-480） | 哨兵三函数（键 `INTERRUPT_RECOVERY_INJECTED_KEY = "jiuwenclaw_interrupt_recovery_injected"`；clear 写 None） |
| `merge_supplementary_into_request_params(params, supplementary)`（@423-434） | 追加 `supplementary_info`，空串 no-op |
| `clear_session_interrupt_state(session)`（@553-565） | 单键清 `INTERRUPTION_KEY`（写 None），import 失败仅 debug |
| `is_resume_user_query(query)`（common/tools/todo_resume.py @408-420） | 续跑短文本启发式（≤32 字符 + 正则；排除 replan 短语） |

## 数据结构

```python
# 相位记录（session state，INTERRUPT_PHASE_SESSION_KEY 下）
{"phase": "paused", "ts": 1761..., "prev": "idle", "card_id": "{base}#2", "source": "ask_user"}

# 台账内部（进程本地，单 adapter 一份）
_permission_response_ledger._active: dict[(session_id, response_id), PermissionResponseReservation]
_permission_response_ledger._recent: OrderedDict[(session_id, response_id), None]  # FIFO ≤1024

# HITL 活性卡注册表（adapter 实例内存态）
_hitl_card_instances:   dict[final_id, base_tcid]   # 卡片身份登记
_hitl_base_live_instance: dict[base_tcid, final_id]  # 当前活卡
_hitl_base_live_questions: dict[base_tcid, questions_key]  # 活卡 questions 指纹
_hitl_dead_card_ids:    set[final_id]               # 已死卡（被取代/终态/轮次结束）
```

## 执行路径全景

- **P1 发卡（同步，`_dedupe_ask_user_card`）**：`chat.ask_user_question` 事件 → questions_key 判重（同 request_id 同 questions 的多通道重复直接跳过）→ **挂起表复用**：活卡仍挂着且 questions 一致 → 重发原卡（不改代次、不动相位）；questions 不同 → 新代逻辑 → `OuterCallIDDeduplicator.next_final_id`（[[S_19]]）→ `_register_hitl_card_instance` 登记取代 → `_mark_interrupt_paused_inmemory` 相位重开 paused。
- **P2 应答受理（unary，`interface.py` process 前段）**：`guard_stale_interrupt_response`（N19 双判）→ 命中回 `_stale_interrupt_response` 终止 → ask_user 应答额外执行 `_consume_hitl_live_card_for_answer`（N17）→ 权限应答 `reserve`（N9）→ 重复回 `_duplicate_permission_response` → 计算 `settle_by_team_landing`（Team + `inputs["query"]` 是 `InteractiveInput`）→ `submit_and_wait` 排队 → 任务内 `start()`（N10 CAS）→ `reset_team_permission_resume_landed()` → `process_message_impl` → **finally `settle_permission_reservation`**（N13）→ 外层 **finally `release_if_unstarted`**（N12）。流式路径（`process_message_stream`）同构：守卫换 `_stale_interrupt_chunk`，任务换 `run_stream_task`（`async for process_message_stream_impl`），start 拒绝经队列回 duplicate chunk，消费者外层异常兜底再 release。
- **P3 重放与轮次收尾（流消费循环）**：interaction_stream 出现 `RESUME_SIGNAL` chunk → **先** `_mark_interrupt_resumed_inmemory`（paused→resumed）**再**交给 suppress 清除决策（suppress 生效时该 chunk 会被 skip，标记必须在 continue 前）→ 重放中再次中断回 P1（resumed→paused）→ 轮次正常完成 `_mark_round_end_inmemory`：idle（resumed→idle）+ 全活卡转死卡。
- **P4 cancel / supplement（请求处理侧）**：`_clear_session_persisted_interrupt_state(session_id, reason=..., clear_todo_resume_snapshot_pending=..., terminal_phase=PHASE_CANCELLED / PHASE_SUPPLEMENTED)` → 临时 session `pre_run` → proxy 双写清 `INTERRUPTION_KEY` + 哨兵（+ 可选 snapshot-pending）→ 落终态 → `_invalidate_all_hitl_card_instances` → `post_agent_execute_for_session` 落盘 → 清 skill_turbo resume ctx → `post_run`。此后同 session 旧卡应答被 P2 守卫拒绝（N3）。
- **P5 续跑注入（prepare hook，非权限应答的新请求）**：早退链（`adapter._instance` None / 无 session_id / params 非 dict / mode ∉ 白名单 N14 / 非 resume 短语 / 无 todo modify tool / 无活跃 todo）→ 临时 session + proxy → 哨兵已注入 or `plan_paused` → 跳过（N15）→ `format_todo_snapshot_lines` + `build_interrupt_resume_decision_prompt(language)` → `merge_supplementary_into_request_params` → `set_todo_resume_snapshot_pending(True)` + `mark_interrupt_recovery_injected`（均经 proxy 双写，N16）→ `post_agent_execute_for_session` 落盘。异常仅 warning 不抛，`finally` 必 `post_run`。

## 异常分支与退出路径

| 触发 | 行为 |
|---|---|
| 状态机读写抛异常 | warning/debug 记日志继续（N4 fail-open） |
| `guard_stale_interrupt_response` 自身异常 | warning + fail-open 放行（False） |
| 守卫命中（死卡 / 终态） | 回 `stale_interrupt_response`（unary）或 `_stale_interrupt_chunk`（流式），不进 runtime |
| `reserve` 返回 None | 回 `duplicate_permission_response` / `_duplicate_permission_chunk` |
| `start()` 返回 False（任务内） | unary 返回 duplicate；流式经队列 put duplicate chunk 后 return |
| Team landed 为 falsy | `abandon` + info 日志（key 可重试） |
| 流式外层消费者异常 / 取消 | `stream_task.cancel()` + await 后 `release_if_unstarted` |
| `_clear_session_persisted_interrupt_state` 失败 | warning（终态守卫降级为内存态死卡集 + fail-open） |
| 续跑注入任一步失败 | warning 不抛（该请求按无注入继续） |
| 注册表操作失败 | debug（不影响发卡主流程） |

## 边界 case 清单

| case | 预期 |
|---|---|
| cancel 后点旧卡 | stale 拒绝（N3），不再弹新卡 |
| cancel 后发起新中断 | 相位重开 paused，应答正常（N2） |
| 同一权限卡快速双击 | 第二次 reserve 命中 → duplicate（N9） |
| Team 点击但 resume 未落地 | abandon，重试可再 reserve（N13） |
| 单代理权限点击（无论落地） | 无条件 complete（N13） |
| 进程重启后点旧卡 | 台账清零可入队；磁盘态相位终态 / 活性 fail-open 仍守卫（N4/N19） |
| 重放中再次中断 | resumed → paused 新代，旧代应答按死卡拒（N1/N2） |
| 轮次结束后点残留卡 | 活卡已全转死卡 → stale 拒绝（`_mark_round_end_inmemory`） |
| 同代卡重复应答（挂起表已销毁） | 台账按原始 key 幂等去重（N17） |
| 迟到的 `{base}#{n}` 旧代应答 | base 当前代在案且非该 id → 陈旧（BUG20260928373903） |
| base 未登记的应答 | 活性 fail-open，交相位守卫裁决（重启恢复路径） |
| 续跑查询含「从头开始」 | `is_resume_user_query` False，不注入（全量重启）（N15） |
| plan_paused 与续跑同周期 | plan pause 优先，续跑跳过（N15 互斥） |
| 多恢复机制同周期竞争 | 哨兵先到先得，后到跳过（N15） |
| `__new__` 构造的 adapter（单测） | 相位标记 no-op 不抛（N6 防御） |
| 注册表膨胀 | 超限淘汰最旧 / 全清（N18） |

## 修改检查单

改本域任何代码前，逐条确认：

1. 改转换表 / 终态集合 → 重验 N2（新卡重开）与 N3（旧卡拒绝）两路径 + `test_hitl_superseded_invalidation` / `test_hitl_resume_signal_protocol`。
2. 改 ledger key 结构或 `max_recent_keys` → 重验幂等（N9）与 FIFO（N11），跑 `test_permission_response_dedup`。
3. 改 `settle_permission_reservation` 规则 → 验证单代理无条件 complete 与 Team landed 判（N13），防"记账却未落地"永久吞授权。
4. 改活性注册表四表结构 → 验证有界淘汰与悬空条目清理（N18）、`_consume` 语义锚点（N17）。
5. 改守卫判定序 → 保持活性快判在前（N19，避免每次应答都开临时 session 读盘）；新增守卫条件默认 fail-open。
6. 改续跑注入白名单 / 判定 → 同步检查 `is_resume_user_query` 正则与 replan 排除（N14/N15）。
7. 新增任何恢复注入机制 → 必须读写 `INTERRUPT_RECOVERY_INJECTED_KEY` 哨兵（N15），不得绕过。
8. 改 `SessionFlagProxy` 读写语义 → 全量复核中断恢复域所有标志调用点（N8：update 扇出 / get 首 None）。
9. 改相位标记函数 → 保持只标内存态与幂等条件转换（N6）；发卡是同步方法，不得引入异步盘写。
10. 改 `_clear_session_persisted_interrupt_state` 清理集 → 与 [[S_15]] 五状态键、[[S_04]] plan pause 标志、skill_turbo resume ctx 的键清单逐一对账，漏清即残留误判（N7）。
11. 改 `runtime_session` 形参传递链 → 确认哨兵落 `_interaction_session`（N8/N16），否则 before_invoke 不可见。
12. 改 ask_user 应答与权限应答的守卫差异 → ask_user 绕台账是 by design（工作流输入），不绕守卫（N19），不得反向"补齐"。

## 与其它 spec 的关系

- 上游：[[S_15]]——`INTERRUPTION_KEY` 是 N7 清理对象；`RESUME_SIGNAL` chunk（handler resume 分支发出）是 P3 相位转换信号源；恢复分类四问决定哪些应答走本域 P2 受理序。
- 上游：[[S_16]]——权限卡（弹卡侧）产生本域台账受理的 continuation；auto_confirm / batch allow 记忆（S_16）与本域响应幂等（N9~N13）是互补的两层去重：前者在 rail 判定层，后者在 server 入口层。
- 上游：[[S_17]]——发卡路径 `questions_key` 判重与挂起表复用分支运行在本 spec P1（复用判定不标相位）；宿主装配决定 `source="ask_user_interrupt"` 标记的产生。
- 下游：[[S_19]]——`next_final_id` 序号代际由 P1 调用（算法本体归 S_19）；cancel 结算的 `settle_cancelled_confirm_interrupt` 与 P4 终态落盘共同构成取消全链；`{id}#{n}` 后缀剥离是 `_consume` / `_is_stale_hitl_card_answer` 的前置。
- 跨域：[[S_04]]（harness）——plan pause 与续跑注入共享哨兵与互斥序（N15）；`PLAN_PAUSED_*` 标志生命周期归 S_04。skill_turbo [[S_06]] / [[S_09]]——P4 清 skill_turbo resume ctx、`TODO_RESUME_SNAPSHOT_PENDING_KEY` 的消费侧。agent_adapter/AGENTS.md 铁律 2（卡片注册表）即本 spec N17/N18 域的入口文档。
- 测试组 6：本 spec 对应 `tests/unit_tests/agentserver/test_hitl_superseded_invalidation.py`（终态守卫 / 活性失效 / superseded）、`test_hitl_resume_signal_protocol.py`（相位转换信号序）、`test_permission_response_dedup.py`（台账全生命周期）、`test_facade_mode_compatibility.py`（受理序回归）。
