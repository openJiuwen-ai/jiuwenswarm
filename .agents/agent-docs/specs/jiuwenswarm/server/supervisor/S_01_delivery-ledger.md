---
id: S_01_delivery-ledger
title: 交付台账与占位卡契约（P6 静默吞回复兜底）
type: S
author: nawenyu139
author_note: feishu-msg-defects-20261106 批次 P6 实施
modules:
  - jiuwenswarm/server/supervisor
files:
  - jiuwenswarm/server/supervisor/runtime_supervisor.py
  - jiuwenswarm/server/supervisor/delivery_ledger.py
  - jiuwenswarm/server/ipc/protocol.py
  - jiuwenswarm/common/utils.py
---

# 交付台账与占位卡契约（P6 静默吞回复兜底）

| 项 | 值 |
| --- | --- |
| 类型 | spec |
| 关联模块 | `jiuwenswarm/server/supervisor`、`jiuwenswarm/server/ipc`、`jiuwenswarm/common` |
| 最近一次修订日期 | 2026-11-07 |
| 关联 feature | — |
| 修复依据 | feishu-msg-defects-20261106 批次 P6（.omo/plans/feishu-msg-defects-20261106/P6-v9-design.md） |

## 范围

supervisor 进程（Front 侧）对已受理未交付请求维护交付台账：派发接受起 600s 管线零帧（P7 活性语义）即经网关 socket 直发 `chat.error` 占位卡，消除「请求生命周期结束零出站无兜底」的静默吞回复。控制面请求（`__cron__`/`__health_check__` 渠道、`interrupt_` 前缀 request_id）零用户可见回复属设计使然，**登记时刻直接跳过**（P7）。台账只存投递身份与游标，**不存用户正文**。占位卡出站与 worker 状态完全解耦（FAILED 粘滞态照发）；`FRONT_LOOPBACK` 模式本契约不覆盖（启动重载跳过重放，遗留条目当场过期审计）。

## 交付台账

- 落盘：`get_delivery_ledger_dir()` = `{root}/run/delivery-ledger/<request_id>.json`（尊重 `JIUWENSWARM_DATA_DIR`；jiuwenswarm/common/utils.py）。
- 字段（delivery_ledger.LedgerEntry）：scope（request_scope 五元组）/channel_id/user_id/app_id/agent_ref_json/session_id/request_id/dispatch_id/worker_instance/sequence 游标/last_visible_output_ts/deadline（墙钟）/placeholder_state/dump_ref（诊断转储关联路径）/registered_at/send_failures/identity_metadata（仅投递身份键交集，非用户正文）。
- 落盘分级：注册/deadline/状态转移**同步写**（temp+os.replace 原子写）；sequence 游标与 last_visible_output_ts **批量写**（50 帧/2s 阈值，`_flush_batched`）。终态条目（sent/retracted）的迟到白名单帧只更新内存簿记，deadline/游标随批量阈值落盘——**终态帧禁逐帧 fsync**（该链跑在 supervisor 事件循环线程上，逐帧 fsync 阻塞同 loop IPC 收发）。
- 登记豁免（P7）：`register()` 对 channel_id ∈ `CONTROL_CHANNEL_IDS`（`__cron__`/`__health_check__`）或 request_id 前缀 ∈ `CONTROL_REQUEST_ID_PREFIXES`（`interrupt_`）的请求返回 None 不建条目——这些请求无用户可见回复语义，登记只会恒发占位卡。
- Front 启动重载：`DeliveryLedger.load()` 逐条读盘，墙钟 deadline 原样保留，由 sweep 判到期。

## 计时与白名单

- 计时起点=派发接受时刻（`_delivery_register`，deadline=now+600s 墙钟，`_DELIVERY_IDLE_DEADLINE`）。
- **活性语义（P7）**：deadline 以「管线帧流」为活性源——forwarder send True 的**任意帧**（含非白名单工具/进度帧）重置 deadline=now+600s；纯工具执行的长轮次零可见帧属健康形态（2026-11-07 实锚：流式活跃轮 10 分钟推进 4925 帧被误判超时）。占位卡只对「管线整体静默 idle_seconds」报警——worker 卡死/崩溃后帧流停止，deadline 到期照发。
- 用户可见白名单={chat.final, chat.error, chat.ask_user_question}，流式模式 chat.delta 也计（`STREAM_VISIBLE_EVENT_WHITELIST`）；白名单帧额外更新 last_visible_output_ts（活性重置不更新该字段——可见静默时长仍可观测）。
- 占位卡 sent 后 deadline 停止重置；sent 为吸收终态，真实回复迟到时保持 sent（不重发、不改状态；终态条目不参与 sweep 到期判定，deadline 值惰性）。

## sweep 生命周期

- supervisor 自有 30s 独立任务（`_DELIVERY_SWEEP_INTERVAL`；新常量，不复用 watchdog 5s 周期、不挂靠 watchdog 循环）。
- 创建点：Front 启动屏障（run_until_ipc_ready）、attach_gateway_connection、dispatch_parsed_request（幂等 `_ensure_delivery_sweep`）；**仅 `_finish_stop` 终止时取消**；不进入 `_start_side_tasks`/`_cancel_side_tasks` 面，不随 worker 代际（generation 递增、恢复、重启、FAILED）取消。
- 到期动作：gateway 已 attach → 直发占位卡（forwarder，不等重连）；未 attach → `pending_flush`，attach 后 sweep 周期补推；T+24h 无 attach → `failed` 过期审计留痕（`_DELIVERY_EXPIRE_SECONDS`）；连续发送失败 ≥3 次 → `failed` 留痕。

## 占位帧线缆五要素

构造先例 `server/gateway_push/wire.py` `build_server_push_wire`（chunk 分支）。占位帧按五要素构造即被网关 push 分支受理（agent-gateway agent_client.py:385 现有分支，无需网关代码改动；受理关系以契约测试实锚）：

1. 顶层 metadata 含 `E2A_WIRE_SERVER_PUSH_KEY`（唯一真源 `openjiuwen_protocol.e2a.constants`，值 `"_jiuwenswarm_server_push"`；jiuwenswarm common/e2a/constants.py 系 re-export 别名，两仓同源同值）；
2. 顶层 request_id = `rid + ":p6"`（线缆顶层 request_id 字段，线缆无独立 id 字段；网关 Message.id 取该值，流隔离键）；
3. 顶层 session_id / channel_id；
4. payload = chat.error（含原始 rid 与重发建议文案）；
5. metadata 携 request.metadata 投递身份键（`DELIVERY_IDENTITY_METADATA_KEYS`，网关 message_handler `_DELIVERY_IDENTITY_METADATA_KEYS` 的镜像，测试断言两侧同值）与 app_id。

身份键实测结论（2026-11-06 实施窗回写）：飞书入站构造 Message.metadata 携 feishu_chat_id/feishu_open_id/open_id/chat_type（feishu_connect.py:3783-3794），经 im_pipeline 透传为 AgentRequest.metadata，app_id 入站已透传（message_handler.py:3952 网关注释自证）。传输成功判定=forwarder send True；飞书端渲染成败不在 supervisor 可观测面。

## 身份型重放契约 KIND_SUBSCRIBE_BY_ID

- 常量：`execution.subscribe_by_id`（jiuwenswarm/server/ipc/protocol.py，PROTOCOL_VERSION 不变）。
- supervisor→worker 请求载荷：仅身份字段（channel_id/user_id/app_id/agent_ref_json/session_id）+ worker_instance + after（sequence 游标），不含用户正文。
- worker 反查键=`(scope[:4], request_id)`（与 `WorkerEventStore._active` 同键构造）；miss → 回执 `status=miss`；worker_instance 不匹配（worker 重启丢保留帧）→ `status=history_required`；命中 → `status=ok` 后按 request_id 逐帧 `KIND_WIRE` 回放（携 wire_id，`replayed` 标记）。重放帧 send 全程持有 `publication_lock(record.scope)`（与 live 帧经 `publication_scope` 解析出的同一 scope 锁）——重放与同 scope 实时帧到达顺序有全序（同 `_subscribe` 重放先例）。
- worker 侧实现：dispatcher `_subscribe_by_id`（保留帧读取走 `WorkerEventStore.replay_by_request`）。
- supervisor 回执处理：miss/history_required → 按不可恢复处理，sweep 到期直接占位卡（`_delivery_unrecoverable`）；回放帧走 `_forward_wire` 正常出站与 KIND_DELIVERY_ACK 回执链；delivered=False → 回落台账重试或转 pending_flush。
- 触发点两处：attach_gateway_connection 与 Front 启动重载。
- 路由重绑（主路径）：重放受理时 `_routes[dispatch_id]` 重绑为当前 attach 的 (ws, send_lock)；server-push 标记仅路由缺失时经 `_wire_route` 回落。
- **重放路由清理=新增清理代码**（`_delivery_note_forward`）：重放终态（chat.final/chat.error）送达或 delivered=False 时 pop 该路由；非沿用 dispatch finally（:413-415 请求生命周期）、`_fail_forwarding`（失败路径）、`_fail_pending`（清空全部失败路径）既有清理点。

## 占位状态机与互斥

- `pending（已注册未到期）→ pending_flush（到期但 gateway 未 attach）→ sent（占位卡已送达，吸收终态）`；`pending/pending_flush → retracted（真实回复送达后取消未送达占位，纯簿记无线缆动作）/ failed（T+24h 过期审计或连续发送失败留痕）`。
- **终态吸收守卫**：`transition` 拒绝从终态迁出（返回 False 并日志留痕）；sweep 在 send 前复查条目最新状态（`ledger.entry`），快照到 send 之间已转终态的条目直接跳过——防「真实回复已送达（retracted）后占位卡仍发出且被快照竞态覆盖成 sent」。
- **终态回收**：终态条目经 `reap_terminal`（sweep 周期调用）在保留窗（`_REAP_SECONDS`=3600s，自 terminal_at 起算，旧盘档回退 registered_at）后删除内存与磁盘副本，回收前日志留痕（state/age/request_id）；内存与 `{root}/run/delivery-ledger/*.json` 不随请求数无界增长。
- sent 条目在真实回复迟到时保持 sent（不重发、不改状态）；**本契约无撤回帧**——已送达的飞书卡不做线缆撤回。
- 互斥：真实回复送达取消未送达占位卡；占位卡送达后迟到的真实回复仍送达（不压制，保内容不丢）。
- 幂等：sweep 对 sent/retracted/failed 终态条目不重发；同一到期项仅成功送达一次。dispatch_id:worker_instance=多对一，台账条目内字段换算 (dispatch_id, sequence) ↔ (worker_instance, sequence)。

## 验收面

单测 A~K（tests/unit_tests/agentserver/test_delivery_ledger_placeholder.py）：A 在线到期推卡；B 白名单重置；C 重启重载墙钟判定；D pending_flush 补推；E 重放三分支；F 分级落盘；G 互斥；H 重启后非流式重放送达（重绑主路径+回落双通道+新清理点）；I FAILED 粘滞态占位卡必达（出站与 worker 状态解耦；sweep 不随代际取消）；J 迟到回复送达（流隔离端到端）；K 契约测试（五要素经网关 push 分支受理→Message.id=rid+":p6"→飞书接收者可解析；占位 chat.error 无真实回复副作用；push 键常量两仓同源；wire 解码版本固定 openjiuwen_protocol）。
