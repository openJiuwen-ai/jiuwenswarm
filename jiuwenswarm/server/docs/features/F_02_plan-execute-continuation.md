# Plan Execute 服务端幂等续跑（issue 5223）

日期：2026-11-05。关联：issue 5223（PR !7578 CR-2 评审建议立项）、关联特性 issue #4942。

Web plan_execute 批准原依赖前端补发执行消息开启执行轮：审批 chat.send 超时（>15s）
或断连时前端撤回乐观标记不补发，已批准的计划永不执行。本特性将续跑兜底移到服务端
（方案 A 全面接管）：批准轮结束且 plan→normal 检出后，server 代发执行消息（普通用户
消息），前端删除补发逻辑。

调度点在 `_check_post_process_plan_exit`（server/handlers/_default.py）的 plan 退出
确认分支：仅当 resume 载荷 `source=confirm_interrupt` 且 answers 含
`PLAN_EXECUTE_OPTION_VALUES` 取值时登记（TUI approve / 跳过 / 修订不含该取值，不触发；
mode 未退出即 rail 拒绝或 payload 校验失败，天然不触发）。记账键 `(session_id,
request_id)`，request_id 即审批 interrupt 的 tool_call id；进程内记账，重启丢失窗口
等价旧行为（无兜底），不劣化（同 `_shared.py` CR-9 取舍）。同一审批轮重复触发（双
事件、对账并发）凭记账去重，续跑恰好一次；双标签页重复批准由既有
`PermissionResponseLedger` 先行去重。

登记后进入 ~3s grace window：窗口内该会话到达非 interrupt-resume 的普通 `chat.send`
即取消续跑，由该消息充当执行消息——兼容滚动发布期旧前端缓存包的补发，零双发。取消
面还包括显式中断 `chat.interrupt`（cancel/supplement 意图，`handlers/chat.py` 挂点，
评审 CR-3）与 session delete / rewind（同 `_plan_exited_sessions` 清理位点）；客户端
断连**不**取消（服务端兜底正是为此）。已领取未代发的竞态窗口由取消墓碑在代发前
复核拦下（双保险，同 CR-3）。窗口期满未
取消则代发：合成 `AgentRequest(is_stream=True)`，mode 取
`resolve_request_runtime_mode(原请求).normal_mode`，project_dir/work_mode/agent_scope
抄自原请求，身份/记忆白名单键（顶层 user_id + routing 三元组 + enable_memory）与
thread_id/chat_id 归档维度一并抄送（评审 CR-2：不抄则配额策略回退 fallback 额度、
记忆开关失真、归档维度丢失），query/display_query 为服务端双语执行文案
（`plan_approval.PLAN_EXECUTE_PROMPT_OUTPUT`，与前端 i18n `plan.executePrompt` 逐字一致，
语言沿 agent 配置）；`source=plan_execute_continuation` 标记来源（不进入
`_should_record_user_history` 跳过名单，用户历史照常落盘，重连/刷新经历史恢复可见）。
执行轮经 `agent.process_message_stream` 驱动，chunk 与回显帧走 `server.send_push`
（proactive_adapter 配方）；前端在 `chat.processing_status` 处理器按
`source=plan_execute_continuation` 回显用户气泡（heartbeat automation 回显同款）。

会话忙（`is_deep_agent_executing_for_session`，理论上批准轮已收尾不应发生）按 2s×3 次
退避重试后放弃并留日志，用户可手动补发。执行轮自身的失败（`process_message_stream`
中途抛异常）补推 `chat.error` + `chat.processing_status{is_processing:false}` 终态帧：
续跑由 server 发起，前端没有本地 `chat.send` 的 15s 超时兜底，缺终态帧会停在
processing=true（2026-11-09 评审补充）。后台续跑 task 由模块级集合持强引用防 GC
（asyncio 官方要求保存 `create_task` 返回值），done 回调自清理，取消语义不变。

TUI 不受影响（纯 approve 同轮继续）；不新增协议方法与请求参数；agent-core 零改动
（`restore_mode_after_plan_exit` 保留 plan_slug 的两阶段设计即本特性的前置）。

契约见[计划审批规约](../../agents/harness/docs/specs/S_04_plan-approval.md)（N3/P1）。
本地验证：`tests/unit_tests/agentserver/test_plan_execute_continuation.py`（识别真值表/
幂等/状态隔离/grace 取消/合成请求字段/身份抄送/中断取消墓碑/busy 避让）+ 前端回显与撤发源断言
（`channels/web/frontend/tests/planExecuteContinuation.test.mjs`）；手工 E2E 覆盖断网
重连、审批延迟、双标签页。
