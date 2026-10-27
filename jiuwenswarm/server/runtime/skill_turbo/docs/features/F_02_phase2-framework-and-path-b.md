# F_02 · SkillTurbo 阶段2框架优化与 ask_user resume Path B 统一

## 元信息

| 项 | 值 |
|---|---|
| 类型 | feature |
| 编号 / slug | F_02 / phase2-framework-and-path-b |
| 归档日期 | 2026-10-16 |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（skill_turbo_tools.py、plan_node.py、executor.py、context_binding.py、hitl_dedup.py、resume_context.py、rails/permission_rail.py）+ `agent_adapter/interface.py`、`agent_adapter/interface_deep.py`、`agents/harness/common/rails/`（stream_event_rail.py、task_execution_rail.py） |
| 涉及 specs | [[S_01]] / [[S_04]] / [[S_06]] / [[S_07]] / [[S_08]] / [[S_09]]（本次同步修订） |
| 状态 | 已落地 |

## 背景与动机

阶段1（HITL 状态机收口 + specs 首批归档）落地后，遗留六类结构性问题，本特性一次收口：

1. **resume 双入口漂移**：ask_user 作答存在两条恢复路径——adapter 侧 `_try_skill_turbo_resume`（Path A，adapter 直连 `resume_stream`）与 harness 原生 resume → 工具体内恢复（Path B）。两入口的 user_input 组装、session 解析、清理时机是独立实现（[[S_06]] 检查单第 10 条的长年负担），Path A 的 `_resume_impl` 还自带一套发卡/收尾流，与 harness 语义持续漂移。
2. **失败不诚实**：节点失败横幅（`failed/error/warning`）与后续 delta 拼成一行，前端无法分栏渲染；用户看到的"失败"信息被视觉吞掉。
3. **停止提示一刀切**：skill_turbo 停止提示恒定"不要再调工具"，外层 todo 多任务场景下把"标完成当前项并继续下一项"也一并阻断。
4. **附件必现失败**：带附件请求走加速通道时 `request.params.files` 断在 adapter 层，流水线拿不到文档输入，P3 文档解析恒失败。
5. **节点"软失败"无法兜底**：三态节点（ok/partial/failed）以 dict 返回失败而非 raise 时，节点级 fallback 完全不触发（fallback 只吃异常），只能整 plan 终止。
6. **死代码与散装计数**：executor `_build_permission_rail`（阶段1遗留零调用）、`should_suppress_subplan_start_banner` callback slot（被 task.start 内联跳过取代后仍占槽位）、发卡序号计数散落 adapter。

## 设计决策

### D1 ask_user resume 统一走 handle_resume（删 Path A）

**决策**：删除 `_try_skill_turbo_resume` / `_make_skill_turbo_resume_stream` / `_make_skill_turbo_resume_{duplicate,stale}_placeholder` / `_derive_outer_call_id` / `_emit_skill_turbo_hitl_chunks` / `_deep_agent_has_skill_turbo_interrupt` 全套 Path A 实现；作答统一走 harness 原生 resume（`ReactAgent._handle_resume` → `ToolInterruptHandler.handle_resume` 重跑 `skill_acceleration_exec` 工具体，工具体内 resume 分支载入断点执行 `resume_stream`）。

Path A 的两个不可丢职责迁移到 `interface.py` 公共入口（unary/stream 双路径）：
- **终态守卫** `guard_stale_interrupt_response`：死卡 / cancelled / supplemented 相位拒绝；
- **作答消费销毁** `_consume_hitl_live_card_for_answer`：guard 放行（含 fail-open）即摘除 base 挂起表条目。

**拒绝的方案**：
- *保留 Path A 修 bug*：两入口语义对齐（user_input 组装 / in_flight 防重 / 清理时机）需要永久性双份维护，历史上每修一处都要"同步另一侧"；Path B 天然复用 harness 的工具重放、TIE 重写、`__interaction__` 发卡全链，代码量净减 ~700 行。
- *Path A 改为薄委托 Path B*：委托后 Path A 只剩守卫 + 转发，而守卫已可在公共入口更早执行（覆盖所有中断续聊形态，不限于 skill_turbo），中转层无存在价值。

**连带决策**：
- `SkillTurboPermissionRail.before_tool_call` 对结构化 ask_user resume 短路（`_is_ask_user_resume`：`skill_acceleration_exec` + `RESUME_USER_INPUT_KEY` + `_is_structured_ask_user_payload` 命中 P2 协议化 dict `{"status","answers"}` 或裸 answers list，非 ConfirmPayload）——否则 handle_resume 重跑工具时权限卡重弹（R3 修复：P2 dict 形态最初漏判）。
- 工具体 resume 分支补 `resume_in_flight` 判重 + `mark_resume_in_flight` + `finally clear_resume_in_flight`（仅实际 mark 过才清，`_did_mark_in_flight` 局部标志防重复作答的提前返回误清原始请求的 in-flight）——对齐 Path A 原防重语义。
- `mark_in_flight` 重构为 isolated session 通道强制落盘（对齐 `clear_in_flight`）——防 post_run 主 session 把中断过程整份状态写盘。
- 发卡收口为**单一产出源**：harness `commit_interrupt` 写 `__interaction__` → DeepAdapter 转换 → `_dedupe_ask_user_card`。resume 内二次中断不再由 `_emit_skill_turbo_hitl_chunks` 单独发卡。

### D2 发卡去重收口 OuterCallIDDeduplicator（hitl_dedup.py 新建）

**决策**：`derive_outer_call_id`（剥 `#{n}` 后缀）与 `next_final_id`（同 call id 第 N 次中断 → `{id}#{n+1}`，计数挂 adapter 实例跨请求存续）收口为独立类；adapter 经 `_outer_call_dedup` 实例持有，`_ask_user_card_seq` 改 thin delegate property（getter/setter 薄委托，守护直接断言该属性的既有测试）。

**拒绝的方案**：序号计数改请求级集合——同 base 卡序号跨请求存续是"连续多卡"场景的正确语义（[[S_06]] N8），请求级会回卷出旧 `#n` 冲突，语义红线写死在模块 docstring。

### D3 停止提示 todo 感知（outer_todo_pending 第 8 通道 + CONTINUE/POST 拆分）

**决策**：新增 `outer_todo_pending` ContextVar（`context_binding` 第 8 通道）：
- `TaskExecutionRail.before_tool_call` 在 `skill_acceleration_exec` 调用时经 `_pending_items_after` 导出"当前绑定项之后未完成项快照"（`{count, next_summary}`，无后续项为 None）；
- `StreamEventRail` 读 extra 统一 bind/reset；
- `_wrap_skill_turbo_result` PPT 骨架成功分支按 pending 拆分：有后续项 → CONTINUE 提示（先 `todo_modify` 标完成当前项，再继续下一项 `{next_summary}`）；无 → 原 POST 提示。

与外层 todo close nudge（dev-stable `536a4d050`，纯文本收尾强制关闭当前项）互补：pending 管住"关了之后继续跑"，nudge 管住"没关就想收尾"。

**边界**：拆分仅覆盖 PPT 交付骨架路径；通用确认 / NEUTRAL 分支维持原固定提示（无 todo 感知），见 follow-up。

### D4 失败如实显示（五状态换行）

`_make_node_delta_chunk` 的消息补换行状态从 `progress/ok` 扩展为 `progress/ok/failed/error/warning`，且改为 `rstrip("\n") + "\n"`（先剥净尾部换行再补恰好一个）——防失败/降级横幅与后续 delta 粘连成一行，也防多个 `\n` 累积空行。

### D5 附件支持（request.params.files → inputs.files）

四跳链路：`request.params["files"]` → `_RuntimeConfig.files`（三处请求入口构造时提取）→ `_update_runtime_config` 注入 `request_metadata["files"]` → 工具体复制 `inputs["files"]`（**原样透传**，dict（平台 `files.uploaded`）与 list（结构化附件）两种形态都过，由技能侧 `intent_classify._collect_files_paths` 统一展平做 `has_documents` 检测进 P3 文档解析）。

**拒绝的方案**：引擎侧展平 files 形态——平台格式是上层契约，引擎侧"猜格式"会复制一份解析逻辑且随平台演进腐化；`inputs["files"]` 是引擎侧唯一对接点，展平权留给技能侧。

### D6 节点 fallback 标志检测（_needs_fallback + 防循环 guard）

**决策**：三态节点以 dict/last_chunk 携带 `_needs_fallback=True` 表示"节点硬失败需要修复"，模板方法在**正常返回后**检测并转入与 raise 异常**同一 fallback 通道**：
- `PlanNode.run()`：返回 dict 带标志且未触发过 → 构造 `RuntimeError` 进 `_fallback_callback`；
- `PlanNode.execute_subplan_stream()`：子节点流 last_chunk 带标志 → 剥标志 → 经 `subplan_fallback_stream` 续接产出（fallback chunk 续在原流尾部，`last_chunk` 逐个替换为修复结果，after 回调看到修复后结果）。

**防循环 guard**：`_fallback_triggered` 同节点一次 run 至多触发一次；进入 fallback 前剥离标志位（防 fallback 结果再带标志形成循环）。父节点经公开访问器 `try_begin_fallback()`（原子判定+置位）与 `fallback_stream_callback`（只读）访问，不直访子节点受保护成员（G.CLS.11 门禁）。AbortError / FallbackContractError 仍不进 fallback（标志检测在异常分流之后，不影响既有透传顺序）。未标注节点行为不变（无标志 = 零改动）。

### D7 死代码清理

- 删 `executor._build_permission_rail`（阶段1遗留，审批已收口外层工具一次性授权，内层只挂 stream_event / ask_user / llm_retry / artifact 四类 rail）；
- 删 `should_suppress_subplan_start_banner` callback slot（plan_node / executor 双侧 + 测试类）：resume 重入的 task.start 跳过已内联进 `_before_subplan_execute` 按 task_states 快照判定，横幅抑制接口无消费价值——**接受的行为变化**：in_progress stage resume 重入时技能侧"开始执行"横幅会重新发出（此前被抑制），换取 12 槽位 → 11 槽位的契约简化；
- 删 `should_suppress_subplan_start_banner` 后 S_04/S_06/S_08 中相关引用本次同步清理。

## 已知边界与 follow-up

| # | 项 | 现状 | 处置 |
|---|---|---|---|
| 1 | `hitl_dedup.derive_outer_call_id` 零生产调用 | 原消费方（`_derive_outer_call_id`）随 Path A 删除；剥后缀职责实际由 `interrupt_helpers.strip_hitl_seq_suffix` 承担 | 保留作收口 API（测试持有）；若长期无消费者可随下次清理删除 |
| 2 | `permission_bridge.build_interaction_output_from_abort` 零调用 | 原消费方（executor 直写与 `_emit_skill_turbo_hitl_chunks`）均不在 | 已修：docstring 改写为不再引用已删符号（注释清理提交），标注零调用现状；函数保留待未来消费方，长期无消费者随下次清理删除 |
| 3 | 非 PPT 骨架路径停止提示无 todo 感知 | confirmed / NEUTRAL 分支仍固定文案 | follow-up：待多任务场景验证后再推广拆分 |
| 4 | resume 重入横幅不再抑制 | in_progress stage 重入重发"开始执行"横幅 | 有意接受（见 D7）；前端若反馈噪声再评估 |
| 5 | [[S_06]] 清理五入口拼装 | Path A 删除后清理入口减少一处拼装组合，但"无单一 owner"现状未变 | follow-up：中断状态单一 owner 重构 |
| 6 | 代码注释通道数 | context_binding.py / stream_event_rail.py 注释原写"7 个"，本次随 feat 修正为 8 个 | 已修 |
| 7 | C-2 生产者位置 | `_needs_fallback` 标注**已随技能侧仓落地**（pptx-craft：P8.0 预处理失败 / P8 qa 全败 / P9 页面恢复与 convert 失败，共 6 处），不在本仓——引擎侧消费链（plan_node 检测 / guard / 剥离 / executor 帧级剔除）已就绪并有单测锁定 | 已修（B_02 批次）：`_make_node_delta_chunk` data 浅拷贝剔除该键，防随 chat.delta.data 下发前端；技能侧标注演进以技能侧仓为准 |

## 与 specs 的对应

| 决策 | 修订的 spec 章节 |
|---|---|
| D1 | [[S_06]]（一句话状态机 / N8 / P2 发卡路径 / P3 恢复路径全节 / 边界 case / 检查单 2·10）；[[S_09]]（装配点 8 / 4.6 接缝点表 / P7） |
| D2 | [[S_06]]（P2 / 数据结构第二层）；[[S_09]]（4.6 / adapter 内存态） |
| D3 | [[S_09]]（N1 / 4.2 八通道表 / 数据结构）；[[S_07]]（P12 收尾三分支的 CONTINUE 拆分） |
| D4 | [[S_08]]（通道 A `chat.delta` 行为说明） |
| D5 | [[S_09]]（4.5 附件链路 / 数据结构 request_metadata） |
| D6 | [[S_04]]（契约 8 / N4 / 异常分流 / 回调槽公开访问器）；[[S_07]]（4 层决策表行 3 / 不变量 18 / P3 / 边界 case / 检查单 / 测试资产） |
| D7 | [[S_01]]（回调数 12→11 / 横幅抑制表述）；[[S_04]]（回调槽 12→11）；[[S_06]]（P4 机制 4）；[[S_08]]（P3/P9 横幅表述） |
