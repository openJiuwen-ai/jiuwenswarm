# taskProgress 槽位泄漏：skip 抑制终态快照致 Stage 永驻 in_progress + 槽位归属登记清算

## 元信息

| 项 | 值 |
|---|---|
| 类型 | bugfix |
| 编号 / slug | B_03 / leaked-task-slot-settlement |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/skill_turbo_tools.py`（转发 gate / 清算）；`jiuwenswarm/server/runtime/skill_turbo/task_slot_guard.py`（登记簿，新增）；`jiuwenswarm/agents/harness/common/rails/task_execution_rail.py`（外层夺回注销，1 行） |
| 归档日期 | 2026-10-21 |
| 关联 spec | S_08_event-stream-protocol.md（不变量 5 修订 + tool 层协议第 2 步 + P3 五件套→六件套 + 边界 case 21/22 + 修改检查单 + 测试资产）；S_02_skill-routing-and-registration.md（N17 串行契约，配套指南收紧同批落地） |
| 发现渠道 | 线上会话定界（thread_mvhh8wd92emmnrlx / conversation 20c6bbe5…，"李白+杜甫两份 PPT"轮） |

## 复现场景

线上会话实证链（定界报告 2026-10-21）：

1. **首入口误判 forwarded**：外层 LLM 同轮并行 `todo_create` +
   `skill_acceleration_exec`（仅隔 8ms）。入口 gate 在工具进入时读
   `get_current_task_id()` / rebound ContextVar——外层 rail 的 `_ACTIVE_TASK_ID`
   要到 task.start / 懒启动才设置，竞态窗口内读空 → `has_outer_todo=False`
   → turbo 内部 task.* 事件整流转发，**首帧全 pending 快照即顶掉前端唯一的
   taskProgress 槽位**（外层 todo 列表消失）。
2. **被转发的这轮终态前被打断**：李白 PPT 在 Stage 4 附近 HITL 中断
   （agent_server.log 15 条 forwarded task 全在 stage1-4 区间，之后零转发）。
3. **续跑入口判 skip**：HITL 作答后 resume 走新入口，此时 rail ContextVar
   已设置 → `has_outer_todo=True` → 全部 task.* 在转发层 `continue` 丢弃——
   **executor 仍完整计算终态**（log 33617：`task.update: 13 tasks - 12 completed,
   1 in_progress`，被抑制），但终态快照永远到不了前端。
4. **槽位永驻 in_progress**：外层 LLM 在 turbo 流内被阻塞在工具调用里，物理上
   无法调 `todo_modify` 触发外层 rail 快照夺回槽位。用户视角"杜甫卡 Stage 4"
   （实为李白陈旧快照），永不完闭。

合取条件（缺一即无泄漏）：gate 误判 forwarded ∧ 该轮未跑到自身终态 ∧ 期间无
外层快照夺回。单独误判 + 一口气跑完会自闭合（gate 入口一次性判定、恒定全程，
终态快照本就在转发流里）；正确 skip 无污染。

## 根因

三层"开/关"语义里只有一层有人管：前端槽位是 last-wins 纯渲染（无 close 事件）、
executor 的任务集终态判定完备（`_after_subplan_execute` 在 task.complete 后
无条件发全量快照）——但**管它的 executor 恰好被转发层掐掉声音**，而转发层
（skip 分支）不感知"槽位当前显示的是谁"。skip 抑制是正确语义（放行 task.update
会顶掉外层 todo、放行 task.start/complete 会 taskStack 嵌套/segment 分裂），
缺的是**例外**：槽位已被本引擎任务集占用时，终态快照是关账而非污染。

泄漏跨入口存在（登记在首入口、清算在续跑入口），任何 per-entry / per-request
状态都无法表达——需要跨请求的槽位归属登记。

## 修复口径

1. **槽位登记簿**（`task_slot_guard.py`，模块级跨请求）：键 = 外层父会话
   session_id（turbo 的 parent_session 与 rail 的 ctx.session 同一对象）；
   值 = 占用任务集 task_id 集（仅观测）。claim（转发分支放行非终态 task.update）
   / release（两类注销：转发分支终态快照自然闭合；外层 rail 快照夺回）/
   peek（清算查询，只读不删）。上限 4096 会话按插入序淘汰。
2. **skip 分支采集**：被抑制的 task.update 缓存最后一条（不改变抑制行为）。
3. **终止性出口清算**（`_settle_leaked_task_slot`）：成功 / SkillTurboNotHandled
   / 普通异常三类出口，登记存在 ∧ 缓存快照终态（tasks 全部
   completed/failed，逐项判定不依赖计数字段）→ 补发关账（FIFO write_stream +
   OOB send_push 双通道）→ release。FIFO 写失败保留登记下次重试。
   **AbortError（HITL 暂停，含无 TIC 中断）绝不清算**——任务集还要续跑，
   关账等于把 in_progress 谎报成终态（S_08"暂停态禁终态帧"同族约束）。
   **CancelledError 不参与**（按 S_01 不 flush，缓存快照可能非终态；是否合成
   全 cancelled 快照留二期）。
4. **外层夺回注销**（rail 1 行）：`_emit_task_update_event` 写流成功后
   `release_task_slot(session_id)`——外层 todo 快照进流 = 槽位易主；写失败
   保留登记（槽位未被夺回，turbo 侧仍可关账）。
5. **只放行 task.update**：task.start / task.complete 在清算路径同样禁止
   （taskStack 归段保护不因清算破例）。

### 拒绝的方案

- **无条件放行 skip 入口终态快照**：正常 skip 路径（无泄漏）每次 turbo 结束
  都会把外层 todo 列表替换成 PPT 内部已完成清单——把 skip 机制要防的 bug
  变成 100% 发生。清算必须以登记为前提。
- **task_id 严格交集匹配才清算**：会放走"首任务集被弃（resume 未发生）、
  次任务集完成"的治愈路径——槽位已污染时"最新终态覆盖陈旧 in_progress"
  优于永驻陈旧。登记簿的 id 集仅作观测。
- **协议新增"释放槽位"事件**：需要 relay-claw 前端消费新事件类型，违背本次
  "jiuwenswarm 侧自包含"的定界；清算后槽位显示"已完成的 turbo 内部清单"
  而非外层 todo 列表，完全还原依赖外层 rail 下一次快照（模型调 todo_modify
  即触发，指南串行契约保证该路径存在）。
- **executor 侧清算**：executor 不感知转发 gate 与槽位归属，清算属转发层职责。

### 已知边界

- **同会话并发两条 turbo 流**：claim 后写胜（登记跟随后进快照）；该场景两套
  Stage 快照本已交错顶替（退化态），登记簿不额外仲裁。
- **relay-claw 折叠层**（定界症状 2 的 collapse 逻辑）若把清算帧整行折叠，
  前端历史视图仍可能显示陈旧——清算帧已进 journal 且 OOB 实时直推，实时链路
  优先保障；历史视图对账是 relay-claw 侧独立修复线。
- **泄漏登记后本会话再无 turbo 终止性出口且无外层快照**：槽位维持陈旧
  （与修复前现状相同，不劣化）；登记条目由上限淘汰兜底。
- **登记簿为单进程内存态（多 worker / 多进程部署）**：各进程登记独立；claim
  落在进程 A、HITL 续跑请求路由到进程 B 时 `peek` 返回 None、不清算——槽位维持
  陈旧，与修复前现状相同（不劣化，不误伤）。
- **HITL 暂停窗口内进程重启 / 部署更新**：模块级登记随进程销毁丢失，续跑入口
  在新进程无法关账，同样与修复前现状相同（不劣化）。跨进程存续如需支持，参照
  checkpointer 持久化先例（node_artifact_store / resume_context）单独立项，
  不在 B_03 范围内。

## 测试

- `tests/unit_tests/server/runtime/skill_turbo/test_task_slot_guard.py`（新增，
  9 用例）：claim/peek/release 基础语义、后写胜、幂等注销、空输入忽略、
  上限淘汰（含已存键不触发淘汰）、外层 rail 写流成功注销 / 写失败保留。
- `tests/unit_tests/agentserver/test_skill_turbo_tools.py`（新增 5 用例 +
  autouse 隔离 fixture）：
  - `test_skip_mode_terminal_settles_leaked_task_slot`——本 bug 复现（登记遗留
    + skip 终态出口 → 补发一帧终态快照 + OOB + 注销 + task.start 仍禁）；
  - `test_skip_mode_without_leak_keeps_slot_untouched`——正常 skip 回归守卫
    （无登记不放行任何 task.*，副作用防线）；
  - `test_forwarded_run_closes_own_slot_on_terminal_snapshot`——转发分支流内
    两阶段转移（非终态 claim → 终态 release）；
  - `test_forwarded_interrupt_leaves_slot_claimed`——中断保留登记（跨请求
    清算的前提）；
  - `test_skip_mode_abort_keeps_leak_for_resume`——暂停不清算（谎报守卫）。
- 既有回归全绿：skill_turbo 模块 393 用例 + HITL 套件 48 + rails 189。

配套（同批落地，属 F_03 特性线）：指南串行契约收紧
（`test_guide_mandates_sequential_todo_before_acceleration`）——从行为层消除
竞态触发路径，与登记清算（机制层兜底）互补：前者防泄漏发生，后者治已发生的
泄漏。
