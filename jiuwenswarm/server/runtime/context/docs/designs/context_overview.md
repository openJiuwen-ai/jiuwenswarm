# 上下文管理（Context Management）特性文档

## 概述

**上下文模块**管理 Agent 与 LLM 之间的**有限上下文窗口**：在多轮对话、长工具输出与 reasoning 链堆积时，通过 processor 链压缩、卸载、召回与溢出恢复，尽量在窗口内保留任务关键信息；同时通过 **system prompt 分层**（工作区人设 + 动态 runtime/response rails）让模型每轮看到一致的规则与实时环境。

实现横跨 **agent-core**（ContextEngine、ContextAssembleRail、ContextProcessorRail、压缩处理器与召回工具）与 **jiuwenswarm**（adapter 装配、`ContextOverflowRecoveryRail`、prompt rails、`context_read_patch`、config.yaml）。本文从概念层面介绍生命周期与能力边界；改代码前请读 `docs/specs/S_*` 规约。

---

## 核心概念

### 上下文窗口生命周期（概念）

```text
会话消息进入 ContextEngine
  → Processor 链（offload / 对话·轮次压缩 / session memory / reasoning 循环折叠 …）
  → ContextAssembleRail 组装送模窗口（含 tools 段，allowlist 对齐渐进工具）
  → RuntimePromptRail + ResponsePromptRail 注入动态 system sections
  → ContextOverflowRecoveryRail：模型调用前主动预压缩（上一轮 usage ≥ 窗口 90%）
  → LLM 调用
  → 若溢出错误 → ContextOverflowRecoveryRail：force_compact + retry → 或熔断提示 /compact
  → 收尾 save_contexts 落盘 checkpointer
```

手动路径：用户 **`/compact`**（`command.compact`）或 partial compact 主动压缩，不依赖 LLM 报错。

### ContextEngine 与 Processor 链

ContextEngine 持有会话消息池与 processor 列表。**Preset 链**（forked.activate 等）由 ContextProcessorRail 挂载；swarm 通过 `react.context_engine_config` 下各 `*_config` 键**按需启用** MessageSummaryOffloader、Dialogue/CurrentRound/RoundLevel 压缩、SessionMemory、ReasoningToolLoopCompact 等。

**FullCompactProcessor** 是整窗兜底：日常 add 不触发；配置存在时由 swarm 注入 processor 链，在 **GET** 路径上响应 `set_force_compact`（溢出恢复或极高占用）。

### 压缩召回 vs 消息重载

| 机制 | 配置 | 模型侧 |
|---|---|---|
| **压缩召回** | `compression_recall_config.enabled`（yaml 默认 true；代码缺键 false） | `[[COMPRESSION_RECALL: id=...]]` / 工具 `recall_compressed_context`（常驻 schema） |
| **卸载重载** | `enable_reload` | core 注入 reload prompt 段 + `ContextMessageBuffer.reload`；`[[HANDLE:xxx]]` 由调用方/模型按提示触发 |

二者独立开关；归档目录为会话下 `compression_recall/`（召回开启时）。

### 溢出恢复（Reactive + Proactive）

`ContextOverflowRecoveryRail`（priority **100**）在模型调用前与 LLM 异常时：

0. **主动预压缩** — `before_model_call` 读取上一轮真实 usage，达到窗口 **90%**（`PROACTIVE_TRIGGER_RATIO`）时先 `set_force_compact(threshold_override = limit × 0.85)`；每个 invoke 至多一次  
1. **检测** — 413；或 400 + 上下文关键词；或无 status 的关键词兜底（含 ModelArts `input token limit is N`）  
2. **标记 FullCompact** — `set_force_compact(True)`，limit 已知时 `threshold_override = limit × 0.85`  
3. **Retry** — `request_retry`  
4. **熔断** — 超过 `max_recovery_attempts`（deep 装配为 **3**）→ 流式 `context_overflow_circuit_break`，建议 `/compact` 或新会话；事件发送成功后计数归零  

**Flash 模式**保留此 rail（未列入白名单的动态 rail 仍被裁剪，整体容错弱于 agent），长文档场景需知容错边界（见 [[S_06]]）。

### 系统提示词分层

| 层 | 来源 | 内容示例 |
|---|---|---|
| **工作区人设** | core 读 AGENT/SOUL 等 | 角色、长期指令 |
| **动态 runtime** | RuntimePromptRail | 时间、runtime_state.yaml、路径、shell 环境、附件 |
| **回复契约** | ResponsePromptRail | 回复格式、思考纪律、最终可见回复、条件 A2UI |
| **窗口内工具/消息** | ContextAssembleRail + processors | 对话历史、tools 段、卸载索引 |

人设读路径经 **context_read_patch** 使用 `only_read=True`，避免多会话并发读 AGENT/SOUL 时全局 FS 锁排队；该 patch 仅企业版 `__init__` 分支安装，个人版 no-op。

### 配置与装配

单一产品配置根：**`react.context_engine_config`**（`jiuwenswarm/resources/config.yaml`）。Deep adapter 将子集映射为 `ContextEngineConfig` 并构建 rails。

**已知遗留**：deep adapter 认为 `enabled` 缺省为 **false**，团队 runtime 继承 helper `get_context_engine_enabled` 缺省为 **true**——改默认须两端对齐（[[S_06]]）。

---

## 术语表

| 术语 | 含义 |
|---|---|
| **ContextEngine** | 会话上下文引擎（core） |
| **ContextAssembleRail** | 组装送模窗口（priority 85） |
| **ContextProcessorRail** | 挂载 processor 链（priority 85，受 enabled 门控） |
| **ContextOverflowRecoveryRail** | 溢出 reactive 恢复 + proactive 预压缩（priority 100，swarm） |
| **FullCompactProcessor** | 整窗 LLM 摘要压缩；GET + force_compact |
| **force_compact** | 强制下一次 GET 路径执行 FullCompact |
| **RECOVERY_THRESHOLD_RATIO** | 0.85，溢出恢复时压缩阈值占模型窗口比例 |
| **PROACTIVE_TRIGGER_RATIO** | 0.90，上一轮真实 usage 达窗口该比例时先主动预压缩 |
| **RuntimePromptRail / ResponsePromptRail** | 动态 system 注入（priority 5） |
| **context_read_patch** | 人设文件读跳过跨进程锁；仅企业版 `__init__` 安装 |
| **recall_compressed_context** | 压缩召回工具（core） |

## 能力边界

- **不管** PersonalContext/Distill、RequestContext、team_context、skill_turbo context_binding——见 [[S_01]] 边界表。
- **不实现** ContextEngine/processor 算法本体——在 agent-core；改那些代码读 agent-core `context_engine/AGENTS.md`。
- **双仓归档**——产品装配 / 溢出 / prompt 在本目录；引擎实现契约在 agent-core（[[S_01]] N4），禁止两边各写一套漂移全文。

深入规约：`docs/specs/S_01`（边界）→ `S_02`（引擎/processor）→ `S_03`（rails）→ `S_04`（召回/重载）→ `S_05`（溢出）→ `S_06`（装配/config）→ `S_07`（prompt 分层）。
