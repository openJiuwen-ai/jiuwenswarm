---
id: B_20261121_1545_team-pause-teardown-admission-race
title: 团队暂停双执行与收尾窗口新消息误投递：回合交接竞态
type: B
author: zhanglongmin
author_email: zhanglongmin@huawei.com
designs: []
design_note: 依据运行日志时间线与代码逐跳核实定位，无独立设计文档；修复范围限定回合交接的暂停单入口、统一收尾屏障与路由守卫，不改变 Core 侧收尾顺序。
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness/team
files:
  - jiuwenswarm/runtime/service.py
  - jiuwenswarm/runtime/session/model.py
  - jiuwenswarm/runtime/session_lifecycle.py
  - jiuwenswarm/runtime/team_session_lifecycle.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/server/runtime/agent_adapter/interface.py
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - tests/unit_tests/runtime/test_runtime_service.py
  - tests/unit_tests/runtime/test_team_session_lifecycle.py
  - tests/unit_tests/agentserver/test_team_manager_registry.py
  - tests/unit_tests/agentserver/test_interface_interrupts.py
  - tests/unit_tests/agentserver/test_team_helpers.py
---

# 团队暂停双执行与收尾窗口新消息误投递

## 现象

code_team 会话产生约 **210 条空完成**：用例侧记「响应完成，耗时 25–35 s」，
会话侧只有 user 行与约 10 s 后的 `chat.request_completed status=success`，
0 次 LLM 调用、0 条 chat.final——用户消息发出后没有任何回答，且被记成成
功。会话持续 `gate_closed` 3.5 小时不能自愈，直到 worker 重启才恢复。

另有 9 条良性表现：消息恰好在上一轮自然结束时到达，后端 1–3 s 落
success，回答错位到后续回合才交付——同一竞态窗口、无暂停点击也触发。

## 根因

**一句话**：一次暂停点击在后端执行了两次暂停，而新消息的入场与旧回合的收尾
没有对齐——新消息在两次暂停之间（或自然收尾窗口内）被按 follow-up 投递给已
停的 Leader，随后又被迟到的第二次暂停杀掉新回合。

### 实测时间线（agent_server.log / team.log）

1. 08:13:40 用例点停止；14 个成员逐个 `coordination stopping`，08:14:09 第
   一次暂停完成；
2. 08:14:10 下一条 query 到达，08:14:11.876
   `deliver_to_leader_failed:[123023] NativeHarness already stopped`；
3. 08:14:12.364 `follow-up interact reclassified … reason=gate_closed`，
   08:14:12.427 新回合 `stream started round_id=17`（早于第一次暂停收尾 40ms）；
4. 08:14:17.002 迟到的 `interrupt(intent=pause): team session paused:
   runner_paused=True` 把第 17 轮也暂停掉；
5. 此后会话一直 `gate_closed`（重试 21,572 条），每条新消息耗尽重试后被包成
   空成功。

### 结构性成因（三层叠加）

1. **暂停双执行**：`AgentRuntime.cancel_request`（jiuwenswarm/runtime/service.py）
   对团队 pause 先走 generation-aware 的 lifecycle 暂停（`pause_session` →
   `pause_session_runtime`），随后又把**同一个** interrupt 请求转发给
   Adapter（`_process_team_interrupt` → 仅按 `session_id` 的第二次
   `pause_session_runtime`，不带 generation、不绑回合）。两次暂停之间新消息可
   入场开新回合，第二次暂停把它杀掉。
2. **Core 收尾顺序与 Swarm 标记晚期清理天然错位**：Core 的 Runner 在流退出
   的 finally 里先 `finalize()` 再关 interact gate
   （agent-core `core/runner/team_runner.py:436-443`）；Swarm 侧
   `_team_session_has_runtime` 的三个标记（active/pending runtime、stream
   task）要等 producer 自己的 finally（team_helpers.py 流收尾段）才清，晚于
   Core finalize。窗口内 gate 仍开、Leader 已死，follow-up 投递必然
   `NativeHarness already stopped`，重试循环
   （`_deliver_followup_interact_across_boundary`）只认「标记消失」为
   first-run ready，对死 Leader 每 50 ms 重投直至 10 s 上限。
3. **入场不等待**：发消息入口本来就有 `wait_for_request_stop()` 等待机制，
   但 `_begin_request_stop` 只有两处调用（cron `finish_cron_stream`、
   `stop_session_runtime(expected_request_id=...)`），普通 pause 与自然收尾
   都不登记，新消息在收尾期间照常入场。且标记机制为 set+event，两个既有
   持有窗口（cron 交接、围栏 stop）一旦重叠，先结束的一方会提前唤醒等待
   者。同会话连续回合共享同一 `generation`
   （`SessionLifecycleContext` 标识的是进程内代次），所以单靠 generation 校验
   也无法把旧回合与新回合区分开。

### 引入背景

组合性缺陷：`cb10c331b6`（fix(team): push pause states outside chat streams）
把暂停收尾与终态发布迁入 Runtime 后，lifecycle 暂停与 Adapter 转发共存，暴露
出双执行；`wait_for_request_stop` 机制与 gate_closed 静默分支更早存在，分别
覆盖 cron 停止与 shutdown race 的窄场景。任何「收尾中 + 新消息到达」的组合
（显式暂停、stop、自然收尾）都会命中窗口，长期批次的 12 小时高密度收发使触
发概率收敛到 1。

## 修复

三件套，一次消除恶性链（双重暂停杀新回合）与良性 churn（死 Leader 重投）；屏障只由
**有明确主人的收尾**持有，回合释放本身不登记——正常 `team.idle` 后 Leader 流按设计
保持打开等下一条消息唤醒（team_helpers「Do not finalize here: the leader stream
stays open after idle」），把"回合已释放"当收尾会反过来挡住本应由该消息唤醒的流。

1. **暂停单入口**（runtime/service.py + interface.py + team_manager.py）
   - lifecycle 暂停成功后不再把请求转发给 Adapter，直接构造与 Adapter 相同
     形状的 `chat.interrupt_result`（intent=pause、success=True、
     message=团队已暂停）返回；终态发布保持不变。
   - lifecycle 未命中（兜底）时转发前把快照的 generation 与业务回合
     request_id 集合注入 params（`TEAM_PAUSE_FENCE_GENERATION_PARAM` /
     `TEAM_PAUSE_FENCE_REQUEST_IDS_PARAM`）；Adapter 读取并下传。
   - `pause_session_runtime` 新增 `expected_request_ids` 回合围栏：活跃回合
     不在集合内即拒绝暂停。**空集也是围栏**——快照时无业务回合，则任何活跃
     回合都比暂停请求新、必须拒绝；只有 Runtime 无快照（无围栏信息）才不设
     围栏。围栏拒绝后 Adapter 跳过按 `session_id` 的
     `cancel_session_task`，避免误杀新回合的任务。
   - 团队 pause 的协调器清理不再按「会话+代次」全清，改为按**入口快照捕获
     的业务 execution id** 逐个取消（`_drain_team_pause_executions`，复刻
     cron 分支的聚合方式）——快照之后入场的新 execution 不会被本请求触碰。
2. **有主收尾屏障**（team_manager.py + team_helpers.py）
   - `begin_request_stop`/`end_request_stop` 改为计数式（depth 归零才唤醒
     等待者），支持暂停与 cron/stop 嵌套；每个持有者在自己的作用域内
     try/finally 配对，**流工件清理点不释放任何持有**——否则一次流 pop 会
     提前释放外层 pause 或 cron stop 的等待，重开竞态窗口。
   - 屏障持有者仅四类：团队 pause 的整个请求处理期（lifecycle 暂停 → 兜底
     转发 → 定点 drain → 终态发布，异常 finally 释放）、cron 回合交接
     （`finish_cron_stream`，既有）、带围栏的 stop（既有）、以及收到
     `team.completed` 的旧流——该事件明确宣告本流即将退出（Leader 已停、
     gate 要等 Core finalize 才关），分支在发布终态前登记，producer 的
     finally 全部清理完成后释放；生成器局部标志保证 begin/end 严格配对，
     且释放位于**独立 finally**——登记清理（clear pending/active、pop 流任
     务）与闸门释放同在该 finally、清理先行：`release_current_round` 抛异
     常或被取消时异常照常向上可观察，但陈旧登记不会越过开闸时刻存活，
     下一条消息不会被残留的 stream task 误导成已死运行时的续聊，闸门也
     不会悬挂。
     `team.idle` 刻意不登记：其流按设计保留打开、等下一条消息唤醒，登记
     会挡住本应由该消息唤醒的流（检视轮 P1 纠正了最初按 release_round
     挂点的过宽登记）。
   - 发消息入口的等待加 60 s 上限（最差实测收尾：14 成员 ≈29 s + 流退出），
     超时产出显式 `chat.error`。原无限等待同时变为有界，属行为变化。
3. **路由守卫与显式失败**（team_helpers.py）
   - `_deliver_followup_interact_across_boundary` 循环内：**先查屏障再查
     runtime presence**——`pause_session_runtime` 会在暂停请求的 drain 与终
     推送完成前就清掉 runtime 标记，先查 presence 会让已入场的 follow-up
     拿到 first_request_ready=True、在暂停尾段内放行新回合（检视轮 P2）。
     屏障 pending 即停止投递，等待收尾完成后交给恢复/新开回合路径，等待
     复用 60 s 上限且重投预算在等待后重置。守卫只锚定停止标记本身，不看
     stream/回合形态——持久流跨回合驻留与首轮启动期都是「有流无回合」，
     按形态等待会死等一个等这条消息来唤醒的流。
   - 调用方在重分类循环取得新回合启动锁后、复查 presence 前，再等一次
     屏障：边界函数返回与取锁之间可能出现新的暂停/completed 收尾，锁内
     等待（屏障持有者均不需要启动锁，无死锁）确保新回合不与暂停尾段重
     叠；等待超时则结束本次处理并返回可重试错误，不再进入
     `_prepare_first_team_request`。
   - 消息入口的启动锁**取得后同样复查屏障**：入口等待通过后消息可能在
     启动锁上排队，排队期间出现的暂停/completed 收尾不会被发现；锁后复
     查 + 有界等待（超时按入口同款 `chat.error` 结束）保证运行时只在收尾
     结束后才被选择与激活。
   - gate_closed 分支不再静默以空 payload 结束流，改为映射文案
     （`Team is shutting down, please try again later`）显式 `chat.error` +
     终帧。

## 复现场景

时序敏感竞态，最稳定触发方式（08:14 事件的等效操作序列）：

1. 团队会话执行长任务（多成员，收尾慢）；
2. 用例在回合进行中点停止（`chat.interrupt{intent:"pause"}`）；
3. **暂停请求仍在收尾时**（成员逐个 stopping 期间或 stream 退出前）发送下
   一条消息；
4. 观察：新消息先报 `NativeHarness already stopped`、被重分类为新回合，随
   后被迟到的第二次暂停停掉，会话从此 `gate_closed`，后续消息全部空成功。

修复后预期：第 3 步的新消息在入口等待，暂停收尾完成后经恢复路径续跑并收到
回答；全程 `Runner.pause_agent_team` 恰好执行一次。

## 验证

- `pytest tests/unit_tests/agentserver/test_team_helpers.py`：272 通过（含
  新增边界函数排空等待、等待超时、入口等待超时、暂停尾段等待（断言在主测
  试内，经事件同步，bug 存在时必红）、completed 流屏障登记与配对释放、
  清理异常仍释放、idle 不登记、启动锁排队后复查屏障八用例；gate_closed
  用例由静默断言改为显式报错断言）；
- `pytest tests/unit_tests/agentserver/test_team_manager_registry.py`：新增围
  栏用例（错配拒绝、空围栏拒绝、匹配放行且仅此一次 Runner.pause），全套通过；
- `pytest tests/unit_tests/agentserver/test_interface_interrupts.py`：新增围栏
  拒绝后跳过 `cancel_session_task`、空围栏保持校验两用例，`pause_fences` 断
  言化，全套通过；
- `pytest tests/unit_tests/runtime/test_runtime_service.py`：新增兜底围栏注入
  用例（含定点 drain 只取消快照业务 execution 的断言）；既有 pause 顺序用例
  更新为「lifecycle 成功即不转发」，全套通过；
- `pytest tests/unit_tests/runtime/test_team_session_lifecycle.py`、
  `tests/unit_tests/runtime/test_web_cron_cancellation.py`、
  `tests/unit_tests/server/extensions/test_expert_team_busy_chat.py`：全部通
  过（受影响定向套件合计 355 + team_helpers 272）。
- 端到端复跑未执行：触发窗口依赖真实 Runner 收尾时长，以单测时间线断言替
  代；建议在后续长稳回归中观察空完成与收尾窗口错位两类记录是否消失。

## 遗留风险

- 屏障窗口内 `begin_round` 会按既有 `_assert_session_not_deleting` 语义拒绝
  新回合（RuntimeError「session is stopping its previous request」）——与
  stop/cron 停止窗口现状一致；消息主路径已被入口等待覆盖。
- 无终态事件的静默流退出窗口（Core 生成器不发 team.completed 直接结束）不
  设屏障：消息仍会短暂重投后经重试自愈；`team.interact.failed` 分支同属
  此类，本次未登记。completed 与 idle 之外的窄场景维持现状。
- 60 s 等待上限为经验值（最差收尾实测 ≈29 s + 流退出与 drain）；若未来成员
  规模显著扩大需同步调整 `_TEAM_TEARDOWN_WAIT_TIMEOUT_SEC`。
- gate_closed 由静默改为显式失败后，用例侧统计会看到明确的发送失败而非
  「假成功 + 无答复」，对以 success 计数的自动化脚本属预期口径变化。
- Core 侧「finalize 后才关 gate」的顺序未改动：单仓视角该顺序合理（收尾完
 成才拒新），对齐责任在 Swarm 入场侧；若未来 Core 提前关 gate，可相应收窄
  Swarm 守卫。
