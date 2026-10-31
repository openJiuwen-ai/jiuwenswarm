# ContextEngine 与 Processor 链

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_02 / context-engine-and-processors |
| 关联模块 | agent-core `openjiuwen/core/context_engine/` + jiuwenswarm `interface_deep._build_context_processor_rail` |
| 最近一次修订日期 | 2026-10-25 |
| 关联 feature | 暂无（初版建档） |

## 范围 / 边界

### 本规约管什么

- **ContextEngine** 与会话 context 的读写、`compress_context` / `get_context_window` 等与 processor 的协作语义（产品侧如何配置、注入、触发）。
- **Preset processor 链**（ContextProcessorRail `preset=True`）与 swarm **user_processors** 合并规则。
- **FullCompactProcessor**：不在 preset 默认链中，由 swarm 在配置存在时注入；**GET 路径**与 `set_force_compact` 溢出语义。

### 本规约不管什么

- Processor 内部 LLM 提示词、分块算法实现细节 → agent-core 源码与 core 单测。
- KV cache affinity → [[S_06]] 声明正交。

## 不变量

- **N1（Engine 权威）**：同一 `session_id`，消息列表与 processor 附着状态以 ContextEngine 为准；流式/chat 收尾 `save_contexts` 须调用（interface_deep 多处补 commit 注释所述场景）。
- **N2（FullCompact 被动兜底）**：`full_compact_processor_config` 注释与实现一致——日常 **add 路径不依赖** FullCompact；真溢出时由 [[S_05]] 对 processor 调 `set_force_compact(True)`，下一次 **GET**（`get_context_window`）无条件走整窗压缩。
- **N3（FullCompact 配置类型）**：`_build_context_processor_rail` 对 FullCompact 传 **完整** `FullCompactProcessorConfig` 实例（非 preset dict 合并）；改 config 形状时必须同步构造逻辑。
- **N4（Preset 链 + forked）**：ContextProcessorRail preset 链经 forked.activate；识别 processor 时用 `processor_type()` 同名键（RecoveryRail 查找 FullCompact 同理，不用 fragile isinstance）。
- **N5（SessionMemory 默认关）**：preset 中 SessionMemoryCompressor 默认禁用；仅当 `session_memory_compressor_config` / `session_memory` 配置显式提供时在 swarm 侧启用（见 `_resolve_session_memory_config`）。

## 接口契约

### Swarm 构建 ContextEngineConfig

`_deep_agent_context_engine_config(react_cfg)` 从 `react.context_engine_config` 映射：

| 配置键 | ContextEngineConfig 字段 |
|---|---|
| `enable_reload` | `enable_reload` |
| `enable_openrouter_model_context_window_tokens` | 同名 |
| `context_window_tokens` | 解析为 int 或 None |
| `enable_context_debug` / `context_debug_dir` | debug 落盘 |
| `compression_recall_config.*` | `CompressionRecallConfig` |

### User processors（`_build_context_processor_rail`）

配置 dict **非空**时按名追加 tuple（顺序即合并意图）：

| 配置键 | Processor 名 |
|---|---|
| `message_summary_offloader_config` | MessageSummaryOffloader |
| `session_memory_compressor_config` | SessionMemoryCompressor |
| `dialogue_compressor_config` | DialogueCompressor |
| `current_round_compressor_config` | CurrentRoundCompressor |
| `round_level_compressor_config` | RoundLevelCompressor |
| `full_compact_processor_config` | FullCompactProcessor（BaseModel 实例） |
| `reasoning_tool_loop_compact_config` | ReasoningToolLoopCompactProcessor（附 `language`） |

构造：`ContextProcessorRail(processors=user_processors or None, preset=True, session_memory=session_memory_cfg)`。

### Core 工具（协作）

- `recall_compressed_context`：见 [[S_04]]。

### FullCompact 溢出 API（协作）

- `FullCompactProcessor.set_force_compact(bool)`
- `FullCompactProcessor.set_overflow_threshold_override(int | None)`（RecoveryRail 在解析到 limit 时用 `limit * RECOVERY_THRESHOLD_RATIO`，ratio=0.85）

## 数据结构

- **Context 会话状态**：engine 内存 + checkpointer；processor 列表挂在 context 对象 `_processors`（RecoveryRail 遍历查找 FullCompact）。
- **压缩 debug**：`enable_context_debug` 时 JSONL 落盘路径由 core 解析（config 注释：`OPENJIUWEN_CONTEXT_DEBUG_DIR` 或 workspace 默认）。

## 执行路径全景

### P1：Processor 链在 GET 路径生效

```
ContextProcessorRail.before_model_call (agent-core)
  → context.get_context_window(...)
  → 各 processor 按链顺序处理（含 offload / compress / forked 行为）
  → FullCompact：仅当 trigger 或 force_compact 时在 GET 路径执行
```

### P2：Swarm 注入 FullCompact

```
interface_deep._build_context_processor_rail
  if full_compact_processor_config dict non-empty:
    user_processors.append(("FullCompactProcessor", FullCompactProcessorConfig(**dict)))
```

### P3：手动压缩

```
handle_command_compact → react_agent.context_engine.compress_context(...)
（与 overflow 强制 compact 不同入口，但共用 engine API）
```

## 修改检查单

- 新增 processor 配置键 → `config.yaml` 注释 + `_build_context_processor_rail` + 本 spec 表 + team 侧 `_build_context_processor_rail` 镜像（若适用）。
- 改 preset 链默认成员 → 先读 agent-core ContextProcessorRail，再更新 [[S_03]] 与 RecoveryRail 查找逻辑说明。
- 改 FullCompact 触发语义 → 同步 [[S_05]] 与 config.yaml 中「仅溢出恢复」注释。

## 与其它 spec 的关系

- [[S_03]]：rails 如何挂载 engine/processor
- [[S_04]]：CompressionRecallConfig 与召回工具
- [[S_05]]：force_compact / threshold_override
- [[S_06]]：config 消费与 enabled 门控
