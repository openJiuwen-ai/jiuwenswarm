---
id: B_20261117_0452_team-attach-subscription-inflight
title: Team 旁听订阅被记为在途请求，导致会话一直忙碌、无法从 Team 切回 Agent
type: B
author: null
author_email: null
author_note: 创建时仓库未配置 Git user.name 与 user.email，未推断身份。
modules:
  - jiuwenswarm/server/runtime/agent_adapter
  - jiuwenswarm/agents/harness/team
  - jiuwenswarm/runtime
files:
  - jiuwenswarm/server/runtime/agent_adapter/team_helpers.py
  - jiuwenswarm/agents/harness/team/team_manager.py
  - jiuwenswarm/runtime/service.py
  - tests/unit_tests/agentserver/test_team_helpers.py
  - tests/unit_tests/runtime/test_session_mode_switch.py
designs: []
design_note: 修复既有忙碌判定对旁听订阅的误判，不改变 mode.set 与归档的契约，无独立设计。关联实施记录见 F_20261116_1710_web-session-mode-switch。
---

# Team 旁听订阅被记为在途请求修复记录

## 现象与影响

- Web 会话用 `mode.set` 从 Agent 切到 Team 成功，之后 Team → Agent 一直返回 `SESSION_BUSY`，等多久都一样；会话内没有任何对话在执行。
- 触发条件：Web 进入 Team 模式时前端自动发送的旁听请求（`chat.send` 带 `attach_only: true`，`services/teamAttach.ts`）。不发该请求时切换 1 秒内成功。
- 同一误判也作用于所有读取"在途请求"的地方：`team_session_has_active_work`、定时任务回调的归属校验（`cron_completion_is_current`）、按请求停止运行时。

## 关联设计与当前契约

- `TeamManager.begin_request` 的契约是"标记一轮 Team 对话在其轮次建立之前处于在途"，由轮次释放（`release_round`）或流结束时的 `end_request` 清除。
- 旁听订阅的契约（`_consume_attach_only_stream`）是"只观察事件流，不提交输入，不创建轮次"。

## 根因与修复决策

- 根因：入口 `_process_team_message_stream_locked` 对所有 Team 请求都调用 `begin_request`，包括旁听订阅。旁听订阅没有轮次，只有连接断开时才走到 `finally` 里的 `end_request`，所以它在整个订阅期间一直是"在途"。
- 传导：`mode.set` 的忙碌判定第一条是"有挂起的对话请求且不属于停靠"。`team_session_has_parked_request` 的第一步是"有在途请求即不算停靠"，于是返回忙碌。
- 修复一（本记录）：入口处旁听订阅不再调用 `begin_request`。在源头修，使所有读取在途请求的地方同时得到正确结果。
- 修复二（同一工作区的先行改动）：`team_session_has_parked_request` 对已登记的旁听者不再要求存在流任务。两处都需要：只有修复二时仍被在途标记挡住；只有修复一时没有流任务的旁听者仍不算停靠。
- 可诊断性：`switch_session_mode` 拒绝时记录是哪一个信号（`Session mode switch refused as busy: ... reason=...`）。此前 `SESSION_BUSY` 不带原因，无法从日志定位。
- 未采用：在 `team_session_has_parked_request` 里忽略属于旁听者的在途项。它只修一个读取方，在途标记的语义仍然是错的。

## 回归证据与适用范围

| 验证 | 环境 | 结果 |
| --- | --- | --- |
| 隔离实例复现（独立数据目录与端口，真实 Gateway → AgentServer → Worker）：建会话 → `mode.set` 到 Team → 旁听请求 → `mode.set` 回 Agent | 修复前 | 20 秒内 6 次全部 `SESSION_BUSY`；日志原因 `pending_chat_requests=[<旁听请求>]`，诊断显示 `inflight=True round=False stream=False`、该请求已登记为旁听者 |
| 同上，对照组不发旁听请求 | 修复前 | 1 秒内成功 |
| 同上 | 修复后 | 1 秒内成功，无拒绝记录 |
| 往返：Agent → Team（旁听）→ Agent → 发一条消息 → Team → Agent | 修复后 | 4 次切换均无忙碌重试；消息被正常受理（实例未配置模型，得到预期的模型未配置错误） |
| `test_attach_only_stream_is_not_an_inflight_turn`（经真实入口 `process_team_message_stream`） | 单测 | 修复后通过；撤销修复一后失败 |
| Team helpers / manager / 归档 / 模式切换 / 请求归一化相关套件 | 单测 | 698 通过 |

- 未覆盖：浏览器界面上的实际操作；带真实模型的 Team 轮次进行中与结束后的切换（进行中应当拒绝，属预期）。

## 防错模式与资料同步

- 既有测试 `test_mode_switch_with_team_observer` 直接调用 `add_waiter` 登记旁听者，没有经过入口，所以在缺陷存在时也通过。涉及"请求是否算忙"的测试应当经过真实入口，或同时断言入口写下的全部标记。
- 排查 `SESSION_BUSY` 时先看拒绝原因日志，不靠阅读判定代码推断。

## 遗留与后续

- 切回 Agent 后，原来的旁听订阅仍保持连接，直到页面断开。目前未见副作用（后续 Agent 对话与再次切换均正常），未单独处理。
