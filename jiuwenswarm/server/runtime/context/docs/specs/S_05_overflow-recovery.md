# 上下文溢出检测与恢复

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / overflow-recovery |
| 关联模块 | agents/harness/common/rails/context_overflow_recovery_rail.py + interface_deep 装配 |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- **ContextOverflowRecoveryRail**：LLM 调用失败时的 reactive 恢复链（检测 → force_compact → retry → 熔断）。
- **溢出错误判定**三分支（413 / 400+关键词 / 无 status 关键词）。
- **熔断流式事件** `context_overflow_circuit_break` 与用户操作建议（`/compact`、新会话）。
- 与 **FullCompactProcessor** 的桥接（`set_force_compact` / `set_overflow_threshold_override`）。
- 用户主动 **`command.compact`** / **`compact_partial`**（commands.py）作为手动恢复手段。

### 本规约不管什么

- LLM 提供商限流、非上下文类 400 错误 → 通用错误处理 / 其它 rails。
- flash 模式未装配本 rail 时的行为 → [[S_06]] N5（无自动恢复）。

## 不变量

- **N1（检测三分支）**：`_is_context_overflow_error` 判定顺序——**413** 直接成立；**400 且** 消息含 overflow 关键词；**status 缺失** 且含关键词；另支持 ModelArts 组合（`Error code: 400` + `ModelArts.81001` + 关键词）。Good：参数错误 400 无关键词不触发。Bad：所有 400 触发 compact。
- **N2（force_compact GET 触发）**：恢复动作对 `FullCompactProcessor` 调 `set_force_compact(True)`；可选 `threshold_override = limit * RECOVERY_THRESHOLD_RATIO`（**0.85**）；随后 `ctx.request_retry()` 重入 LLM。processor 缺失时仍 retry 但记录 warning（见实现）。
- **N3（熔断）**：连续溢出次数 **>** `max_recovery_attempts` 时 `_circuit_break`：写 `OutputSchema(type="error")`，`payload.error_type="context_overflow_circuit_break"`，message 建议 `/compact` 或新会话；成功后 `after_model_call` 清零计数。
- **N4（Priority）**：`priority = 100`，保证 `on_model_exception` 早于 priority 85 的 context 工程 rail 与 80 的 StreamEventRail（见类 docstring）。
- **N5（构造默认值 vs 产品）**：类 `__init__` 默认 `max_recovery_attempts=2`；**deep adapter** `_build_context_overflow_recovery_rail` 使用 **3**。文档以 adapter 装配为准；单测覆盖 adapter 行为。

## 接口契约

### ContextOverflowRecoveryRail

| 方法 / 属性 | 语义 |
|---|---|
| `priority` | 100 |
| `on_model_exception` | 检测溢出 → compact 标志 → `request_retry` 或熔断 |
| `after_model_call` | 成功则清零 `_consecutive_overflow_count` |
| `RECOVERY_THRESHOLD_RATIO` | 0.85，用于 threshold_override |

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

- Rail 实例状态：`_consecutive_overflow_count`（内存，随 successful model call 重置）。
- `_context_engineering_rail`：可选桥（若未来挂接 SessionMemory 增量更新，改时同步本 spec）。

## 执行路径全景

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
interface_deep._build_context_overflow_recovery_rail
  → ContextOverflowRecoveryRail(max_recovery_attempts=3)
  → _RailBuildInfo in _build_agent_rails
interface_flash：白名单不含 _context_overflow_recovery_rail
```

## 修改检查单

- 改关键词表或 413/400 逻辑 → `test_code_adapter_context_overflow_recovery_rail.py` + 本 spec N1。
- 改 RECOVERY_THRESHOLD_RATIO 或 override 解析 → 同步 config.yaml FullCompact 注释（0.85 自适应说明）。
- 改熔断 error_type 或文案 → 前端/CLI 消费方 grep + 本 spec 接口表。
- 在 code/deep 以外新模式装配 rail → 更新 [[S_06]] 模式表。

## 与其它 spec 的关系

- [[S_02]]：FullCompact GET 语义
- [[S_03]]：rail priority 相对顺序
- [[S_06]]：flash 不装配、full_compact 配置注入
