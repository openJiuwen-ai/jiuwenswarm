# S_07 LLM 调用层失败恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_07 / llm-call-failure-recovery |
| 关联模块（jiuwenswarm 仓） | `jiuwenswarm/agents/harness/common/rails/llm_retry_notify_rail.py`、`context_overflow_recovery_rail.py`；装配点 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`（`_build_llm_retry_rail` / `_build_context_overflow_recovery_rail`）、`interface_code.py`（`_FIXED_RAIL_NAMES`）、`jiuwenswarm/server/runtime/skill_turbo/executor.py`（`_build_llm_retry_rail`）、`jiuwenswarm/agents/swarm/config_specs.py` + `providers/builtin_rails.py`（成员 rail 投影） |
| 关联模块（agent-core 仓，跨仓契约） | `openjiuwen/harness/rails/llm_retry_rail.py`（`LLMRetryRail` 父类：repeat 检测参数、`backoff_delay`、`_is_repeat_exception` / `_is_stream_timeout_exception` / `_is_transient_invoke_exception`）、`openjiuwen.harness.rails.base.DeepAgentRail`、`openjiuwen/core/session/stream.OutputSchema` |
| 最近一次修订日期 | 2026-10-28 |
| 关联 feature | 暂无 |

## 范围 / 边界

本规约管**LLM 调用层的失败恢复**——`on_model_exception` 挂点上的两条自愈链：

- **瞬时失败重试链**（`NotifyingLLMRetryRail` / `TeamMemberNotifyingLLMRetryRail`）：三类原因（repeat / stream_timeout / transient_invoke）的判定分流、per-call 预算、指数退避 + jitter、用户可见通知（重试中 / 耗尽）
- **上下文溢出恢复链**（`ContextOverflowRecoveryRail`）：溢出异常检测（三家厂商格式）、FullCompact 强制压缩 + `threshold_override`、立即重试、连续溢出熔断
- 配置消费契约：`execution_guard.llm_retry_rail` 节（deep / skill_turbo / swarm 成员三处装配共用）
- 通知 schema：`retry_notification` 与 `error` 两类 `OutputSchema` 事件的 payload 契约

不管：

- 工具循环层的熔断（重复调用 / ping-pong / 未知工具）→ [[S_08]]
- 并发安全与计划一致性 → [[S_09]]
- 输入侧守卫（路径修复 / 读校验 / 配额）→ [[S_10]]
- `agent_ras`（openjiuwen 侧的 RAS 检测器族，`repeat_tool` / `llm_thinking_loop`）——与 `execution_guard.circuit_breaker` 的双挂互斥关系归 [[S_08]]（S_08 N1）
- skill_turbo 降级体系（retry rail 只是 skill_turbo 的装配件之一）→ `jiuwenswarm/server/runtime/skill_turbo/AGENTS.md`
- `LLMRetryRail` 父类的 repeat 片段检测算法本体（`_is_repeat_exception` 的滑窗 / 词频实现）——实现在 agent-core，本 spec 只登记构造参数契约

**范围排除甄别**（名实不符处，勿在本 spec 内扩展）：

- `ContextOverflowRecoveryRail` 的 docstring 声称恢复链有五步，其中第 2 步 "SessionMemory — force an incremental notes update (bypass trigger_tokens)" **在当前实现中无对应代码**——`on_model_exception` 的实际恢复动作只有 `_set_force_compact_flag`（FullCompact）+ `request_retry`。SessionMemory 若发生更新只能由 FullCompact 链内部吸收，改本 rail 时以实现为准、勿以 docstring 为准
- code 侧（`interface_code.py:741`）的注释 "靠 set_force_compact 触发 FullCompact" 是对 deep 侧行为的复述，code 侧经 `_FIXED_RAIL_NAMES` 固定挂载同一 rail 类

## 不变量

- **N1（per-call 预算隔离）**：重试计数唯一权威存储是 `ctx.extra["_llm_retry_counts"]`（三键 `repeat` / `stream_timeout` / `transient_invoke` 各自独立计数）。`before_invoke` 只重置**本 ctx** 的计数——并发共享同一 rail 实例的 `call_llm` / `stream_llm`（SkillTurbo PPT 页收集）互不偷、互不清对方的计数。实例属性（`repeat_retry_count` 等，`_COUNT_ATTRS` 映射）只是观测镜像，供日志 / 外部读数，不参与判定。
- **N2（原因三分流有序互斥）**：`on_model_exception` 依次判 `_is_repeat_exception` → `_is_stream_timeout_exception` → transient_invoke，**先命中先归类**，一次异常只消耗一个原因的预算。transient_invoke 受 `retry_transient_invoke_errors` 开关；前两类不受。
- **N3（peek / consume 分离）**：`_peek_retry` 纯预演（供通知文案取 attempt 编号），**不消耗**计数；`_request_retry_or_reset` 才 `counts[reason] += 1` 并 `ctx.request_retry(delay_seconds=delay)`。两处对同一 delay 的 jitter 独立采样——通知展示的 delay 与实际生效 delay 允许不同（均为合法 jitter 值）。
- **N4（耗尽重置不吞异常）**：某原因计数达 `max_retries` 后，`_request_retry_or_reset` 把该原因计数与镜像属性归零、**不再** `request_retry`，异常继续向上传播（由上层 / 父类框架决定终态）。rail 永不吞掉模型异常。
- **N5（通知 best-effort）**：session 为 None、无 `write_stream` 方法 → 通知静默跳过；`write_stream` 抛异常 → `logger.warning` 吞掉。恢复主链（retry / force_compact）**永远先于且不依赖**通知成功。
- **N6（退避几何固定）**：自动生成序列 = `_exponential_backoff(i) = min(5.0 × 2^i, 60.0)`，长度取**父类已解析的** `self.max_retries`（不在子类复制父类默认值）。仅在构造时未传 `backoff_seconds`（含显式 `None`）才生成，并置 `_backoff_generated = True`。
- **N7（jitter 双限定）**：jitter 因子 `random.uniform(0.5, 1.5)` 仅作用于自动生成序列（显式调用方序列精确执行、零抖动）；且**每次重试请求时重新采样**（非构造时烘进序列——并发调用者共享实例，构造期采样会让他们永远同步，jitter 失效）。
- **N8（TeamMember 404 全标记命中）**：`TeamMemberNotifyingLLMRetryRail` 额外认定的 async-stream 404 传输错误要求 `_MEMBER_NETWORK_404_MARKERS`（`async stream error` / `notfounderror` / `error code: 404`）**全部**命中。agent-core wrapper 措辞漂移即静默禁用该规则（有意设计：宁漏勿滥），通用六标记重试不受影响。
- **N9（溢出检测三级判定）**：`_is_context_overflow_error` 判定序——① `status_code == 413` 直接判；② `status_code == 400` 且命中七关键词之一；③ 无 `status_code` 且命中关键词；另：`"error code: 400"` + `"modelarts.81001"` + 关键词三条件组合。400 单独不判（参数错 / 权限错同码，必须靠词消歧）。
- **N10（溢出处理优先权）**：`priority = 100`，高于 StreamEventRail（80）与 ContextAssembleRail / ContextProcessorRail（均 85，context_engineer 域）——溢出恢复必须**先于**流处理与上下文工程 rails 处理 `on_model_exception`，否则压缩动作会晚于其它 rail 的错误消费。
- **N11（恢复动作与阈值）**：恢复 = `set_force_compact(True)` + `set_overflow_threshold_override(limit × RECOVERY_THRESHOLD_RATIO)`，比例恒 0.85（压缩后上下文至多占窗口 85%，留 15% 给输出）。`limit` 三级解析：异常文本解析值 → `deep_config.context_window_limit_tokens` → `None`（FullCompactProcessor 用默认 `trigger_total_tokens`）。**无论 force_compact 设置成败一律 `ctx.request_retry(delay_seconds=0)`**——压缩失败也要重试一次让失败可见。processor 识别用**同名键**（`processor_type() == "FullCompactProcessor"`）而非 isinstance：`forked.activate()` 会把注册键覆盖为 forked 类，其实例不继承原始类，isinstance 漏判。
- **N12（连续溢出计数三态）**：`_consecutive_overflow_count` 是 **rail 实例级**（跨 ctx / 跨 session 共享，装配层每 agent 一实例）。归零时机恰两处：`after_model_call` 且 `ctx.exception is None`（成功即清）；`_circuit_break` 的 error 事件**写出成功后**（写出失败只 warning、计数保留，下轮再熔断报）。超过 `max_recovery_attempts`（构造默认 2，装配层传 3）→ 熔断：发 `context_overflow_circuit_break` error 事件（建议 `/compact` 或新会话）、不再 retry。

## 接口契约

### NotifyingLLMRetryRail（deep / skill_turbo / swarm 成员通用）

构造参数（`**kwargs` 全透传父类 `LLMRetryRail`）：

| 参数 | 类型 / 默认 | 语义 |
|---|---|---|
| `notify_user_on_retry` | bool / True | 重试进行中是否推 `retry_notification` |
| `notify_user_on_exhausted` | bool / True | 耗尽后是否推 `error` 事件 |
| `retry_transient_invoke_errors` | bool / True | transient_invoke 类是否参与重试（经 `_parent_accepts_retry_transient_invoke_errors` 签名探测决定是否也传给父类——父类签名演进时的兼容垫片） |
| `backoff_seconds`（kwargs） | list[float] / None | 显式序列精确执行；缺省 / None 生成（N6/N7） |
| `max_retries` + repeat 检测六参数（kwargs） | —— | 透传父类；deep / skill_turbo / swarm 三处装配的取值表见"配置消费" |

挂点方法：

| 方法 | 行为 |
|---|---|
| `before_invoke(ctx)` | 重置本 ctx 三键计数（N1）后调父类 |
| `on_model_exception(ctx)` | N2 分流 → `_peek_retry` → 按 will_retry 发 retry / exhausted 通知（N5）→ `_request_retry_or_reset` |
| `_looks_like_transient_invoke(exc)`（classmethod） | 两级：父类存在 `_is_transient_invoke_exception` 则委托；否则按 `_TRANSIENT_MESSAGE_MARKERS` 六标记（connection error / reset / refused、request timed out、read timeout、connect timeout）小写子串匹配 |

### TeamMemberNotifyingLLMRetryRail

仅重写 `_looks_like_transient_invoke`：父级判定通过即 True；否则 `_MEMBER_NETWORK_404_MARKERS` 三标记 **all** 命中才 True（N8）。swarm 成员专用（builder `swarm.team_member_llm_retry`）。

### ContextOverflowRecoveryRail

| 成员 | 契约 |
|---|---|
| `__init__(max_recovery_attempts=2)` | 装配层（deep `interface_deep.py:9651` / code callable）均传 3 |
| `on_model_exception(ctx)` | N9 检测 → N12 计数 → 熔断判断 → N11 恢复 + `request_retry(0)` |
| `after_model_call(ctx)` | `ctx.exception is None` 时清零（N12） |
| `_parse_token_limits(exc)` | 三家格式：Anthropic `N tokens > M`、华为 `prompt length N ... maximum input length M`、OpenAI `maximum context length is M`（actual 为 None）→ `(actual, limit)` |
| `_resolve_threshold_override(ctx, parsed)` | N11 三级解析 |
| `_find_full_compact_processor(context)` | N11 同名键识别 + `set_force_compact` 能力探测 |

### 模块级常量（改动即受本 spec 约束）

`_REASON_LABELS`（中文文案：重复输出 / 流式超时 / 连接/超时）、`_RETRY_COUNTS_KEY = "_llm_retry_counts"`、`_COUNT_ATTRS`、`_INITIAL_BACKOFF=5.0`、`_MAX_BACKOFF=60.0`、`_BACKOFF_FACTOR=2.0`、`_BACKOFF_JITTER_RANGE=(0.5,1.5)`、`_TRANSIENT_MESSAGE_MARKERS`、`_MEMBER_NETWORK_404_MARKERS`、`RECOVERY_THRESHOLD_RATIO=0.85`。

### 通知 schema（前端契约）

- 重试中：`OutputSchema(type="retry_notification", index=999, payload={output: {output: 消息, result_type: "text"}, reason, attempt, max_attempts, delay_seconds})`——前端映射为 `chat.delta`
- 耗尽：`type="error"` + `payload.error_type="llm_retry_exhausted"` + `reason`
- 溢出熔断：`type="error"` + `payload.error_type="context_overflow_circuit_break"` + `message`（建议文案）+ `consecutive_errors`

### 配置消费（`execution_guard.llm_retry_rail`，config.yaml）

| 键 | yaml 值 | 装配代码 fallback | 说明 |
|---|---|---|---|
| `enabled` | true | False（缺省关） | 非 `is True` 即不注册 |
| `max_retries` | 2 | 2 | 单 invoke 内同类异常重试上限 |
| `retry_transient_invoke_errors` | true | True | N2 第三类开关 |
| `notify_user_on_retry` / `notify_user_on_exhausted` | true / true | True / True | 通知开关 |
| `repeat_min_pattern_chars` / `repeat_max_pattern_chars` | 2 / 64 | 2 / 64 | 透传父类 |
| `repeat_min_count` / `repeat_min_total_chars` | 6 / 160 | 6 / 160 | 透传父类 |
| `repeat_window_chars` | 1024 | 1024 | 透传父类 |
| `single_char_repeat_count` | **90** | **100** | yaml 生效值 90；改默认时两处对齐 |

`ContextOverflowRecoveryRail` 无配置节（硬编码 `max_recovery_attempts=3` 于装配点）。

### 装配点

| 场景 | 位置 | 形态 |
|---|---|---|
| deep 主链 retry | `interface_deep.py` `_build_llm_retry_rail`（:10113） | enabled 非 True → None；失败 → warning + None（不炸装配） |
| deep 溢出 | `interface_deep.py` `_build_context_overflow_recovery_rail`（:9648） | `max_recovery_attempts=3` |
| code 侧溢出 | `interface_code.py` `_FIXED_RAIL_NAMES`（:459）+ build callable | 固定名单去重挂载；断言传 3 |
| skill_turbo | `executor.py` `_build_llm_retry_rail`（:946） | 同一配置节、同一参数集（与 DeepAgent 一致） |
| swarm 成员 | `config_specs.py` `_llm_retry_params`（:397，:547/:638 两处 spec 构造）→ `builtin_rails.py` builder（:98） | 配置投影为可序列化 params，`TeamMemberNotifyingLLMRetryRail(**params)` |

## 数据结构

`ctx.extra["_llm_retry_counts"] = {"repeat": int, "stream_timeout": int, "transient_invoke": int}`——缺键 / 非法值时 `_retry_counts` 就地修复为 0（int 强转失败归零）。

镜像属性（实例上，仅观测）：`repeat_retry_count` / `stream_timeout_retry_count` / `transient_invoke_retry_count`（后者父类无时构造补 0）。

`backoff_seconds: list[float]` + `_backoff_generated: bool`；`_consecutive_overflow_count: int`（N12 实例级）。

## 执行路径全景

- **P1 装配**：config 加载 → deep / skill_turbo 各自 `_build_llm_retry_rail`（同一节取参）→ swarm 侧 `_llm_retry_params` 投影进成员 rail spec → 成员实例化时 builder 造 `TeamMemberNotifyingLLMRetryRail`；溢出 rail 经 deep `_build_context_overflow_recovery_rail(3)` 或 code `_FIXED_RAIL_NAMES` 挂载。
- **P2 retry 判定**：模型调用抛异常 → 父类框架触发 `on_model_exception` → N2 分流 → `_peek_retry`（N3）→ 通知（N5）→ `_request_retry_or_reset`：未耗尽则 `counts+1` + mirror + `ctx.request_retry(delay)`（delay 走 `backoff_delay(count)` + jitter，N6/N7）；耗尽走 N4。
- **P3 通知双形态**：will_retry → `retry_notification`（P2 内）；耗尽 → `error` + `llm_retry_exhausted`。
- **P4 溢出检测与恢复**：`on_model_exception`（priority 100 先跑，N10）→ N9 检测 → 计数 +1 → 未超限 → N11 恢复（force_compact + override）→ `request_retry(0)`。
- **P5 溢出熔断**：计数超 `max_recovery_attempts` → `_circuit_break`：error 事件 + 计数归零（N12）。
- **P6 成功归零**：`after_model_call` 且无异常 → `_consecutive_overflow_count = 0`。

## 异常分支与退出路径

| 分型 | 触发 | 行为 |
|---|---|---|
| 非三类原因 | `_is_repeat_*` / `_is_stream_timeout_*` / transient 均不中 | `on_model_exception` 直接 return，异常交上层 |
| 耗尽 | 某原因计数 ≥ `max_retries` | 计数 + 镜像归零、发 exhausted 通知（可关）、异常上抛（N4） |
| session 不可写 | session None / 无 `write_stream` | 通知静默跳过（N5） |
| write_stream 抛错 | 任何通知路径 | `logger.warning` 吞掉，主链不受影响 |
| 非溢出异常 | N9 不中 | 溢出 rail 直接 return |
| compact 失败 | 找不到 processor / 无 `set_force_compact` | 返回 False 但**仍** `request_retry(0)`（N11），日志 warning |
| limit 解析全失败 | 三家正则不中且无 config | `threshold_override=None`，FullCompact 用默认阈值，仍走恢复 |
| 连续溢出超限 | 计数 > `max_recovery_attempts` | 熔断：error 事件 + 不 retry（N12） |
| 熔断事件写出失败 | `write_stream` 抛错 | 只 warning，**计数保留**（下轮再报熔断） |
| 装配失败 | 任一 `_build_*` 抛异常 | warning + 返回 None，不炸 agent 启动 |

## 边界 case 清单

| 场景 | 现行为 | 风险 | 测试覆盖 |
|---|---|---|---|
| 并发 call_llm / stream_llm 共享 rail 实例 | per-ctx 预算独立（N1） | 计数互偷 → 提前耗尽或超限重试 | `test_concurrent_ctx_keep_independent_retry_budgets`、`test_before_invoke_does_not_reset_sibling_ctx_budget` |
| 未传 / 显式 None `backoff_seconds` | 生成序列 5/10/…/60 封顶 | 误把 None 当显式空表 | `test_generated_backoff_sequence_when_not_provided`、`test_explicit_none_backoff_still_uses_generated_sequence`、`test_generated_backoff_sequence_caps_at_max` |
| 显式传 `backoff_seconds` | 精确执行、零 jitter | 调用方精确调度被抖动 | `test_explicit_backoff_sequence_is_respected`、`test_explicit_backoff_has_no_jitter` |
| 生成序列逐请求 jitter | 每次采样（N7），两次请求 delay 不同 | 构造期采样导致并发同步 | `test_generated_backoff_applies_per_request_jitter` |
| TeamMember async-stream 404（三标记全中） | 按瞬时错误重试 | wrapper 措辞漂移即静默失效（有意，N8） | 无专属用例（依赖 agent-core 措辞，改动需人工核对 wrapper 异常文本） |
| ModelArts.81001 溢出 | 400 + 错误码 + 关键词三条件组合判定 | 词表 / 正则漂移漏检 | jiuwenswarm 仓无 `_parse_token_limits` / `_is_context_overflow_error` 专属单测——改词表须人工过三家错误样例 |
| code 侧挂载 | `_FIXED_RAIL_NAMES` 固定名单（不走动态 build）+ `max_recovery_attempts=3` | 动态注册路径漏挂 | `test_code_adapter_context_overflow_recovery_rail.py` 四用例（固定名单含 / 动态名单不含 / build 含 entry / callable 返回 rail 或 None） |
| forked FullCompactProcessor | 同名键 + 能力探测命中（N11） | isinstance 改回会漏判 forked 实例 | 无专属用例（改识别方式须过 forked 场景） |

## 修改检查单

- 改退避常量（5.0 / 60.0 / 2.0 / jitter 范围）→ `test_llm_retry_notify_rail_ctx.py` 生成序列 / 封顶 / jitter 三组用例同步
- 改 `_RETRY_COUNTS_KEY` 或计数结构 → 并发预算两用例同步；grep 全仓无其它消费者方可改
- 改通知 payload / type / index → 前端 `retry_notification` → `chat.delta` 映射方同步（server / 前端两侧）
- 改 `_TRANSIENT_MESSAGE_MARKERS` / `_MEMBER_NETWORK_404_MARKERS` → 与 agent-core `OpenAIModelClient.stream` 实际异常文本核对（404 规则尤其脆弱）
- 改 `RECOVERY_THRESHOLD_RATIO` → 压缩后窗口占比语义（留多少给输出）联动评估；无单测护栏，人工验证
- 改溢出关键词 / 三家正则 → 无既有测试，必须人工过 Anthropic / 华为 / OpenAI 各至少一条真实错误样例
- 改 `max_recovery_attempts` 默认值 → deep（:9651）与 code callable 两处装配传值是否跟随
- 改 `priority` → 与 StreamEventRail（80）/ ContextAssembleRail / ContextProcessorRail（均 85）的相对顺序重估（N10）
- 改 `_parent_accepts_retry_transient_invoke_errors` 探测逻辑 → agent-core `LLMRetryRail.__init__` 签名同步核对
- 改装配取参（`_build_llm_retry_rail` / `_llm_retry_params`）→ 三处装配点参数集保持一致（deep / skill_turbo / swarm 投影同源同默认）

## 与其它 spec 的关系

- [[S_08]]（工具循环熔断）：同属 `execution_guard` 域的兄弟件——本 spec 管 LLM 调用层异常，S_08 管工具结果层的循环 / 熔断；`execution_guard.circuit_breaker` 默认关闭（防与 agent_ras 双挂）的裁定归 S_08
- [[S_10]]（输入侧守卫）：同为 rail 家族但守输入侧（路径 / 读结果 / 配额），异常源不同不重叠
- agent-core `LLMRetryRail`：父类契约（repeat 检测算法、`backoff_delay`、异常判定方法族）——agent-core 仓无 AGENTS.md 覆盖该文件，本 spec 登记其构造参数契约；跨仓改动同样受本 spec 约束
- `jiuwenswarm/server/runtime/skill_turbo/AGENTS.md`：executor 的 retry rail 装配是 skill_turbo 降级体系的组成件，装配职责归其管辖
- `jiuwenswarm/agents/swarm/`：成员 rail 投影（`config_specs._llm_retry_params` / `builtin_rails.py` builder）的注册机制归 swarm 装配体系
- 前端契约：`retry_notification` → `chat.delta`、`error` → `chat.error` 的映射在 server 流转换层消费——改 payload 结构需两侧同步
