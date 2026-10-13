# S_06 · SkillTurbo HITL 中断-恢复状态机

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / hitl-interrupt-resume |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（executor.py、agent.py、plan_node.py、permission_bridge.py、resume_context.py、interactive_ask.py、rails/ask_user_rail.py、skill_turbo_tools.py、context_binding.py、node_artifact_store.py）+ `agent_adapter/interface_deep.py`、`agent_adapter/interface.py` + `agents/harness/common/rails/stream_event_rail.py` + `openjiuwen/core/single_agent/interrupt/state.py` |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs） |

## 范围 / 边界

### 本规约管什么

ask_user / 权限类中断的**完整生命周期**：

```
中断 → 断点持久化 → 发卡 → 作答路由 → 恢复重放 → 续执行 → 清理 / 取消
```

具体契约面：

- **三层中断视图**（L1 内层工具中断 / L2 外层工具暂停 / L3 adapter 发卡暂停）的触发者、载体、持久化分工——三层全依赖 `AbortError` 不被吞。
- **tcid**（确定性 tool_call_id）生成公式全展开、两个解析函数、回退匹配三分支，及其残余风险边界。
- **中断路径 10 步全链**：skill_code `call_tool("ask_user")` → 14 处 AbortError 守卫点逐层透传（穷尽全表见 P1 步 6）→ TIE 重写 → harness 暂停 → adapter `chat.invocation_paused` 收尾。
- **发卡路径**：两个产出源同口径、`_dedupe_ask_user_card` 三分支、卡片活性登记与有界约束。
- **恢复路径**：作答入站路由链四段、入口 B（adapter 直连 resume）八步、`_resume_impl` 核心序列、入口 A（工具内自恢复）与 `release_checkpoint` 三态。
- **重放续执行语义**：executor 侧五机制。
- **取消 / 清理路径**：先终止后清的顺序律、`_clear_pending_skill_turbo_hitl` 五步、五处清理入口全表。
- **持久化状态字段全表**（checkpointer / adapter 内存态 / 请求内存态三层）、两大持久化陷阱（merge 语义、post_run 全量覆盖）与当前封堵。
- 15 条不变量（N1-N15）、28 个边界 case、修改检查单。

本域是 skill_turbo 模块缺陷密度最高、修改影响面最广的领域。典型缺陷形态：修复只覆盖部分执行路径、遗漏边界 case、修 A 引入 B。本 spec 以**全路径清单 + 全边界 case 矩阵 + 修改检查单**作为根治手段：任何 HITL 相关改动，先在此对齐"改哪些路径才算改全"。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 执行循环主链路、退出路径全景表 E1-E13 | [[S_01]] |
| skill 路由与注册（planner 匹配、外部覆盖内置） | [[S_02]] |
| 安全模型（受限命名空间、import 白名单） | [[S_03]] |
| ask_user 的节点侧调用契约（skill_code 视角） | [[S_04]] |
| 动态包与 runtime facade（含 resume 自愈重注册） | [[S_05]] |
| fallback 与降级（AbortError 不进 fallback 的判定） | [[S_07]] |
| 事件流协议细节（三件套事件帧格式、invocation_paused 字段定义） | [[S_08]] |
| 外层集成（ContextVar 桥 7 通道 bind/reset） | [[S_09]] |

## 总览：一句话状态机与三层中断视图

**一句话状态机**：SkillTurbo HITL 是"内层确定性重放 + 外层 harness 原生中断"的嵌套结构。skill_code 调 `ask_user` → 内层 rail 抛 `AbortError(cause=ToolInterruptException)` → executor 在 `use_tool` 捕获并把断点（plan_code / inputs / pending_tool_call_id / task_states 快照）持久化到隔离 checkpointer 键 `__skill_turbo_resume_ctx__` → 异常逐层透传到外层 `skill_acceleration_exec` 工具，被转成可深拷贝的 HITL 标记 dict 随工具返回值返回 → 外层 `StreamEventRail.after_tool_call` 把 tool_result 重写为 TIE → harness 原生 HITL 暂停 ReAct 循环（写 `__react_agent_interruption__`）→ adapter 把 `__interaction__` 转成 `chat.ask_user_question` 卡片并以 `chat.invocation_paused` 终结帧收尾。用户作答后经终态守卫 → adapter `_try_skill_turbo_resume` 读断点 → `SkillTurbo.resume_stream` 跳过 planner 从根节点重放 plan_code：completed 阶段短路、in_progress 阶段完整重入，重放到中断点由 `_consume_pending_resume_input` 精确 / 回退命中注入 user_input，rail `resolve_interrupt` 把作答回填给 skill_code 继续执行，成功后清理全部持久化断点。

### 三层中断视图

| 层 | 触发者 | 载体 | 持久化 |
|---|---|---|---|
| **L1 内层工具中断**（ask_user / 权限） | `SkillTurboAskUserRail.resolve_interrupt`（skill_code 内 `call_tool("ask_user")` 触发） | `AbortError(cause=ToolInterruptException)` 异常对象 | executor 落 `__skill_turbo_resume_ctx__`（隔离键，`{card.id}__skill_turbo` agent_id 空间） |
| **L2 外层工具暂停** | `skill_turbo` tool 捕获 L1 → 返回 HITL 标记 dict；`StreamEventRail.after_tool_call` 重写 tool_result 为 TIE | 工具返回值（deepcopy 安全的纯数据 dict） | harness 写 `__react_agent_interruption__`（DeepAgent 键空间） |
| **L3 adapter 发卡暂停** | adapter 流循环把 `__interaction__` 转 `chat.ask_user_question` + `chat.invocation_paused` | 事件流 + adapter 内存卡片注册表 / 相位机 | 相位标记（内存 + 随 loop session 落盘）；卡片注册表仅内存 |

三层是**同一次中断的三个投影**，靠 `AbortError` 一路不吞串起来：L1 的异常既是 L2 标记 dict 的来源，也是 L3 发卡的触发信号。任何一层把 `AbortError` 吞掉 / 转译为普通错误，下两层全部失效。

### 全生命周期状态机（相位视角）

```
 running ──ask_user rail interrupt──▶ paused
                                        │
                    ┌───────────────────┼──────────────────────┐
                    │                   │                      │
              用户作答（首答）      重复作答              cancel / supplement
                    │                   │                      │
                    ▼                   ▼                      ▼
                resuming          duplicate no-op       cancelled / supplemented
              （mark_in_flight     （空流，不清           （先终止执行，再按
               + 清 INTERRUPTION     in_flight）           五步清理断点与中断键）
               _KEY + 起 resume 流）
                    │
          ┌─────────┴─────────┐
          ▼                   ▼
      正常完成            二次中断（重放途中再次 ask_user）
     （清全部断点，        （use_tool 落新断点覆盖旧值，
      空 content            不清 resume_ctx，发新卡
      chat.final 收尾）      回到 paused，等下一轮作答）
          │
          ▼
        idle（轮末：相位回 idle + 活卡转死卡）
```

**状态载体三分离**：paused / resumed / cancelled / supplemented 相位标记随 loop session 落盘 + adapter 内存双源；resume_ctx 在隔离 checkpointer 键（`{card.id}__skill_turbo` 空间）；`__react_agent_interruption__` 在 DeepAgent 键空间。三者清理时机不同（见 P5 清理触点表）——这是"清理五入口拼装"的根源。

## 不变量

任意时刻，HITL 子系统必须满足下列断言（可判定口径）：

1. **N1 断点清理完备性**：任何退出路径（rail approve / resume 成功 / resume 不可复用（SkillTurboNotHandled）/ cancel / supplement）必须清理 HITL 持久化状态（`__skill_turbo_resume_ctx__` 与 `__react_agent_interruption__`）；**瞬时失败是唯一合法保留场景**。当前由 5 处清理入口拼装实现（见 P5 清理触点表），无单一 owner 断言——改动任一入口必须对照全表。
2. **N2 AbortError 不吞**：引擎侧 **14 处守卫点**一律透传——plan_node 4 处（`PlanNode.run` / `run_stream` / `execute_subplan` / `execute_subplan_stream`）、executor 7 处（`execute_plan_stream`、`_run_rail_hook`、`use_tool` 两层【工具执行分支 + 外层捕获落盘分支】、`call_llm` / `stream_llm` 重试循环【不进重试】、`_execute_node_stream` 消费循环【AbortError/TIE 分支】）、agent 3 处（`SkillTurbo.run` / `run_stream` / `resume_stream`）。穷尽全表见 P1 步 6；任何新增宽泛 `except` 必须回答"AbortError 怎么过"。
3. **N3 AbortError 单一符号**：仅从 `plan_node` re-export（re-export 自 openjiuwen core），禁止二次定义 / 别处 import 源。
4. **N4 tcid 确定性**：tcid 是 `(request_id, tool_name, canonical_args, idx)` 的确定性函数，无随机成分；两个解析函数（`_parse_tool_name_from_call_id` / `_parse_call_idx_from_call_id`）与生成格式强耦合，改一处必改另一处。request_id 混入哈希后，**resume 的 name+idx 回退命中是预期路径而非异常**（日志按 info 定级）。
5. **N5 checkpointer 键同一性**：`__skill_turbo_resume_ctx__` / `__skill_turbo_node_artifacts__` / `__skill_turbo` agent_id 后缀三个键为单一权威定义（resume_context.py、node_artifact_store.py）；save / load / clear / mark 全走 `ResumeContextManager`（产物走 `NodeArtifactStore` 封装），禁止手写字面量键名。
6. **N6 clear 落盘语义**：清隔离键必须走 **isolated session 通道**（独立 create_agent_session + set_skill_turbo_id + pre_run + update_state(None) + post_run）+ **同步清调用方 session 内存快照**。checkpointer post_run 是全量覆盖而非增量合并，若调用方随后 post_run 自己的 session，含旧条目的内存快照会把磁盘上刚清掉的 resume_ctx 写回复活。
7. **N7 save 替换语义**：`save_resume_ctx` 必须显式写全量键（含 `resume_in_flight=None`），不得依赖 merge 增量——openjiuwen `update_state` 是 update_dict merge 语义，缺键不删旧键。
8. **N8 发卡单路 owner + 外层 call id 对齐**：首卡唯一来源是 harness `__interaction__` 转换；resume 内二次中断卡唯一来源是 `_emit_skill_turbo_hitl_chunks`；两者 request_id 都等于外层 `skill_acceleration_exec` call id（或其 `#n` 变体），且必经 `_dedupe_ask_user_card`。`StreamEventRail` 不主动 emit SkillTurbo 的 ask_user 卡片。
9. **N9 暂停态禁 chat.final**：`suppress_stream_after_hitl` 未清除时不得合成 chat.final，必须以 `chat.invocation_paused(awaiting_user_input=True, is_complete=True)` 终结帧收尾——防止上层 sidecar 提前关 FrameQueue 丢 recoverable_pause。
10. **N10 cancel 顺序律**：先终止执行（`_stop_session_interrupt_work` + abort），后清状态。顺序反了，fresh 执行中被 cancel 的 executor 会在退出路径落盘 resume_ctx，覆盖掉刚清的状态——下一条消息命中残留断点、重放已被取消的任务。
11. **N11 resume 不重规划**：`resume_stream` 跳过 planner 直接执行持久化 plan_code；completed 短路 + task_id 复用 + 产物 hash 继承三者共同保证"不重跑已完成阶段、不叠两套 Stage"。
12. **N12 HITL 中断不触发 after_subplan_execute**：`_execute_subplan` / `_execute_subplan_stream` 的 AbortError 分支不调 after 回调——防前端把暂停中的 stage 误标完成，同时 in_progress 状态得以保留进 task_states 快照。
13. **N13 断点标记 deepcopy 安全**：跨执行上下文传递的 HITL 信号只允许纯数据（dict / JSON-able）随工具返回值走；ContextVar 仅作同上下文兜底。SDK tracer 会对含返回值的 span 深拷贝，含不可深拷贝对象的标记会在 trace 层崩溃。
14. **N14 守卫 fail-open 边界**：死卡 / 终态守卫（`guard_stale_interrupt_response`）异常时放行（可用性优先），但守卫**命中**必须拒绝且不触发 runtime 重放。
15. **N15 暂停占位语义**：外层模型看到的 tool_result 必须是"已暂停等待回答……没有失败"的占位文案，禁止让模型误判失败而放弃加速通道；resume 完成收尾区分"已交付 / 未确认"。

## 接口契约

### tcid 生成（executor.py `_next_tool_call_id`）

```
args_canonical = json.dumps(kwargs, sort_keys=True, default=str)
                （TypeError/ValueError 时回退 repr(sorted(kwargs.items()))）
args_hash      = sha1(f"{request_id}|{args_canonical}".encode())[:8]   # 8 位 hex
key            = f"{tool_name}|{args_hash}"
idx            = self._tool_call_counter[key]   # per-Executor，每次执行从 0 重数
tool_call_id   = f"skill_turbo-tc-{tool_name}-{args_hash}-{idx}"
```

- **request_id 混入的意义**：`_request_id_var`（executor 请求级 ContextVar）参与哈希，使不同会话 / 请求对相同 ask_user 参数生成不同 tcid 与卡片 id，实现跨会话隔离（防作答串台）。
- **代价**：resume 是新请求、request_id 必然变化 → 精确匹配必然失效，**name+idx 回退命中成为预期主路径**（executor `_consume_pending_resume_input` 注释显式声明该 trade-off，日志 info 级，不刷告警）。
- **idx 语义**：`_tool_call_counter` 按 `(tool_name, args_hash)` 组合计数，区分"同一组合的第几次调用"；per-Executor 实例（每请求新建），重放时从 0 重数，中断点与重放点的 idx 天然对齐。**注意**：不同参数的同名 ask_user 各自 idx 从 0 起算，`name+idx` 不全局唯一。
- **两个解析函数**（与生成格式强耦合）：
  - `_parse_tool_name_from_call_id`：从尾部剥 `-{idx}` 再剥 `-{args_hash}`，剩余为 tool_name（tool_name 自身可含 `-`）。
  - `_parse_call_idx_from_call_id`：取最后一个 `-` 后段转 int。

### 回退匹配三分支（executor.py `_consume_pending_resume_input`）

resume 时 `set_pending_resume` 注入 `{expected_tool_call_id, expected_tool_name（解析自 tcid）, user_input, task_states}`；`use_tool` 每次调用先消费：

| 分支 | 条件 | 行为 |
|---|---|---|
| **精确命中** | `current_tcid == expected_tcid` | 返回 user_input，清空 pending，用 current_tcid |
| **回退命中** | `current_tool_name == expected_tool_name` 且 两者末段 idx 相等 且 `user_input is not None` | 返回 user_input，清空 pending，**改用 expected_tcid**（对齐中断点，rail 才能把 user_input 注入 `ctx.extra[RESUME_USER_INPUT_KEY]`——不对齐则同一调用点再次中断，形成二次中断死循环） |
| **未命中** | 其余 | 返回 `(None, current_tcid)`，pending 保留待后续匹配 |

- **idx 段的作用**：区分"同一 stage 内多个同名工具调用"，防止把 user_input 误注入非中断点的同名调用。
- **残余风险（显式边界，无防护断言）**：若重放在到达真正中断点之前出现"同名、不同参数、同调用序"的调用（参数漂移 → 新 args_hash → idx 重新从 0 数），回退命中会把作答注入错误调用。当前靠"plan_code 确定性重放 + completed 短路"压低概率。任何动 tcid 格式 / 回退条件的改动必须重新评估此误注入面。
- **漂移示例**（精确匹配为何必然失效）：中断时 `request_id=req_a`、ask_user 参数 P → `tcid = skill_turbo-tc-ask_user-3f2a1b9c-0`；resume 是新请求 `request_id=req_b`，重放到同一调用点生成 `skill_turbo-tc-ask_user-9d8e7f6a-0`——精确匹配失败，但 `tool_name`（`ask_user`）相同、末段 idx（`0`）相同且 `user_input` 非空 → **回退命中**，改用 expected_tcid `…-3f2a1b9c-0` 对齐注入。这是 resume 的预期主路径，日志 info 级（定级口径：回退命中不是异常，不该刷告警噪声）。

### ResumeContextManager（resume_context.py）

```python
set_skill_turbo_id(session, card)   # 写 agent_id = f"{card.id}__skill_turbo"；必须在 pre_run() 前调用
save(session, *, plan_code, inputs, pending_tool_call_id, task_states=None)
    # 显式写全量键含 resume_in_flight=None（N7）；pre_run → update_state → post_run 三段落盘
load(session)                        # pre_run + get_state；校验 plan_code 非空
clear(session, *, card=None)         # 有 card：isolated 通道强制落盘 + 同步清调用方内存快照（N6）
                                    # 无 card：原 session 写 None + warning（双键空间下清不到隔离键，已知接受）
clear_in_flight(session, *, card=None)  # 同 isolated 通道；无 card 降级时先 get_state 判断存在才 pop 重写
```

`ResumeContextManager` 是 resume_ctx 的单一 owner，其文档字符串明言严禁吸收 `INTERRUPTION_KEY`（DeepAgent 键空间归 harness / adapter 侧管理）。

### 作答入站与 InteractiveInput（interface.py）

作答请求形态：`AgentRequest(params={source:"ask_user_interrupt", answers:[...], request_id:卡id(可含#n), status, original_request})`。

- **权限账本豁免**：ask_user continuation 不进 permission ledger 去重，且**不剥 `#n` 后缀**——TIE 重写场景下 `#n` 标识同 base 的下一个不同权限门，归一化会死锁。
- `_is_ask_user_interrupt_response` 识别为中断应答；`_build_inputs` → `_build_interactive_input_from_answers` → `InteractiveInput.update(剥后缀后的 request_id, payload)`（剥后缀还原逻辑 `strip_hitl_seq_suffix`）。

### SkillTurbo.resume_stream（agent.py）

```python
async def resume_stream(self, *, plan_code, inputs, request_id, channel_id,
                        pending_tool_call_id, user_input, task_states=None) -> AsyncIterator[AgentResponseChunk]
```

内部：`executor.set_pending_resume(...)`（置 `_resume_replay=True` + pending dict + deepcopy task_states）→ `execute_plan_stream` 全树重放。签名任一语义变化必须同步入口 A / 入口 B 两个调用方。

## 数据结构：持久化状态字段全表

### 第一层：checkpointer 持久化（跨请求 / 跨进程存活）

| 字段 | 键 / 结构 | 写入时机 | 读取时机 | 清理时机 | 收口状态 |
|---|---|---|---|---|---|
| **resume_ctx** | `__skill_turbo_resume_ctx__`（隔离 agent_id `{card.id}__skill_turbo` 下）`{plan_code, inputs, pending_tool_call_id, resume_in_flight(None/True), task_states?}` | ① 中断时 `use_tool` except AbortError → save（显式 `resume_in_flight=None`）；② resume 受理时 mark_in_flight | 恢复两入口 load（interface_deep.py `_try_skill_turbo_resume` / skill_turbo_tools.py 工具体 resume 分支） | ① rail approve（use_tool 内）；② resume 成功 / NotHandled；③ cancel / supplement isolated 清；④ 工具体 release_checkpoint | 基本收口，**三个已知洞**：(a) `resume_in_flight` 无 TTL——mark 后进程崩溃则 True 永久残留，后续作答全部判 duplicate，恢复手段仅 cancel / supplement；(b) save 失败仅 warning，中断继续但断点丢失，作答退化 DeepAgent 路径；(c) 无 card 降级 clear 清不到隔离键 |
| **ToolInterruptionState** | `__react_agent_interruption__`（DeepAgent 键空间）`{ai_message(含尾部 pending tool_calls), iteration, original_query, interrupted_tools{tcid→{tool_call, interrupt_requests, is_sub_agent}}, auto_confirm_mapping}` | harness build_interrupt_state（TIE 暂停时） | `_deep_agent_has_skill_turbo_interrupt`；harness resume 重放；`_clear_pending_skill_turbo_hitl` 校验 | ① cancel / supplement；② resume 受理时置 None；③ resume 成功（经 `_clear_pending_skill_turbo_hitl`） | 基本收口；纯 ask_user 轮另有 `_drop_pending_pure_ask_user_round` 补清 |
| **node_artifacts** | `__skill_turbo_node_artifacts__`，结构 `{skill, nodes{plan_name→info/文件路径}, plan_code_hash}` | 节点完成逐步 + 流末 finally persist + 中断路径与 save_resume_ctx 合并落盘（skip_post_run 参数） | resume 重放 `_inherit_node_artifacts_for_replay`（hash 匹配） | fresh run `_clear_stale_node_artifacts`；**cancel 保留**（供降级 DeepAgent 复用） | 收口（fresh 清 / cancel 留是有意语义） |
| **中断相位标记** | 随 loop session 状态（内存写 + interrupt_agent_execute 随 loop session 落盘；终态相位由 `_clear_session_persisted_interrupt_state` 落盘） | 发卡（paused）/ RESUME_SIGNAL（resumed）/ 轮末（idle）/ cancel（cancelled/supplemented） | `guard_stale_interrupt_response`（磁盘 + 内存双读） | 轮末 idle / 新卡覆盖 | 内存态与磁盘态**双源**，一致性靠约定 |

### 第二层：adapter 实例内存态（不持久化，重启即失）

| 字段 | 作用 | 失效语义 |
|---|---|---|
| `_ask_user_card_seq` | 同 base 卡片序号（跨请求存续） | 重启归零 → 序号回卷，旧 `{id}#2` 与新 `#2` 理论上可冲突 |
| `_hitl_card_instances` / `_hitl_base_live_instance` / `_hitl_base_live_questions` / `_hitl_dead_card_ids` | 卡片活性 / 死卡注册表（**有界 1024 / 512**，防 adapter 长生命周期泄漏） | 重启丢失 → 死卡守卫 fail-open，仅剩相位守卫兜底 |

### 第三层：请求级内存态（随请求生命周期）

| 字段 | 作用 | 失效语义 |
|---|---|---|
| `_pending_resume`（executor 实例） | `{expected_tcid, expected_tool_name, user_input, task_states}` | 命中消费置 None；未命中保留；Executor 每请求新建 |
| ContextVar 组 | `skill_turbo_hitl_tic`（旧兜底）、`skill_turbo_resume_answers`、`current_skill_turbo_adapter`、`current_request_metadata`、`interactive_ask`、executor 的 session / request / channel | bind / reset 由 context_binding 统一收口（详见 [[S_09]]） |

### 两大持久化陷阱与当前封堵（机制，非故事）

**陷阱一：openjiuwen `update_state` 的 merge 语义（update_dict）。** dict.pop + 回写无法删键——新 entry 缺 `resume_in_flight` 键不会删除旧 `True`，残留标志会把下一次作答判为 duplicate 空流。**封堵**：save 必须显式写全量键、含 `resume_in_flight=None`（None 在 update_dict 语义下删键，N7）；`clear_in_flight` 无 card 降级时先 get_state 判断存在才 pop 重写。

**陷阱二：checkpointer post_run 全量覆盖 + 幂等 no-op。** 同一 session 第二次 post_run 不 commit（幂等）→ 清理若复用调用方 session，清除不落盘；且 post_run 是全量状态覆盖——clear 之后若调用方 session 的**内存快照**仍含旧条目并随后 post_run，磁盘上刚清掉的 resume_ctx 被写回复活（表现为"resume 跑通后下一条消息重放已完成任务"）。**封堵**：clear 必须走 isolated session 通道强制落盘 + 同步清调用方内存快照（N6）；`_clear_pending_skill_turbo_hitl` 第五步显式 `save_contexts` + `commit`。

**无 card 降级的限制**：`clear(card=None)` 在双键空间部署下清不到隔离键，残留 resume_ctx 会触发任务重跑；单键空间测试桩可清。调用方应显式传 card——这是接口契约而非可选参数。

### 已知收口缺口与接受面（现状清单）

下列为当前实现的显式边界（非待办清单；改动相关代码时必须知晓并在检查单对应条目下重新评估）：

1. **`resume_in_flight` 无 TTL**：mark 后进程崩溃 → True 永久残留 → 该会话后续所有作答被判 duplicate 空流；恢复手段仅 cancel / supplement 全清。
2. **save_resume_ctx 失败静默降级**：中断继续上抛但断点丢失，用户作答后走 DeepAgent 兜底，体验降级且无用户可见反馈。
3. **无 card 降级 clear 清不到隔离键**（已知接受）：见上段。
4. **卡片注册表 / 相位内存态不持久化**：重启后死卡守卫 fail-open，仅剩相位守卫；`_ask_user_card_seq` 回卷存在旧 `#n` id 与新卡冲突的理论风险。
5. **回退命中的理论误注入**：见接口契约"残余风险"——无防护断言，仅靠确定性重放压概率。
6. **作答 request_id 不匹配仅 warning**：作答路由错卡时静默继续，可能把 A 卡答案注入 B 断点（ask_user 类有 questions 结构校验兜底；权限类 ConfirmPayload 无此校验）。
7. **清理五处入口拼装、三套组合**：靠注释与测试守护一致性；重构方向候选是"中断状态单一 owner"（resume_context.py 已开先例：单一 owner + 严禁吸收 INTERRUPTION_KEY 的有意边界）。
8. **发卡幂等依赖上层链路幂等**：挂起表复用分支重发同 request_id，注册早退 + 前端 requestId 去重均为跨系统隐式契约，wire 协议无显式声明。

## 执行路径全景

### P1 中断路径（10 步全链）

以 skill_code 内 `call_tool("ask_user", questions=[...])` 为例（权限类中断同构，rail 换成 PermissionInterruptRail）：

| 步 | 动作 | 代码位置 | 关键约束 |
|---|---|---|---|
| 1 | skill_code → executor.use_tool | executor.py `use_tool` | 生成 tcid（`_next_tool_call_id`）→ `_consume_pending_resume_input`（首次运行为 no-op） |
| 2 | 构造 tool ctx | permission_bridge.py `build_tool_ctx` | `SkillTurboToolCall(id=tcid, name, arguments)` 鸭子类型 + `extra[RESUME_USER_INPUT_KEY]=resume_input`（仅命中时） |
| 3 | rail 链 before_tool_call | executor.py `_run_rail_hook` | 按 priority 依次 `StreamEventRail → SkillTurboAskUserRail → LLMRetryRail → ArtifactRail`；**`except AbortError: raise`，其余 Exception 吞掉打 warning**（单 rail 失败不影响主链） |
| 4 | SkillTurboAskUserRail.resolve_interrupt | rails/ask_user_rail.py `resolve_interrupt` | 三分支：`user_input is None` 且非引导模式且 question 带 preview → **不中断**，直接 `reject({"status":"skipped",...})` 沿用当前内容；`user_input is None` 其余情形 → `self.interrupt(...)` 抛 `AbortError(cause=ToolInterruptException)`；`user_input` 无法解析（不认识形态）→ **re-interrupt**（确定性 tcid 相同，重问帧可能被 sidecar dedup 吞，注释已警示） |
| 5 | use_tool 捕获 AbortError → 落盘断点 | executor.py `use_tool`（except AbortError 分支） | `save_resume_ctx(session, plan_code=self._current_plan_code, inputs=self._execution_inputs, pending_tool_call_id=tcid, task_states=self._snapshot_task_states())`；**save 失败仅 warning——中断继续上抛但断点丢失**（后续作答无 resume_ctx，退化为 DeepAgent 路径）→ raise 透传 |
| 6 | 逐层透传（14 处 AbortError 守卫点） | 见下表 | 改任一处要同步审查其余 |
| 7 | 外层工具捕获 → 转 HITL 标记 dict | skill_turbo_tools.py `skill_turbo`（except 分支） | **不能直接 raise TIE**——会被 AbilityManager 包成 AbilityExecutionError；改为 `tic = extract_tool_interrupt(e)`（permission_bridge.py，沿 `__cause__/cause` 链抽取）→ `set_skill_turbo_hitl_tic(tic)`（ContextVar 兜底）→ 返回 `build_skill_turbo_hitl_result(tic)`：`{__skill_turbo_hitl__: True, "request": request.model_dump(mode="json")}`——**deepcopy 安全纯数据**（N13，SDK tracer 深拷贝 span 返回值） |
| 8 | 外层 StreamEventRail.after_tool_call 重写 tool_result 为 TIE | stream_event_rail.py `after_tool_call` | 主信号：返回值里的 `__skill_turbo_hitl__` 标记；兜底：ContextVar TIC（**校验 tool_name 必须是 `skill_acceleration_exec`**，否则按 stale 丢弃）。重写 `ctx.inputs.tool_result = ToolInterruptException(request, 外层tool_call)` + tool_msg = 占位文案「已暂停等待回答……没有失败」（N15，防外层模型误判失败放弃加速通道）→ **提前 return，跳过 _emit_tool_result**。标记校验失败（model_validate 抛错）降级为可读占位文案 |
| 9 | harness 原生 HITL 暂停 | harness `build_interrupt_state` | 检测 TIE → ReAct 循环暂停 → 写 `INTERRUPTION_KEY`（`__react_agent_interruption__`）= ToolInterruptionState → 产出 `__interaction__` OutputSchema（id=外层 tool_call id，payload=ToolCallInterruptRequest） |
| 10 | adapter 发卡收尾 | interface_deep.py 流循环 | `__interaction__` chunk → `convert_interactions_to_ask_user_question` → `chat.ask_user_question` 卡（request_id=外层 call id）；`hitl_pending_stream=True` + `suppress_stream_after_hitl=True`；流末以 `chat.invocation_paused(awaiting_user_input=True, is_complete=True)` 终结帧收尾（N9）；排除 subagent 两类 payload |

**14 处 AbortError 透传守卫全表**（步 6 展开，plan_node 4 + executor 7 + agent 3；改任一处要同步审查其余）：

| # | 位置 | 行为要点 |
|---|---|---|
| 1 | plan_node.py `PlanNode.run` | 不走 fallback，直接 raise |
| 2 | plan_node.py `PlanNode.run_stream` | 同上 |
| 3 | plan_node.py `PlanNode.execute_subplan` | **不调 after_subplan_execute**（N12） |
| 4 | plan_node.py `PlanNode.execute_subplan_stream` | 同上（hitl_interrupt 标志抑制 finally-after） |
| 5 | executor.py `execute_plan_stream`（AbortError 分支） | raise，**不发任何终态帧**（无 plan.finished / complete / chat.error——暂停语义，终帧归 adapter） |
| 6 | executor.py `_run_rail_hook` | AbortError 重抛；其余 Exception 吞 + warning |
| 7 | executor.py `use_tool`（工具执行分支，except Exception 内 isinstance 分流） | 不作为普通工具错误处理，trace 记 interrupted 后 raise |
| 8 | executor.py `use_tool`（外层 except AbortError） | 先 `save_resume_ctx`（断点唯一写入点）再 raise |
| 9 | executor.py `call_llm`（重试循环） | AbortError 不进重试直接 raise |
| 10 | executor.py `stream_llm`（重试循环） | 同上 |
| 11 | executor.py `_execute_node_stream`（消费循环 AbortError / extract_tool_interrupt 命中分支） | 先 `_flush_deferred_task_lifecycle_events` + drain task 事件（保住最新任务快照）再 raise，**不转 node_error chat.error** |
| 12 | agent.py `SkillTurbo.run` | 非流式入口同构透传（不包 SkillTurboNotHandled） |
| 13 | agent.py `SkillTurbo.run_stream` | raise，**不降级** SkillTurboNotHandled |
| 14 | agent.py `SkillTurbo.resume_stream` | HITL 恢复入口同构透传（resume 内二次中断经此回到 paused） |

范围边界：本表只收**引擎侧透传守卫**；skill_code 侧 tool_utils 的守卫（`call_tool_with_retry` / `run_bash` 对 AbortError 不重试直接透传，[[S_05]] N16）与 fallback validator 白名单（[[S_07]] N5）是另外两处独立防线，不在本表计数内。

**步 4 展开：`SkillTurboAskUserRail.resolve_interrupt` 三分支**（rails/ask_user_rail.py）：

| 分支 | 条件 | 行为 |
|---|---|---|
| **preview skip** | `user_input is None` + 非引导模式（`get_interactive_ask()` 为 False，ContextVar 由 executor `_setup_execution_context` 绑定）+ question 带 preview | **不中断**，`reject({"status":"skipped",...})` 沿用当前内容 |
| **首次 interrupt** | `user_input is None` + 其余情形 | `self.interrupt(...)` → 抛 `AbortError(cause=ToolInterruptException(request, tool_call))` |
| **re-interrupt** | `user_input` 无法解析（不认识形态） | 重新中断等待（确定性 tcid 相同 → 重问帧可能被 sidecar dedup 吞，注释已警示） |

三分支共用一个入口、靠 `user_input` 的取值（None / 不可解析 / 有效作答）分化——参数语义一旦混淆，会把"该中断的"变 skip、或把"该注入的"变重问（检查单第 8 条）。

### P2 发卡路径

当前为"准单路 owner"：**两条产出路径、同一口径（request_id 都在外层 `skill_acceleration_exec` call id 域）、统一经 `_dedupe_ask_user_card` 去重**。

| 路径 | 触发场景 | 发卡代码 | request_id |
|---|---|---|---|
| **首卡**（fresh run） | 外层 DeepAgent ReAct 暂停后的 `__interaction__` 转换 | interface_deep.py `convert_interactions_to_ask_user_question` | 外层 `skill_acceleration_exec` call id（TIE 重写对齐） |
| **二次中断卡**（resume 内） | `_resume_impl` 捕获 AbortError | interface_deep.py `_emit_skill_turbo_hitl_chunks`：chat.tool_call(pending_approval) → chat.tool_update(in_progress) → ask_user_question → chat.invocation_paused | `_derive_outer_call_id`（剥 `#{n}` 后缀取 base）覆盖；base 无后缀时不得回退空串 |

**单路约束**：`StreamEventRail` 不主动 emit SkillTurbo 的 ask_user 卡片（代码注释明言"卡片由 harness `__interaction__` 转换统一发出……避免同一次中断发出两张 ask_user 卡片"）；`_emit_ask_user_question_if_interrupted` 仅服务 DeepAgent 原生 `ask_user` 工具（tool_name=="ask_user"），与 SkillTurbo 内层无关。

**`_dedupe_ask_user_card` 三分支**（interface_deep.py）：

1. **同 call id + 同 questions → 跳过**（同一中断多通道重复：独立 `__interaction__` 与 controller 嵌套）。
2. **挂起表复用**：该 base 仍有同 questions 活卡（作答消费时销毁）→ **重发原卡、不改代次**。机制背景：乱序点击后 resume 重放会重新走到未作答的发卡点，若无此分支会诞生 `{id}#{n+1}` 新卡取代原卡，用户点回原卡被终态守卫拒绝 → 空流被上层空回弹启发式误判中止。重发同 request_id 依赖上层链路幂等（注册早退 + 前端 requestId 去重）——跨系统隐式契约。
3. **同 base 不同 questions**（同外层调用第 N 次中断）→ `request_id` 改 `{id}#{n}` 放行；作答回传由 `interface._build_inputs` 剥后缀还原。

**卡片活性登记**：新卡发出即取代同 base 旧卡（`_register_hitl_card_instance`：`_hitl_card_instances` / `_hitl_base_live_instance` / `_hitl_base_live_questions` / `_hitl_dead_card_ids`，**有界 1024 / 512**），旧卡应答由死卡守卫拒绝；同时 `_mark_interrupt_paused_inmemory` 重开相位（仅内存态）。

**事件时序**（两条路径同构）：`chat.tool_call(status=pending_approval)` → `chat.tool_update(status=in_progress)` → `chat.ask_user_question(request_id=外层 call id 域)` → `chat.invocation_paused(awaiting_user_input=True, is_complete=True)` 终结帧。帧字段定义与前端消费契约见 [[S_08]]，本 spec 只约束"何时发、谁发、发几张、request_id 用哪个"。

**内存态不持久化风险**：卡片注册表 / 序号计数均为 adapter 实例内存态——进程重启后死卡守卫 fail-open，只剩相位守卫兜底（N14 边界）。

### P3 恢复路径

#### 作答入站路由链（4 段）

1. 前端提交 → 上层前端按外层 call id 路由 → `AgentRequest(params={source:"ask_user_interrupt", answers, request_id:卡id(可含#n), ...})`。
2. interface.py **权限账本豁免**（ask_user continuation 不进 ledger、不剥 `#n` 后缀）→ `_is_ask_user_interrupt_response` 识别。
3. prepare-hook 阶段先过**终态守卫** `guard_stale_interrupt_response`：死卡命中 → 拒绝；读磁盘态 + 内存态相位，cancelled / supplemented 终态 → 拒绝（防"点旧卡→resume→再弹新卡"反馈循环）；守卫异常 **fail-open**（N14）。
4. `_build_inputs` → `_build_interactive_input_from_answers` → `InteractiveInput.update(剥后缀后的 request_id, payload)`。

#### 作答路由决策表

作答（或相关用户输入）到达后的分流判定，按判定顺序：

| 判定顺序 | 条件 | 去向 |
|---|---|---|
| 1 | 终态守卫命中（死卡 / cancelled / supplemented 相位） | stale placeholder 空流收口，不触发 runtime |
| 2 | `answers` 非空 + `source == "ask_user_interrupt"` | 进入口 B `_try_skill_turbo_resume` |
| 2a | 入口 B 内 `resume_in_flight` 为 True | duplicate placeholder 空流 no-op |
| 2b | 入口 B 内 resume_ctx 可加载 | `_make_skill_turbo_resume_stream` 直连 resume（主路径） |
| 2c | 入口 B 内 resume_ctx 为 None 且 `_deep_agent_has_skill_turbo_interrupt()` 为真 | deferred to DeepAgent（嵌套 HITL：外层权限中断 own the turn） |
| 3 | 入口 B return None → InteractiveInput 进 DeepAgent `send_input`，ReAct resume 重放 `skill_acceleration_exec` | 入口 A：工具体内 `load_resume_ctx` → resume_stream |
| 4 | 暂停期间用户重发纯文本（非作答形态） | `_drop_pending_pure_ask_user_round` / 清状态后进全新 invocation 由 LLM 意图判断 |

#### 入口 B（主路径）：adapter 直连 resume（interface_deep.py `_try_skill_turbo_resume`，八步）

1. 前置：`answers` 非空且 `source == "ask_user_interrupt"`；否则 return None 交正常流程。
2. **终态守卫**（P2 场景第二道）：命中 → `_make_skill_turbo_resume_stale_placeholder`（chat.interrupt_result / stale_interrupt_response + 空流收口）。
3. `_consume_hitl_live_card_for_answer`：挂起表摘除该 base 活卡条目（作答被接受为当前代；此后 resume 重放再走到同 base 发卡点，走新代兜底而非复用已作答卡）。
4. 建 isolated session（create_agent_session + set_skill_turbo_id）→ `load_resume_ctx`（校验 plan_code 非空）。
5. **resume_ctx is None → 退回 DeepAgent**：若 `_deep_agent_has_skill_turbo_interrupt()`（读 loop session 的 INTERRUPTION_KEY 含 skill_acceleration_exec）为真 → "deferred to DeepAgent"（嵌套 HITL：外层权限中断 own the turn，此时 answers 只更新外层 AskUserRail）；否则仅 warning。
6. **`resume_in_flight` 为 True → duplicate placeholder**（重复作答空流 no-op，不弹错、不重跑）。
7. 作答 request_id 与 pending tcid 不一致 → **仅 warning 继续**（宽容策略；权限类 ConfirmPayload 无 questions 结构校验兜底，属已知接受面）。
8. `_skill_turbo_mark_resume_in_flight`（持久化 True 防重复作答双跑）→ 清 `INTERRUPTION_KEY` → `_make_skill_turbo_resume_stream` 起 resume 流。

**`_resume_impl` 核心序列**（interface_deep.py）：

- `resolve_resume_interactive_ask`（interactive_ask.py）：入站无 interactive_ask 字段时**从中断点 saved_inputs 恢复引导模式**——防 resume 后被强制 False → preview 类 ask_user 误判 skipped / 引导管线退化。
- `outer_call_id = _derive_outer_call_id(params.request_id)`；`_resume_emitted_ask_ids` / `_resume_emitted_ask_questions` 去重集。
- `skill_turbo.resume_stream(plan_code=ctx["plan_code"], inputs=resume_inputs, pending_tool_call_id, user_input=_skill_turbo_answers_to_confirm_payload(answers, ctx), task_states)`。user_input 转换：answers 全含 question / selected_options → 原样返回（结构化 ask_user）；否则按文本映射 ConfirmPayload（"拒绝"→approved=False；"总是允许"→auto_confirm + persist）。
- **正常完成**：`_clear_resume_ctx()`（命中 `_clear_pending_skill_turbo_hitl` 则清 DeepAgent 侧；miss 则 `_clear_skill_turbo_resume_ctx_via_isolated_session`）→ finish_text（P10 骨架 / 安全短句，**禁止机器产物摘要进主气泡**）→ delta + **空 content chat.final**（防上层前端终稿拼接 computeFinalTextDelta 双拷贝）。
- **二次中断**（`except _SkillTurboAbortError`）：usage 摘要 → `_emit_skill_turbo_hitl_chunks(request, e, outer_call_id)` → 卡片过 `_dedupe_ask_user_card` → **直接 return，不清理 resume_ctx**——新断点已由 use_tool 保存，等下一轮作答。
- **SkillTurboNotHandled**：`_clear_resume_ctx()` + 失败 finish_text（断点不可复用，属合法清理路径）。
- **finally**：`_skill_turbo_clear_resume_in_flight`（isolated 通道强制落盘，防 in_flight 残留把重试作答判 duplicate）+ trace ContextVar 复位。

#### 入口 A：harness 原生 resume（工具内自恢复）

`_try_skill_turbo_resume` return None（如无 resume_ctx 但外层权限中断在）→ InteractiveInput 进 DeepAgent `send_input` → ReAct resume → 重放 `skill_acceleration_exec`，`ctx.extra[RESUME_USER_INPUT_KEY]` 携带作答：

- **StreamEventRail.before_tool_call**：resume 场景（skill_acceleration_exec + RESUME_USER_INPUT_KEY 存在）**跳过重复 tool_call / tool_update emit**；随后 `bind_skill_turbo_context`（context_binding.py）把 resume_answers 绑进 ContextVar（7 通道统一 bind / reset，见 [[S_09]]）。
- **skill_turbo 工具体**：`get_skill_turbo_resume_answers()` 非空 → `_resolve_skill_turbo_resume_session_id`（metadata sid 优先，回退 parent session）→ 建 turbo_session + `load_resume_ctx` → 命中则走 `resume_stream`（user_input 经 `_resume_user_input_from_raw`：InteractiveInput.user_inputs 取任意 value / list 转 ConfirmPayload）。
- **finally 的 `release_checkpoint` 三态**（skill_turbo_tools.py 工具体 finally）：
  - **resume 正常完成 → True**：`clear_resume_ctx(turbo_session)` + post_run。
  - **SkillTurboNotHandled（断点不可复用）→ True**：同上清理。
  - **瞬时失败 → False**：**保留断点不清且不 post_run**——turbo_session 与 executor 共享 checkpointer 键，stale 内存态 post_run 会 clobber 磁盘上仍有效的 resume_ctx（陷阱二的变体）。

#### 入口 A / B 对比（两入口殊途同归 `resume_stream`）

| 维度 | 入口 B（主路径） | 入口 A |
|---|---|---|
| 触发 | adapter `chat_stream` 分流命中（answers + source） | DeepAgent ReAct resume 重放 `skill_acceleration_exec` 工具 |
| 前置守卫 | 终态守卫 + 活卡摘除 + in_flight 判重 | 无（作答已过入口侧守卫） |
| session 解析 | request.session_id 建 isolated session（set_skill_turbo_id） | `_resolve_skill_turbo_resume_session_id`（metadata sid 优先，回退 parent session） |
| user_input 组装 | `_skill_turbo_answers_to_confirm_payload`（answers → ConfirmPayload / 结构化原样） | `_resume_user_input_from_raw`（InteractiveInput.user_inputs 取任意 value / list 转 ConfirmPayload） |
| 清理时机 | 正常完成 / NotHandled 清；二次中断不清；finally 清 in_flight | finally 按 release_checkpoint 三态 |
| 重复事件抑制 | `_resume_emitted_ask_ids` / `_resume_emitted_ask_questions` 去重集 | StreamEventRail before_tool_call 跳过重复 tool_call / tool_update emit |

两入口的 user_input 组装、session 解析、清理时机是**独立实现**——任何一侧语义变化必须同步另一侧（检查单第 10 条）。

### P4 重放续执行语义（executor 侧五机制）

`SkillTurbo.resume_stream` → `set_pending_resume`（置 `_resume_replay=True`）→ `execute_plan_stream`，主循环细节见 [[S_01]]，此处只列 HITL 相关五机制：

1. **跳过 planner**：直接执行保存的 plan_code 字符串（`_prepare_root_node` 重新加载 / 注册计划树，含动态包自愈，见 [[S_05]]）。
2. **node_artifacts 继承（hash 门禁）**：resume 重放不清盘，`_inherit_node_artifacts_for_replay` 按 `plan_code_hash` 匹配继承已完成节点产物——不继承则 finally 用"空底 + 新增"覆盖 session，中断前产物丢失；hash 不匹配不继承、旧记录无 hash fail-open、出错 fail-open、无记录 no-op。
3. **task_states 复用 task_id**：`_initialize_pending_tasks` → `_take_resume_task_states`（一次性取出）→ `_restore_task_states_from_snapshot`（按 `task_content` 优先 / `task_index` 兜底对齐复用原 `task_id`，防前端叠出第二套 Stage）。
4. **completed 短路 + in_progress 重入**：`_should_skip_subplan_execute`（depth==1 且 status==completed → 跳过真实执行，返回 skip result **仍触发 after 回调收产物**）；`_should_suppress_subplan_start_banner`（in_progress 完整重入 + 横幅抑制 + 跳过重复 task.start）。
5. **tcid 命中注入 → approve 即清**：use_tool → `_consume_pending_resume_input`（三分支，见接口契约）→ `build_tool_ctx(resume_user_input=...)` → rail 链带作答跑 before_tool_call → `SkillTurboAskUserRail.resolve_interrupt(user_input=answers)` → `reject({"status":"answered","answers":[...]})` → **skill_code 的 call_tool 直接拿到结构化作答继续执行**；`resume_input is not None` 时（rail 放行 / 批准后）use_tool 内 `clear_resume_ctx(session)`（失败仅 debug）。

### P5 取消 / 清理路径

#### cancel / supplement 的顺序律

**先终止执行（`_stop_session_interrupt_work` + `_abort_shared_agent_if_safe` / cancel_round），后清状态。** 两处调用点注释一致强调：顺序反了，fresh 执行中被 cancel 的 executor 会在退出前落盘 resume_ctx（HITL 中断点写入），先清会被覆盖——下一条消息仍命中残留断点、重放被取消的任务。这是 N10，任何取消路径重构的第一约束。

#### `_clear_pending_skill_turbo_hitl` 五步（interface_deep.py）

1. 校验 loop session 匹配目标 session 且 `INTERRUPTION_KEY.interrupted_tools` 含 `skill_acceleration_exec`（防止误清其它 session / 纯 ask_user 轮）。
2. 上下文尾部弹掉待回答 tool_call（`pop_messages(1, with_history=True)`，不留悬挂调用——否则模型看到未完成调用）。
3. 清空 `INTERRUPTION_KEY`（`update_state({INTERRUPTION_KEY: None})`）——下一条消息进全新 invocation 由 LLM 意图判断（skillTurbo 全新任务 or 非 skillTurbo 基于产物继续）。
4. 经 `{card.id}__skill_turbo` 隔离键清 `__skill_turbo_resume_ctx__`——**loop session 命中的是 DeepAgent 键空间，清不到隔离键**（键空间区分是 N5 的直接推论）。
5. **保留 `__skill_turbo_node_artifacts__`**（供降级 DeepAgent 复用）+ `save_contexts` + `commit` 强制落盘（防 post_run 幂等导致清除不持久化，陷阱二）。

**两路调用**：

- 非 interaction 路径（intent 为 cancel / supplement）。
- interaction 路径：`_drop_pending_pure_ask_user_round`（纯 ask_user 轮：**残留 INTERRUPTION_KEY 会把下一条纯文本当 resume 输入塞进 rail.resolve_interrupt，非 dict parse 失败被当 skip → 不弹卡直接重生成**——这是已识别的缺陷形态，`_drop` 是其封堵）→ `_clear_session_persisted_interrupt_state`（终态相位 PHASE_CANCELLED / PHASE_SUPPLEMENTED）→ `_clear_pending_skill_turbo_hitl`。

#### 清理触点全表（五处入口拼装，无单一 owner）

| # | 触点 | 位置 | 清理内容 |
|---|---|---|---|
| 1 | rail approve（resume 注入后工具真正执行前） | executor.py use_tool | `clear_resume_ctx` |
| 2 | resume 正常完成 / NotHandled | interface_deep.py `_clear_resume_ctx` | `_clear_pending_skill_turbo_hitl` 或 isolated 清 resume_ctx |
| 3 | resume finally | interface_deep.py `_resume_impl` finally | clear_resume_in_flight（isolated 落盘） |
| 4 | 工具体瞬时失败 | skill_turbo_tools.py 工具体 finally | **不清理、不 post_run**（保留断点，见入口 A 三态） |
| 5 | 新一轮 fresh 执行 | executor.py `execute_plan_stream` | `_clear_stale_node_artifacts`（无条件，防全新任务误复用旧产物） |
| — | 轮次正常收尾（无新卡） | interface_deep.py `_mark_round_end_inmemory` | 相位回 idle + 全部活卡转死卡 |

五处入口、三套组合，靠注释与测试守护一致性。**新增清理入口或改动任一处，必须对照 N1 全表核查"哪些退出路径还没清、哪些路径多清了"**；重构方向候选是"中断状态单一 owner"（resume_context.py 已开先例）。

## 异常分支与边界 case（28 条全矩阵）

| # | 场景 | 当前处理 | 测试覆盖 |
|---|---|---|---|
| 1 | **嵌套 HITL**：外层 skill_acceleration_exec 中断 + 内层 ask_user 中断并存 | resume_ctx 存在时必须继续 SkillTurbo resume（否则 answers 只更新 AskUserRail 而无人跑 resume 流，重发外层权限被去重成 no-op）；无 resume_ctx 时 deferred to DeepAgent（外层权限中断 own the turn） | test_hitl_resume_react |
| 2 | **resume_in_flight 重复作答** | 第二次作答 → duplicate placeholder 空流 no-op（不弹错、不重跑）；mark 跨 session 持久 | test_hitl_resume_react |
| 3 | **merge 语义陷阱**（update_dict 无法删键） | save 显式写 `resume_in_flight=None`（None 删键），防 stale in_flight 残留 | test_hitl_resume_react |
| 4 | **post_run 幂等 no-op** | 同一 session 再 post_run 不 commit → clear 必须走独立 session（isolated 通道） | test_hitl_resume_react |
| 5 | **post_run 全量覆盖复活** | clear 后必须同步清调用方内存快照，否则 turbo_session.post_run 把旧条目写回复活 | test_hitl_resume_react（real session 复现守护） |
| 6 | **resume 内二次中断的卡片 id** | 必须用 outer_call_id（剥 `#n` 后缀），否则前端按外层 call id 路由作答收不到第二张卡 → 答案丢失、会话卡死；无后缀 base 不得回退空串 | test_hitl_resume_react |
| 7 | **TIE 重写生存性**（三约束） | 标记 dict 必须穿过 AbilityManager 链（不能 raise TIE，会被包成 AbilityExecutionError）+ 跨执行上下文（ContextVar 不可见 → 返回值为主信号）+ deepcopy（SDK tracer 深拷贝 span 返回值） | test_tie_rewrite |
| 8 | **stale TIC 劫持无关工具** | ContextVar TIC 仅对 skill_acceleration_exec 生效；其他工具名命中 → 丢弃 + warning | test_tie_rewrite |
| 9 | **标记校验失败** | model_validate 失败 → 降级为可读占位文案（防无语义 JSON 让模型误判失败退回 skill_tool） | test_tie_rewrite |
| 10 | **暂停后取消**（interaction / 非 interaction 两路） | 先停执行再清状态（N10）；清 INTERRUPTION_KEY + 隔离键 resume_ctx，留 node_artifacts；跳过其它 session 与纯 ask_user 轮 | test_interrupt_recovery |
| 11 | **resume 成功后清理** | `_clear_pending_skill_turbo_hitl` 命中则不再 isolated fallback；miss（loop session 不匹配）则退化仅清隔离键 | test_interrupt_recovery |
| 12 | **resume 重放产物继承**（五情形） | plan_code_hash 匹配才继承；不匹配跳过；旧记录无 hash fail-open；出错 fail-open；无记录 no-op；persist 必写 hash | test_interrupt_recovery |
| 13 | **暂停后重发纯文本** | cancel 已清状态 → 全新 invocation 意图判断；未清时的缺陷形态：文本被塞进 rail.resolve_interrupt，parse 失败被当 skip → 直接重生成（`_drop_pending_pure_ask_user_round` 封堵） | test_interrupt_recovery（间接） |
| 14 | **乱序点击 / 多卡并发** | 挂起表复用分支重发原卡（不新增代次）；死卡守卫拒绝旧卡应答；守卫 fail-open | test_hitl_resume_react |
| 15 | **跨会话并发同参数 ask_user** | request_id 混入 args_hash 生成不同 tcid / 卡 id，作答不串台；代价是精确匹配失效，name+idx 回退成预期路径 | test_repro_cross_session_tcid_collision（tcid 生成层）；作答路由隔离端到端无 |
| 16 | **tcid 漂移**（重放参数非确定） | name+idx 回退命中，改用 expected_tcid 对齐注入（防二次中断死循环） | executor consume 相关用例 |
| 17 | **作答 request_id 与 pending 不一致** | 仅 warning，继续 resume（宽容策略） | —（无专门测试） |
| 18 | **resume 时 interactive_ask 缺省** | 从 saved_inputs 恢复中断前引导模式（防 preview 类 ask_user 误判 skipped / 引导管线退化） | interactive_ask 单测；resume 两入口均调用 |
| 19 | **非引导模式 + preview 类 ask_user** | 不中断不弹卡，直接 `{"status":"skipped"}` 沿用当前内容（`_should_skip_for_non_interactive`） | test_ask_user_rail_gates |
| 20 | **user_input 形态不认识** | re-interrupt（重新中断等待）；确定性 tcid 相同 → 重问帧可能被 sidecar dedup 吞卡（注释警示） | test_ask_user_rail_gates（形态不认识 → re-interrupt 分支）；重问帧 dedup 吞卡为注释警示面，无单测 |
| 21 | **resume 时 skill_acceleration_exec 重复工具事件** | RESUME_USER_INPUT_KEY 存在 → before_tool_call 跳过 tool_call / tool_update emit；首次调用仍 emit | test_hitl_resume_react |
| 22 | **权限账本误吞 ask_user 应答** | ask_user continuation 不进 permission ledger；且不归一化 `#n` 后缀（防死锁）；非 ask_user source 被忽略 | test_hitl_resume_react |
| 23 | **HITL 占位与上下文修复** | `_fix_incomplete_tool_context` 识别占位模板；STOP hint 优先于占位（防暂停被当完成） | test_hitl_resume_react |
| 24 | **连续多卡**（同外层调用第 N 次中断） | `{id}#{n}` 序号区分；回传剥后缀还原；序号计数跨请求存续（adapter 实例） | test_hitl_resume_react |
| 25 | **resume 完成收尾帧** | delta 承载终稿 + **空 content 的 chat.final**（防上层前端终稿拼接 computeFinalTextDelta 双拷贝）；HITL resume 绕过 DeliverySummaryRail，只发 P10 骨架 / 安全短句 | test_ppt_delivery_summary（resume 终稿骨架用例） |
| 26 | **瞬时失败保留断点** | release_checkpoint=False → 不清 resume_ctx、不 post_run（防 stale 内存态 clobber 磁盘有效断点） | test_skill_turbo_tools（保留三态 + NotHandled 对照用例） |
| 27 | **save_resume_ctx 失败** | 仅 warning，中断照常上抛（断点丢失 → 作答退化 DeepAgent 路径） | —（无单测） |
| 28 | **session_id 缺失** | inputs 无 session_id / conversation_id → 随机 UUID sid，恢复按 request.session_id 无法命中（warning 提示查 metadata 绑定链路）；无 card → pre_run / post_run 崩溃无法持久化 | —（无单测） |

## 修改检查单

动 HITL 相关代码前，逐条对照（每条附"为什么"）：

1. **动 HITL 任何代码 → 必跑全场景矩阵**：单卡 / 连续多卡 / 暂停后取消（两路）/ 暂停后重发纯文本 / 嵌套外层中断 / 重复作答 / 二次中断 / 跨会话并发同参数 / resume 内权限卡。为什么：本域缺陷的主形态就是"只改部分路径"——九个场景分别压 P1-P5 的不同组合分支，单场景通过不代表其它路径正确。
2. **改发卡 → 核对两个产出源口径一致**（request_id 同为外层 call id 域，二次中断卡用 `_derive_outer_call_id` 剥 `#n`）+ `_dedupe_ask_user_card` 三分支行为不变。为什么：两源口径漂移会直接造成"第二张卡收不到作答"，且前端按外层 call id 路由，任何偏移都是静默丢答案。
3. **改清理 → 枚举五处清理入口逐一确认** + 先终止后清顺序（N10）。为什么：清理是拼装式设计（无单一 owner），漏一处入口 = 某条退出路径残留断点；顺序颠倒 = fresh 执行退出路径覆盖清理。
4. **新增宽泛 except → 必答"AbortError 怎么过"**。为什么：14 处守卫点任何一处被新 except 吞掉，三层中断视图下两层直接失效，且无报错——表现为"卡不发、断点不存"的静默故障。
5. **改 tcid 格式 → 同步两个解析函数**（`_parse_tool_name_from_call_id` / `_parse_call_idx_from_call_id`）**+ 回退命中链 + 重新评估误注入面**（残余风险：同名同 idx 不同参数的调用会被误注入）。为什么：解析函数与生成格式强耦合，单边改动使回退命中静默失效，形成二次中断死循环；误注入面当前无防护断言，只有确定性重放兜底。
6. **改 save / clear → 核对 merge 语义**（显式全量键、`resume_in_flight=None`）**+ isolated 通道 + 调用方内存快照同步清**。为什么：两大持久化陷阱（update_dict 无法删键 / post_run 全量覆盖复活）都只在"看起来正常的写法"下触发，且后果是断点复活或 duplicate 误判，测试桩难以复现。
7. **改 use_tool → 检查 `_consume_pending_resume_input` 与 `save_resume_ctx` 的配对**（先消费 pending 再落盘断点，两处共用 tcid）。为什么：消费与落盘错位会导致"作答被消费但断点指向错误 tcid"或反之，下一轮 resume 永远命中不了。
8. **改 ask_user rail → 检查 preview skip 与 re-interrupt 两分支**（非引导模式 preview 直接 skipped；user_input 不认识 re-interrupt）。为什么：两分支共用 `resolve_interrupt` 入口，参数语义（user_input None / 不可解析）一旦混淆，会把"该中断的"变 skip 或把"该注入的"变重问。
9. **改工具体 finally → 核对 release_checkpoint 三态**（正常完成清 / NotHandled 清 / 瞬时失败保留且不 post_run）。为什么：瞬时失败误清断点 = 用户作答后无法恢复；误 post_run = stale 内存态 clobber 磁盘有效断点（陷阱二变体）。
10. **改恢复入口 → 入口 A / B 同步改**（adapter `_try_skill_turbo_resume` 与 skill_turbo 工具体 resume 分支）。为什么：两入口殊途同归 `resume_stream`，但 user_input 组装、session 解析、清理时机各有独立代码——单边改动造成"一条路能恢复另一条路不能"。
11. **改卡片注册 → 检查有界约束（1024 / 512）与重启语义**（内存态丢失后死卡守卫 fail-open）。为什么：注册表无界 = adapter 长生命周期内存泄漏；忽略重启语义 = 把 fail-open 兜底误当强保证设计新逻辑。
12. **改 `_clear_pending_skill_turbo_hitl` → 五步完整性与键空间**（DeepAgent 键 vs 隔离键；保留 node_artifacts；save_contexts + commit）。为什么：漏一步即"清了一半"——弹消息 / 清中断键 / 清断点三者任一缺失都让下一条消息进错误分支。
13. **改 TIE 重写 → 三生存性约束重验**（AbilityManager 链 / 跨执行上下文 / deepcopy 安全）。为什么：标记 dict 的三个约束各挡一个运行环境，任一退化都在不同部署形态下爆炸（错误包装 / ContextVar 不可见 / tracer 崩溃），本地测试常只覆盖其一。
14. **改作答路由 / 权限账本 → 豁免与 `#n` 后缀语义**（ask_user continuation 不进 ledger、不剥后缀；回传侧 `_build_inputs` 剥后缀还原）。为什么：`#n` 同时承载"同 base 第 N 个权限门"语义，归一化时机错位（入站剥 vs 回传剥）会死锁或串卡。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] execution-pipeline | 本 spec 的中断路径嵌在 S_01 主循环内；退出路径全景表 E1-E13 中 **E8（HITL 中断）** 的完整展开即本 spec P1。executor 层"AbortError 不发终态帧"的收口约束由 S_01 固化，本 spec 补 adapter 层收尾。重放续执行五机制与 S_01 的 task_states / node_artifacts 生命周期共用地基。 |
| [[S_02]] skill-routing-and-registration | resume 不重规划（N11）依赖 plan_code 持久化；`_sync_skill_name_to_inputs` 把技能名写入 inputs、resume 经 "not in merged" 保护还原，技能名同步链的契约归 S_02。 |
| [[S_03]] security-model | plan_code 的受限命名空间与 import 白名单在 resume 重放时同样生效（`_prepare_root_node` 重新加载）；本 spec 不重复安全判定。 |
| [[S_04]] node-contract | ask_user 的节点侧调用契约（skill_code 视角的 `call_tool("ask_user")` 语义、返回形态）归 S_04；本 spec 只管 executor 侧 rail 的中断判定三分支（首次 interrupt / re-interrupt / preview skip）。 |
| [[S_05]] dynamic-package-and-facade | resume 重放时 `_prepare_root_node` → `_load_plan_namespace` 的动态包自愈重注册保证 plan_code import 可解析；断点跨进程存活依赖该机制。 |
| [[S_07]] fallback-and-degradation | AbortError 不进 fallback 是两 spec 的共同边界：plan_node 模板方法中 AbortError / FallbackContractError 直通，fallback_handler validator 内 AbortError 也原样上抛。本 spec P1 步 6 的透传链在 S_07 有降级矩阵视角。 |
| [[S_08]] event-stream-protocol | 三件套事件（tool_call(pending_approval) / tool_update / ask_user_question）与 invocation_paused 终结帧的字段定义、前端消费契约归 S_08；本 spec 只定"何时发、谁发、发几张"。 |
| [[S_09]] outer-integration | ContextVar 桥（7 通道 bind / reset、`skill_turbo_resume_answers` / `skill_turbo_hitl_tic` 等）的机制细节归 S_09；本 spec 引用其结论：跨上下文主信号走工具返回值，ContextVar 仅同上下文兜底。 |
