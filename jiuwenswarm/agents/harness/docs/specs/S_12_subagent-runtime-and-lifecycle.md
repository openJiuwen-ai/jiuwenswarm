# S_12 子代理持久运行时与生命周期

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_12 / subagent-runtime-and-lifecycle |
| 关联模块（agent-core 仓，机制主体） | `openjiuwen/harness/subagent_runtime/`（`control.py` 1227 行 `SubagentControl`、`instance.py` `SubagentInstance`、`models.py` 状态机、`config.py` 常量、`session_manager.py`、`__init__.py` 事件常量 re-export）、`openjiuwen/harness/tools/subagent/_control_registry.py`（属性挂载协议）、`openjiuwen/harness/tools/subagent/subagent_tools.py`（runtime 六工具）、`openjiuwen/harness/deep_agent.py`（释放三入口 @3027 / @3097） |
| 关联模块（jiuwenswarm 仓，compat 与投影） | `jiuwenswarm/agents/harness/common/tools/subagent_compat.py`（`CompatibleSubagentControl` + `install_subagent_control_compat_patch`）、`jiuwenswarm/server/runtime/agent_adapter/subagent_stream.py`（3A 事件投影 / 批槽 / 子行持久化）、`interface_deep.py`（`release_subagent_runtime_for_session` @3468、`_session_has_live_subagent_runtime` @3437、compat patch 装配 @9860） |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**子代理创建后的持久运行时**——per-parent-session 的编排器、串行 worker、状态机、容量与驱逐、超时体系、事件广播：

- `SubagentControl`（spawn / wait / resume / close / cancel_all / 容量槽 / LRU 驱逐 / closed 记录裁剪 / activity 节流）
- `SubagentInstance`（串行 turn worker；interrupt 与 cancel 二分；cleanup shield）
- 状态机七态与 `is_final()` 判定
- 超时三级体系（turn 2400s / completion 1800s / wait clamp 1h）
- 事件三常量（`subagent_updated` / `subagent_message` / `subagent_activity`）的唯一 re-export 入口
- `_control_registry` 属性挂载协议与三释放入口
- jiuwenswarm compat 层：`CompatibleSubagentControl`（resume idle 修复 + 子流镜像 + reasoning 合并）与 `install_subagent_control_compat_patch`
- jiuwenswarm 投影层：3A 事件 → Web `chat.subtask_update` / legacy 批槽 / 子 transcript 落父 session

不管：

- 实例创建与 factory 分发 → [[S_11]]
- 授权与审批（scope 判定 / Grant / Future）→ [[S_13]]
- 通道选择（runtime 六工具 vs task_tool vs session 工具的互斥装配）→ [[S_14]]；本 spec 只管 runtime 被选中后的**行为**
- 父 Agent 会话 idle TTL 策略本体（`interface_deep` 只按"有 live subagent 槽位在用"判保活）
- checkpointer 存储格式（agent-core 会话持久化域）

## 不变量

- **N1（per-parent-session 编排器）**：`SubagentControl` 一父会话一实例，经属性挂载协议 `_subagent_controls`（dict[parent_session_id → SubagentControl]）挂在 DeepAgent 上。`get_subagent_control` 懒创建 + `hydrate()` + `merge_persisted_records()`；已存在则 `set_parent_session` 复用。**不是**全局单例——跨父会话不共享容量。
- **N2（spawn 二段式）**：`spawn`（control.py @169）先 reservation（容量预留）再 `commit/rollback`——预留必须二选一收口，**不允许悬挂预留**。失败路径必须 rollback 释放槽位。
- **N3（容量与 LRU）**：`_acquire_slot` @728 容量不足时按 `enable_lru_eviction=True` 走 LRU 驱逐。默认容量 `max_subagents=10` / `max_concurrent_running=10`（config.py）。closed 记录裁剪至 `max_subagents × 2`（`_trim_closed_records` @977）——防内存膨胀。
- **N4（串行 turn）**：`SubagentInstance` 一个实例同时只有一个 active turn。`_turn_claimed` **先于** semaphore 等待置位（@198）——防等待期间被误判 idle 而驱逐。
- **N5（状态机七态）**：`PENDING_INIT / RUNNING / COMPLETED / INTERRUPTED / ERRORED / CLOSED / NOT_FOUND`。`is_final()` = {COMPLETED, ERRORED, CLOSED, NOT_FOUND}——**INTERRUPTED 非终态**（可 resume）。`SubagentStatus` 是 frozen dataclass：状态迁移 = 整体 `replace`，禁止原地改。
- **N6（interrupt / cancel 二分）**：`_settle_cancelled_turn` @258——interrupt → INTERRUPTED（保留会话，resume 回放 `SubagentCreateOptions`：thinking / model_name / model_tier）；cancel → 视为放弃。
- **N7（turn 超时与熔断）**：turn 级超时 `TURN_TIMEOUT_S_DEFAULT = 2400.0`（config.py @18，覆盖实测 1694s + 余量）；超时 → `code="TIMEOUT"` → ERRORED。cleanup 必须 `asyncio.shield`（`_finalize_turn` @371）——turn 协程被取消时清理仍执行。
- **N8（close 禁 RUNNING）**：`close` @503 拒绝关闭 RUNNING 态子代理——必须先 cancel。运行中直接 close 会留下孤儿 worker。
- **N9（resume 先校验）**：`resume` @457 先经 `checkpointer.session_exists` 校验，不存在直接报错——不盲恢复。
- **N10（wait 超时 clamp）**：wait 超时 clamp 到 `[WAIT_TIMEOUT_MS_MIN, WAIT_TIMEOUT_MS_MAX = 3_600_000]`（1 小时）；等待用 `FIRST_COMPLETED` 竞速（@243）。
- **N11（事件三常量唯一入口）**：`SUBAGENT_UPDATED_EVENT_TYPE` / `SUBAGENT_MESSAGE_EVENT_TYPE` / `SUBAGENT_ACTIVITY_EVENT_TYPE` 只能从 `openjiuwen.harness.subagent_runtime` 包入口 import——jiuwenswarm 侧 `subagent_stream.py` 的 ImportError 兜底（`except ImportError` 三常量硬编码）仅为旧 dest-stable SDK 兼容，新代码不得依赖兜底值。
- **N12（compat 类替换协议）**：`install_subagent_control_compat_patch`（jiuwenswarm subagent_compat.py @220）通过**类属性替换**生效——`_control_registry.SubagentControl = CompatibleSubagentControl`（registry 自行构造实例，无法逐实例注入）。`mirror_child_stream` 是**类级**开关（同理由）。该 patch 在 `_build_subagent_rail`（interface_deep @9860）装配；patch 失败 → `enable_runtime` 降级 False（runtime 整体关闭而非裸奔 stock 类）。
- **N13（wait 卡片超时豁免）**：runtime 六工具的 wait 卡片 `_attach_call_timeout(wait_card, None)`（subagent_tools.py @469）——合法等待可跨多个父轮 turn，不适用通用工具超时。
- **N14（镜像流白名单与合并）**：子 chunk 镜像到父 session 流仅三类 `_MIRROR_CHUNK_TYPES = {llm_reasoning, tool_call, tool_result}`；`answer`（→ chat.final）/ `content_chunk`（→ chat.delta）/ `error` / 子 `task.*` 留在子会话。镜像帧必剥 `_MIRROR_STRIP_KEYS = (task_id, parent_request_id)`（防父流幽灵任务段）并盖 `stream_source_id = subagent_id`。llm_reasoning 按 200 字符 / 300ms 合并（`_REASONING_FLUSH_CHARS` / `_REASONING_FLUSH_INTERVAL_S`）、单子代理帧上限 `_REASONING_FRAME_CAP = 2000`。镜像写失败只 warning——**镜像永不打断子代理 turn**。
- **N15（镜像尾部与状态清理）**：turn 结束（`is_turn_finished`）必 `_release_mirror_tail`（合并中的短尾不滞留到下一 turn）；`_evict_from_memory` finally 必 `_drop_mirror_state`。`CompatibleSubagentControl.resume`：restored 且仍 PENDING_INIT → 置 idle（`SubagentStatus.completed()` 公开态）——OpenJiuWen 用终态 turn status 表示"活跃可输入"，COMPLETED 映射公开 idle。
- **N16（投影不覆写权威状态）**：`project_subagent_updated_for_web` 投影 `legacy_status`（closed→completed/error、idle→completed/error、其余 starting）仅供 Web SubtaskProgress 兼容，**不回写** runtime 权威 status。并行批槽（`resolve_subagent_parallel_fields`）按 parent_session_id 记 0-based index/total，完成即从槽中移除、槽空即清 dict——父会话批槽泄漏即清。
- **N17（子行落父 session）**：`persist_subagent_transcript_message` / `persist_subagent_activity` / `persist_subagent_roster_history` 全部把子事件写为**父 session** 下的行（`session_id=parent_session_id` + `subagent_id` + `channel_id="subagent"` + `mode="subagent"`），request_id 以 `{subagent_id}:{seq}` / `{subagent_id}:activity:{task_id}:{seq}` / `subagent-roster-{subagent_id}:{revision}` 区分。三写全走 `_safe_persist`（吞异常只 warning）——持久化失败不得打断流转发。
- **N18（释放三入口全吞异常）**：DeepAgent `_release_interaction_subagents`（@3027，parent_ended）、`aclose`（@3097，agent_closed，调 `release_all_subagent_controls`）、`SubagentRail.uninit`（rail_uninit）三处释放路径均吞异常（noqa BLE001）——释放失败不得掩盖主流程。`interface_deep.release_subagent_runtime_for_session`（@3316）额外先 `clear_subagent_progress_batch(sid)` 再释放，逐级向 child adapter 转发。
- **N19（保活探测容错）**：`_session_has_live_subagent_runtime`（@3437）按 session 从 `_subagent_controls` 取 control、以 `capacity().used > 0` 判定有 live 槽；无该属性（stock agent）视为无 live 槽；`capacity()` 抛异常**按占用处理**并 warning（保守取向——TTL 不得驱逐可能持有子代理的 Adapter，与 [[S_14]] N12 一致）。

## 接口契约

### `SubagentControl`（agent-core，经 compat 类替换后实际类型为 `CompatibleSubagentControl`）

| 方法 | 契约 |
|---|---|
| `spawn(...)` | N2 二段式；任务描述截断至 `_TASK_DESCRIPTION_MAX_LEN = 2000` |
| `wait(...)` | N10 clamp + FIRST_COMPLETED |
| `resume(subagent_id)` | N9 校验 → compat 层 idle 修复（N15） |
| `close(...)` | N8 禁 RUNNING |
| `cancel_all()` | 全量取消（释放路径内部调用） |

### runtime 六工具（agent-core subagent_tools.py）

`Spawn` / `Wait` / `List` / `SendInput` / `Close` / `Resume`——工具卡与通道互斥装配归 [[S_14]]；本 spec 管其触发的 control 行为。

### `SubagentRuntimeConfig`（agent-core config.py）

| 字段 | 默认 | 语义 |
|---|---|---|
| `max_subagents` | 10 | N3 容量上限 |
| `max_concurrent_running` | 10 | 并发 RUNNING 上限 |
| `enable_lru_eviction` | True | N3 驱逐开关 |
| `activity_queue_size` | 256 | activity 队列容量 |
| `activity_throttle_ms` | 500.0 | activity 消费节流（control.py @853-903 gate） |

### jiuwenswarm 投影 API（subagent_stream.py）

| 函数 | 契约 |
|---|---|
| `try_handle_subagent_chunk(chunk_type, payload, parent_session_id)` | 3A 事件分流入口：updated → 投影 + roster 落库 + 返回 web_payload；message → 仅落库（不 web emit）；activity → 落库 + activity payload；其余 → (False, None) |
| `clear_subagent_progress_batch(parent_session_id)` | N16 批槽清理（释放路径必调，N18） |
| `clear_all_subagent_progress_batches()` | 测试专用全清 |

### activity 持久化门槛

`SubagentActivity.is_persistable`（models.py @364）仅对 `tool_call` / `tool_result` / `error` 为 True——活动流只持久化高价值事件。

## 数据结构

- `_control_registry` 挂载：`DeepAgent._subagent_controls: dict[parent_session_id → SubagentControl]`
- 状态迁移图：`PENDING_INIT → RUNNING → COMPLETED | ERRORED`；`RUNNING → INTERRUPTED`（可 resume）；`RUNNING → TIMEOUT 熔断 → ERRORED(code="TIMEOUT")`；任意 → CLOSED（先 cancel）；查询不存在 → NOT_FOUND
- `SubagentCreateOptions`（models.py @88）：`thinking` / `model_name` / `model_tier`，resume 时回放

## 执行路径

1. 通道（[[S_14]]）触发 spawn → `get_subagent_control`（N1 懒创建）
2. reservation → commit → instance 串行执行 turn（N4/N7）
3. turn 结束 → 状态广播 `subagent_updated`；transcript 走 `subagent_message`；activity 走 gate（节流）+ `subagent_activity`
4. jiuwenswarm adapter 收 chunk → `try_handle_subagent_chunk`（N16/N17 投影与落库）
5. 父会话结束 / agent 关闭 / rail uninit → 三入口释放（N18）

## 异常分支

- turn 超时：`_on_turn_timeout` → code="TIMEOUT" → ERRORED；cleanup shield 兜底（N7）
- cancel 协程取消：interrupt/cancel 二分落 INTERRUPTED / CANCELLED（N6）
- checkpointer 无会话：resume 直接报错（N9）
- 容量探测异常：按占用处理（N19，保守取向）
- 投影 / 落库异常：`_safe_persist` 吞掉（N17）
- 镜像写失败：warning 吞掉（N14）

## 边界 case

- 子代理已被驱逐后镜像状态残留：`_drop_mirror_state` finally 清理（N15）
- 短尾 reasoning 无后续 chunk 触发合并窗口：turn 结束钩子 `_release_mirror_tail`（N15）
- 多个 page 子代理并发镜像压垮父 websocket：200 字 / 300ms 合并 + 帧上限 2000（N14）
- 父会话 idle 但有 live subagent：`_session_has_live_subagent_runtime` 保活（N19）
- stock DeepAgent（无 `_subagent_controls`）：兼容为无 live（N19）

## 修改检查单

改 `subagent_runtime/` / `subagent_compat.py` / `subagent_stream.py` 前：

- [ ] spawn 失败路径 rollback（N2）？
- [ ] `is_final()` 集合与 INTERRUPTED 语义（N5）未被改？
- [ ] compat 类替换（N12）与 patch 失败降级是否仍闭环？
- [ ] 镜像白名单 / 剥键 / 合并参数（N14）变更是否评估父 websocket 压力？
- [ ] 投影持久化（N17）的 channel/mode 标记未漂移（Web 路由依赖）？
- [ ] 三释放入口（N18）是否仍覆盖新增的释放路径？
- [ ] 事件常量引用是否都走包入口（N11）？

## spec 关系

- 上游：[[S_11]]（实例创建）
- 下游：[[S_13]]（子代理 invoke/stream 生命周期挂 scope 清理）、[[S_14]]（通道选择决定 runtime 是否启用）
- 跨域：debug 捕获（[[S_14]]）复用 runtime 之外的 invoke/stream 路径；harness 域 [[S_08]] 熔断是父工具循环层，不涉子代理内部 turn
