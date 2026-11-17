# 上下文溢出检测与恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / overflow-recovery |
| 关联模块 | agents/harness/common/rails/context_overflow_recovery_rail.py + deep/adapter_statics.py 构建 / interface_deep 注册 |
| 最近一次修订日期 | 2026-11-09 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- **ContextOverflowRecoveryRail**：模型调用前 proactive 预压缩 + LLM 调用失败时的 reactive 恢复链（检测 → force_compact → retry → 熔断）。
- **溢出错误判定**三分支（413 / 400+关键词 / 无 status 关键词）。
- **熔断流式事件** `context_overflow_circuit_break` 与用户操作建议（`/compact`、新会话）。
- 与 **FullCompactProcessor** 的桥接（`set_force_compact` / `set_overflow_threshold_override`）。
- 用户主动 **`command.compact`** / **`compact_partial`**（commands.py）作为手动恢复手段。

### 本规约不管什么

- LLM 提供商限流、非上下文类 400 错误 → 通用错误处理 / 其它 rails。
- flash 模式装配本 rail、但按白名单裁剪未列入的动态 rail 时的整体容错差异 → [[S_06]] N4。

## 不变量

- **N1（检测三分支）**：`_is_context_overflow_error` 判定顺序——**413** 直接成立；**400 且** 消息含 overflow 关键词；**status 缺失** 且含关键词。ModelArts 变体有两条路径：组合模式（`Error code: 400` + `ModelArts.81001` + 关键词）与纯文本 `input token limit is N`（关键词 `input token limit`，limit 由 `_parse_token_limits` 解析）。Good：参数错误 400 无关键词不触发。Bad：所有 400 触发 compact。
- **N2（force_compact GET 触发）**：恢复动作对 `FullCompactProcessor` 调 `set_force_compact(True)`；可选 `threshold_override = limit * RECOVERY_THRESHOLD_RATIO`（**0.85**）；随后 `ctx.request_retry()` 重入 LLM。processor 缺失时仍 retry 但记录 warning（见实现）。**limit 解析优先级**：显式窗口（`context._context_window_tokens` / 配置）→ 模型窗口表 → 配置；解析不到真实窗口时放弃主动预压缩，不用 core 的 200000 默认值误触发。
- **N3（熔断）**：连续溢出次数 **>** `max_recovery_attempts` 时 `_circuit_break`：写 `OutputSchema(type="error")`，`payload.error_type="context_overflow_circuit_break"`，message 建议 `/compact` 或新会话；**熔断事件发送成功后计数归零**，正常成功也由 `after_model_call` 清零。
- **N4（Priority）**：`priority = 100`，保证 `on_model_exception` 早于 priority 85 的 context 工程 rail 与 80 的 StreamEventRail（见类 docstring）。
- **N5（构造默认值 vs 产品）**：类 `__init__` 默认 `max_recovery_attempts=2`；**deep adapter** `_build_context_overflow_recovery_rail` 使用 **3**。文档以 adapter 装配为准；单测覆盖 adapter 行为。

## 接口契约

### ContextOverflowRecoveryRail

| 方法 / 属性 | 语义 |
|---|---|
| `priority` | 100 |
| `before_model_call` | 上一轮真实 usage ≥ 窗口 90% 时主动 `set_force_compact`（每 invoke 至多一次） |
| `on_model_exception` | 检测溢出 → compact 标志 → `request_retry` 或熔断 |
| `after_model_call` | 成功则清零 `_consecutive_overflow_count` |
| `RECOVERY_THRESHOLD_RATIO` | 0.85，用于 threshold_override |
| `PROACTIVE_TRIGGER_RATIO` | 0.90，主动预压缩触发阈值 |

### 熔断 payload（契约）

```python
{
  "error_type": "context_overflow_circuit_break",
  "message": "<中文用户提示，含 /compact 与新会话建议>",
  "consecutive_errors": <int>,
}
```

### 手动压缩

- `handle_command_compact` → `agent.compress_context(session_id, return_state=True)`
- `handle_command_compact_partial` → `agent.compact_partial(...)`

## 数据结构

- Rail 实例状态：`_consecutive_overflow_count`（内存，随 successful model call 或熔断事件发送成功重置）。
- `_context_engineering_rail`：可选桥（若未来挂接 SessionMemory 增量更新，改时同步本 spec）。
- `ctx.extra["_context_overflow_proactive_done"]`：invoke 级幂等标记，保证主动预压缩每次 invoke 至多一次；随请求上下文生命周期清理。

## 执行路径全景

### P0：模型调用前主动预压缩

```
before_model_call（每个 invoke 至多一次，ctx.extra["_context_overflow_proactive_done"] 幂等）
  → _resolve_model_context_limit（显式窗口 → 模型窗口表 → 配置）
  → _last_prompt_tokens（最近一条消息 usage；dict/Pydantic/dataclass 兼容，input_tokens → prompt_tokens → total_tokens）
  → used ≥ limit × PROACTIVE_TRIGGER_RATIO(0.90) → set_force_compact(threshold_override = limit × 0.85)
```

### P1：溢出 reactive 链

```
LLM 抛错 → on_model_exception
  → _is_context_overflow_error?
  → count += 1; if count > max_attempts → circuit_break
  → _resolve_threshold_override(limit from 413 parse or deep_config.context_window_limit_tokens)
  → _find_full_compact_processor(ctx.context) by processor_type() == "FullCompactProcessor"
  → set_force_compact(True); optional set_overflow_threshold_override
  → ctx.request_retry(delay_seconds=0)
```

### P2：成功后复位

```
after_model_call (exception is None) → reset count
```

### P3：装配

```
deep/adapter_statics._build_context_overflow_recovery_rail
  → ContextOverflowRecoveryRail(max_recovery_attempts=3)
  → interface_deep 以实例方法暴露并注册为 _RailBuildInfo in _build_agent_rails
interface_flash：_FLASH_RAIL_KEEP 保留 _context_overflow_recovery_rail 与 _llm_retry_rail
```

## 修改检查单

- 改关键词表或 413/400 逻辑 → `test_code_adapter_context_overflow_recovery_rail.py` + 本 spec N1。
- 改 RECOVERY_THRESHOLD_RATIO / PROACTIVE_TRIGGER_RATIO 或 override 解析 → 同步 config.yaml FullCompact 注释（0.85 自适应说明）与本 spec。
- 改熔断 error_type 或文案 → 前端/CLI 消费方 grep + 本 spec 接口表。
- 在 deep/code/flash 之外新增模式装配 rail → 更新 [[S_06]] 模式表。

## 与其它 spec 的关系

- [[S_02]]：FullCompact GET 语义
- [[S_03]]：rail priority 相对顺序
- [[S_06]]：flash 装配、动态 rail 白名单裁剪、full_compact 配置注入
