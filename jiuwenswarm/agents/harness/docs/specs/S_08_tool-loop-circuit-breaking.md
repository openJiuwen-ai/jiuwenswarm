# S_08 工具循环熔断与错误检测

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_08 / tool-loop-circuit-breaking |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/rails/execution_guard/circuit_breaker_rail.py`（rail + `ToolResultErrorDetector` + 三 dataclass）；装配点 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_build_circuit_breaker_rail` / `_circuit_breaker_config_from_mapping` / `_coerce_circuit_breaker_threshold`） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen.harness.rails.base.DeepAgentRail`、`openjiuwen.core.single_agent.rail.base.AgentCallbackContext / InvokeInputs`；互斥方 `agent_ras`（RAS 检测器族，`repeat_tool` / `llm_thinking_loop`） |
| 最近一次修订日期 | 2026-10-24 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**工具结果层的循环检测与断路**：

- `CircuitBreakerRail` 四检测器（generic_repeat / unknown_tool_repeat / global_breaker / ping_pong）的判定算法、阈值、告警 / force-finish 语义
- per-session 调用历史（`ToolCallRecord` 滚动窗口）与 sid 解析
- `ToolResultErrorDetector` 共享契约：结构化错误判定链、归一化链、裸前缀注册机制——它是 unknown_tool streak 的判定基础，**可被其它模块直接 import 复用**（类文档明示）
- 装配侧阈值校验（序关系、强转、默认关）

不管：

- LLM 调用层的重试与溢出恢复 → [[S_07]]
- 并发安全与计划一致性 → [[S_09]]
- 输入侧守卫 → [[S_10]]
- `agent_ras` 检测器族的算法本体（`repeat_tool` 的 ignored_tool_calls 豁免、`llm_thinking_loop` 的 L1/L2/L3 检测）——实现在 openjiuwen，本 spec 只钉死**双挂互斥**关系（N1）
- StreamEventRail 主体（`__jiuwenswarm_session_id__` 键的生产者，circuit breaker 只读它作 sid fallback）→ 任务 7 事件流域
- `execution_guard` 下其它节（`llm_retry_rail` 归 [[S_07]]）

**范围排除甄别**：`execution_guard/circuit_breaker_rail.py` 是该目录下唯一件；本 spec 的 "circuit breaker" 是**循环检测断路器**（重复 / ping-pong / 未知工具打断），**不是** OPEN/HALF_CLOSED/CLOSED 恢复型熔断器（config.yaml 注释明示"不是 issue 的 OPEN/HALF_OPEN 恢复型熔断"）。做故障恢复设计时勿在此找半开逻辑。

## 不变量

- **N1（默认关闭、与 agent_ras 双挂互斥）**：`execution_guard.circuit_breaker.enabled` 默认 **false**（config.yaml），装配点缺省也按 False 处理。原因：openjiuwen `agent_ras` 默认开启（`agent_ras.enabled: true`，含 `repeat_tool` 检测器），与本 rail 四检测器语义重叠，同开会对同一循环**双重 force-finish**。开启本 rail 属显式决策；`test_dest_agent_ras_passthrough_is_not_disabled` 钉死"关 circuit breaker 不得顺手关 RAS"。
- **N2（四检测器优先级序短路）**：`_detect` 依次判 global_breaker（≥`global_breaker_threshold`，CRITICAL）→ unknown_tool_repeat（≥`unknown_tool_threshold`，CRITICAL）→ ping_pong（no_progress 且 rounds ≥`critical_threshold`，CRITICAL；≥`warning_threshold`，WARNING）→ generic_repeat（≥`warning_threshold`，WARNING）。**先中先断**——一次 `after_tool_call` 至多产出一个 `DetectionResult`，高级别检测器命中后低级别不再评估。
- **N3（args_hash 剥离 metadata 键）**：`_METADATA_ONLY_TOP_LEVEL` 中 bash / powershell / Agent 三工具的顶层 `description` 键在哈希前剔除——仅改描述文字的重复调用不算新调用。其余工具不剥离。
- **N4（result_hash 缺失即断链）**：`_get_no_progress_streak` / `_get_side_tail_streak` 遇 `result_hash is None` 立即停止累计——"结果未知"既不算进展也不算停滞，不喂给 no_progress 判定（`_hash_outcome(None)` 产出 None）。
- **N5（ping-pong 判定口径）**：`_collect_alternating_streak` 从尾部收集**完整交替序列**（末条与前一不同 args_hash 组成 AB 对，向前按 ABAB 交替延伸，破坏交替即止）；`no_progress_rounds = min(两侧各自尾部连续相同 result_hash 数)`；`no_progress = rounds >= 2`。单侧变动即有进展，rounds 归 0 侧。
- **N6（错误判定三级回退）**：`infer_record_has_error` 序：① `has_error(tool_result)`（结构化判错）→ ② `has_explicit_success(tool_result)`（结构化判成功，**压过** ctx 噪声）→ ③ `ctx_has_error(ctx)`（ctx.exception 非 None 或 tool_msg.content 判错）。结果不可解析时才信 ctx，避免成功结果被 ctx 误判。
- **N7（critical 即 force-finish，warning 只日志）**：CRITICAL → `ctx.request_force_finish({"output": 文案, "result_type": "answer"})` + `logger.error`；WARNING → 仅 `logger.warning`，不中断循环。
- **N8（per-session 历史隔离与生命周期）**：sid 解析优先级 = `_invoke_sid` ContextVar → `ctx.extra["__jiuwenswarm_cb_session_id__"]` → `ctx.extra["__jiuwenswarm_session_id__"]`（stream 侧键，须与 invoke sid 一致才复用）→ `"default"`。`before_invoke`（InvokeInputs）置 sid + **清空该 sid 历史**（每条新消息重开窗口）；`after_invoke` 清理该 sid 历史 / 锁并复位 ContextVar；`cleanup_session(sid)` 显式清理。append + detect 全程持 per-sid `asyncio.Lock`。
- **N9（history_size 派生）**：`max(4 × critical_threshold, 2 × global_breaker_threshold, 2 × unknown_tool_threshold)`——窗口为最苛检测器保 2 倍、为 critical 保 4 倍采样余量；超窗截尾（保留最近）。
- **N10（裸前缀契约）**：`_PLAIN_ERROR_PREFIXES` 只放项目级通用约定（当前仅 `[ERROR]:`，search / image / audio / video / pdf / web_fetch 等纯文本工具的失败约定）；功能专属前缀必须经 `register_plain_error_prefix` 注册（类级共享 set，strip + upper 归一，大小写不敏感、须开头匹配）。新前缀准入标准：**失败结果独有，成功输出绝不以其开头**——一个坏前缀会同时污染所有复用方。裸错误散文（如 `"Error happened"`）不匹配，只有前缀形态算。

## 接口契约

### CircuitBreakerConfig

| 字段 | 默认 | 说明 |
|---|---|---|
| `warning_threshold` | 10 | generic_repeat / ping_pong WARNING 线 |
| `critical_threshold` | 20 | ping_pong CRITICAL 线（须 ≥ warning） |
| `global_breaker_threshold` | 30 | 无进展兜底 CRITICAL 线（须 ≥ critical） |
| `unknown_tool_threshold` | 10 | 未知工具连续错误 CRITICAL 线 |
| `history_size`（property） | 派生 | N9 |

### CircuitBreakerRail

- 构造：`(config: CircuitBreakerConfig | None, language="cn")`；语言经 `_normalize_language`（config 的 `zh` → 内部 `cn`，非法回落 `cn`）。
- `set_language(language)`：per-request 更新文案语言（builder 每次装配调 `_resolve_runtime_language` 传入）。
- 四挂点：`before_invoke`（N8 置 sid + 清史）/ `before_tool_call`（把解析出的 sid 回写 ctx.extra）/ `after_tool_call`（记录 + 检测 + 动作）/ `after_invoke`（清理）。
- `_hash_args`：`sha256(f"{tool_name}:{canonical}")`，canonical = `json.dumps(sort_keys=True, default=str)` 且经 N3 剥离。
- `_hash_outcome` / `_normalize_result`：dict 取 content / output / error / status 四键 str 化（strip）；非 dict 包成 `{"raw": str}`；None 结果 → hash 为 None。

### ToolResultErrorDetector（共享，其它模块可 import）

| 方法 | 语义 |
|---|---|
| `has_error(value)` | 归一化后 `_dict_has_error` |
| `has_explicit_success(value)` | 归一化后有 `success` 键且 boolish true |
| `ctx_has_error(ctx)` | ctx.exception 非 None；或 tool_msg.content 判错 |
| `infer_record_has_error(tool_result, ctx)` | N6 三级回退 |
| `register_plain_error_prefix(prefix)` | N10 注册 |

`_normalize` 归一链：None → dict 原样 → `model_dump()`（pydantic）→ 有 `success` 属性的对象取 `{success, data, error}` → 字符串：`{` 开头尝试 `json.loads`（失败弃）→ `success = False/True` 的 repr 正则（`^success\s*=\s*...`）→ 裸前缀（upper 后 startswith，N10）→ 不可归一返回 None。

`_dict_has_error` 判定链（顺序即优先级）：`success` 键 boolish 定夺（false → True，true → False）→ `is_error` / `isError` boolish true → `status ∈ {error, failed, failure}` → `result_type == "error"` → `error` 非空且非 `"none"` → exit code（`exit_code` / `exitCode` / `returncode` / `return_code`）int 非 0 → `data` 嵌套递归（`check_data=False` 只下探**一层**）。boolish：`False` / `"false"` / `"0"` / `"no"` 与对偶。

### 装配（interface_deep）

- `_coerce_circuit_breaker_threshold(value, default)`：int 强转，失败或 ≤0 回 default（字符串阈值可强转）。
- `_circuit_breaker_config_from_mapping(cb_cfg, defaults)`：**序关系校验**——`warning > critical` 或 `critical > global_breaker` 返回 None（拒挂）。
- `_build_circuit_breaker_rail()`：enabled 非 `is True` → None（N1）；config 非法 → warning + None；成功 → `CircuitBreakerRail(config, language=runtime language)`。装配异常一律 warning + None，不炸启动。

### 配置（`execution_guard.circuit_breaker`，config.yaml）

`enabled: false`（默认）/ `warning_threshold: 10` / `critical_threshold: 20` / `global_breaker_threshold: 30` / `unknown_tool_threshold: 10`。

## 数据结构

- `ToolCallRecord`：`tool_name` / `args_hash` / `result_hash (str | None)` / `timestamp` / `has_error`
- `DetectionResult`：`stuck` / `level ("critical"|"warning")` / `detector` / `count` / `msg_key` / `tool_name`
- `PingPongResult`：`no_progress_rounds` / `paired_tool` / `no_progress`
- rail 实例：`_histories: dict[sid, list[ToolCallRecord]]` 与 `_locks: dict[sid, asyncio.Lock]` 双字典；`_MESSAGES` 文案表按语言 × 五 msg_key（`global_circuit_breaker` / `unknown_tool_repeat` / `ping_pong_critical` / `ping_pong_warning` / `generic_repeat`），占位 `{tool_name}` / `{count}`。

## 执行路径全景

- **P1 装配**：config 读 `execution_guard.circuit_breaker` → 默认关直接 None（N1）→ 显式开后阈值强转 + 序校验 → rail 实例。
- **P2 invoke 开场**：`before_invoke` 取 `conversation_id`（缺省 "default"）为 sid → 写 ctx.extra + ContextVar → **清空该 sid 历史**。
- **P3 工具后置**：`after_tool_call` → tool_call / tool_name 缺失即 return → 解析 sid → args_hash（N3）+ result_hash + has_error（N6）→ 持锁入史 + 超窗截尾 → `_detect`（N2）→ CRITICAL 走 force-finish、WARNING 走日志（N7）。
- **P4 检测序**：global（尾部同工具同参同结果 streak ≥ 30）→ unknown（尾部同名全错 streak ≥ 10）→ ping-pong（N5，≥20 critical / ≥10 warning）→ generic（全窗同工具同参计数 ≥ 10）。
- **P5 invoke 收场**：`after_invoke` 清 sid 历史 / 锁 + ContextVar 复位。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| 装配关闭 | enabled 非 True | 不注册（N1） |
| 阈值序非法 | warning > critical 或 critical > global | builder 返回 None，warning 日志 |
| 阈值非数字 / ≤0 | config 脏值 | 强转失败 / 非正回默认 |
| tool_call None / tool_name 空 | 非工具回调 | `after_tool_call` 直接 return，不入史 |
| result None | 工具无结果 | result_hash=None，N4 断链 |
| 结果不可归一 | 未知形态字符串 | `_normalize` 返回 None，`has_error`/`has_explicit_success` 均 False，落 N6 第三级信 ctx |
| 装配异常 | 任一 builder 抛错 | warning + None，不炸 agent 启动 |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| generic_repeat 达 10 | 只 WARNING 日志，不中断 | 误期断路 | `test_generic_repeat_warns_without_force_finish` |
| no_progress 尾部遇不同调用 | streak 立即停 | 长历史误累 | `test_no_progress_streak_stops_at_a_different_call` |
| 尾部 result_hash 缺失 | streak 停（N4） | None 当同结果误判停滞 | `test_no_progress_streak_stops_when_result_hash_is_missing` |
| 同参同结果 30 连 | global CRITICAL force-finish | —— | `test_global_breaker_interrupts_no_progress` |
| 同名工具连续报错 10 | unknown CRITICAL force-finish | 成功调用重置 streak | `test_unknown_tool_repeat_interrupts_error_streak`、`test_after_tool_call_force_finishes_unknown_tool` |
| 两工具交替、双侧结果不变 | ping-pong CRITICAL | 单侧变动即有进展（N5） | `test_ping_pong_critical_when_both_sides_stall` |
| cleanup 后同 sid | 历史已清，重新累计 | —— | `test_cleanup_session_drops_history` |
| 两 session 并行 | 历史互不可见 | 跨会话误累 | `test_detect_is_isolated_per_session_history` |
| builder 无配置 | 默认关（N1） | 误以为默认开 | `test_circuit_breaker_builder_stays_disabled_by_default` |
| 阈值 warning > critical | builder 拒挂返回 None | 静默关 | `test_circuit_breaker_builder_rejects_invalid_thresholds` |
| 阈值传字符串 "15" | int 强转生效 | —— | `test_circuit_breaker_builder_coerces_string_thresholds` |
| 关 circuit breaker 时 RAS | agent_ras 透传保持开 | 顺手关掉 RAS | `test_dest_agent_ras_passthrough_is_not_disabled` |
| detector：失败形态族 | 结构化判错 | —— | `test_failures_are_recognised`（参数化） |
| detector：成功形态族 | 不误报 | —— | `test_successes_are_not_flagged`（参数化） |
| detector：不可分类 | 两不认（N6 落第三级） | 强行二分 | `test_unclassifiable_shapes_claim_neither` |
| explicit success 须有字段 | 无 success 键不算显式成功 | —— | `test_explicit_success_requires_the_success_field` |
| success=true + exit_code=1 | success 压过 exit | —— | `test_a_success_field_beats_a_nonzero_exit_code` |
| `[ERROR]:` 大小写 | 前缀匹配不敏感 | —— | `test_plain_error_prefix_is_matched_case_insensitively` |
| 前缀须开头 | 句中含 `[ERROR]:` 不匹配 | —— | `test_a_prefix_must_start_the_string` |
| 裸错误散文 | 不匹配（N10） | 泛匹配误报 | `test_bare_error_prose_is_not_matched`（参数化） |
| register 注册前缀 | 识别且大小写不敏感；空白注册 no-op | —— | `test_registered_prefix_is_recognised`、`test_registered_prefix_is_matched_case_insensitively`、`test_registering_a_blank_prefix_is_a_no_op` |

## 修改检查单

- 改四阈值默认值 → `history_size` 派生（N9）联动评估 + builder 三用例（默认关 / 拒非法 / 强转）同步
- 改检测器优先级序（N2）→ force-finish 语义重估：global 提前会让 unknown / ping-pong 永远不触发
- 向 `_PLAIN_ERROR_PREFIXES` 加项 → 必须先证明"失败独有"（全仓工具成功输出样例过一遍）；功能前缀一律走 `register_plain_error_prefix`
- 改 `_METADATA_ONLY_TOP_LEVEL` → 轮询类工具（带 description 的 bash 脚本轮询）误报面变化
- 改 `_normalize` / `_dict_has_error` 链序 → `test_tool_result_error_detector.py` 全 12 用例回归（success 压过 exit 的优先级尤其敏感）
- 改 sid 解析或生命周期键名 → stream 侧 `__jiuwenswarm_session_id__`（StreamEventRail / skill_active_state）双写方对齐
- 改文案表 `_MESSAGES` → 与 openjiuwen `SUPPORTED_LANGUAGES` 对齐；占位符 `{tool_name}` / `{count}` 保持
- 改 force-finish payload 结构 → 前端 answer 消费方同步
- 改 N1 互斥关系（如默认开）→ 必须同步关 agent_ras 或重叠检测器，`test_dest_agent_ras_passthrough_is_not_disabled` 重估

## 与其它 spec 的关系

- [[S_07]]（LLM 调用层失败恢复）：`execution_guard` 域兄弟件——S_07 管 `on_model_exception`，本 spec 管 `after_tool_call`；`llm_retry_rail` 与 `circuit_breaker` 是同域两个配置节
- [[S_09]] / [[S_10]]：可靠性域其余两件，挂点与异常源互不重叠
- openjiuwen `agent_ras`：N1 双挂互斥的另一极；`repeat_tool` 的 `ignored_tool_calls` 豁免（`invoke_tool` + `tool_name=image_get_run`）是 RAS 侧机制，本 rail 无豁免机制——若需豁免轮询工具，当前只能靠 N3 的 metadata 剥离
- StreamEventRail（任务 7 事件流域）：`__jiuwenswarm_session_id__` 键的生产者，本 rail 只读
- `ToolResultErrorDetector` 的复用方：任何需要"工具结果是否失败"判定的模块（含 skill_turbo 侧流检查器）都受 N10 前缀契约约束
