# SkillTurbo PlanNode 节点契约规约（node-contract）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_04 / node-contract |
| 关联模块 | `jiuwenswarm/server/runtime/skill_turbo/`（主战场：plan_node.py / json_utils.py / thinking_seam.py；executor.py 的回调绑定、LLM 与 ask_user 实现侧） |
| 最近一次修订日期 | 2026-10-13 |
| 关联 feature | 暂无（首批 specs，由现状代码逆向固化） |

> 代码定位约定：本文一律用"文件 + 方法名"定位；随附行号必然随代码演进漂移，以方法名为准。

## 范围与边界

### 本规约管什么

PlanNode 基类契约，**双视角**：技能代码作者视角（怎么写节点才合法）+ 引擎实现视角（模板方法与回调注入的语义）：

- **契约 v1 全文 11 条**（plan_node.py 文件头 docstring）逐条展开，含违反后果与重点条款展开。
- 类结构：`__init__` 参数与 depth 传播、**12 个回调槽**全表、`set_runtime_callbacks` 递归传播、group / leaf 节点差异。
- `run` / `run_stream` 模板方法的异常三分流语义。
- `execute_subplan*` / `skip_subplan*` 钩子序列与 **after 触发条件矩阵**（成功/异常/AbortError/skip 四态）。
- plan_name / display_names 双层命名机制；inputs / outputs 约定（含 `__artifact__` / `_partial_artifact`）；**节点能力面**（可访问/不可访问）。
- LLM 调用契约（call_llm / stream_llm / stream_llm_collect 签名 + executor 侧实现要点）。
- ask_user 调用契约（与普通工具同 API 的 HITL 通道）。
- DisableThinkingMixin；require_abort_reraise 技能侧写法规范；json_utils 四级提取链。

### 本规约不管什么

| 子主题 | 归属 spec |
|---|---|
| 执行循环（双生产者队列、E1-E13 退出路径、终态收口双轨制） | [[S_01]] |
| 技能发现 / 注册 / planner 路由 / 入口 5 级优先级链 | [[S_02]] |
| validator 三 profile 规则全集 / 运行期沙箱 namespace / 白名单 | [[S_03]] |
| HITL 状态机（tcid 机制、发卡、恢复分流、引擎侧 AbortError 透传表） | [[S_06]] |
| fallback 机制（FallbackCall、4 层降级决策、fallback 子代理内部） | [[S_07]] |
| 事件流协议（llm_* / task_* 事件全集、缓冲、stream_source_id 的前端消费） | [[S_08]] |
| adapter 侧 HITL 三件套 / ContextVar 桥 / 外层集成 | [[S_09]] |

## 不变量

任意时刻，PlanNode 契约必须满足下列 16 条断言：

1. **N1 模板方法封闭**：`run` / `run_stream`（plan_node.py `PlanNode.run` / `PlanNode.run_stream`）是框架模板方法，skill_code 禁止覆盖（契约第 2 条）；异常捕获、AbortError/FallbackContractError 直通与 fallback 入口统一归框架。
2. **N2 异常分流顺序固定**：模板方法内 `except AbortError: raise` → `except FallbackContractError: raise` → `except Exception` 进 fallback 回调（回调缺失则 raise）。顺序重排即破坏 HITL 与终止性降级。
3. **N3 AbortError 不触发 after**：`execute_subplan` / `execute_subplan_stream` 的 AbortError 分支不调 `_after_subplan_execute`——task 保持 in_progress 供 resume 重入，这是中断快照能正确续跑的前提（[[S_06]]）。
4. **N4 失败必须上抛**：节点吞异常 = 剥夺框架 fallback 机会（契约第 8 条）；节点内不得以返回值表达基础设施级失败。
5. **N5 回调 fail-fast**：12 个回调槽任一未初始化即调用 → `RuntimeError("PlanNode xxx 回调未初始化")`；不静默降级、不返回默认值——绑定顺序错误必须在首次调用当场暴露。
6. **N6 回调注入递归全树**：`set_runtime_callbacks` 由 executor 对 root 调一次，逐层传播到全部后代节点；不存在"只绑 root 不绑子树"的合法形态。
7. **N7 深度语义**：root depth=0、二层=1（`_update_subplans_depth` 构造后迭代传播，只访问公开属性 `depth` / `sub_plans`）；**只有 depth==1 的子节点参与 task 事件发射与 resume 跳过判定**（executor 侧消费 depth）。
8. **N8 plan_name 约定唯一**：同一 skill 内唯一（软约束，引擎不静态强制）；它是日志、trace、`node_execution_order`、fallback 定位、task_id 派生、节点产物 key 的共同锚——重名即互相覆盖。
9. **N9 入口显式 root**：入口文件必须暴露模块级 `root: PlanNode`（契约第 9 条）；缺失时 `_extract_root_node` 三分支兜底（0 实例 ValueError / 恰 1 实例脆弱采用 / 多实例校验错误）。
10. **N10 能力面封闭**：节点触达外界的唯一通道是回调注入的方法（工具/LLM/JSON/日志/编排 API）+ inputs；事件发射全部由 executor/rail 代理，节点无发射器句柄。
11. **N11 先 deepcopy 再绑回调**：plan_code import 出的 root 是模块级单例；`_prepare_root_node` 必须 deepcopy 后再 `_bind_node_callbacks`（并发请求回调覆盖防护，[[S_01]]）。节点树因此不得持有不可深拷贝资源（锁/连接/文件句柄）。
12. **N12 DisableThinkingMixin 仅 opt-in**：业务节点显式多重继承才生效；默认 PlanNode `thinking=None` 零注入（请求体与不传时完全一致）。
13. **N13 thinking seam 永不 abort**：`resolve_skill_turbo_thinking_kwargs` 遇降级/import 异常/任何异常一律返回空 kwargs（fail-open，仅 WARNING）；参数被 API 拒绝时由 executor 裸参重试一次兜底。
14. **N14 LLM 观测两原则**：`llm_usage` 仅成功轮发送（防失败轮泄漏后成功轮重复计费）；`stream_llm` 失败轮丢弃业务侧缓冲（脏前缀不进输出流）。
15. **N15 validate_fallback_success fail-closed**：实现抛异常同样触发终止性降级；基类默认放行（return None）。
16. **N16 两套 JSON 提取均不做 LLM 级重试**：json_utils.extract_llm_json 与 tool_utils.parse_llm_json（[[S_05]]）只做解析防御；重试归调用方节点逻辑或 LLMRetryRail。

## 接口契约

### 契约 v1（plan_node.py 文件头 docstring，11 条）

| # | 契约 | 引擎侧行为 / 违反后果 |
|---|---|---|
| 1 | 每个节点继承 PlanNode，实现 `async def _execute(self, inputs: dict) -> Any`（抽象方法）；流式场景可选覆盖 `_execute_stream` | 未实现 `_execute` → 类不可实例化（ABC） |
| 2 | 禁止覆盖 `run()` / `run_stream()`——框架模板方法，统一负责异常捕获与 fallback | 覆盖后异常三分流（N2）、fallback、AbortError 直通全部旁路 → HITL 中断被吞 |
| 3 | 初始化必须显式提供 `plan_name(str)` / `instruction(str)` / `sub_plans(list[PlanNode])` | instruction 是结构化文档（职责/前置/输入/输出/流程/兜底），兼作单测参考与 fallback 子代理上下文 |
| 4 | 输入统一 `dict[str, Any]`；输出推荐 dict，至少含 `node/status/result` | 非 dict 输出可运行但丢失业务失败识别与产物收集（见 inputs/outputs 约定） |
| 5 | 复合节点遍历 `self.sub_plans` 并 `await child.run(ctx)`（或 `execute_subplan*` 带钩子版本）完成调度 | executor 不做拓扑调度；遍历顺序/循环/并发全部由 skill_code 决定 |
| 6 | 外部能力仅可通过 `self.has_tool / call_tool / call_llm / stream_llm` | 配套通道还有 extract_json / log / 编排 API，全量见能力面表（N10） |
| 7 | 节点禁止直接 import `os/subprocess` 等系统模块 | validator 白名单强制（[[S_03]]）；违反 → 注册期整技能跳过，用户视角"无匹配 skill" |
| 8 | 节点失败应直接 raise，框架自动触发 fallback；**不要在节点内吞异常** | 吞异常 = fallback 不触发（N4），失败被静默洗白 |
| 9 | 每个 skill_code 必须有入口文件，且暴露 `root: PlanNode` | 缺失走 `_extract_root_node` 脆弱兜底（N9）；多实例无显式 root → 校验错误 |
| 10 | `plan_name` 同一 skill 内唯一 | 唯一性是日志/trace/fallback 定位/task 派生/产物 key 的前提（N8） |
| 11 | 编排类节点应覆盖 `validate_fallback_success(inputs, contract_result) -> str \| None` | 引擎在 fallback 契约自证通过后、结果写回 inputs 前强制调用；返回拒绝原因 str 触发终止性降级，None 放行；实现异常同样 fail-closed（N15）。参考实现：技能侧仓 pptx-craft 的 `turbo_codes/ppt/ppt_gen_root.py` |

#### 重点条款展开

**第 5 条（复合节点调度）的三种合法形态**：

```python
result = await child.run(inputs)                        # a) 裸调度：无任务追踪钩子
result = await self.execute_subplan(child, inputs)      # b) 带钩子（推荐）：task 事件 + 产物 + resume 跳过
async for chunk in self.execute_subplan_stream(child, inputs): yield chunk   # c) 流式带钩子
```

多子节点并发是技能侧自由（`asyncio.gather` + `stream_llm_collect` 协程形态，见 LLM 契约）；gather 必须遵守 require_abort_reraise 的 gather 变体（重抛 AbortError/CancelledError）。

**第 8 条（失败直接 raise）的反模式**——以下写法都让 fallback 失效：

```python
# 反模式 1（吞异常返回错误 dict）：fallback 永不触发，错误混入业务流
# 反模式 2（宽泛捕获只打日志）：同上，且吞掉 AbortError → HITL 失效
# 正确：业务性"失败态"用结果 dict 表达（export_status/status 键，见 outputs 约定）；
#       基础设施失败直接 raise（含 BashExecError / 解析 ValueError 等）
```

**第 11 条（validate_fallback_success）的调用时机**：

```
节点 _execute 抛 Exception
  → executor.fallback → fallback_handler.fallback(FallbackCall(..., result_validator=node.validate_fallback_success))
      → 子代理 spawn + 契约自证（_parse_fallback_output）
      → 【本钩子】validate_fallback_success(inputs, contract_result)
           str（拒绝原因）→ FallbackContractError → 终止性降级（[[S_07]]）；None → 放行 → 契约结果写回 inputs → 继续下游
```

校验对象是**终端交付物真实存在**（如 PPTX 文件已产出且 delivery_status 达标），不是子代理的自述文本；fail-closed（N15）意味着钩子内部抛异常也按拒绝处理。

### 类结构与回调槽（12 个）

```python
class PlanNode(ABC):
    def __init__(self, plan_name: str, instruction: str,
                 sub_plans: list[PlanNode] | None = None, depth: int = 0): ...
```

- depth 由 `_update_subplans_depth` 在构造后迭代传播（root=0、二层=1、三层=2…）。
- 12 个回调槽初始为 None，由 `set_runtime_callbacks`（executor 经 `_bind_node_callbacks` 调用）注入并递归传播：

| # | 槽位属性 | set_runtime_callbacks 形参 | executor 侧实现 | 用途 |
|---|---|---|---|---|
| 1 | `_has_tool_callback` | `has_tool` | `executor.has_tool` → env | 工具能力探测 |
| 2 | `_call_tool_callback` | `use_tool` | `executor.use_tool` | 工具调用（ask_user 同通道） |
| 3 | `_call_llm_callback` | `call_llm` | `executor.call_llm` | 非流式 LLM |
| 4 | `_stream_llm_callback` | `stream_llm` | `executor.stream_llm` | 流式 LLM |
| 5 | `_fallback_callback` | `fallback` | `executor.fallback` | 节点级非流式兜底 |
| 6 | `_fallback_stream_callback` | `fallback_stream` | `executor.fallback_stream` | 节点级流式兜底 |
| 7 | `_extract_json_callback` | `extract_json` | `json_utils.extract_llm_json` | LLM JSON 提取 |
| 8 | `_log_callback` | `log` | `executor._log_from_node` | 受控节点日志 |
| 9 | `_before_subplan_execute` | `before_subplan_execute` | `executor._before_subplan_execute` | task.start（depth==1） |
| 10 | `_after_subplan_execute` | `after_subplan_execute` | `executor._after_subplan_execute` | task.complete + 产物收集 |
| 11 | `_should_skip_subplan_execute` | `should_skip_subplan_execute` | `executor._should_skip_subplan_execute` | HITL resume：completed stage 跳过 |
| 12 | `_should_suppress_subplan_start_banner` | `should_suppress_subplan_start_banner` | `executor._should_suppress_subplan_start_banner` | HITL resume：横幅抑制 |

回调未初始化即调用 → RuntimeError（N5）。槽 11/12 是 HITL resume 专用**查询接口**（非执行钩子）：编排节点经 `should_skip_subplan` / `should_suppress_subplan_start_banner` 公开方法查询，回调缺失时返回 False，不 raise。

**group / leaf 节点差异**（框架不区分类型，差异纯靠组合）：

| 维度 | leaf 节点 | group（编排）节点 |
|---|---|---|
| sub_plans | 空 | 非空 |
| `_execute` | 做实际工作（LLM/工具） | 遍历 sub_plans 调度 |
| 调度方式 | — | `execute_subplan(_stream)`（带钩子）或直接 `child.run()` |
| validate_fallback_success | 通常不覆盖 | **应覆盖**（校验终端交付物，契约第 11 条） |
| HITL resume | 被 skip 时收到 resume_skip 结果 | 提供 should_skip / should_suppress 查询 |

**回调生命周期时序**（节点实例与回调的关系）：

```
技能模块 import（一次，进程级）→ root 是模块级单例，槽位全 None
executor._prepare_root_node（每请求）→ deepcopy(root)（N11）→ set_runtime_callbacks（N6，一次性）
执行期（本请求内）→ 槽位可用；未初始化调用 = RuntimeError（N5）
请求结束 → 实例随 executor 丢弃；模块级单例不动
```

技能侧不得在 `_execute` 内缓存/转存回调或 self 到模块级变量——deepcopy 隔离（N11）会被这样的共享击穿。

### run / run_stream 模板方法语义

```
try: _execute / _execute_stream
  except AbortError:            raise   # HITL 中断：不进 fallback，透传上层（[[S_06]]）
  except FallbackContractError: raise   # 终止性降级信号：不进节点 LLM fallback，交 executor 包 SkillTurboNotHandled（[[S_07]]）
  except Exception as e:                # 其余异常
      run:         WARNING（plan_name/instruction/error）→ 无 _fallback_callback 则 raise，
                   否则 return await _fallback_callback(self, inputs, e)
      run_stream:  无 _fallback_stream_callback 则 raise，
                   否则 yield from _fallback_stream_callback(self, inputs, e)
```

- `run` 是非流式入口（`execute_plan` 消费，生产不使用）；`run_stream` 是流式入口（生产主链，[[S_01]]）。
- `_execute_stream` 默认实现调用 `_execute` 并单值 yield；子类可产出进度片段（`{"status": "progress", ...}`）与终态（`{"status": "ok", "result": ...}`）。
- CancelledError / KeyboardInterrupt 是 BaseException，不被 `except Exception` 捕获，自然穿透模板方法。

### execute_subplan / execute_subplan_stream / skip_subplan：钩子序列与 after 触发矩阵

`execute_subplan(subplan, inputs)`（编排节点调用，执行一个子节点）固定序列：
`_before_subplan_execute` → `_maybe_skip_subplan_execute`（resume 跳过则构造 skip result 并触发 after 后返回）→ `subplan.run(inputs)` → `_after_subplan_execute`。

**after 触发条件矩阵（四态）**：

| 场景 | before | 真实执行 | after | 返回 / 上抛 |
|---|---|---|---|---|
| 成功 | ✓ | ✓ | ✓ 传 result | result |
| 普通异常 | ✓ | ✗ | ✓ 传 exception 对象，再 raise | raise（after 先于 raise 触发） |
| AbortError | ✓ | ✗ | **✗ 不触发**（N3） | raise（task 保持 in_progress 供 resume 重入） |
| resume skip | ✓ | ✗ | ✓ 传 skip result | skip result |

`execute_subplan_stream` 流式变体（伪代码，finally-after 是关键差异）：

```python
async def execute_subplan_stream(self, subplan, inputs):
    await self._before_subplan_execute(subplan, inputs)
    if await self._maybe_skip_subplan_execute(subplan, inputs) is not None:
        return
    last_chunk, error, hitl_interrupt = None, None, False
    try:
        async for chunk in subplan.run_stream(inputs):
            last_chunk = chunk          # 累积最后一个 chunk 作为子节点最终 result
            yield chunk
    except AbortError:
        hitl_interrupt = True          # 不触发 after（N3），task 保持 in_progress
        raise
    except Exception as e:
        error = e
        raise
    finally:
        # 正常 / 普通异常 / 外层提前 close 生成器均触发；仅 hitl_interrupt 抑制；after 自身失败仅日志
        if self._after_subplan_execute is not None and not hitl_interrupt:
            await self._after_subplan_execute(subplan, inputs, error if error is not None else last_chunk)
```

- `skip_subplan` / `skip_subplan_stream(subplan, inputs, *, message="已跳过", extra=None)`：**业务性跳过**（如能力不支持），仍触发 before/after 任务追踪，返回 `{node, status:"ok", message, skipped:True}` + extra。
- resume skip result（`_resume_skip_result`）：`{node, status:"ok", message:"resume skip completed stage", skipped:True, resume_skip:True}`。

### LLM 调用契约（call_llm / stream_llm / stream_llm_collect）

```python
await node.call_llm(prompt, system_prompt="", node_name=None, concurrent=False, thinking=None) -> str
async for chunk in node.stream_llm(prompt, ...)        # AsyncIterator[str]
await node.stream_llm_collect(prompt, ...) -> str      # 协程（非 async generator），可直接进 asyncio.gather
```

- `node_name` 缺省用 `self.plan_name`。
- `concurrent=True` 时 executor 生成 `stream_source_id`（`_gen_stream_source_id`：`skill_turbo:{node_name}:{token_hex(4)}`，8 个 hex 字符）注入本次 llm_reasoning / llm_output / llm_usage 事件供前端按调用分桶；串行保持 None，行为不变。
- executor 侧实现（`executor.call_llm` / `executor.stream_llm`）要点：

```
1. client = env.model_client；None → RuntimeError
2. resolve_skill_turbo_thinking_kwargs(thinking, client)   # thinking_seam；thinking=None 零注入
3. before_invoke rail hook；每次调用独立重置重试计数（防整份 plan 共享重试额度）
4. while True:                                            # 重试循环
     before_model_call → Semaphore 排队（llm_concurrency_limit，懒初始化绑定运行时事件循环；
       排队 ≥1000ms 升 INFO 日志）→ client.invoke(messages, max_tokens=_DEFAULT_LLM_MAX_TOKENS
       （env LLM_MAX_TOKENS，默认 65536）, **thinking_kwargs) → _run_stream_chunk_inspectors
       （重复输出检测，异常上抛供重试消费）→ llm_usage（仅成功轮，N14）
     → llm_reasoning / llm_output（注入显示名 + source_id）
   异常分支:
     AbortError → raise（不进重试）
     is_skill_turbo_thinking_param_error → 裸参重试一次（不带 thinking kwargs）
     其余 Exception → on_model_exception rail → ctx.consume_retry_request()
       有额度: sleep(delay) 重试整轮（stream_llm 失败轮丢弃 buffered_chunks）；无额度: raise
5. finally: after_model_call rail
```

- thinking 被拒启发式（thinking_seam `is_skill_turbo_thinking_param_error`）：`isinstance(exc, TypeError)`，或消息含 extra_body / thinking / enable_thinking / reasoning_effort / unexpected keyword / invalid_request / bad request / validation error / unrecognized（小写包含）。
- thinking seam 永不 abort（N13）：degraded / import 异常 / 任何异常 → 空 kwargs fail-open，仅 WARNING；重试 rail 为 `NotifyingLLMRetryRail`（config.yaml `execution_guard.llm_retry_rail`，与 DeepAgent 共用配置；构建失败或未启用返回 None 不阻塞执行）。

### ask_user 调用契约

节点侧唯一形态：`await self.call_tool("ask_user", questions=[...])` —— 与普通工具同一 API，无独立方法。引擎链路：

```
节点 call_tool("ask_user", questions=[...])
  → executor.use_tool：确定性 tcid（skill_turbo-tc-{name}-{sha1(request_id|args)[:8]}-{idx}）
  → before_tool_call rail 链
  → SkillTurboAskUserRail 拦截：
       首次调用 → interrupt() 抛 AbortError（前端弹 ask_user_question 卡片）
       非引导模式（interactive_ask=False，请求级 ContextVar）+ question 带 preview
         → 直接 skipped，不弹卡（技能侧必须处理 skipped 返回值，不得假装已确认）
  → use_tool 捕获 AbortError → save_resume_ctx(plan_code, inputs, tcid, task_states) → 上抛
  → adapter 转 HITL 三件套（[[S_06]] / [[S_09]]）
  →（用户作答）resume 重放 → tcid name+idx 回退命中 → rail resolve_interrupt
  → reject(tool_result={"status":"answered","answers":...})   # 按前端 answers 结构回填，无需二次映射
  → 节点拿到结构化作答继续
```

审批职责已收口到外层 `skill_acceleration_exec` 工具（DeepAgent 层一次性授权内部工具，config `skill_turbo: ask`），内层不挂逐工具权限 rail；`before_tool_call` 仍执行以补发 chat.tool_call / chat.tool_update 事件。引导模式判定链（`resolve_interactive_ask_from_inputs` → ContextVar → rail 元数据）细节归 [[S_06]]。

### DisableThinkingMixin（plan_node.py）

- **显式多重继承才生效**（仅 opt-in）：`class MyNode(DisableThinkingMixin, PlanNode)`。
- 覆盖 `call_llm` / `stream_llm` 强制 `thinking="off"`（以 `PlanNode.call_llm(self, ..., thinking="off")` 显式调基类实现，绕过自身覆盖防递归）。
- `stream_llm_collect` 经 `stream_llm` 透传自动覆盖，无需另覆盖。
- 默认 PlanNode `thinking=None`（零注入，N12）；不改 ThinkingInjectRail / config；注入失败由 executor 跳过或裸重试兜底（N13）。适用场景：加速链路中追求确定性/低延迟的节点（如批量研究、页面生成 worker）。

### require_abort_reraise 技能侧写法规范

策略定义：`CodeValidationPolicy.require_abort_reraise`（validator.py），在 `plan_code()` 与 `builtin_skill_code()` 两个 profile 启用；静态检查 `_check_try_reraise`：bare except / `except Exception` / `except BaseException` 体内必须存在 `if isinstance(<name>, AbortError): raise`（或 `raise <name>`）守护；AbortError 可写作裸名或 `X.AbortError` 属性形式；**具体异常类（如 `except ValueError`）不受约束**；未通过 → 注册期整技能跳过（[[S_03]]）。

四种写法（静态检查只覆盖第 1 种，其余是技能侧纪律）：

```python
# 1) 标准模式：宽泛 except 前置 isinstance 守卫
try:
    ...  # 业务逻辑（含 call_tool / stream_llm / LLM 解析）
except Exception as exc:
    if isinstance(exc, AbortError):
        raise                     # HITL 中断原样上抛，不吞
    ...                           # 普通异常降级路径（默认值/空串/False）

# 2) gather 变体：return_exceptions=True 会把 AbortError 变成结果对象，
#    gather 之后必须先显式重抛再消费
results = await asyncio.gather(*tasks, return_exceptions=True)
for r in results:
    if isinstance(r, (AbortError, asyncio.CancelledError)):
        raise r

# 3) 窄捕获（如 except (ValueError, TypeError)）天然不含 AbortError，免守卫
# 4) 流式路径：hitl_interrupt 标志 + finally 条件触发 after（与引擎 execute_subplan_stream 同构）
try:
    ...
except AbortError:
    hitl_interrupt = True
    raise
finally:
    if after_callback and not hitl_interrupt:
        await after_callback(...)
```

引擎侧 AbortError 透传表（rail → use_tool → 模板方法 → execute_subplan → 消费循环 → 流式入口 → agent → adapter）归 [[S_06]]；本 spec 只固化技能侧写法。

### 节点能力面

| 能力 | 访问通道 | 引擎侧实现 | 语义要点 |
|---|---|---|---|
| 工具探测 | `has_tool(name)` | `executor.has_tool` → env | 布尔探测不执行；"先探测后调用"是技能侧铁律 |
| 工具调用 | `await call_tool(name, **kwargs)` | `executor.use_tool` | 确定性 tcid → resume 消费 → rail 链 → 执行；AbortError 先 save_resume_ctx 再上抛 |
| LLM | `call_llm` / `stream_llm` / `stream_llm_collect` | `executor.call_llm` / `stream_llm` | 见 LLM 调用契约 |
| JSON 提取 | `extract_json(raw, expected_type)` | `json_utils.extract_llm_json` | 四级提取链（见数据结构） |
| 受控日志 | `log` / `log_debug` / `log_info` / `log_warning` / `log_error` | `executor._log_from_node` | 级别白名单 + request_id/session_id 上下文格式化 |
| 子节点编排 | `execute_subplan(_stream)` / `skip_subplan(_stream)` / `should_skip_subplan` / `should_suppress_subplan_start_banner` | plan_node 自身 + executor 回调 | 任务追踪钩子（after 矩阵） |
| 数据 | `inputs` dict | executor `_merge_env_config_to_inputs` | 唯一数据入口：用户 inputs + `skill_root` / `skill_name` 注入 |

**不可访问**：session 对象、事件发射器、request_id、环境变量、白名单外 import（validator 拒绝，[[S_03]]）、文件 IO 方法（denied_attribute_call_names 封死 pathlib 读写）、dunder 属性（`__init__` / `__name__` 除外）。工具实例语义：`env.get_tool_function` 按 `Runner.resource_mgr` 全局 tool_id 现取共享实例；send_file 等带 session 路由的工具执行期从请求级 ContextVar 解析，节点侧无感知（[[S_01]] N9）。

## 数据结构

### plan_name / display_names 双层命名

- `plan_name`：skill_code 内部稳定标识，**不直接面向用户**。用于：节点日志、trace / `node_execution_order`、fallback 定位（FallbackCall.node_name）、task_id 派生（`_get_or_create_task_state`）、节点产物 key（`_node_artifacts_holder[plan_name]`）。
- `display_names`：**root 节点可选类属性** `dict[str, str]`（plan_name → 界面显示名）。executor 在 `_prepare_root_node` 读取 `getattr(root, "display_names", None) or {}` 存入 `self._display_names`；`_display_name(plan_name)` 查表，未命中原样返回 plan_name（向后兼容）。task.start / task.complete / llm_output / llm_reasoning 事件的 `plan_name` 字段均经此映射；`_replace_plan_names_in_text` 把文本中出现的 plan_name 替换为显示名（面向用户的 summary / message）。

### inputs / outputs 约定

- **inputs 整树共享同一 dict 引用**：executor `merged_inputs`（用户 inputs 浅拷贝 + `skill_root` / `skill_name` 注入，均带 "not in merged" 保护——resume inputs 已有则不覆盖）。节点间数据传递是技能侧约定，executor 不做数据路由。
- **子节点写回约定键**：子节点通常把结果写回 `inputs[plan_name]` 或 merge 结果字段（技能侧约定：跳过 node / status / message / content / skipped / resume_skip 等元键）；"下游未消费的键也保持存在"是技能侧防御惯例（默认键预置，防下游 KeyError）。
- **输出推荐 dict 且至少含 `node/status/result`**；skip 类结果额外 `skipped: True`（resume skip 双标记 `skipped + resume_skip`）。
- **业务失败表达**：结果 dict 含 `export_status=="failed"` 或 `status=="error"` → executor 侧 `_is_subplan_business_failure` 识别 → task.complete 标 failed（节点不抛异常也可表达失败终态）。
- **产物表达**：成功结果 dict 可携带 `__artifact__` 键（dict，含 `info` / `files` / 可选 `delivery_summary`）——`_after_subplan_execute` → `_collect_node_artifact` 读取并从结果中 pop，记入内存 holder（status="completed"）；异常路径读异常对象上的 `_partial_artifact` 属性（getattr，status="interrupted"）。holder 单条结构：

```python
{ "task_id": ..., "status": "completed|interrupted", "info": {...},
  "files": [...], "finished_at": ..., "delivery_summary": "..." }   # 键入 _node_artifacts_holder[plan_name]
```

只存摘要/小结构/路径，不存大文件内容；落盘与继承生命周期归 [[S_01]]。

### json_utils.extract_llm_json（四级提取链）

`extract_llm_json(raw, expected_type=dict)` 是 `PlanNode.extract_json` 回调的底层（`_bind_node_callbacks` 注入）：

1. **目标类型直返**：已是 expected_type（或已是 dict/list 结构化对象）直接返回；非 str 非结构化 → ValueError。
2. **纯 JSON 字符串直解**：`json.loads` 成功且类型匹配即返回。
3. **fence 正则提取**：` ```json ... ``` ` / ` ``` ... ``` ` code block 内解析。
4. **括号计数法**：`_extract_outermost_json`（跳过字符串内字符与转义；按 expected_type 选 `{}` 或 `[]` 起止）提取第一个完整 JSON 结构。

全部失败 → ValueError 携带**首个 JSONDecodeError 的精确位置上下文**（pos±80 字符，换行转义，含行列号）。与 tool_utils.parse_llm_json 的分工：后者 = strip_code_fence + parse_json_tolerant（含内嵌引号修复），面向 skill_code 手写解析的便捷入口（[[S_05]]）；两者都不做 LLM 级重试（N16）。

## 执行路径全景（P1-P5）

- **P1 root 树准备**（executor `_prepare_root_node`）：plan_code（`from {module} import root`，[[S_02]]）→ validator 校验（[[S_03]]，失败 E5）→ `_load_plan_namespace`（动态包自愈 [[S_05]] + 受限 namespace exec）→ `_extract_root_node`（N9 三分支）→ **deepcopy**（N11）→ `_bind_node_callbacks`（12 回调一次注入，N6）→ 读 display_names。节点视角：构造即绑定全部回调，执行期不存在"补绑"。
- **P2 模板方法执行**：`root.run_stream(inputs)`（流式主链）/ `root.run(inputs)`（非流式）→ 子类 `_execute_stream` / `_execute` → 三类异常分流（N2；AbortError/FallbackContractError 直通，其余进 fallback 回调，回调缺失 raise；[[S_07]]）。run 失败先打 WARNING 再进 fallback；run_stream 直接进流式 fallback。
- **P3 子节点调度**：编排节点 `_execute[_stream]` 内调 `execute_subplan[_stream]`（before → maybe_skip → run[_stream] → after，四态矩阵）；业务性跳过走 `skip_subplan[_stream]`。depth==1 的子节点才产生 task 事件（N7，[[S_01]]）。
- **P4 LLM 调用**：节点 `call_llm` / `stream_llm` / `stream_llm_collect` → executor 重试循环（见接口契约）；`concurrent=True` 生成分桶 source_id（前端消费归 [[S_08]]）。
- **P5 ask_user HITL**：`call_tool("ask_user")` → tcid → rail interrupt（AbortError）→ save_resume_ctx → adapter 三件套 →（用户作答）→ resume 重放 → tcid name+idx 回退命中 → answers 回填 → 节点拿到结构化作答继续（细节归 [[S_06]]）。

## 异常分支与边界 case

| # | case（场景 / 现行为 / 风险） | 测试覆盖 |
|---|---|---|
| 1 | **回调未初始化**：`RuntimeError("PlanNode xxx 回调未初始化")`——正常链路（deepcopy 后先绑回调再执行）不可达；出现即绑定顺序错误，必须修绑定而非捕获。 | 无 |
| 2 | **_execute_stream 默认包装**：未覆盖时调用 `_execute` 并单值 yield（流式/非流式行为一致性兜底）；产出进度片段与终态是技能侧可选增强。 | 部分：test_fallback_contract_error_task_events（间接使用默认包装） |
| 3 | **display_names 缺省兼容**：无类属性 → 空 dict → `_display_name` 原样返回 plan_name，技能完全可用。 | 无 |
| 4 | **stream_llm_collect 协程语义**：是协程非 async generator，可直接进 `asyncio.gather`（多页并发范式）；DisableThinkingMixin 经 stream_llm 透传自动覆盖 collect。 | 部分：test_skill_turbo_thinking（Mixin 面）；collect 协程语义无专测 |
| 5 | **execute_subplan_stream 的 finally-after**：正常 / 普通异常 / 外层提前 close 生成器均触发 after；仅 hitl_interrupt 抑制；after 自身失败仅 `logger.exception` 不影响主流程。 | 部分：test_abort_error_guards（AbortError 抑制 + 普通异常触发两态）；提前 close / after 自身失败无 |
| 6 | **skip_subplan 业务性跳过**：仍触发 before/after，task 事件完整，标记 skipped + status ok（能力不支持不算失败）。 | 部分：test_resume_skip_completed（resume skip 分支） |
| 7 | **resume skip result 的 "node" 键**：= subplan.plan_name，即"子节点写回约定键"的形态示例；下游读 `inputs[plan_name]` 不因跳过而缺键。 | test_resume_skip_completed |
| 8 | **业务失败 vs 异常的双表达**：`export_status=="failed"` / `status=="error"` 走 task.complete(failed)；基础设施失败走 raise → fallback。两种表达不得混用（后者会消耗 fallback 预算）。 | 无（_is_subplan_business_failure 无单测） |
| 9 | **should_skip_subplan 二次判定**：引擎判定与技能侧二次判定不一致时，技能侧回退真实执行（防 subplan 被静默丢弃）。 | 无（技能侧纪律） |
| 10 | **env.model_client 为 None** → RuntimeError("model_client 未配置")（配置错误 fail-fast）。 | 无 |
| 11 | **thinking 参数被拒** → 裸参重试一次（启发式判定见 LLM 契约；重试仍失败则按普通异常进重试 rail）。 | 部分：test_skill_turbo_thinking（启发式）；executor 内组合路径无 |
| 12 | **LLM 排队观测**：≥1000ms（`_LLM_QUEUE_WAIT_LOG_THRESHOLD_MS`）升 INFO 日志，可观测 Semaphore 排队时长。 | 无（日志观测） |
| 13 | **非引导模式 + preview 类 ask_user** → 直接 skipped 不弹卡（`interactive_ask` ContextVar；技能侧需处理 skipped 返回值，不得假装已确认——交付告警如实上报）。 | test_ask_user_rail_gates + test_interactive_ask |
| 14 | **节点视角的 AbortError**：`call_tool` 直接抛 AbortError，断点已由 use_tool 落库（save_resume_ctx）；节点只需保证不吞（require_abort_reraise 写法）。 | test_abort_error_guards + test_validator_external_profile（require_abort_reraise） |
| 15 | **run / run_stream 日志不对称**：run 进 fallback 前打 WARNING（含 plan_name/instruction/error），run_stream 直接进 fallback_stream（无 WARNING）——排查流式路径失败需依赖 fallback trace 而非节点日志。 | 无（日志不对称） |
| 16 | **display_names 读取时机**：`_prepare_root_node` 在绑回调后一次性读取类属性；执行中途修改 root.display_names 不影响本请求的事件映射（要生效需改类属性后重启/重新 import）。 | 无 |
| 17 | **inputs 浅拷贝语义**：`merged_inputs` 是用户 inputs 的浅拷贝——顶层键写入互不影响，但嵌套 dict/list 是共享引用；技能侧避免原地修改深层共享结构（约定：读后新建写回，不 in-place 改嵌套）。模块级 root 单例在并发请求间共享（deepcopy 前），任何在构造函数里捕获 executor 状态的写法都会造成跨请求串扰——N11 deepcopy 是唯一防线。 | 部分：test_executor_plan_code_import（deepcopy 防串扰断言） |

## 修改检查单

1. 改 `run` / `run_stream` 模板方法 → 必查三类异常分流顺序（N2）不变 + [[S_06]] 引擎侧透传表逐处复核 + [[S_07]] fallback 触发语义。
2. 新增回调槽 → 必查三处同步：`PlanNode.__init__` 槽位定义、`set_runtime_callbacks` 形参与递归传播、`executor._bind_node_callbacks` 注入；fail-fast RuntimeError 分支保留（N5）。
3. 改 `execute_subplan` / `execute_subplan_stream` → 必查 after 触发条件矩阵（成功/异常/AbortError/skip 四态）与 [[S_01]] task 状态机、resume 快照的耦合（N3）。
4. 新增节点能力（新回调方法）→ 必查能力面表更新 + [[S_03]] validator（白名单 / denied_call / dunder）+ executor 沙箱 namespace 三方一致（N10）。
5. 改 stream_source_id 格式 → 必查前端分桶消费（llm_reasoning / llm_output / llm_usage 注入点）与 [[S_08]] 缓冲桶 key；`skill_turbo:` 前缀与 subagent 前缀的区分语义不得丢失。
6. 改 DisableThinkingMixin → 必查 stream_llm_collect 透传链 + thinking vendor_map 白名单语义（[[S_02]]）+ N13 fail-open 兜底。
7. 改 json_utils 提取链 → 必查 `PlanNode.extract_json` 回调绑定（executor `_bind_node_callbacks`）+ 与 tool_utils.parse_llm_json 的分工边界（[[S_05]]）+ N16（不引入 LLM 级重试）。
8. 改 skip result 形态 / resume 标记 → 必查 [[S_06]] `_should_skip_subplan_execute` 消费、产物继承（[[S_01]]）与前端 task 展示。
9. 改 require_abort_reraise 静态检查 → 必查 [[S_03]] 两个 profile + 技能侧写法规范与门禁测试同步（技能侧仓 test_turbo_registration_gate）。
10. 改 inputs 环境注入（skill_root / skill_name）→ 必查 "not in merged" 保护（resume 不覆盖持久化值，[[S_01]]）。
11. 改 `__artifact__` / `_partial_artifact` 语义 → 必查 [[S_01]] 产物三操作（clear / inherit / persist）与外层交付摘要消费（`__artifact__.info.send_file_status`）。
12. 改 display_names 机制（类属性名 / 映射时机）→ 必查 executor `_prepare_root_node` 读取点、`_display_name` / `_replace_plan_names_in_text` 全部消费点与 [[S_08]] 事件字段。
13. 改回调生命周期（绑定时序 / deepcopy）→ 必查 N11（先拷贝后绑定顺序不可倒置）与 [[S_01]] 并发模型。

## 与其它 spec 的关系

| Spec | 关系 |
|---|---|
| [[S_01]] | 本 spec 的 P2/P3 从节点侧描述模板方法与钩子；执行循环（双生产者、E1-E13、终态收口）与产物落盘归 S_01。S_01 把节点 IO 约定与 `__artifact__` / `_partial_artifact` 语义归本 spec。 |
| [[S_02]] | 本 spec 的入口 root 消费 S_02 的 plan_code 组装结果；display_names 与技能路由元数据（meta.json）分属两层。 |
| [[S_03]] | 契约第 7 条（import 禁令）与 require_abort_reraise 的 validator 强制细节归 S_03；能力面表（N10）的 enforcement（白名单/沙箱）同归 S_03。 |
| [[S_05]] | 节点 import 的门面符号（AbortError / PlanNode 等）与 tool_utils（run_bash / parse_llm_json）契约归 S_05；本 spec 是这些符号的节点侧消费语义。 |
| [[S_06]] | 本 spec 的 P5 / N3 只固化节点侧行为（call_tool 同 API、after 不触发）；tcid 机制、发卡、恢复分流、引擎侧 AbortError 透传表归 S_06。 |
| [[S_07]] | 本 spec 的 N2 异常三分流是 fallback 的触发入口；FallbackCall 构造、validate_fallback_success 调用时机（契约第 11 条）、4 层降级决策归 S_07。 |
| [[S_08]] | stream_source_id 生成（本 spec P4）的下游——事件注入、缓冲分桶、前端消费协议归 S_08。 |
| [[S_09]] | ask_user 的 adapter 三件套、引导模式上游（request_metadata）归 S_09。 |
