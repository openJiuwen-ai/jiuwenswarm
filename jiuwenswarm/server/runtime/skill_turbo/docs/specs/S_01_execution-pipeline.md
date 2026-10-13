# SkillTurbo 执行管线规约（execution-pipeline）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_01 / execution-pipeline |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（主战场：agent.py / executor.py / skill_turbo_tools.py；environment.py / planner.py 仅涉执行链一侧） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs，由现状代码逆向固化） |

> 代码定位约定：本文一律用"文件 + 方法名"定位；随附行号必然随代码演进漂移，以方法名为准。

## 范围与边界

### 本规约管什么

SkillTurbo 技能加速引擎的**端到端执行循环**与其上的**执行状态管理**：

- 端到端链路 P1-P12：工具触发 → SkillTurbo 实例化 → Environment 装配 → inputs 组装 → plan 确定 → 流式执行 → 交付收尾 → trace 收尾。
- 两对执行入口（`execute_plan` / `execute_plan_stream` / `resume_stream`）的公开契约与 `execute_plan_stream` 内部 10 步序列。
- `_execute_node_stream` 双生产者流式管线与节点调度模型（skill_code 自驱）。
- 执行状态：task_states 三层存储、node_artifacts 生命周期、关键执行态字段。
- 退出路径全景 E1-E13 与终态收口双轨制。
- 并发模型：每请求新实例、deepcopy 隔离、LLM Semaphore 懒初始化、确定性 tool_call_id。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 技能发现/扫描/meta.json/入口文件链/planner 路由决策/工具装载细节 | [[S_02]] |
| validator 三 profile / 运行期沙箱 / 审批收口 | [[S_03]] |
| PlanNode 子类契约（11 条契约/12 回调的节点侧定义）、节点 IO 约定、LLM 调用参数语义 | [[S_04]] |
| 动态包注册 / 门面符号 / tool_utils | [[S_05]] |
| HITL tcid 机制/发卡/恢复/取消/持久化状态机细节 | [[S_06]] |
| 4 层降级决策 / fallback 子代理内部 / 契约判定细节 | [[S_07]] |
| 事件全集/顺序/缓冲/转发的协议级定义 | [[S_08]] |
| adapter 装配 / ContextVar 桥 / WS-gateway / 双仓部署 | [[S_09]] |

## 不变量

任意时刻，执行管线必须满足下列 15 条断言：

1. **N1 请求隔离**：每次 `skill_acceleration_exec` 调用新建 SkillTurbo / SkillTurboEnvironment / SkillTurboExecutor 三实例；实例属性不跨请求。唯一跨实例共享状态是 environment 的 3 个类级缓存（`_scan_cache` / `_external_scan_cache` / `_tool_info_last_export`）。
2. **N2 外部覆盖内置**：`_scan_external_turbo_skills` 注册晚于 `_scan_skills_dir`；同名 Skill 外部版本胜出（environment.py `_load`）。
3. **N3 plan_code 形态唯一**：executor 只接受 `from {allowed_prefix}xxx import root` 单条 import 形态（validator 与 `_safe_import` 前缀白名单双重强制）；root 必须是 PlanNode 实例。细节见 [[S_03]]。
4. **N4 AbortError 全链不吞**：rail → use_tool → PlanNode.run/run_stream → `_execute_node_stream` → `execute_plan_stream` → SkillTurbo.run_stream → adapter，每一层都有显式 re-raise 分支；任何一层不得把 AbortError 转 node_error / chat.error / complete。传播详表见 [[S_06]]。
5. **N5 resume 重放三跳过**：completed stage 跳真实执行（after 回调仍收集产物）；completed / in_progress stage 跳重复 task.start；in_progress stage 抑制开始横幅。task_id 按 display name / index 对齐复用，不新发 UUID。
6. **N6 产物继承 hash 门禁**：`_inherit_node_artifacts_for_replay` 仅当持久化 `plan_code_hash` 与当前重放 plan_code 哈希一致才继承；无哈希的旧记录视为匹配（fail-open）。
7. **N7 checkpointer 键隔离**：resume_ctx / node_artifacts 的所有持久化读写经 `set_skill_turbo_id` 使用 `{card.id}__skill_turbo` 隔离键，与 DeepAgent 主链路键空间分离。
8. **N8 session_id 稳定性**：中断与恢复两侧必须解析到同一 session_id（inputs.session_id 优先，conversation_id 回退）；不一致时断点按随机 UUID 落库、恢复必然 miss（executor 仅 warning，不 fail-fast）。
9. **N9 工具按全局 tool_id 现取 + ContextVar 路由**：`get_tool_function` 每次从 `Runner.resource_mgr` 现取实例（并发下后注册者覆盖是已知行为）；携带 session 语义的工具（send_file_to_user）必须走请求级 ContextVar 解析，不得依赖实例字段。
10. **N10 交付真话契约**：p10 `delivery_status=="failed"` 时工具层必须返回 `success=False`；无 P10 骨架且无 `send_file_status=="sent"` 确认时，交付文案只能是中性提示，不得宣称文件已发送。
11. **N11 workspace_base 显式性**：`inputs["workspace_base"]` 只能来自 `request_metadata["output_dir"]`，且仅当其非空时设置；缺失时不设置、无任何 agent workspace 兜底。
12. **N12 缓存 mtime 失效**：environment 类级扫描缓存 key 含 max .py mtime；mtime 计算失败返回 0.0 等价于禁用缓存强制重扫。
13. **N13 确定性 tool_call_id**：同一次执行内相同顺序的 `(tool_name, canonical_args)` 调用产生相同 id 序列（`_setup_execution_context` 每次重置 `_tool_call_counter`）；request_id 混入 hash，故 resume 跨请求时精确命中必然失效，由 name+idx 回退命中兜底——回退命中是预期路径，不是降级路径。
14. **N14 task_states 读取统一入口**：所有 task_states 运行期读取必须走 `_live_task_states()`（实例 holder 优先，回退 ContextVar）；新增读取点不得直接读 ContextVar。
15. **N15 终态收口双轨制**：executor 只负责 `plan.finished` + `complete` 两类终帧；`chat.final` / `chat.invocation_paused` 归 adapter。AbortError 分支（E8）executor 不发任何终态帧。

## 接口契约

### 两对执行入口（公开签名）

```python
# agent.py — SkillTurbo
async def run_stream(self, task: str, inputs: dict, request_id: str, channel_id: str) -> AsyncIterator[AgentResponseChunk]
async def resume_stream(self, *, plan_code: str, inputs: dict, request_id: str, channel_id: str,
                        pending_tool_call_id: str, user_input: Any,
                        task_states: list[dict] | None = None) -> AsyncIterator[AgentResponseChunk]
async def run(self, task: str, inputs: dict) -> Any          # 非流式包装，生产不使用

# executor.py — SkillTurboExecutor
async def execute_plan(self, plan_code: str, inputs: dict) -> Any                       # 非流式
async def execute_plan_stream(self, plan_code: str, inputs: dict, request_id: str, channel_id: str) -> AsyncIterator[AgentResponseChunk]
def set_pending_resume(self, *, expected_tool_call_id: str, user_input: Any, task_states: list[dict] | None = None) -> None
async def use_tool(self, tool_name: str, **kwargs) -> Any
async def call_llm(...) -> str; async def stream_llm(...) -> AsyncIterator[str]
async def fallback(...); async def fallback_stream(...)
def validate(self, plan_code: str) -> list[str]
@property node_artifacts -> dict
```

入口语义表：

| 入口 | 形态 | 超时 | 异常语义 | 生产地位 |
|---|---|---|---|---|
| `execute_plan` | 非流式 | 整体 `asyncio.wait_for` 300s（`ExecutorConfig.execution_timeout`） | 各类异常全部 re-raise（E13） | 不使用（保留为完整执行面） |
| `execute_plan_stream` | 流式 | **无整体超时**（底层 LLM/工具各有自身超时） | 按 E1-E12 分型收口 | 生产主入口 |
| `resume_stream`（agent 层） | 流式 | 同上 | 跳过 planner，直接 `set_pending_resume` + `execute_plan_stream` 全树重放 | HITL 恢复唯一入口 |

两条 executor 入口共享 `_prepare_root_node`（安全校验单点维护，不得复制第二份校验）。

### execute_plan_stream 内部 10 步序列（executor.py `execute_plan_stream`）

1. `_merge_env_config_to_inputs`：注入 `skill_root` / `skill_name`，均带 "not in merged" 保护（resume inputs 已有则不覆盖）。
2. `env.register_tools`：静态工具幂等加载 + send_file 工具每请求刷新（先 remove 旧 tool_id 再 add）+ `_export_tool_info` 导出。
3. `_setup_execution_context`：`create_agent_session`（`set_skill_turbo_id` 隔离键）+ 8 组 ContextVar 绑定（session/request_id/channel/task 队列/task_states/current_task_holder/send_file/interactive_ask）+ trace 创建 + `_tool_call_counter` 重置。
4. 产物分支：`not _resume_replay` → `_clear_stale_node_artifacts()`（清 session 持久层残留）；else → `_inherit_node_artifacts_for_replay(plan_code)`（hash 门禁，fail-open）。
5. yield `plan.started`——**必须最先**（前端依赖它建根节点）。
6. `_prepare_root_node`：validate（`PlanCodeValidator`）→ exec 受限命名空间加载 → `_extract_root_node` → **deepcopy** → `_bind_node_callbacks`（12 个回调）→ 读 display_names。校验失败走 E5、加载失败走 E6（chat.error + complete 后 return，不发 plan.finished、不上抛）。
7. `_initialize_pending_tasks(root)`：首跑为每个 depth==1 sub_plan 预置 pending；resume 走 `_take_resume_task_states` → `_restore_task_states_from_snapshot`（按 display name/index 对齐复用 task_id）。随后立即 drain 首个 task.update 快照。
8. 主循环 `async for chunk in self._execute_node_stream(root, ...)`：
   - `chat.error` → 记 plan_failed 标记；
   - `_bubble_progress` 横幅事件：先 flush 全部缓冲再透传（不进缓冲桶、不带 task_id）;
   - 非缓冲事件：先 flush 全部缓冲后透传；
   - 缓冲事件（`chat.delta` / `chat.reasoning`）：按 `(source_id, event_type)` 分桶；首 chunk 立即发（低首字延迟）、3s 到期 flush（`_SKILL_TURBO_STREAM_FLUSH_INTERVAL_SECONDS`）、事件类型切换 flush 对方桶、markdown 流式增量处理 + 悬空 code fence 补全、`last_emitted` 跨桶存活防相邻拼接。
9. 流末：flush 全部桶 → `plan.finished`（final:True，status 依 plan_failed）→ `complete`（is_complete=True, payload=None）。
10. finally：session 非空且 holder 非空 → `_persist_node_artifacts`（**必须在 reset context 前**，否则 session ContextVar 已失效）→ `_reset_execution_context`（token 逆序恢复）→ `_execution_inputs={}` → `_resume_replay=False` → `_finish_trace`。

异常分派（穿插于 8-10 之间）：TimeoutError → E7；AbortError → 直接 raise（E8）；FallbackContractError / FallbackLimitExceededError → 不发 chat.error，仅 finished(failed)+complete 后 raise（E10/E11）；其余 Exception → E12。

### _execute_node_stream 双生产者模型（executor.py `_execute_node_stream`）

- **生产者 1** `produce_node_output`（asyncio.Task）：`node.started` → `node.run_stream(inputs)` 逐 chunk `enqueue_chunk` → `node.finished`；内部异常入队 + `None` 哨兵终止。
- **生产者 2** `drain_session_stream`（asyncio.Task）：读 `session.stream_iterator()` → `parse_stream_chunk` → session_event chunk（LLM 相关事件走此通道）。
- 两者汇入共享 `output_queue`（异常对象与 None 哨兵也走队列）；消费循环按异常分型转发（详见 E 表）。
- `_task_event_drain_lock`（asyncio.Lock）串行化 `enqueue_chunk` 与 `drain_session_stream` 的排空；`enqueue_chunk` 持锁内：先 flush 延期 task 生命周期事件 + drain task 事件，再按 chunk 形态构造 event / node_delta chunk（携带 current_task_id）。
- task.start 经 deferred 列表延期到开始横幅入队后，防止双协程排空时 start 反超横幅；task.complete 入队前必须先 flush deferred start（防 complete 抢在 start 前）。
- 外层 finally：cancel 两个生产者 task 并 gather（return_exceptions=True）。

### 节点调度由 skill_code 自驱

- executor **不做拓扑调度**：节点遍历顺序、循环、并发（`asyncio.gather` + `stream_llm_collect`）全部由编排节点 `_execute_stream` 内的 `execute_subplan` 调用决定。
- `PlanNode.execute_subplan` 钩子固定序列：`before_subplan_execute` → `_maybe_skip_subplan_execute`（resume completed 短路，返回 `_resume_skip_result`）→ 真实 `subplan.run_stream` → `after_subplan_execute`。AbortError 路径**不调 after**（in_progress 状态得以保留进快照）。
- depth 语义：root=0、二层=1；**只有 depth==1 参与 task 事件发射与 resume 跳过判定**。
- `run` / `run_stream` 是不可覆盖的模板方法：AbortError / FallbackContractError 直通上抛，其余异常进 `_fallback_callback`。
- 并发分桶：`concurrent=True` 时 call_llm/stream_llm 生成 `stream_source_id`（`skill_turbo:{node}:{hex8}`）注入 llm 事件，前端按桶分组。

### inputs 单一载体总线

- 整树共享**同一** `merged_inputs` dict 引用；skill_code 节点读 `inputs[...]` 并自行写回结果字段（业务约定，executor 不做数据路由）。
- env 注入字段仅 `skill_root` / `skill_name`，均 "not in merged" 保护。
- skill_name 同步链：planner 路由命中 → `env.set_skill_name(skill)` → `_sync_skill_name_to_inputs` 写入 inputs → resume 重放经 "not in merged" 保护还原。中断时 inputs 已持久化进 resume_ctx，故恢复侧技能名不丢失。

### 并发模型

- **每请求新实例**：SkillTurbo / Environment / Executor 三实例随请求创建、随请求丢弃（N1）。
- **deepcopy 隔离**：plan_code import 出的 root 是模块级单例，并发请求共享同一对象；`_prepare_root_node` 必须 deepcopy 后再绑回调——否则后启动请求覆盖先启动请求的回调、产物写错 executor。
- **LLM Semaphore 懒初始化**：Executor 构造发生在 agent 初始化阶段，事件循环可能尚未确定；`asyncio.Semaphore`（`llm_concurrency_limit`，<=0 不限）延迟到首次使用时创建，绑定运行时事件循环。
- **`_tool_call_counter` 每次执行重置**：保证重放确定性（N13）。

## 数据结构

### task_states 三层存储

| 层 | 位置 | 生命周期 | 用途 |
|---|---|---|---|
| 运行态（双写） | `_task_states_var`（ContextVar）+ `_task_states_holder`（实例属性） | 单次执行 | **双写原因**：`asyncio.create_task` 复制 ContextVar，子协程读写与主协程漂移（跨协程失联）；实例 holder 跨协程共享兜底 |
| 中断快照 | `resume_ctx["task_states"]` → session state → checkpointer | 跨请求/跨实例 | HITL 断点组成（`_snapshot_task_states` deepcopy 导出） |
| 只读消费 | `_live_task_states()` | 运行期 | 统一读取入口：holder 优先回退 ContextVar（N14） |

单条记录字段：

```python
{
  "task_id": "task_{uuid8}",        # 首跑生成；resume 复用原值（防前端叠第二套 Stage）
  "task_content": display_name,     # resume 对齐键之一
  "task_index": int,                # resume 对齐键之二
  "source": "skill_turbo",
  "status": "pending|in_progress|completed|failed",
  # in_progress 时: "start_time"；终态时: "end_time", "duration_ms", 可选 "error"
}
```

状态机与特殊转移：

```
pending ──_before_subplan_execute──▶ in_progress ──_after_subplan_execute──▶ completed / failed
   ▲                                     │
   └──── _park_later_in_progress_tasks ──┘   （更靠后 in_progress 收回 pending，单 running 保证）

特殊转移：
- AbortError 中断：保持 in_progress（execute_subplan 不调 after 回调）→ 进中断快照
- resume 重放：in_progress stage 完整重入（不发重复 task.start，横幅抑制）
- resume 重放：completed stage 静默跳过（skip result，不发 start/complete）
- 业务失败：结果 dict 含 export_status=="failed" 或 status=="error" → task.complete(failed)
```

其中业务失败判定函数为 `_is_subplan_business_failure`；单 running 保证由 `_park_later_in_progress_tasks` 实施；AbortError 保持 in_progress 是 resume 快照能正确续跑的前提。

写入时机表：

| 时机 | 写入点 | 动作 |
|---|---|---|
| 初始化 | `_initialize_pending_tasks` | 首跑全量 pending；resume 从快照 restore（复用 task_id） |
| start | `_update_task_state_on_start`（经 `_before_subplan_execute`） | status=in_progress + start_time；resume 时 completed/in_progress 跳过重复 start |
| complete | `_update_task_state_on_complete`（经 `_after_subplan_execute`） | 终态 + end_time/duration_ms/error；resume 时 completed 跳过 complete（仍收产物） |
| park | `_park_later_in_progress_tasks` | 更靠后 in_progress 收回 pending |
| 动态节点 | `_get_or_create_task_state` | 运行时新出现的子节点兜底建 state |
| 中断快照 | `_snapshot_task_states`（use_tool AbortError 分支） | deepcopy 导出进 resume_ctx |
| resume 恢复 | `_take_resume_task_states`（一次性取出）→ restore | task_id 对齐复用 |

### node_artifacts 生命周期

- **收集**：`_after_subplan_execute` → `_collect_node_artifact`——成功读 `__artifact__`（并 pop），异常读 `_partial_artifact`，写入内存 holder `_node_artifacts_holder[plan_name]`。
- **落盘**（两时机）：use_tool AbortError 分支提前落盘（中断保产物）+ 流末 finally `_persist_node_artifacts`（session 仍有效时）；经 `save_node_artifacts` 写 session 持久层（`{card.id}__skill_turbo` 键，N7）。
- **清理/继承**：非 replay 首跑 `_clear_stale_node_artifacts`（防上一轮残留产物被误复用）；replay `_inherit_node_artifacts_for_replay`（plan_code_hash 门禁 + fail-open，N6）。
- **内容约束**：holder 与持久层只存摘要/小结构/路径，不存大文件本体。
- **独立 session**：`_clear_stale_node_artifacts` / `_inherit_node_artifacts_for_replay` 用独立 session 而非主 session——`post_run` 会 close_stream，副作用将影响主 session 的 drain（session 生命周期交错约束）。产物三操作（clear/inherit/persist）全部 fail-open：异常仅 warning/debug 日志，不阻断主流程。
- E8 中断路径不落盘，holder 留内存由下轮 replay 继承（与 N6 配合防产物丢失）。

### 关键执行态字段（executor 实例）

| 字段 | 谁写 | 谁读 | 清理时机 |
|---|---|---|---|
| `_execution_inputs` | 入口赋值 | use_tool 落盘 resume_ctx / `_persist_node_artifacts` / `get_workspace_base_path` | finally 置 `{}` |
| `_current_plan_code` | `_setup_execution_context` | use_tool `save_resume_ctx` / `_persist_node_artifacts` 哈希 | 随实例丢弃 |
| `_tool_call_counter` | `_next_tool_call_id` | 同左（重放确定性 idx） | 每次执行重置 `{}` |
| `_pending_resume` | `set_pending_resume` | `_take_resume_task_states` / `_consume_pending_resume_input` | 命中消费置 None；未命中保留 |
| `_resume_replay` | `set_pending_resume` 置 True | 跳过判定/横幅抑制/产物分支 | finally 置 False |
| `_task_states_holder` | 初始化/`_get_or_create_task_state` | `_live_task_states` / `_snapshot_task_states` | 初始化开头 clear；随实例丢弃 |
| `_current_task_id_holder` | `_set_current_task_context` / after 回调 | `_current_task_id` | 每任务 complete 置 None |
| `_node_artifacts_holder` | `_collect_node_artifact` / replay 继承 | `artifact_holder` 属性 / `_persist_node_artifacts` | 新请求新实例天然清空 |
| `_current_trace` | `_setup_execution_context` 创建、use_tool/fallback 追加 | `_finish_trace`（仅日志） | 每次执行重建 |

### evolver trace（预留未启用）

- executor 内部执行级追踪 `ExecutionTrace`（plan_code_hash / input_keys / start_time / end_time / duration_ms / success / error / fallback_count / node_execution_order）在 `_setup_execution_context` 创建、`use_tool` / `_record_fallback_call` 追加、`_finish_trace` 收尾——**仅打日志**。
- `_send_trace` 是空壳（只有 logger.debug，不调用 `evolver.record`）；`SkillTurboEvolver` / `PlanExecution` 生产路径零调用（agent.py 构造 executor 不传 trace_collector），`get_optimization_hints` raise NotImplementedError。
- `evolver.ExecutionTrace`（节点级字段）与 `executor.ExecutionTrace`（执行级字段）**同名不同构**——两套类型独立演化。接线 evolver 前必须先统一两者，否则按任一方消费都会字段错位。

## 执行路径全景（P1-P12）

- **P1 工具触发**：外层 DeepAgent ReAct 主循环 LLM 自主调用 `skill_acceleration_exec`（skill_turbo_tools.py `skill_turbo`，`@tool stateless=True`）；`query` 为 LLM 对任务的忠实总结（不得扩写）。前置两块拦截：`[TEMP-TEMPLATE-BYPASS]` 正则（模板路径类请求引导走 skill_tool）、`[REGION-EDIT-BYPASS]` 关键词（PPT 选区/编辑语义，主防线在 SkillTurboPromptRail 排除提示词）。工具卡 `resilience.timeout_s=None` 豁免默认 300s 超时。
- **P2 实例化**：`adapter.build_skill_turbo_config()`（tool_cards / model_client / fallback_handler / card / llm_concurrency_limit / vision / audio / sys_operation / permissions）→ `SkillTurbo(config)` → Environment → Planner → Executor（不传 trace_collector，evolver 预留未启用）。
- **P3 Environment 装配**（environment.py `__init__` + `_load`）：`_resolve_skill_root`（config skills_dir → `resolve_agent_registered_skill_dirs()` 首目录 → 空串）→ `ensure_runtime_facade()`（幂等注册 `skill_turbo_runtime` 门面）→ 注册 model_client/tool_cards → `_scan_skills_dir` → `_scan_external_turbo_skills`（同名外部覆盖内置，N2）。
- **P4 inputs 组装**（skill_turbo_tools.py）：query / conversation_id / session_id / request_id / channel_id（读 ContextVar `get_current_request_metadata()`，防 adapter 实例属性并发覆盖）/ user_id / chat_id / effective_project_dir / workspace_base（仅 metadata.output_dir 非空时设置，N11）/ metadata 整体。resume 场景补读 `get_skill_turbo_resume_answers()`。
- **P5 plan 确定**：首跑 planner.py `plan`——`match_skill`（流式 LLM + 重试 1 次，confidence < 0.6 或未知 skill → None）→ `env.set_skill_name` → `build_plan_code`（入口文件四级优先级，收口于 skill_meta.find_skill_root_file）→ `from {module} import root` 字符串；无匹配 → agent.py 抛 `SkillTurboNotHandled` 上层降级。随后 `_sync_skill_name_to_inputs`。resume 不走 planner，直接用持久化 `resume_ctx["plan_code"]`。
- **P6 执行入口分派**：`execute_plan`（非流式 wait_for 300s，生产不用）或 `execute_plan_stream`（流式无整体超时）。两入口共享 `_prepare_root_node`。
- **P7 节点执行与回调**：`root.run_stream(inputs)`（模板方法）→ skill_code 定义 `_execute_stream`；编排节点经 `execute_subplan` 钩子序列驱动子树；`_bind_node_callbacks` 注入 12 个回调（has_tool / use_tool / call_llm / stream_llm / fallback / fallback_stream / extract_json / log / before_subplan_execute / after_subplan_execute / should_skip_subplan_execute / should_suppress_subplan_start_banner）。
- **P8 use_tool 单次工具调用**（executor.py `use_tool`，6 步）：① `_next_tool_call_id` 确定性 tcid（N13）；② `_consume_pending_resume_input`（精确命中或 name+idx 回退命中 → 注入 user_input）；③ `build_tool_ctx` → before_tool_call rail 链（`_skip_tool` 标记 reject 直接返回 rail 注入结果；resume approve 命中时 `clear_resume_ctx`）；④ `env.get_tool_function(tool_name)` 按全局 tool_id 现取（N9，优先 `tool.invoke(inputs)` 协议，回退 `tool.run(**kwargs)`）；⑤ 执行 + after_tool_call；⑥ AbortError → `save_resume_ctx(session, plan_code, inputs, pending_tool_call_id, task_states)` → 上抛（断点唯一持久化点）。
- **P9 HITL 中断透传**：executor flush task 事件后 raise（E8）→ 工具层 `extract_tool_interrupt` 取 TIC 存 ContextVar → 返回 `build_skill_turbo_hitl_result` → 外层 StreamEventRail 把 tool_result 改写为 TIE → harness 原生 HITL 三件套。细节归 [[S_06]]。
- **P10 resume**：两条路径——A（主流）adapter `_try_skill_turbo_resume`（stale guard → load_resume_ctx → resume_in_flight 去重 → 新建 SkillTurbo → `resume_stream`）；B 外层 LLM 再调工具 → 工具层读 resume answers → `resume_stream`。重放走三跳过（N5）；清理语义：仅 `release_checkpoint`（正常完成或 NotHandled）时 `clear_resume_ctx` + `turbo_session.post_run()`，瞬时失败保留断点。
- **P11 交付收尾**：流转发（chunk 过滤 + `_prepare_parent_stream_output` 反向映射 event_type + `parent_session.write_stream`）；`_wrap_skill_turbo_result`——success 时追加产物摘要 + 停止提示（PPT 走 p10_delivery 骨架 + pending summary ContextVar；非 PPT 认 `send_file_status=="sent"`，否则中性提示）；失败时 error + 摘要。`_ppt_delivery_failed_error` 强制 success=False（N10）。SkillTurboDeliverySummaryRail 在外层 tool_result 后流式发出 PPT 交付总结。
- **P12 trace 收尾**：见"evolver trace（预留未启用）"——`_finish_trace` 仅日志，`_send_trace` 空壳，evolver 链路当前不发生。

## 异常分支与退出路径（E1-E13）

13 条退出路径。列：触发条件 / 事件收口（executor 层 yield 的终态帧）/ 状态清理 / 是否上抛 / 终态标志。

| # | 退出路径 | 触发条件 | 事件收口 | 状态清理 | 上抛 | 终态标志 |
|---|---|---|---|---|---|---|
| E1 | 正常完成 | `_execute_node_stream` 自然结束 | flush 全桶 → plan.finished(final) → complete | 落盘 artifacts；reset context；resume_replay=False | 否 | complete(is_complete=True) |
| E2 | 节点失败被 fallback 吸收 | plan_node.run_stream 内 fallback 成功 | 同 E1（降级输出继续流；task.complete 按 result 状态） | 同 E1；fallback 契约结果必须写回 inputs（防下游读旧值连锁失败至超限） | 否 | complete |
| E3 | 节点普通异常（未被 fallback 吸收） | 消费循环收到非特殊型 BaseException | flush task.* → yield node_error(chat.error) → **continue**（流不终止） | — | 否（错误事件化） | 由后续流决定 |
| E4 | chat.error 业务事件透传 | payload event_type==chat.error | 记 plan_failed → 流末 plan.finished(failed,error) + complete | 同 E1 | 否 | complete |
| E5 | 校验失败 PlanCodeValidationError | `_prepare_root_node` validate | chat.error + complete 后 return（**无 plan.finished**） | finally 落盘（holder 空则跳过；首跑已提前清 stale） | **否（吞）** | complete |
| E6 | 加载失败 PlanCodeLoadError | exec / 根节点提取失败 | 同 E5 | 同 E5 | **否（吞）** | complete |
| E7 | 执行超时 asyncio.TimeoutError | 外层 except 分支 | chat.error + plan.finished(failed) + complete | 同 E1 | 否（吞） | complete（分支当前近似死代码：流式无整体 wait_for） |
| E8 | HITL 中断 AbortError | rail / 工具内部 / fallback validator 抛出 | **无任何终态帧，直接 raise**；终态三件套归 adapter | `save_resume_ctx`（plan_code/inputs/tcid/task_states 快照，**唯一写入点**）；in_progress 保留；不落盘 artifacts（holder 留内存下轮继承） | **是** | adapter: chat.invocation_paused(is_complete=True)；executor 禁发 chat.final（N15） |
| E9 | 用户取消 CancelledError/KeyboardInterrupt | 消费循环分型 | **不 flush task 事件，直接 raise**（曾被误判 completed 的防线：显式分型） | 不写 resume_ctx；不落盘 artifacts | **是** | 无（上层处理） |
| E10 | Fallback 契约失败 FallbackContractError | handler 抛出（spawn 失败/契约自证失败/result_validator 拒绝，均 fail-closed） | 内层：flush deferred + drain 全部 task.*（含 in_progress stage 的 complete(failed)，防前端面板停滞）→ raise；外层：plan.finished(failed,error) + complete（**不发 chat.error**，防前端 activeRequestIdRef 抢跑）→ raise | 同 E1（finally） | **是** | complete + 上抛（agent.py 包 SkillTurboNotHandled → 外层降级） |
| E11 | Fallback 超限 FallbackLimitExceededError | `_check_fallback_limit`（count >= max_fallback_count=3） | 同 E10 | 同 E10 | **是** | 同 E10 |
| E12 | 未知异常 Exception | 外层兜底 except | chat.error + plan.finished(failed) + complete | 同 E1 | **否（吞）** | complete |
| E13 | 非流式 execute_plan 异常 | TimeoutError→ExecutionTimeoutError；FallbackLimitExceededError re-raise；Exception re-raise | 无事件通道（非流式） | finally reset context / finish trace | **是** | 异常即结果；agent.py 统一包 SkillTurboNotHandled（AbortError 除外） |

**矩阵透视**（4 条契约）：

1. **事件收口完整性**：E1-E7、E10-E12 均有 complete 帧；E8 故意无（暂停语义）；E9 无（取消语义，收口最薄弱的路径）。
2. **是否上抛**：E8/E9/E10/E11 上抛，E13 非流式全部上抛；E5/E6/E7/E12 吞（事件化收口）。
3. **resume_ctx 写入**：仅 E8。
4. **task 事件 flush**：E9 是唯一不 flush task 事件的异常路径。

**流式吞异常与非流式 re-raise 的语义分叉（现状，如实固化）**：E5/E6/E12 在流式入口把异常就地消费为 chat.error + complete，agent 层不包 `SkillTurboNotHandled`——外层 LLM **失去降级信号**，仅见工具"正常返回"（失败信息在文本里）；而同类异常在非流式入口（E13）re-raise 并由 agent.py 统一降级。新增退出路径必须先明确归属哪一语义（事件化吞 / 上抛降级 / 暂停），不得默认。

## 边界 case 清单

| # | case（场景 / 现行为 / 风险） | 测试覆盖 |
|---|---|---|
| 1 | **流式入口校验失败不降级**（E5/E6）：plan.started 已发出后仅 error+complete 直接 return，无 plan.finished，agent 层无 NotHandled——前端靠 complete 兜底，外层 LLM 无降级信号。 | test_exit_path_frames（E5/E6 帧序列） |
| 2 | **E9 不 flush task 事件**：取消瞬间前端任务面板可能滞留 in_progress，依赖上层终止流收口。 | test_exit_path_frames（E9 用例） |
| 3 | **`_extract_root_node` 兜底脆弱**：root 变量缺失时取"最后一个 PlanNode 实例"（依赖 namespace dict 迭代顺序）；多实例且无显式 root 变量才报校验错误。 | test_executor_plan_code_import（三分支用例） |
| 4 | **类级缓存无淘汰**：`_scan_cache` / `_external_scan_cache` / `_tool_info_last_export` 只增不减；skills 目录集合变更后旧 key 永久驻留（长驻进程内存缓慢增长面）。 | 无（结构性风险记录） |
| 5 | **tool_info.json 多实例写同默认路径**（`~/.jiuwenswarm/tool_info.json`）：内容去重 + 原子写，但不同 agent 实例注册不同工具集时最后写者胜，CodeGen 可能读到别家 schema。 | test_environment_export_tool_info（写盘协议面） |
| 6 | **session_id 缺失断点失联**：executor 仅 warning 不 fail-fast；checkpointer 落随机 UUID，恢复侧按 request.session_id 必然 miss。 | 无（S_06 #28 同项） |
| 7 | **`_finish_trace` 静默返回**：`_setup_execution_context` 部分失败（如 session 创建抛异常）时 `_current_trace` 为 None，duration/fallback 统计丢失（可观测性缺口）。 | 无（可观测性缺口） |
| 8 | **ContextVar 绑定失败仅 warning**：send_file / interactive_ask 绑定失败后工具行为降级但流程继续，故障面转移到运行期。 | 无 |
| 9 | **E7 超时分支近似死代码**：流式路径无整体 wait_for；若未来引入整体超时，必须同步审查 flush / task 事件 / 终态帧三件收口。 | 无（近似死代码；引入超时须同步补三件收口测试） |
| 10 | **resume 回退命中依赖 tcid 末段 idx 解析**：`_next_tool_call_id` 格式变更会使 `_parse_call_idx_from_call_id` 回退命中静默失效，形成二次中断循环。 | test_resume_tool_call_alignment（idx 比较用例组） |
| 11 | **metadata 缺 output_dir 时 workspace_base 不设置**：pipeline 输出路径完全依赖 skill_code 内部回退逻辑，行为不在本模块内静态可判定。 | test_context_binding_channels（workspace 派生条件）+ test_entry_gates_and_honesty（入口负例） |
| 12 | **deepcopy 假设**：PlanNode 树若持有不可深拷贝资源（锁/连接/文件句柄），`_prepare_root_node` 的 deepcopy 直接爆炸——节点资源约束见 [[S_04]]。 | 无（资源约束类） |
| 13 | **全局 tool_id 覆盖竞态**：并发下"后注册者覆盖"；当前仅 send_file 经请求级 ContextVar 兜住，其他携带实例状态的工具是隐患（N9）。 | 无（send_file 请求级 ContextVar 兜底为生产逻辑，无专测） |
| 14 | **主/临时 session 生命周期交错**：产物 clear/inherit 用独立 session 的原因是 post_run close_stream 副作用；改动 session API 时该约束易碎。 | 部分：test_hitl_resume_react（isolated session 语义间接锁定） |

## 修改检查单

1. 改执行循环（`execute_plan_stream` 步序）→ 对照 E1-E13 收口对称性：每条路径的 complete / plan.finished / chat.error 组合是否仍完整。
2. 新增或修改异常分支 → 先答三问：AbortError 怎么过（不得吞，N4）；task 事件 flush 了吗；终态帧发了吗。
3. 改 task_states → 检查双写（ContextVar + holder）、resume 快照（`_snapshot_task_states`）、park 单 running 语义，以及所有读取点走 `_live_task_states()`（N14）。
4. 改 node_artifacts → 检查三约束：非 replay 首跑清 stale、replay 按 hash 继承、落盘走独立 session + `{card.id}__skill_turbo` 键。
5. 新增退出路径 → 明确归属 E 表语义（事件化吞 / 上抛降级 / 暂停零帧），并确认 resume_ctx 是否写入（当前仅 E8）。
6. 改节点遍历或回调绑定 → 检查 depth==1 假设（task 事件/跳过判定只对二层生效）与 execute_subplan 钩子序列完整性。
7. 改 `_prepare_root_node` / deepcopy → 检查 PlanNode 树不可持有不可深拷贝资源；校验逻辑不得复制第二份。
8. 改 tool_call_id 生成（`_next_tool_call_id`）→ 连带检查 `_parse_call_idx_from_call_id` / `_parse_tool_name_from_call_id` 与 resume 回退命中链（N13）。
9. 改 inputs 注入 → 保持 "not in merged" 保护（resume 不覆盖持久化值）+ workspace_base 显式性（N11）。
10. 改并发模型 → LLM Semaphore 懒初始化必须绑定运行时事件循环；不得引入跨请求实例状态（N1）。
11. 改 session 创建 / checkpointer 读写 → session_id 稳定性（inputs.session_id 优先）+ `set_skill_turbo_id` 键隔离（N7/N8）。
12. 改 use_tool → AbortError 分支 `save_resume_ctx` 四元组（plan_code / inputs / tcid / task_states）完整性；瞬时失败不得清断点。
13. 改流式缓冲 → 四约束不得破坏：首 chunk 立即发、3s 到期 flush、非缓冲事件前 flush、悬空 code fence 补全。
14. 改终态事件 → 终态收口双轨制（N15）：executor 不发 chat.final / invocation_paused；E8 分支保持零终态帧。
15. 改 Environment 装配 → 外部覆盖内置顺序（N2）与缓存 mtime 失效（N12）不得破坏。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_02]] | 本 spec 的 P3/P5 只描述装配与 plan 确定在执行链上的位置；技能发现/扫描/入口文件优先级/路由决策细节归 S_02。 |
| [[S_03]] | 本 spec 的 P6 `_prepare_root_node` 消费 validator 结果（E5/E6）；validator 三 profile、沙箱命名空间、`_safe_import` 白名单细节归 S_03。 |
| [[S_04]] | 本 spec 从 executor 视角引用 12 回调与 execute_subplan 钩子序列；PlanNode 子类契约、节点 IO 约定、`__artifact__` / `_partial_artifact` 语义归 S_04。 |
| [[S_05]] | P3 的动态包注册（`ensure_turbo_package`）、门面符号（`ensure_runtime_facade`）细节归 S_05。 |
| [[S_06]] | 本 spec 的 E8/E9/P9/P10 只固化执行侧行为（raise 时机、断点四元组、三跳过）；tcid 生成细节、发卡、恢复分流、取消、持久化状态机归 S_06。 |
| [[S_07]] | 本 spec 的 E2/E10/E11 只固化退出路径；4 层降级决策、fallback 子代理内部、契约判定归 S_07。 |
| [[S_08]] | 本 spec 涉及的事件顺序约束（plan.started 最先、task.start/complete 顺序、终态帧）是执行侧视角；事件全集、缓冲协议、转发归 S_08。 |
| [[S_09]] | 本 spec 的 P1/P2/P11 从 executor 视角描述接缝；adapter 装配、ContextVar 桥、WS-gateway、双仓部署归 S_09。 |
