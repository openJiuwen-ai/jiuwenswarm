# S_19 取消结算与收尾

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_19 / cancellation-settlement-and-finalization |
| 关联模块（agent-core 仓） | 无本体文件——`INTERRUPTION_KEY` 与 `ToolInterruptionState` / `ToolInterruptEntry`（[[S_15]]）是结算对象；`ToolMessage` / `AssistantMessage`（`core/foundation/llm`）是写入载体；`CheckpointerFactory` / `create_agent_session` / `AgentCard` 是持久化结算的装载器 |
| 关联模块（jiuwenswarm 仓） | `server/runtime/session/permission_interrupt_cancel.py`（取消结算全模块，551 行：三级入口 + 纯 confirm 判定 + pending 收集 + 合法 ToolMessage 写入 + 清键 + 持久化）；`server/runtime/skill_turbo/hitl_dedup.py`（`OuterCallIDDeduplicator`，77 行：`{id}#{n}` 序号代际）；调用方：`agents/harness/team/team_manager.py`（Team cancel 两级结算 `_settle_live_cancelled_permission_interrupt` / `_settle_persisted_cancelled_permission_interrupt`）、`agent_adapter/interface_deep.py`（发卡路径 `next_final_id` 调用点）、`agent_adapter/interface.py`（应答回传 `_build_interactive_input_from_answers` 剥后缀入口） |
| 最近一次修订日期 | 2026-10-26 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管 **HITL 中断被用户取消后的 ReAct 消息合法性结算与序号代际收尾**——"取消"事件如何不破坏会话结构、如何防同 id 卡片重复：

- **取消结算三级入口**（`permission_interrupt_cancel.py`）：`settle_cancelled_confirm_interrupt`（核心，任意 session）、`settle_live_cancelled_confirm_interrupt`（仍存活的 TeamAgent / NativeHarness）、`settle_persisted_cancelled_confirm_interrupt`（checkpoint 重载会话清残留）
- **纯 confirm 判定**：`is_pure_confirm_payload_interrupt` 本地实现（不 import agent-core `resume_guard`——CI pin 的旧版不含该函数）与 `_is_confirm_payload_schema` 判定式
- **pending 收集**：`collect_cancelled_confirm_tool_calls`（`interrupted_tools` 主源 + `state.ai_message.tool_calls` 兜底，按 tool_call_id 去重）
- **合法 ToolMessage 写入**：`insert_cancelled_tool_messages` 插入算法（跟随匹配 assistant tool_calls、不产孤儿、不重复）、三通道降级写入（context `set_messages` → `add_messages` → session state context blob 双写清写）、`cancelled_tool_result_content` 双语占位文案
- **清键与持久化**：清 `INTERRUPTION_KEY`、`hitl_handler.clear`、`save_contexts` + `commit`
- **序号代际**（`hitl_dedup.py`）：`OuterCallIDDeduplicator` 的 `derive_outer_call_id`（剥 `#数字` 后缀）与 `next_final_id`（`{id}#{n}` 序号计数）、`card_seq` thin delegate 兼容层

不管：

- resume 时未匹配 confirm 的关闭路径（`abandon_unmatched_confirm_interrupt` 写 cancelled ToolMessage 的 agent-core 侧实现）→ [[S_15]] N13——本 spec 是 **server 侧用户取消**路径，S_15 N13 是 **resume 应答未命中**路径，两者文案与"去重后追加"语义须保持一致
- 中断相位机 / 终态守卫 / 权限响应台账 / cancel·supplement 的会话标志清理（`_clear_session_persisted_interrupt_state`）→ [[S_18]]（Team cancel 场景 S_18 P4 终态落盘与本 spec 结算配合成链）
- 发卡路径的 questions_key 判重 / 挂起表复用 / 活性注册表 → [[S_17]] / [[S_18]]（本 spec 只管 P1 发卡序号段的 `next_final_id` 调用点）
- Team cancel 的任务终止 / runner pool 拆除 / 团队生命周期 → agent_teams 域（本 spec 只管 team_manager 调用的两个结算包装函数）
- SkillTurbo HITL 作答协议本体（answers 规范化 / `normalize_ask_user_response`）→ skill_turbo 域 [[S_06]]（本 spec 只管剥后缀对齐 harness 原始 id 的共用规则）

## 不变量

- **N1（取消绝不视为批准）**：cancel 结算不把取消当 approved——只写 cancelled `ToolMessage` 占位 + 清 `INTERRUPTION_KEY`，绝不写入批准性 tool_result，绝不触发工具重放。历史 bug：Team cancel 在客户端隐藏审批卡却在 session 残留 `INTERRUPTION_KEY`，下一条纯文本被当 resume 输入 → re-interrupt 死循环。
- **N2（混合中断不动）**（@39-64）：`is_pure_confirm_payload_interrupt` 为真须同时满足——`interrupted_tools` 非空 dict，且**每个 entry**：工具名 ∉ `_SKIP_CONFIRM_TOOL_NAMES`（`{ask_user, skill_acceleration_exec}`）、`interrupt_requests` 非空 dict 且**每个 request** 的 payload_schema 均为 confirm 判定式。混合中断（含 ask_user 答案 / SkillTurbo / 未知 schema）一律返回 False 不结算——cancel 不得丢掉下一轮仍需要的问卷。
- **N3（本地实现与 agent-core 同步）**：`_is_confirm_payload_schema`（properties 含 `"approved"` 且不含 `"answers"`）与 `_SKIP_CONFIRM_TOOL_NAMES` 是 agent-core `resume_guard` 的本地复制——CI pin 的 openjiuwen 旧版不含这些函数，不能 import。修改 agent-core 侧判定式（[[S_15]] N5）或本侧任一处，必须同步另一侧，否则恢复路由与取消结算对同一 state 给出矛盾分类。
- **N4（合法 ReAct 结构，不产孤儿）**（@376-428）：cancelled `ToolMessage` 只允许插在其 tool_call_id 对应 assistant 消息之后——一个 checkpoint 可能含多个 context blob，盲目尾部追加会产生没有 assistant 调用对应的孤儿 ToolMessage；transcript 已有同 id ToolMessage 的不重复写（`missing` 过滤）。
- **N5（插入位置语义）**：`insert_cancelled_tool_messages` 扫描消息序列，找到带 tool_calls 的 assistant 消息后，先收编其**连续既有 ToolMessage 块**，再对该块补插缺失的 cancelled 项；全序列未匹配到的 pending 原样留存（返回值不含），**不得**追加到尾部。
- **N6（pending 收集双源去重）**（@67-87）：主源 `interrupted_tools`（entry.tool_call 缺 id 时回退 outer_id），兜底 `state.ai_message.tool_calls`（state 结构不完整时补齐）；`seen` 按 tool_call_id 去重；名称缺省回退 `"tool"`。
- **N7（写入三通道降级序）**（@431-455）：① `context.get_messages` + `insert` + `context.set_messages(rebuilt, with_history=True)`；② set_messages 不可用时 `context.add_messages(to_add)`（按既有 tool_call_id 过滤后追加）；③ 前两者 written ≤ 0 或无 context_engine 时降级 session state `"context"` blob 逐个插入。written 取各通道最大值——同一批 pending 在多通道写入不叠加计数。
- **N8（session state 双写清写）**（@496-531）：blob 通道写入后必须 `update_state({"context": None})` 再 `update_state(context_state)`——直接整量覆盖会与既有键合并残留旧 messages；遍历**全部** blob（非首个命中即停）。
- **N9（结算序固定）**（@90-139）：读 `INTERRUPTION_KEY`（读失败 False）→ 纯 confirm 判（非纯 False，session 不动）→ 收集 pending → 写 ToolMessage → **清 `INTERRUPTION_KEY`（update None）** → `hitl_handler.clear`（失败仅 debug）→ `_persist_settled_session`（`context_engine.save_contexts` + `session.commit`）→ 返回 True。True 仅当确有纯 confirm 中断被结算。
- **N10（live 入口解析容错）**（@223-296）：`resolve_live_confirm_interrupt_resources` 全程 getattr 链 best-effort——`harness._native` 缺失用 harness 本体；session 候选序 `harness._interrupt_session()` → `agent_like._session` → `loop_session`，取**首个持 `INTERRUPTION_KEY`** 者（全无则首个候选）；`context_engine` 从 agent_like 或 react_agent 解析；language 从 `system_prompt_builder.language` 解析，缺省 `"cn"`。任一步失败记 debug 降级，不抛。
- **N11（persisted 入口会话预检）**（@167-220）：`CheckpointerFactory.get_checkpointer().session_exists(sid)` 为 False 时跳过（session_missing）；`exists` 检查本身异常按存在处理继续尝试；重载序 `create_agent_session`（card 缺省 `AgentCard(id="jiuwenswarm", name="jiuwenswarm")`）→ `pre_run` → settle → `post_agent_execute_for_session` → `commit` → `finally post_run`。
- **N12（双语占位文案）**（@28-36）：cn 默认 `[工具执行被中断] 工具 {name} 执行过程中被用户打断，没有执行结果。`；language 以 `en` 开头用英文 `[Tool interrupted] Tool {name} was interrupted by the user and has no result.`；工具名空白回退 `"tool"`。文案是 ReAct 协议占位符——模型依赖它理解"被打断、无结果"，措辞变更需过回归。
- **N13（序号代际语义与 ⛔ 红线）**（hitl_dedup.py @66-77）：同外层 call id 第 N 次中断：首次 `final_id = request_id`，之后 `{request_id}#{seq+1}`；序号计数**挂在 adapter 实例上跨请求存续**（`_outer_call_dedup` 持有，`_ask_user_card_seq` 为 thin delegate）。⛔ 不得改成请求级集合（R3 变种）——跨请求递增是去重代际（[[S_18]] 死卡正判 / 挂起表复用）的根基，改为请求级会让 resume 内二次中断的新卡与旧卡同 id，去重失效。
- **N14（derive 剥后缀规则）**（@54-64）：`derive_outer_call_id` 用正则 `^(?P<base>.+)#\d+$` 剥 `#数字` 后缀；有后缀取 base，无后缀**保留原值不返回空串**。与 `interface._build_inputs` / `strip_hitl_seq_suffix` 同一套规则——作答回传靠剥后缀把 `{base}#{n}` 对齐 harness 原始 id（TIE 重写后用外层 call id）。
- **N15（thin delegate 兼容层）**（@40-52）：`card_seq` property 返回内部 dict **本体**（非拷贝）——保留 `adapter._ask_user_card_seq[tcid] = N` 直接赋值与整体 setter 两种历史用法，守护直接断言该属性的既有测试不破。删掉该兼容层前必须先迁移全部守护测试。
- **N16（Team cancel 两级结算 best-effort）**（team_manager.py @2044-2124）：live 结算在 runtime 拆除前跑（写活 session 的 context），persisted 结算在 cancel 落定后跑（checkpoint 清残留）；两级外层异常均仅 warning **不阻断 cancel 主流程**——结算失败的最坏后果是残留（由 [[S_18]] 终态守卫兜底拒绝旧卡应答），不得反噬取消本身。

## 接口契约

### 取消结算（`session/permission_interrupt_cancel.py`）

| 符号 | 签名 | 语义 |
|---|---|---|
| `cancelled_tool_result_content(tool_name, *, language="cn") -> str` | N12 双语占位文案 | 工具名空白回退 `"tool"` |
| `is_pure_confirm_payload_interrupt(state) -> bool` | N2 纯 confirm 判 | 本地实现，N3 与 agent-core 同步 |
| `collect_cancelled_confirm_tool_calls(state) -> list[tuple[str, str]]` | N6 双源收集 | `(tool_call_id, tool_name)` 去重序 |
| `insert_cancelled_tool_messages(messages, pending, *, language="cn") -> tuple[list, int]` | N4/N5 插入算法 | 返回 (rebuilt, inserted)；未匹配 pending 留存不计 |
| `settle_cancelled_confirm_interrupt(session, *, context=None, context_engine=None, hitl_handler=None, language="cn") -> bool` | N9 核心结算序 | True = 确有纯 confirm 被结算 |
| `settle_live_cancelled_confirm_interrupt(team_agent, *, language="cn") -> bool` | 活 TeamAgent 入口 | 经 N10 解析资源后转核心序 |
| `settle_persisted_cancelled_confirm_interrupt(session_id, *, card=None, language="cn") -> bool` | checkpoint 重载入口 | N11 预检 + 重载序 |
| `resolve_live_confirm_interrupt_resources(team_agent) -> tuple[session, context, context_engine, hitl_handler, language]` | N10 容错解析 | 全 best-effort，session 可为 None |
| `prompt_language_from_team_agent(team_agent) -> str` | 活 agent 语言 | 缺省 `"cn"` |

### 序号代际（`skill_turbo/hitl_dedup.py`）

| 符号 | 签名 | 语义 |
|---|---|---|
| `OuterCallIDDeduplicator()` | 无参构造，`_card_seq: dict[str, int]` | adapter 实例持有，跨请求存续 |
| `.derive_outer_call_id(params_request_id) -> str`（classmethod） | N14 剥后缀 | 无后缀保留原值 |
| `.next_final_id(request_id) -> str` | N13 序号 | 首次原 id，之后 `{id}#{n+1}` |
| `.card_seq`（property + setter） | N15 thin delegate | dict 本体，非拷贝 |

### 调用方包装（`team/team_manager.py`）

| 方法 | 语义 |
|---|---|
| `_settle_live_cancelled_permission_interrupt(session_id, team_agent, *, language)` | runtime 拆除前 best-effort；无 live agent / 非 confirm / 异常均 info/warning 后放行 |
| `_settle_persisted_cancelled_permission_interrupt(session_id, *, card, language)` | cancel 落定后 checkpoint 清残留；同样 best-effort |

## 数据结构

```python
# pending 收集结果（保持插入顺序，tool_call_id 去重）
pending: list[tuple[tool_call_id, tool_name]]

# 写入的占位消息（ReAct 合法结构：tool_call_id 必须对应既有 assistant tool_call）
ToolMessage(content=cancelled_tool_result_content(name, language), tool_call_id=tid)

# 序号计数（adapter 实例内存态，跨请求存续；重启归零——代际守卫由 S_18 磁盘态兜底）
_outer_call_dedup._card_seq: dict[outer_call_id, int]
# 正则：_SUFFIX_RE = re.compile(r"^(?P<base>.+)#\d+$")
```

## 执行路径全景

- **P1 Team cancel 结算全链**（team_manager）：用户取消权限卡 → `_settle_live_cancelled_permission_interrupt`（runtime 拆除前）：解析 live team_agent → `resolve_live_confirm_interrupt_resources` → 核心结算序（P2）→ 终止任务 / 拆 runner → `_settle_persisted_cancelled_permission_interrupt`（checkpoint 残留兜底）：预检 session_exists → 重载 session → 核心结算序 → commit。同链配合 [[S_18]] P4：终态落盘（相位 cancelled + 清哨兵 + 活卡失效）与 [[S_16]] 授权记忆不受影响（cancel 不写任何 allow key）。
- **P2 核心结算序**（`settle_cancelled_confirm_interrupt`）：读 `INTERRUPTION_KEY` → `is_pure_confirm_payload_interrupt`（N2，False 即返回不动 session）→ `collect_cancelled_confirm_tool_calls`（N6）→ `_write_cancelled_tool_results`（P3）→ 清 `INTERRUPTION_KEY` → `hitl_handler.clear` → `_persist_settled_session`。
- **P3 三通道降级写入**（N7）：context 通道（`get_messages` → insert → `set_messages(with_history=True)`，或 `add_messages` 追加）→ 不足时 session state `"context"` blob 通道（逐 blob insert → 双写清写，N8）。
- **P4 发卡序号代际**（interface_deep `_dedupe_ask_user_card`，[[S_18]] P1 的一段）：新代逻辑确定 `final_id = self._outer_call_dedup.next_final_id(request_id)`——同 base 第 2/3 次中断得 `{base}#2` / `{base}#3`，供挂起表 / 死卡集区分代次。
- **P5 应答回传剥后缀**（interface.py `_build_interactive_input_from_answers`）：入站 `params.request_id` 剥 `#数字` 后缀（`strip_hitl_seq_suffix`，与 N14 同规则）→ 以 base id 更新 `InteractiveInput`——resume 重放时 harness 侧按原始 outer_id 命中 interrupted_tools 条目。

## 异常分支与退出路径

| 触发 | 行为 |
|---|---|
| `INTERRUPTION_KEY` 读取失败 | debug 记日志，返回 False |
| 非纯 confirm（混合 / 空 / 非 confirm schema） | 返回 False，session 不动（N2） |
| transcript 已有同 id ToolMessage | 跳过该 id（missing 过滤） |
| pending 无对应 assistant 调用 | 不插入、不追加尾部（N4） |
| `set_messages` / `add_messages` 不可调用 | 降级下一通道（N7） |
| blob 通道 `get_state("context")` 失败 / 非 dict | 返回 0，交由前序通道结果 |
| `hitl_handler.clear` 失败 | debug 记日志继续（N9） |
| live 解析任一步失败 | debug 降级该候选（N10） |
| persisted `session_exists` 检查异常 | 按存在继续尝试（N11） |
| 两级 Team cancel 结算外层异常 | warning，不阻断 cancel（N16） |
| persisted settle 非纯 confirm | info（not_confirm）返回 False |

## 边界 case 清单

| case | 预期 |
|---|---|
| confirm + ask_user 混合中断后取消 | 不结算（N2），问卷留给下一轮 |
| `interrupted_tools` 为空 / 非 dict | False |
| entry.tool_call 无 id | 回退 outer_id 收集（N6） |
| `ai_message.tool_calls` 有 state 未登记的调用 | 兜底补齐（N6） |
| 同 tool_call_id 出现于两源 | seen 去重只收一次 |
| transcript 已有该 id 的 ToolMessage | 不重复写（N4） |
| checkpoint 多 blob 各含匹配 | 逐 blob 插入（N8） |
| 会话 checkpoint 不存在 | 跳过（session_missing，N11） |
| 同 base 第 2/3 次中断发卡 | `{base}#2` / `{base}#3`（N13） |
| 应答回传 `{base}#2` | 剥回 base 对齐 harness 原始 id（N14） |
| adapter 进程重启 | 序号归零从原 id 重数；代际守卫由 [[S_18]] 磁盘态相位 + 活性 fail-open 兜底 |
| language="en" / "EN" | 英文占位文案（N12） |
| live team_agent 无可解析 session | False（no_interrupt_session） |
| 取消后再发新请求 | `INTERRUPTION_KEY` 已清，按新 query 处理（N1/N9） |

## 修改检查单

改本域任何代码前，逐条确认：

1. 改 `is_pure_confirm_payload_interrupt` / `_is_confirm_payload_schema` / `_SKIP_CONFIRM_TOOL_NAMES` → 与 agent-core `resume_guard`（[[S_15]] N5/N6）逐字同步（N3），两侧跑同一组分类用例。
2. 改 `insert_cancelled_tool_messages` 插入算法 → 验证不产孤儿（无匹配不插）、不重复（同 id 跳过）、插入位置在 assistant 后连续 ToolMessage 块之后（N4/N5），跑 `test_permission_interrupt_cancel`。
3. 改三通道降级序（N7）或 blob 双写清写（N8）→ 验证多 blob checkpoint 与 `set_messages` / `add_messages` 缺失的降级路径。
4. 改 `next_final_id` 序号语义 → ⛔ 先读 N13 红线：计数必须留在 adapter 实例跨请求存续；任何"请求级集合"方案都会破坏 resume 内二次中断的代际区分。
5. 改 `card_seq` delegate → 保持 dict 本体返回与 setter 薄委托（N15），先迁移直接断言 `_ask_user_card_seq` 的守护测试。
6. 改 `derive_outer_call_id` 正则 → 与 `strip_hitl_seq_suffix` / `_build_inputs` 剥后缀规则同步（N14），三者不一致会让应答回传错位。
7. 改 `cancelled_tool_result_content` 文案 → 中英双语同步改（N12），过 ReAct 占位符回归。
8. 在 Team cancel 链新增清理步骤 → 保持 best-effort（N16），任何新失败模式不得阻断取消主流程。
9. 改 `settle_cancelled_confirm_interrupt` 返回值或结算序 → N9 序固定：清 `INTERRUPTION_KEY` 必须在写 ToolMessage 之后（先清后写会让中断态与消息态短暂不一致且写失败时已丢中断）。
10. 改 live/persisted 入口 → 验证 N10 候选序与 N11 预检，`finally post_run` 不得遗漏。

## 与其它 spec 的关系

- 上游：[[S_15]]——`INTERRUPTION_KEY` / `ToolInterruptionState` 结构与 confirm 判定式（N3 同步义务）；`abandon_unmatched_confirm_interrupt`（S_15 N13）与本 spec 是同一"写 cancelled ToolMessage + 清键"语义在 resume 未命中与用户取消两个触发点的实现，文案与去重追加语义须一致。
- 上游：[[S_16]]——confirm 卡即权限卡，取消结算不动 auto_confirm / batch_allow 记忆（cancel 永不产生授权）。
- 上游：[[S_18]]——P1 发卡序号调用点、P5 应答剥后缀是其 `_consume` / `_is_stale_hitl_card_answer` 的前置；Team cancel 全链中 S_18 P4 终态落盘与本 spec P1 两级结算配合（一个管相位与守卫，一个管 ReAct 消息修复）。
- 上游：[[S_17]]——`strip_hitl_seq_suffix` 与 `_build_interactive_input_from_answers` 的应答回传路径承载 N14 剥后缀规则的消费侧。
- 跨域：skill_turbo [[S_06]]——作答统一走 harness 原生 resume（F_02 删除 Path A 后，本模块序号器是其发卡去重的唯一实现，虽物理位于 skill_turbo 目录但服务于全部 HITL 卡）；agent_teams 域——team_manager 的 cancel 生命周期归其管辖，本 spec 只约束两个结算包装函数。
- 测试组 6：本 spec 对应 `tests/unit_tests/agentserver/test_permission_interrupt_cancel.py`（三级入口 / 混合不动 / 孤儿不写 / 双语文案 / 去重），序号代际断言散布 `test_hitl_superseded_invalidation.py`（`card_seq` delegate 守护）。
