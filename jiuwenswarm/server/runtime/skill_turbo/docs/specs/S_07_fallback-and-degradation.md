# SkillTurbo 降级体系规约（fallback-and-degradation）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / fallback-and-degradation |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`：fallback_handler.py、executor.py、plan_node.py、agent.py、skill_turbo_tools.py；外层装配 interface_deep.py（`spawn_fallback` / `DeepAgentFallbackHandler` 构建） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs） |

## 范围 / 边界

### 本规约管什么

skill_turbo 从"节点失败"到"交还外层 LLM"的**降级体系全景**：

- **4 层降级决策点**（路由级 / plan 代码级 / 节点级 / fallback 终止级）的触发条件与统一收敛出口。
- **两条诚实失败路径**：p10_delivery 交付失败硬门禁、HITL 中断不降级（仅指路）。
- 节点级 fallback 子代理**执行全链**：前置检查 → spawn 隔离 → 权限 rail 挂起/恢复 → 契约判定 → 结果回写。
- **成功判定双层 fail-closed 契约**：`_parse_fallback_output` 自证解析 + `validate_fallback_success` 交付物校验。
- `FallbackContractError` 的**三个产生源与传播链**，含"降级终止不发 chat.error"约束及其机制原因。
- 降级预算（`max_fallback_count=3` / 请求内计数）与 fallback 事件归属。
- 降级后的标准流程机制（决策交还外层 LLM）与收尾诚实性三分支。
- 失败分级的通用哲学（质量缺陷 vs 存在性缺陷 / 失败面最小化 / 磁盘为权威）。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| AbortError 全链传播、HITL 中断/恢复状态机、resume_ctx 持久化 | [[S_06]] |
| 事件帧字段、缓冲、tool 层转发与前端契约 | [[S_08]] |
| 执行主循环与退出路径全景（E1–E13 终态收口矩阵） | [[S_01]] |
| PlanNode 11 条契约全文、validator 安全模型 | [[S_04]] |
| PromptRail 指南内容与技能清单动态化 | [[S_02]] |
| 工具失败双轨契约（success=False vs 抛异常）、瞬态错误节点内自愈 | [[S_05]] |

## 降级体系总览

### 4 层降级决策表

| # | 层级 | 触发条件 | 代码位置 | 结果 |
|---|------|---------|---------|------|
| 1 | 路由级 | planner 无匹配 skill / 置信度 < 0.6 / skill_name 为空或未注册 / model_client 缺失 / 路由输出连续 2 次不可解析（仅解析类失败重试 1 次；confidence 不足与 skill_name=null 属模型正常判定，不重试） | planner.plan / planner._match_skill；agent.run_stream | `SkillTurboNotHandled("无匹配skill")` |
| 2 | plan 代码级 | `PlanCodeValidationError`（AST 安全校验失败）/ `PlanCodeLoadError`（exec 失败 / 无 root 节点） | executor._prepare_root_node；agent.run_stream | `SkillTurboNotHandled("规划代码校验或加载失败: …")`（流式路径在 executor 内以 chat.error+complete 收口后正常结束、不 raise，归属差异见 [[S_01]] E5/E6 与 E13） |
| 3 | 节点级 | 节点 `_execute/_execute_stream` 抛出**非 AbortError、非 FallbackContractError** 的异常 | plan_node.run / plan_node.run_stream（模板方法） | 进入 fallback 子代理流程（P3–P8） |
| 4 | fallback 终止级 | 契约未达成（`FallbackContractError`）或次数超限（`FallbackLimitExceededError`） | fallback_handler；executor._check_fallback_limit | 终止本 plan，不发 chat.error，上抛 → `SkillTurboNotHandled` |

### 统一收敛出口

4 层全部收敛为同一出口：**`skill_acceleration_exec` 工具返回 `success=false`**（skill_turbo_tools `_wrap_skill_turbo_result`），外层 LLM 依据 SkillTurboPromptRail 注入的指南（失败/未处理必须转 skill_tool 走标准流程）自主调用 skill_tool。降级决策权在外层 LLM，turbo 内部**不**自行转标准流程。

```
路由级 ──────────┐
plan 代码级 ──────┤
节点级 fallback ──┼─→ skill_acceleration_exec 返回 success=false
fallback 终止级 ──┘        │
                           ▼
              外层 LLM（读 tool result + PromptRail 指南）
                           │
                           ▼
                  skill_tool 标准流程
```

### 降级矩阵（层级 → 动作）

| 层级 | 降级动作 |
|---|---|
| 节点级 | fallback / fallback_stream（LLM 子代理兜底，单请求最多 3 次） |
| plan 级 | FallbackContractError / FallbackLimitExceededError → SkillTurboNotHandled → 外层 LLM 转 skill_tool |
| 加载级 | PlanCodeValidationError / PlanCodeLoadError：非流式由 agent 包装 SkillTurboNotHandled；流式在 executor 内消化（chat.error+complete+return），agent 层不感知 |
| thinking 级 | 参数被拒 → 裸参数重试一次（call_llm / stream_llm 内联，不属本 spec 主体） |
| 模块级 | ImportError no-op 兜底（接口缺失时降级为空实现，防启动失败） |

### 两条不走 FallbackContractError 的诚实失败路径

1. **p10_delivery 交付失败硬门禁**：skill_turbo_tools `_ppt_delivery_failed_error` 检测 node_artifacts holder 中 `p10_delivery.info.delivery_status == "failed"`，流正常跑完后先过此门禁——命中即强制工具返回 `success=false`（文案明示"不要告知用户已经生成成功"）。终端产物完全不可产出时，即便 plan 流程正常结束也不许报成功。
2. **HITL 中断不降级**：AbortError（承载 ToolInterruptException 的 HITL 信号）在 plan_node、executor、agent、tool 层全部显式 `except AbortError: raise`，绝不进 fallback、也绝不包成 SkillTurboNotHandled（穿透点清单见 [[S_06]]）。

#### p10 硬门禁为何不自动降级（设计原理，勿"顺手修复"）

p10 命中后**既不触发节点级 fallback，也不抛 FallbackContractError**，三条原因都是机制性的：

- **fallback 没有挂载点**：节点级 fallback 只吃"节点 `_execute` 抛出的非 AbortError / 非 FallbackContractError 异常"；`delivery_status=failed` 是 p10 节点的**合法业务返回值**（节点正常执行、如实上报），节点树到工具层检查点时已执行完毕。即便人为让 p10 抛异常，fallback 子代理的能力边界是"完成单个节点的任务"，无法重跑整条 Stage 流水线，只会白耗 3 次预算。
- **FallbackContractError 的语义不匹配**：它是"此任务不适合加速通道，转标准流程重做"的信号，适用于流水线**早期**（p1/p2 意图早拒——已投入工作少、标准流程可等价完成）。p10 失败发生在**终局**：研究、大纲、页面 HTML 等绝大部分产物已落盘（node_artifacts 持久化保留，cancel 也不清），"转标准流程"意味着全部重做，成本远高于复用已有产物补救；且导出 convert 失败 / 发送失败这类终局失败在标准流程下未必能解决——它是执行层失败，不是通道能力不匹配。
- **决策权交还外层 LLM**：门禁只承诺"不虚报成功"，返回形态是 `success=false + 失败阶段提示（"请根据流水线失败阶段重试"）+ 产物摘要`。外层 LLM 按 PromptRail 指南可自主选择：基于已有产物重试 / 转 skill_tool 标准流程 / 如实告知用户。这与 `SkillTurboNotHandled` 分支的文案（"SkillAccelerationExec 未处理"）刻意区分——一个引导"重试/补救"，一个引导"换通道"。

配套的分级哲学使走到 `delivery_status=failed` 的门槛很高：单页失败→安全页降级仍交付（partial）；布局告警 / fix 失败页→软警告点名仍交付；只有 **pptx 完全不可产出或发送失败** 才落 failed。修改校验/导出/发送判定时，先对照该分级——把 partial 场景放大成 failed 属于"单点失败拒整本"反模式。

## 不变量

1. **AbortError 永不进 fallback**：plan_node.run / run_stream 的 except 顺序固定为 AbortError → FallbackContractError → Exception；fallback validator 内冒出的 AbortError 也必须原样上抛。任何一层新增 except 分支必须回答"AbortError 怎么过"。
2. **FallbackContractError 不进节点级 LLM fallback**：它是终止性降级信号（如 p1 意图早拒、p2 模板画布早拒），plan_node 直接上抛，由 executor 统一走降级收口路径。
3. **类型同一性**：`FallbackContractError` 定义于 fallback_handler，经 plan_node 与 `skill_turbo_runtime` 门面纯 re-export，全链为同一个类对象；任何环节重新定义/包装都会使 except 判定失效。
4. **成功判定双层 fail-closed**：第一层 `_parse_fallback_output`——空产出 / 无契约 JSON 块 / 全部不可解析 / 缺 success 字段，一律 `(False, {"reason": …})`；第二层 `_apply_result_validator`——validator 返回拒绝 reason 或 validator 自身异常，均视为未达成。两层任一不过即 `FallbackContractError`。
5. **validator 层唯一白名单是 AbortError**：原样上抛给 executor 转 HITL 三件套；吞掉等于把用户中断降级成契约失败继续执行。
6. **降级终止不发 chat.error**：execute_plan_stream 捕获 `FallbackContractError` / `FallbackLimitExceededError` 后只 yield `plan.finished(status="failed") + complete` 再 raise。机制原因见 P9。
7. **上抛前排空任务事件**：`_execute_node_stream` 收到两类 fallback 终止异常，必须先 flush deferred task 事件 + drain 全部已入队 `task.*`（含 in_progress stage 的 `task.complete(failed)`）再 raise。
8. **契约结果必须回写共享 inputs**：fallback 成功后经 `inputs.update` 写回共享计划上下文；`fallback()` 与 `_fallback_stream_impl` 两条路径都必须执行，且剔除 `node` / `status` 元键。
9. **回写剔除 reason**：contract_result 中的 `reason` 不写入 inputs；成功结果统一打标 `fallback=True`、`fallback_reason=原错误`。
10. **降级预算请求内有效**：`ExecutorConfig.max_fallback_count=3`；`_fallback_count` 在每次 execute_plan_stream 建立执行上下文时重置为 0（每请求新建 executor 实例）；`_check_fallback_limit` 先于 `_record_fallback_call` 执行。
11. **开关与缺 handler 诚实**：`enable_fallback=False` 或 `env.fallback_handler is None` → re-raise 原错误，不得制造新的异常类型。
12. **超限保留因果**：`FallbackLimitExceededError` 必须 `from error` 抛出，保留原始失败链。
13. **子代理会话隔离**：spawn 子代理会话 id 固定格式 `{parent}_skill_turbo_fallback_{uuid8}`；三类权限 rail（PermissionInterruptRail / SkillAuthorizationPermissionRail / SkillTurboPermissionRail）挂起必须在 finally 恢复，且 `host.get_permissions_snapshot` 回调同步临时替换——父 agent 与子代理共享同一 rail 实例。
14. **interrupt 显式报错**：子代理以未解决的 HITL interrupt 结束时，`spawn_fallback` 必须抛 RuntimeError，不得吞成空输出（空输出会被解析层判为契约失败，丢失中断语义）。
15. **交付失败硬门禁**：`delivery_status=failed` → 工具返回强制 `success=false`，与 plan 流程是否"正常结束"无关。
16. **收尾诚实性**：success 且无交付骨架且无 `send_file_status=="sent"` 确认 → 必须追加 NEUTRAL 中性提示（禁止宣称已发送）；失败分支不追加停止提示（留给 LLM 自主降级）。
17. **编排节点覆盖 validator**：编排类节点（sub_plans 非空、负责终端交付物）应覆盖 `validate_fallback_success` 校验终端交付物真实存在；基类默认放行只适用于叶子节点。

## 接口契约

### FallbackCall（fallback_handler）

| 字段 | 类型 | 语义 |
|---|---|---|
| `node_name` | str | 失败节点 plan_name（日志 / trace / 事件定位） |
| `instruction` | str | 节点任务说明（进子代理 prompt） |
| `inputs` | dict | 共享计划上下文（也是回写目标） |
| `error` | Exception | 原始失败异常 |
| `parent_session` | Session | 父会话（子代理隔离会话的派生基准） |
| `result_validator` | ResultValidator | executor 组装时传入 `node.validate_fallback_success` |

`ResultValidator = Callable[[inputs, contract_result], str | None]`：返回 None 放行，返回 str 为拒绝原因。

### FallbackContractError（fallback_handler）

构造携带 `node_name / reason / original_error` 三字段，语义为"fallback subagent 未达成节点契约，需降级到 DeepAgent"的**终止性降级信号**。三个产生源：

1. `spawn_fallback` 执行异常（含子代理未解决 HITL interrupt 的显式 RuntimeError）；
2. 子代理自证 `success=false` 或输出解析失败（`_parse_fallback_output` 第一层）；
3. `result_validator` 拒绝或自身异常（`_apply_result_validator` 第二层）。

### PlanNode.validate_fallback_success（节点契约第 11 条，契约全文见 [[S_04]]）

签名 `(inputs, contract_result) -> str | None`。fallback 契约自证通过后、结果写回 inputs 前强制调用。基类默认放行（plan_node）；编排类节点必须覆盖——参考实现 ppt 技能 `ppt_gen_root.py`：校验 `pptx_file_exists`（Windows 长路径安全）**且** `delivery_status ∈ {ok, partial}`，否则返回拒绝原因。守护测试：`test_fallback_deliverable_gate.py` / `_ppt_gate_fixture.py`。

### executor 侧入口

```python
async def fallback(self, node, inputs, error) -> Any            # 非流式
async def fallback_stream(self, node, inputs, error) -> AsyncIterator  # 流式

def _check_fallback_limit(self, error) -> None:
    # enable_fallback=False            → raise error（原样）
    # _fallback_count >= max(=3)       → raise FallbackLimitExceededError(...) from error
    # 否则放行

def _record_fallback_call(self, node, trace_prefix) -> None:
    # _fallback_count += 1；trace.node_execution_order 记 f"{trace_prefix}:{plan_name}"
```

两条入口均组装 FallbackCall（`result_validator=node.validate_fallback_success`）委托 `env.fallback_handler`；`env.fallback_handler is None` 时 re-raise 原错误并记 error 日志。

### DeepAgentFallbackHandler 双路径

`fallback()` 与 `fallback_stream()` 汇入同一套判定链（解析 → validator → 回写 / FallbackContractError）；`_fallback_stream_impl` 承载流式实现并 yield `fallback.started` / `fallback.finished` 事件。任何契约判定改动必须双路径同步（见修改检查单）。

### adapter.spawn_fallback（interface_deep）

```python
sub_session_id = f"{parent_session_id}_skill_turbo_fallback_{uuid8()}"
subagent = create_subagent("general-purpose", sub_session_id)
try:
    _suspend_subagent_permission_rails(...)   # 三类 rail: _static_config.enabled=False
                                               # + 替换 host.get_permissions_snapshot
    result = await invoke_subagent_with_trace(subagent, query)   # 阻塞执行
finally:
    _restore_subagent_permission_rails(...)   # finally 恢复 + 快照回调还原
# 子代理以未解决 HITL interrupt 结束 → raise RuntimeError（显式，不吞成空输出）
```

### 子代理任务 prompt 的三条硬规则（`_build_fallback_query` 固化）

1. 只有产出**可被下游节点直接消费**才填 `success=true`；部分完成 / 无法确认必须 `false` 并在 result 说明原因。
2. **编排类节点**（如 PPT 根节点）`success=true` 必须以全部阶段完成、终端产物已落盘/已发送为准；仅修复失败的子阶段不算完成，必须填 false。
3. 禁止伪造 `success=true`——会导致下游节点崩溃。

prompt 结构：节点名称 / 任务说明 / 原失败原因 / 输入参数；要求"先输出给用户看的正文内容，末尾另起一行以单行内联代码输出契约声明"。三条硬规则与 prompt 结构共同固化在 `_build_fallback_query`，改动即改变子代理行为契约，须同步守护测试。

## 数据结构

### 契约声明（子代理输出自证形态）

```
`{"success": true|false, "result": {...}}`     # 单行内联代码
```

### 降级成功结果（`_build_success_result`）

```python
success_result = {
    **inputs,
    **{k: v for k, v in contract_result.items() if k != "reason"},
    "node": node_name,
    "status": "completed",
    "fallback": True,
    "fallback_reason": str(原错误),
}
# 回写共享上下文（双路径对齐）：
inputs.update({k: v for k, v in success_result.items() if k not in {"node", "status"}})
```

回写是嵌套场景下下游节点读取新值的**唯一通道**：父编排节点会丢弃 `execute_subplan` 的返回值（只依赖共享 inputs），不回写则下游读到旧值连锁失败，直至 fallback limit exceeded。守护测试：`test_fallback_context_writeback.py`。

### 降级预算（ExecutorConfig）

| 字段 | 值 | 说明 |
|---|---|---|
| `max_fallback_count` | 3 | 单请求内 fallback 总预算 |
| `enable_fallback` | True | 关闭即 re-raise 原错误 |
| `execution_timeout` | 300.0 | 非流式入口整体超时（与降级无关，列全防误改） |

trace：`ExecutionTrace.fallback_count` 累计；`node_execution_order` 记 `fallback:{plan_name}` / `fallback_stream:{plan_name}`，供事后回查每次 fallback 的节点定位。

### fallback 事件

`fallback.started`（node_name / error / timestamp）与 `fallback.finished`（node_name / timestamp）由 `_fallback_stream_impl` yield；它们不在 tool 层 skip 集内，以原 event_type 转发到父会话（帧层规则见 [[S_08]]）。

### fallback 双路径对照

| 维度 | fallback()（非流式） | fallback_stream()（流式） |
|---|---|---|
| executor 入口 | execute_plan 路径（wait_for 整体超时内） | execute_plan_stream 路径（主流） |
| handler 实现 | DeepAgentFallbackHandler.fallback | fallback_stream → _fallback_stream_impl |
| fallback.started / finished 事件 | 不 yield（无流通道） | yield（随流转发） |
| 契约解析 / validator / 回写 | 同一套判定链 | 同一套判定链 |
| trace 前缀 | `fallback:{plan_name}` | `fallback_stream:{plan_name}` |

两路径的契约判定与回写逻辑必须保持逐行等价；仅事件产出与 trace 前缀允许差异。

### FallbackLimitExceededError 消息格式

`"Fallback 次数超过限制: {n}/3。最后错误: {原错误}"`——n 为已用次数，末段保留最后一次原始错误摘要，`from error` 保留完整因果链。

### 执行时序示例 A：fallback 成功吸收（E2）

```
节点 X._execute_stream 抛 ValueError（非 Abort / 非 FallbackContract）
  │
  ├─ PlanNode.run_stream：WARNING 日志 → _fallback_stream_callback
  ├─ executor.fallback_stream
  │    ├─ _check_fallback_limit：count=0 < 3 → 放行
  │    ├─ _record_fallback_call：count=1，trace 记 fallback_stream:X
  │    └─ FallbackCall(result_validator=X.validate_fallback_success)
  ├─ _fallback_stream_impl：yield fallback.started
  │    ├─ spawn_fallback：隔离子会话 + 权限 rail 挂起/恢复
  │    ├─ 子代理输出 → _parse_fallback_output：四级候选 → (True, contract_result)
  │    ├─ _apply_result_validator：validate_fallback_success → None（放行）
  │    └─ yield fallback.finished
  ├─ _build_success_result → inputs.update（剔 node/status）
  └─ 节点以 success_result 继续流；task.complete 按 result 状态判定
```

### 执行时序示例 B：fallback 契约失败终止（E10）

```
子代理自证 success=false（或 validator 拒绝 / spawn 异常）
  │
  ├─ FallbackContractError(node_name, reason, original_error)
  ├─ plan_node.run_stream：except FallbackContractError: raise（不进节点 LLM fallback）
  ├─ _execute_node_stream：flush deferred + drain 全部 task.*
  │    （含 in_progress stage 的 task.complete(failed)）→ raise
  ├─ execute_plan_stream：yield plan.finished(failed) + complete（不发 chat.error）→ raise
  ├─ agent.run_stream：包 SkillTurboNotHandled
  └─ 工具返回 success=false + 产物摘要（无停止提示）
       → 外层 LLM 依 PromptRail 转 skill_tool
```

## 执行路径全景

### P1 路由级降级决策

planner.plan 返回 None（无匹配 / 低置信 / 未注册 / model_client 缺失 / 路由解析两次失败）→ agent.run_stream 包 `SkillTurboNotHandled("无匹配skill")` → 工具 success=false。路由重试仅覆盖解析类瞬态失败（`_ROUTE_MAX_ATTEMPTS=2`，日志记录原始返回前缀区分两类失败）；扩大重试范围会造成循环降级。confidence 解析失败按 0.0 处理并 clamp 0~1。

### P2 plan 代码级降级决策

`_prepare_root_node` 内 validate（→ PlanCodeValidationError）/ exec（→ PlanCodeLoadError）。非流式路径异常上抛由 agent 包装 NotHandled；流式路径在 executor 内 yield chat.error + complete 后 return（不 raise，agent 层不感知）——两条路径语义差异与 E5/E6/E13 的归属见 [[S_01]]。

### P3 节点级 fallback 触发

`PlanNode.run / run_stream` 模板方法（不可覆盖，[[S_04]] 契约第 2 条）捕获非 AbortError、非 FallbackContractError 异常 → WARNING 日志（plan_name / instruction / error）→ 无 fallback 回调则 raise，否则进 `_fallback[_stream]_callback`（executor 绑定）。节点内部吞异常会导致 fallback 不触发（契约第 8 条：失败应直接 raise）。

### P4 fallback 前置检查

`executor.fallback[_stream]` → `_check_fallback_limit(error)` 三分支（False 开关 → re-raise；超限 → FallbackLimitExceededError from error；放行）→ handler None 检查（re-raise + error 日志）→ `_record_fallback_call` 计数 + trace → 组装 FallbackCall → `env.fallback_handler.fallback[_stream]`。

### P5 spawn 子代理执行

`DeepAgentFallbackHandler._fallback_stream_impl` → yield `fallback.started`（node_name / error / timestamp）→ `adapter.spawn_fallback(query, parent_session)`：隔离子会话（`{parent}_skill_turbo_fallback_{uuid8}`）→ 挂起三类权限 rail（finally 恢复 + 快照回调替换）→ 阻塞执行 → 子代理以未解决 HITL interrupt 结束时抛 RuntimeError → yield `fallback.finished`。子代理输出（正文 + 末尾契约声明）进入 P6。

### P6 第一层契约解析（fail-closed）

`_parse_fallback_output` 四级候选 JSON 收集（保持嵌套、不用 `\{.*?\}` 截断）：

| 级 | 候选形态 | 说明 |
|---|---|---|
| 1 | 单行内联代码 `` `(\{.*\})` `` | 契约声明的约定形态 |
| 2 | ```json 围栏 | 子代理把声明包进 json 代码块 |
| 3 | 无标签围栏 | 包进普通代码块 |
| 4 | 裸 JSON 括号平衡扫描 | `_scan_balanced_json_objects`，O(n)，**扫描全文**——兼容契约块出现在开头/中段 |

`dict.fromkeys` 去重后**从后往前**找含 `success` 字段的可解析 dict；`success = bool(payload.get("success", False))`（缺字段视为 False）；`result` 非 dict 包装为 `{"reason": str(result)}`。空产出 / 无契约块 / 全不可解析 → 一律 `(False, {"reason": …})`，空产出与有产出无契约分开记日志（诊断区分"没干活"与"干了但没自证"）。

### P7 第二层 validator 判定（fail-closed）

`_apply_result_validator`：自证通过后运行 `node.validate_fallback_success(inputs, contract_result)`；返回 reason 或自身异常 → 未达成（唯一白名单：AbortError 原样上抛，惰性 import plan_node.AbortError 避免循环依赖）。三层判定结果对应 FallbackContractError 三个产生源中的②③。

### P8 成功回写共享 inputs

`_build_success_result`（合并剔除 reason + 打标 fallback=True / fallback_reason）→ 双路径 `inputs.update`（剔除 node/status）→ 节点以 success_result 继续流；task.complete 按 result 状态判定（业务失败键 `export_status=failed` / `status=error` 亦标 failed，帧层见 [[S_08]]）。

### P9 FallbackContractError 终止传播链

```
三源产生 FallbackContractError
  → plan_node.run/run_stream：except FallbackContractError: raise（不进节点 LLM fallback）
  → executor._execute_node_stream：先排空全部 task.*（含 in_progress stage 的
      task.complete(failed)，否则前端任务面板停滞 in_progress）再 raise
  → executor.execute_plan_stream：不发 chat.error，只 yield plan.finished(failed)+complete
      再 raise
  → agent.run_stream：包成 SkillTurboNotHandled
  → skill_turbo 工具：返回 success=false（+ 产物摘要，不追加停止提示）
  → 外层 LLM 读失败 tool result + PromptRail 指南 → 调 skill_tool 走标准流程
```

**不发 chat.error 的机制原因**：chat.error 会被 executor 透传并经 tool 转发到父会话，**抢先于 tool result 到达前端**；前端 useWebSocket 收到后清空 activeRequestIdRef 并渲染"系统异常"，终结本轮——LLM 来不及按 PromptRail 指引转 skill_tool。`plan.*` 前端无 handler 不会抢跑；新一轮 request 前端会重建 activeRequestIdRef。

### P10 FallbackLimitExceededError 路径

`_check_fallback_limit` 抛出（from error）→ 消费循环同 P9：先 flush 后 raise、不发 node_error → execute_plan_stream 与 FallbackContractError 共用同一条外层分支（E11，见 [[S_01]]）。

### P11 降级后转标准流程

fallback 终止后**不在 turbo 内部转标准流程**，决策交还外层 LLM：失败 tool result（含产物摘要、不含停止提示）+ PromptRail 指南 → LLM 自主调用 skill_tool。已知边界：turbo 任务列表（8-Stage 体系）与标准流程 stage 无映射，降级后前端任务面板冻结在现状、无收敛机制。

### P12 收尾诚实性三分支（`_wrap_skill_turbo_result`）

流正常跑完先过 p10_delivery 硬门禁（delivery_status=failed → 强制 success=false）。成功分支三选一：

1. 有 PPT 交付骨架 → 骨架挂 `_pending_ppt_delivery_summary` ContextVar（rail 之后再发，时序契约见 [[S_08]]）+ 停止提示（禁止本轮重复输出交付总结 / 再调工具）；
2. 通用交付确认（任一节点 info `send_file_status == "sent"`，如 docx D8 / xlsx X9）→ confirmed 停止提示（已发送，禁止重复调用/发送）；
3. 无骨架无确认信号 → NEUTRAL 中性提示（"未能确认文件已生成并发送…不要声称文件已发送"，中英按 metadata.language）。

失败分支：不追加停止提示，追加产物摘要。产物摘要（`_build_artifact_summary`）仅进 tool result 给 LLM，不直接给用户。

### P 路径与退出路径（[[S_01]] E 表）映射

| 本 spec 路径 | 对应 E 编号 |
|---|---|
| P9 FallbackContractError 终止 | E10 |
| P10 FallbackLimitExceededError | E11 |
| P2 流式校验/加载失败 | E5 / E6 |
| P3 触发且 fallback 成功吸收 | E2 |
| P3 触发但异常未被吸收（事件化继续） | E3 |
| p10 硬门禁（流正常结束 + 强制失败） | E1 帧组合 + 工具层改写 |

### 失败分级哲学（从 ppt 校验链提炼的通用原则）

| 档位 | 缺陷性质 | 处置 |
|---|---|---|
| 硬失败重试 | 结构性 / 会传染下游（非法 HTML、DOM 破坏、图表骨架缺失、bash 非零退出） | 阻断 + 定向重试，重试有界 |
| 软警告交付 | 视觉 / 局部性缺陷（mount 错配、布局微溢出、单点图表缺失） | 先确定性修复；修不了软警告 + 交付面点名，不阻断 |
| 降级兜底 | 单点不可救但整体可交付（安全页、partial） | 失败面最小化：单页/单点失败不放大为整任务失败 |
| 整体失败 | 终端产物完全不可产出（delivery_status=failed） | 硬门禁 success=false，诚实失败 |

权威判据：**磁盘真实产物**，不是 LLM / 子节点口头成功——send 空回执视为失败、worker 结果需读盘复验、交付状态 fail-closed 归一。该哲学同时约束 fallback 契约（编排节点 validator 校验磁盘交付物）与收尾三分支（NEUTRAL 不越权宣称）。

## 异常分支与边界 case

| # | 边界 case | 现行为 / 约束 | 测试覆盖 |
|---|---|---|---|
| 1 | fallback 终止时任务面板停滞 | 不排空 deferred + drain task 事件 → 前端 in_progress 冻结；`_execute_node_stream` 现已先排空再 raise，改动必须保持 | test_fallback_contract_error_task_events + test_exit_path_frames（E10/E11） |
| 2 | 子代理以未解决 HITL interrupt 结束 | spawn_fallback 显式 RuntimeError（不吞成空输出）→ 按产生源①转 FallbackContractError | test_spawn_fallback_suspend_and_interrupt |
| 3 | 权限 rail 父子共享实例 | suspend 必须 finally restore；`resolve_interrupt` 首次调用会用 snapshot 刷新覆盖，故 snapshot 回调也要临时替换 | test_spawn_fallback_suspend_and_interrupt |
| 4 | 降级后任务列表脱节 | turbo Stage 体系与标准流程 stage 无映射，任务面板冻结（已知未解边界） | 无（已知未解） |
| 5 | enable_fallback 开关三态 | True（正常走）/ False（re-raise 原错误）/ handler None（re-raise + error 日志）——后两者不得制造新异常 | test_fallback_contract_parsing（TestCheckFallbackLimit / TestHandlerMissing） |
| 6 | 契约块出现在输出开头/中段 | 裸括号平衡扫描全文兼容 off-spec 位置 | test_fallback_contract_parsing |
| 7 | validator 自身异常 | 与返回拒绝 reason 同等对待（fail-closed），不区分"校验失败"与"校验器崩溃" | test_fallback_deliverable_gate |
| 8 | result 非 dict | 包装为 `{"reason": str(result)}` 后继续判定 | test_fallback_contract_parsing |
| 9 | 回写剔除键 | 契约层剔除 `reason`；回写层剔除 `node` / `status`——两层键集不同，勿合并 | test_fallback_context_writeback |
| 10 | fallback 事件透传 | fallback.started / finished 不在 tool 层 skip 集，以原 event_type 转发父会话 | test_fallback_context_writeback（流式事件断言） |
| 11 | 空产出 vs 有产出无契约 | 分开记日志（诊断区分"子代理没干活"与"干了但没自证"） | test_fallback_contract_parsing（分支区分用例） |
| 12 | 缺 success 字段 | `bool(payload.get("success", False))` → False（fail-closed） | test_fallback_contract_parsing |
| 13 | AbortError 在 validator 内冒出 | 白名单原样上抛，转 HITL（[[S_06]]） | test_fallback_deliverable_gate |
| 14 | 输出含多个 JSON 对象 | `dict.fromkeys` 去重后从后往前找含 success 字段者（契约声明约定在末尾） | test_fallback_contract_parsing |
| 15 | JSON 嵌套结构 | 四级候选保持嵌套完整性，禁止 `\{.*?\}` 非贪婪截断 | test_fallback_contract_parsing |
| 16 | 路由重试范围 | 仅解析类失败重试 1 次；confidence 不足 / skill_name=null 是模型判断，不重试 | test_planner_route_retry |
| 17 | 嵌套子计划的回写 | 父节点丢弃 execute_subplan 返回值 → inputs.update 是下游读新值的唯一通道 | test_fallback_context_writeback |
| 18 | 非流式 / 流式降级语义分叉 | execute_plan（非流式）各类异常 re-raise 由 agent 统一包装；流式路径 E5/E6/E12 存在"事件化吞掉"分支——新增降级出口必须先明确归属哪一语义（[[S_01]]） | test_exit_path_frames（流式帧）+ test_executor_plan_code_import（非流式 raise） |

## 修改检查单

| 改动 | 必查 |
|---|---|
| 改契约判定（`_parse_fallback_output` / `_apply_result_validator`） | 双层同步；白名单只有 AbortError；四级候选覆盖回归（内联/围栏/无标签围栏/裸括号、开头/中段位置、多候选、嵌套、result 非 dict） |
| 改 FallbackContractError 路径 | 三连：`_execute_node_stream` flush task 事件；execute_plan_stream 不发 chat.error + 只 yield plan.finished(failed)+complete 再 raise；agent 包装 SkillTurboNotHandled |
| 新增降级出口 | 终态帧组合明确（对照 [[S_01]] E 表）+ 工具 success=false + PromptRail 降级指引同步（[[S_02]]）+ 是否需要 task 事件收口（[[S_08]]）+ 明确流式"吞 vs 抛"语义归属 |
| 改 spawn_fallback | 权限 rail finally restore + snapshot 回调替换；HITL interrupt 显式 RuntimeError；子会话 id 命名格式 `{parent}_skill_turbo_fallback_{uuid8}` 不变 |
| 改 `_parse_fallback_output` | 四级候选顺序与覆盖不变；空产出 / 无契约 / 全不可解析三分支日志不合并 |
| 改回写逻辑 | `fallback()` 与 `_fallback_stream_impl` 双路径对齐；剔除键集合（reason / node / status）同步 |
| 改降级预算 | `_fallback_count` 重置点（execute_plan_stream 建执行上下文处）+ 超限 `from error` 保留因果 |
| 新增编排类技能 | 必须覆盖 `validate_fallback_success` 校验终端交付物真实存在（对照 ppt_gen_root 参考实现） |
| 改收尾三分支 | NEUTRAL / sent 确认 / 骨架三互斥分支齐全；失败分支不追加停止提示；p10 硬门禁先行 |
| 改子代理 prompt（`_build_fallback_query`） | 三条硬规则语义不变；契约声明形态（单行内联代码）与 P6 四级候选兼容 |

## 测试资产

| 测试文件 | 锁定行为 |
|---|---|
| `tests/unit_tests/server/runtime/skill_turbo/test_fallback_deliverable_gate.py` | 编排节点 validate_fallback_success 交付物门禁（部分成功不得虚报全成功） |
| `tests/unit_tests/server/runtime/skill_turbo/test_fallback_context_writeback.py` | 契约结果回写共享 inputs（下游读新值） |
| `tests/unit_tests/server/runtime/skill_turbo/_ppt_gate_fixture.py` | 交付物门禁测试的 ppt 根节点夹具 |
| `tests/unit_tests/agentserver/test_spawn_fallback_suspend_and_interrupt.py` | spawn_fallback 权限挂起保全 + interrupt 显式报错 |
| `tests/unit_tests/agentserver/test_skill_turbo_tools.py` | 工具面：降级收敛出口 / 收尾三分支 / 硬门禁 |

改降级体系的任何路径，上述测试是最低回归集；新增分支必须补对应用例后再动实现。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] | 退出路径全景 E1–E13 是本 spec P 路径的宿主矩阵：P9/P10 对应 E10/E11；E5/E6/E12 的"吞 vs 抛"不对称归 S_01 管，本 spec 只约束 4 层决策的触发条件 |
| [[S_02]] | PromptRail 的降级指南（失败必须转 skill_tool）与"暂停不是失败"提示词，是收敛出口的行为侧配套 |
| [[S_04]] | PlanNode 契约第 11 条（validate_fallback_success）与 require_abort_reraise 静态强制；本 spec 定义其运行期语义 |
| [[S_05]] | 工具失败双轨契约与瞬态错误节点内自愈（call_tool_with_retry）——瞬态自愈不消耗本 spec 的 plan 级 fallback 预算 |
| [[S_06]] | AbortError 全链穿透与 HITL 状态机；validator 白名单（本 spec N5）是其中一环 |
| [[S_08]] | fallback.started / finished 事件帧结构、降级路径"不发 chat.error"的帧层机制、task 事件 flush 的执行细节 |
| [[S_09]] | spawn_fallback 的外层装配、DeepAgentFallbackHandler 注入、外层 LLM 主循环所在链路 |
